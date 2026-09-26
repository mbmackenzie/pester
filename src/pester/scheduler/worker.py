import logging
from collections.abc import Mapping

from pester.config import PesterConfig
from pester.core.clock import Clock
from pester.core.messages import OutboundMessage
from pester.core.models import JobRecord
from pester.delivery.base import DeliveryChannel
from pester.personality.base import PromptContext
from pester.personality.registry import PersonalityRegistry
from pester.scheduler.policy import Candidate, decide
from pester.storage.repository import Repository

log = logging.getLogger(__name__)


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

    async def run_once(self) -> int:
        """Apply one scheduling pass. Returns the number of jobs claimed or expired."""
        now = self._clock.now()
        routable: dict[int, tuple[JobRecord, str, str]] = {}
        candidates: list[Candidate] = []
        for job in await self._repo.queued_jobs():
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
                )
            )

        plan = decide(
            now, candidates, await self._repo.outstanding_counts(), self._config.scheduler.max_outstanding
        )

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
