import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from pester.config import PesterConfig, QuietHours, SchedulerConfig
from pester.core.clock import Clock
from pester.core.messages import OutboundMessage
from pester.core.models import JobRecord
from pester.delivery.base import DeliveryChannel
from pester.live import LiveConfig
from pester.personality.base import PromptContext
from pester.personality.registry import PersonalityRegistry
from pester.scheduler.policy import (
    Candidate,
    NextSend,
    Policy,
    QuietWindow,
    RecipientState,
    decide,
    next_send,
    seeded_jitter,
)
from pester.storage.repository import Repository, SchedulingSnapshot

log = logging.getLogger(__name__)

# Long enough to cover a recipient's whole local day in any timezone, for daily caps.
_PROMPT_HISTORY = timedelta(hours=50)


class SendNowOutcome(StrEnum):
    SENT = "SENT"
    OUTSTANDING = "OUTSTANDING"  # they already have a question open
    NOTHING = "NOTHING"  # nothing queued that may be sent now


@dataclass(frozen=True)
class SendNowResult:
    outcome: SendNowOutcome
    job: JobRecord | None = None


class SchedulerWorker:
    def __init__(
        self,
        repo: Repository,
        live: LiveConfig,
        channels: Mapping[str, DeliveryChannel],
        clock: Clock,
    ) -> None:
        self._repo = repo
        self._live = live
        self._channels = channels
        self._clock = clock

    async def run_once(self) -> int:
        """One pass: time out unanswered prompts, expire stale jobs, claim jobs that may be sent now."""
        snapshot = self._live.current  # one config for the whole pass
        return await self._time_out_unanswered(snapshot.config) + await self._schedule(
            snapshot.config, snapshot.personalities
        )

    async def send_now(self, recipient_id: str, prefer_channel: str | None = None) -> SendNowResult:
        """Claim the recipient's next question now, skipping pacing (quiet hours, spacing, jitter, daily cap).

        For a person asking for one (``/send``). It still honors the producer's ``not_before`` and
        ``expires_at`` and never sends while another question is outstanding. The send counts toward pacing
        like any other.
        """
        if (open_job := await self._repo.outstanding_for(recipient_id)) is not None:
            return SendNowResult(SendNowOutcome.OUTSTANDING, open_job)
        snapshot = self._live.current
        now = self._clock.now()
        policy = _policy(snapshot.config.scheduler)
        pending = await self._repo.queued_for(recipient_id)
        for job in sorted(pending, key=lambda job: policy.order_key(_candidate(job))):
            delivery = job.spec.delivery
            if (delivery.expires_at and delivery.expires_at <= now) or (
                delivery.not_before and delivery.not_before > now
            ):
                continue
            route = self._route(snapshot.config, job, prefer_channel)
            if route is None:
                continue
            text = await _prompt_text(job, snapshot.personalities)
            message = OutboundMessage(text=text, options=job.spec.response_options)
            if await self._repo.claim_for_send(job.pk, *route, message):
                log.info("sending on request", extra={"interaction_id": job.id, "recipient_id": recipient_id})
                return SendNowResult(SendNowOutcome.SENT, job)
        return SendNowResult(SendNowOutcome.NOTHING)

    async def next_send(self, recipient_id: str) -> NextSend | None:
        """When the recipient's next question goes out, and what's holding it (for /status and the UI)."""
        config = self._live.current.config
        now = self._clock.now()
        snapshot = await self._repo.scheduling_snapshot(now - _PROMPT_HISTORY)
        state = _recipient_state(config, recipient_id, snapshot)
        if state is None:
            return None
        pending = [
            _candidate(job)
            for job in snapshot.queued
            if job.spec.recipient_id == recipient_id and self._route(config, job) is not None
        ]
        return next_send(now, pending, state, _policy(config.scheduler))

    async def _time_out_unanswered(self, config: PesterConfig) -> int:
        now = self._clock.now()
        default = config.scheduler.default_answer_within_seconds
        done = 0
        for awaiting in await self._repo.awaiting_jobs():
            if awaiting.has_response:
                continue  # an answer is being collected; its debounce window will close it
            seconds = awaiting.job.spec.delivery.answer_within_seconds or default
            deadline = awaiting.sent_at + timedelta(seconds=seconds)
            if deadline <= now:
                done += await self._repo.mark_unanswered(awaiting.job.pk, awaiting.sent_at, deadline)
        return done

    async def _schedule(self, config: PesterConfig, personalities: PersonalityRegistry) -> int:
        now = self._clock.now()
        snapshot = await self._repo.scheduling_snapshot(now - _PROMPT_HISTORY)
        routable: dict[int, tuple[JobRecord, str, str]] = {}
        candidates: list[Candidate] = []
        for job in snapshot.queued:
            route = self._route(config, job)
            expires_at = job.spec.delivery.expires_at
            if route is None and not (expires_at and expires_at <= now):
                continue  # no enabled channel; leave queued until one is configured
            if route is not None:
                routable[job.pk] = (job, *route)
            candidates.append(_candidate(job))

        recipients: dict[str, RecipientState] = {}
        for recipient_id in {c.recipient_id for c in candidates}:
            if (state := _recipient_state(config, recipient_id, snapshot)) is not None:
                recipients[recipient_id] = state

        plan = decide(now, candidates, recipients, _policy(config.scheduler))
        done = 0
        for key in plan.expire:
            done += await self._repo.expire(key)
        for key in plan.send:
            job, channel, address = routable[key]
            text = await _prompt_text(job, personalities)
            message = OutboundMessage(text=text, options=job.spec.response_options)
            done += await self._repo.claim_for_send(key, channel, address, message)
        return done

    def _route(
        self, config: PesterConfig, job: JobRecord, prefer: str | None = None
    ) -> tuple[str, str] | None:
        """(channel, address): the job's requested channel, else ``prefer``, else the recipient's first."""
        recipient = config.recipients.get(job.spec.recipient_id)
        if recipient is None:
            return None
        names = [job.spec.delivery.channel] if job.spec.delivery.channel else list(recipient.channels)
        if prefer in names:
            names.remove(prefer)
            names.insert(0, prefer)
        for name in names:
            if name in self._channels and name in recipient.channels:
                try:
                    return name, self._channels[name].address_of(recipient.channels[name])
                except ValueError as exc:  # leave the job queued; the rest of the pass goes on
                    log.warning("recipient %s: bad %s config: %s", job.spec.recipient_id, name, exc)
        return None


async def _prompt_text(job: JobRecord, personalities: PersonalityRegistry) -> str:
    if job.spec.delivery.prompt_rendering != "personality":
        return job.spec.prompt
    personality = personalities.resolve(job.spec.personality_id)
    return (await personality.prompt(PromptContext(job.spec))).text


def _candidate(job: JobRecord) -> Candidate:
    return Candidate(
        key=job.pk,
        recipient_id=job.spec.recipient_id,
        priority=job.spec.delivery.priority,
        created_at=job.created_at,
        not_before=job.spec.delivery.not_before,
        expires_at=job.spec.delivery.expires_at,
        snoozed_until=job.snoozed_until,
    )


def _recipient_state(
    config: PesterConfig, recipient_id: str, snapshot: SchedulingSnapshot
) -> RecipientState | None:
    recipient = config.recipients.get(recipient_id)
    if recipient is None:
        return None
    return RecipientState(
        tz=recipient.tz,
        quiet=_window(recipient.quiet_hours or config.scheduler.quiet_hours),
        outstanding=snapshot.outstanding.get(recipient_id, 0),
        recent_prompts=tuple(snapshot.recent_prompts.get(recipient_id, ())),
        paused=recipient_id in snapshot.paused,
    )


def _policy(scheduler: SchedulerConfig) -> Policy:
    return Policy(
        shuffle_jobs=scheduler.shuffle_jobs,
        max_outstanding=scheduler.max_outstanding,
        min_interval=timedelta(minutes=scheduler.min_interval_minutes),
        max_per_day=scheduler.max_messages_per_day,
        jitter=seeded_jitter(scheduler.jitter_seed, scheduler.jitter_minutes),
    )


def _window(quiet: QuietHours | None) -> QuietWindow | None:
    return QuietWindow(quiet.start, quiet.end) if quiet else None
