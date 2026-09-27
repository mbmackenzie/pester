"""Recipient commands (spec §9.3). Each returns the text to send back to the person, or None for no reply."""

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from pester.config import PesterConfig
from pester.core.clock import Clock
from pester.core.durations import parse_duration
from pester.core.messages import InboundMessage
from pester.core.models import JobRecord
from pester.core.states import JobStatus
from pester.live import LiveConfig
from pester.scheduler.policy import Hold, NextSend
from pester.scheduler.worker import SendNowOutcome
from pester.storage.repository import IngestOutcome, RecipientStatus, Repository

if TYPE_CHECKING:
    from pester.scheduler.worker import SchedulerWorker

HELP = "Commands: /send, /skip, /snooze 2h, /pause, /resume, /status"
DEFAULT_SNOOZE = timedelta(hours=1)


class CommandHandler:
    def __init__(
        self, repo: Repository, live: LiveConfig, clock: Clock, scheduler: "SchedulerWorker | None" = None
    ) -> None:
        self._repo = repo
        self._live = live
        self._clock = clock
        self._scheduler = scheduler  # for /send and for when the next question comes

    @property
    def _config(self) -> PesterConfig:
        return self._live.current.config

    async def handle(self, recipient_id: str, message: InboundMessage) -> str | None:
        assert message.command is not None
        name, args = message.command.name, message.command.args
        match name:
            case "send" if self._scheduler is not None:
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
        assert self._scheduler is not None
        result = await self._scheduler.send_now(recipient_id, message.channel)
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
            lines.append("Paused: send /resume to start again, or /send for one now.")
        for awaiting in status.awaiting:
            lines.append(
                f'Waiting on your answer: "{_excerpt(awaiting.job)}" '
                f"(asked {self._local(recipient_id, awaiting.sent_at)})."
            )
        if status.queued:
            upcoming = await self._scheduler.next_send(recipient_id) if self._scheduler else None
            lines.append(self._queued(recipient_id, status, upcoming))
        return "\n".join(lines) or "Nothing pending."

    def _queued(self, recipient_id: str, status: RecipientStatus, upcoming: NextSend | None) -> str:
        noun = "question" if status.queued == 1 else "questions"
        count = f"{status.queued} more {noun} queued" if status.awaiting else f"{status.queued} {noun} queued"
        when = describe_next(self._config, recipient_id, upcoming, self._clock.now())
        if when is None:
            return f"{count}."
        if upcoming is not None and upcoming.waiting_on_answer:
            return f"{count}. The next comes after you answer (or /skip) the open one."
        if when == "now":
            return f"{count}. The next is on its way."
        return f"{count}. The next comes around {when}. Send /send for one now."

    def _local(self, recipient_id: str, when: datetime) -> str:
        return local_time(self._config, recipient_id, when, self._clock.now())


def describe_next(
    config: PesterConfig, recipient_id: str, upcoming: NextSend | None, now: datetime
) -> str | None:
    """When a recipient's next question goes out, in their terms.

    "tomorrow 8:30 AM (quiet hours until 8:30 AM)", "now", "after the open question", or None when nothing's
    coming (or they're paused).
    """
    if upcoming is None or upcoming.paused:
        return None
    if upcoming.waiting_on_answer:
        return "after the open question"
    if upcoming.at <= now:
        return "now"
    reasons = _holds(config, recipient_id, upcoming)
    when = local_time(config, recipient_id, upcoming.at, now)
    return f"{when} ({reasons})" if reasons else when


def _holds(config: PesterConfig, recipient_id: str, upcoming: NextSend) -> str:
    scheduler = config.scheduler
    recipient = config.recipients.get(recipient_id)
    quiet = (recipient.quiet_hours if recipient else None) or scheduler.quiet_hours
    reasons: list[str] = []
    for hold in (Hold.SNOOZED, Hold.NOT_BEFORE, Hold.QUIET_HOURS, Hold.DAILY_CAP, Hold.SPACING):
        if hold not in upcoming.holds:
            continue
        match hold:
            case Hold.SNOOZED:
                reasons.append("you snoozed it")
            case Hold.NOT_BEFORE:
                reasons.append("it's scheduled for later")
            case Hold.QUIET_HOURS if quiet is not None:
                reasons.append(f"quiet hours until {_clock_time(quiet.end)}")
            case Hold.DAILY_CAP:
                reasons.append(f"you've had today's {scheduler.max_messages_per_day}")
            case Hold.SPACING:
                reasons.append(
                    f"at most one every {_duration(timedelta(minutes=scheduler.min_interval_minutes))}"
                )
            case _:
                pass
    return "; ".join(reasons)


def local_time(config: PesterConfig, recipient_id: str, when: datetime, now: datetime) -> str:
    """A time in the recipient's timezone: "8:47 AM", "tomorrow 8:47 AM", or "Sat 8:47 AM"."""
    recipient = config.recipients.get(recipient_id)
    tz = recipient.tz if recipient else ZoneInfo("UTC")
    local, today = when.astimezone(tz), now.astimezone(tz).date()
    clock_time = _clock_time(local.time())
    if local.date() == today:
        return clock_time
    if local.date() == today + timedelta(days=1):
        return f"tomorrow {clock_time}"
    return f"{local:%a} {clock_time}"


def _clock_time(value: time) -> str:
    return f"{value.hour % 12 or 12}:{value.minute:02d} {'AM' if value.hour < 12 else 'PM'}"


def _duration(value: timedelta) -> str:
    minutes = int(value.total_seconds() // 60)
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours}h {rest}m"
    if hours:
        return "hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} minutes"


def _no_target(outcome: IngestOutcome, verb: str) -> str:
    if outcome is IngestOutcome.AMBIGUOUS:
        return f"You have more than one open question. Reply to the one you want to {verb}."
    return f"Nothing to {verb}."


def _excerpt(job: JobRecord, limit: int = 80) -> str:
    prompt = " ".join(job.spec.prompt.split())
    return prompt if len(prompt) <= limit else prompt[: limit - 1] + "…"
