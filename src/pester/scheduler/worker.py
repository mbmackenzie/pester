import logging
from collections.abc import Mapping
from datetime import timedelta

from pester.config import PesterConfig, QuietHours
from pester.core.clock import Clock
from pester.core.messages import OutboundMessage
from pester.core.models import JobRecord
from pester.delivery.base import DeliveryChannel
from pester.personality.base import PromptContext
from pester.personality.registry import PersonalityRegistry
from pester.scheduler.policy import Candidate, Policy, QuietWindow, RecipientState, decide, seeded_jitter
from pester.storage.repository import Repository

log = logging.getLogger(__name__)

# Long enough to cover a recipient's whole local day in any timezone, for daily caps.
_PROMPT_HISTORY = timedelta(hours=50)


class SchedulerWorker:
    def __init__(
        self,
        repo: Repository,
        config: PesterConfig,
        channels: Mapping[str, DeliveryChannel],
        clock: Clock,
        personalities: PersonalityRegistry,
    ) -> None:
        self._repo = repo
        self._config = config
        self._channels = channels
        self._clock = clock
        self._personalities = personalities
        scheduler = config.scheduler
        self._policy = Policy(
            max_outstanding=scheduler.max_outstanding,
            min_interval=timedelta(minutes=scheduler.min_interval_minutes),
            max_per_day=scheduler.max_messages_per_day,
            jitter=seeded_jitter(scheduler.jitter_seed, scheduler.jitter_minutes),
        )

    async def run_once(self) -> int:
        """One pass: time out unanswered prompts, expire stale jobs, claim jobs that may be sent now."""
        return await self._time_out_unanswered() + await self._schedule()

    async def _time_out_unanswered(self) -> int:
        now = self._clock.now()
        default = self._config.scheduler.default_answer_within_seconds
        done = 0
        for awaiting in await self._repo.awaiting_jobs():
            if awaiting.has_response:
                continue  # an answer is being collected; its debounce window will close it
            seconds = awaiting.job.spec.delivery.answer_within_seconds or default
            deadline = awaiting.sent_at + timedelta(seconds=seconds)
            if deadline <= now:
                done += await self._repo.mark_unanswered(awaiting.job.pk, awaiting.sent_at, deadline)
        return done

    async def _schedule(self) -> int:
        now = self._clock.now()
        snapshot = await self._repo.scheduling_snapshot(now - _PROMPT_HISTORY)
        routable: dict[int, tuple[JobRecord, str, str]] = {}
        candidates: list[Candidate] = []
        for job in snapshot.queued:
            route = self._route(job)
            expires_at = job.spec.delivery.expires_at
            if route is None and not (expires_at and expires_at <= now):
                continue  # no enabled channel; leave queued until one is configured
            if route is not None:
                routable[job.pk] = (job, *route)
            candidates.append(
                Candidate(
                    key=job.pk,
                    recipient_id=job.spec.recipient_id,
                    priority=job.spec.delivery.priority,
                    created_at=job.created_at,
                    not_before=job.spec.delivery.not_before,
                    expires_at=expires_at,
                    snoozed_until=job.snoozed_until,
                )
            )

        recipients: dict[str, RecipientState] = {}
        for recipient_id in {c.recipient_id for c in candidates}:
            recipient = self._config.recipients.get(recipient_id)
            if recipient is None:
                continue
            quiet = recipient.quiet_hours or self._config.scheduler.quiet_hours
            recipients[recipient_id] = RecipientState(
                tz=recipient.tz,
                quiet=_window(quiet),
                outstanding=snapshot.outstanding.get(recipient_id, 0),
                recent_prompts=tuple(snapshot.recent_prompts.get(recipient_id, ())),
                paused=recipient_id in snapshot.paused,
            )

        plan = decide(now, candidates, recipients, self._policy)
        done = 0
        for key in plan.expire:
            done += await self._repo.expire(key)
        for key in plan.send:
            job, channel, address = routable[key]
            message = OutboundMessage(text=await self._prompt_text(job), options=job.spec.response_options)
            done += await self._repo.claim_for_send(key, channel, address, message)
        return done

    async def _prompt_text(self, job: JobRecord) -> str:
        if job.spec.delivery.prompt_rendering != "personality":
            return job.spec.prompt
        personality = self._personalities.resolve(job.spec.personality_id)
        return (await personality.prompt(PromptContext(job.spec))).text

    def _route(self, job: JobRecord) -> tuple[str, str] | None:
        """(channel, address) for a job: its requested channel, else the recipient's first enabled one."""
        recipient = self._config.recipients.get(job.spec.recipient_id)
        if recipient is None:
            return None
        names = [job.spec.delivery.channel] if job.spec.delivery.channel else list(recipient.channels)
        for name in names:
            if name in self._channels and name in recipient.channels:
                return name, self._channels[name].address_of(recipient.channels[name])
        return None


def _window(quiet: QuietHours | None) -> QuietWindow | None:
    return QuietWindow(quiet.start, quiet.end) if quiet else None
