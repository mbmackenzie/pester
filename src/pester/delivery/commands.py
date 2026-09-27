"""Recipient commands (spec §9.3). Each returns the text to send back to the person, or None for no reply."""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pester.config import PesterConfig
from pester.core.clock import Clock
from pester.core.durations import parse_duration
from pester.core.messages import InboundMessage
from pester.core.models import JobRecord
from pester.core.states import JobStatus
from pester.live import LiveConfig
from pester.scheduler.worker import SendNowOutcome, SendNowResult
from pester.storage.repository import IngestOutcome, Repository

HELP = "Commands: /send, /skip, /snooze 2h, /pause, /resume, /status"
SendNow = Callable[[str, str], Awaitable[SendNowResult]]
DEFAULT_SNOOZE = timedelta(hours=1)


class CommandHandler:
    def __init__(
        self, repo: Repository, live: LiveConfig, clock: Clock, send_now: SendNow | None = None
    ) -> None:
        self._repo = repo
        self._live = live
        self._clock = clock
        self._send_now = send_now

    @property
    def _config(self) -> PesterConfig:
        return self._live.current.config

    async def handle(self, recipient_id: str, message: InboundMessage) -> str | None:
        assert message.command is not None
        name, args = message.command.name, message.command.args
        match name:
            case "send" if self._send_now is not None:
                return await self._send(recipient_id, message)
            case "skip":
                return await self._skip(recipient_id, message)
            case "snooze":
                return await self._snooze(recipient_id, message, args)
            case "pause":
                await self._repo.set_paused(recipient_id, True)
                return "Paused. I won't ask you anything until you send /resume."
            case "resume":
                await self._repo.set_paused(recipient_id, False)
                return "Resumed."
            case "status":
                return await self._status(recipient_id)
            case "start":
                return f"You're already set up. {HELP}"
            case _:
                return HELP

    async def _send(self, recipient_id: str, message: InboundMessage) -> str | None:
        assert self._send_now is not None
        result = await self._send_now(recipient_id, message.channel)
        match result.outcome:
            case SendNowOutcome.SENT:
                if (await self._repo.recipient_status(recipient_id)).paused:
                    return (
                        "Here's one. You're still paused, so I won't send more on my own until you /resume."
                    )
                return None  # the question itself is the reply
            case SendNowOutcome.OUTSTANDING:
                assert result.job is not None
                if result.job.status is JobStatus.AWAITING:
                    return (
                        f'You still have an open question: "{_excerpt(result.job)}". '
                        "Answer it, /skip it, or /snooze it first."
                    )
                return "One is already on its way."
            case SendNowOutcome.NOTHING:
                return "Nothing is queued for you right now."

    async def _skip(self, recipient_id: str, message: InboundMessage) -> str:
        target = await self._repo.command_target(recipient_id, message)
        if isinstance(target, IngestOutcome):
            return _no_target(target, "skip")
        await self._repo.skip(target.pk)
        return "Skipped."

    async def _snooze(self, recipient_id: str, message: InboundMessage, args: str) -> str:
        duration = parse_duration(args) if args else DEFAULT_SNOOZE
        limit = timedelta(hours=self._config.scheduler.max_snooze_hours)
        if duration is None:
            return "Try /snooze 30m, /snooze 2h, or /snooze 1d."
        if duration > limit:
            return f"I can snooze for at most {self._config.scheduler.max_snooze_hours} hours."
        target = await self._repo.command_target(recipient_id, message)
        if isinstance(target, IngestOutcome):
            return _no_target(target, "snooze")
        until = self._clock.now() + duration
        await self._repo.snooze(target.pk, until)
        return f"Snoozed until {self._local(recipient_id, until)}."

    async def _status(self, recipient_id: str) -> str:
        status = await self._repo.recipient_status(recipient_id)
        lines: list[str] = []
        if status.paused:
            lines.append("Paused (send /resume to start again).")
        for awaiting in status.awaiting:
            lines.append(
                f'Waiting on your answer: "{_excerpt(awaiting.job)}" '
                f"(asked {self._local(recipient_id, awaiting.sent_at)})."
            )
        if status.queued:
            lines.append(f"{status.queued} more queued.")
        return "\n".join(lines) or "Nothing pending."

    def _local(self, recipient_id: str, when: datetime) -> str:
        recipient = self._config.recipients.get(recipient_id)
        tz = recipient.tz if recipient else ZoneInfo("UTC")
        local, today = when.astimezone(tz), self._clock.now().astimezone(tz).date()
        return local.strftime("%H:%M") if local.date() == today else local.strftime("%a %H:%M")


def _no_target(outcome: IngestOutcome, verb: str) -> str:
    if outcome is IngestOutcome.AMBIGUOUS:
        return f"You have more than one open question. Reply to the one you want to {verb}."
    return f"Nothing to {verb}."


def _excerpt(job: JobRecord, limit: int = 80) -> str:
    prompt = " ".join(job.spec.prompt.split())
    return prompt if len(prompt) <= limit else prompt[: limit - 1] + "…"
