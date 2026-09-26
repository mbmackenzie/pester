"""Recipient commands (spec §9.3). Each returns the text to send back to the person."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pester.config import PesterConfig
from pester.core.clock import Clock
from pester.core.durations import parse_duration
from pester.core.messages import InboundMessage
from pester.core.models import JobRecord
from pester.storage.repository import IngestOutcome, Repository

HELP = "Commands: /skip, /snooze 2h, /pause, /resume, /status"
DEFAULT_SNOOZE = timedelta(hours=1)


class CommandHandler:
    def __init__(self, repo: Repository, config: PesterConfig, clock: Clock) -> None:
        self._repo = repo
        self._config = config
        self._clock = clock

    async def handle(self, recipient_id: str, message: InboundMessage) -> str:
        assert message.command is not None
        name, args = message.command.name, message.command.args
        match name:
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
            case _:
                return HELP

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
