"""Scheduling policy as a pure function (spec §8). No I/O, no clock: everything is an argument.

For each queued job, ``earliest_send`` computes the first moment it may go out given every constraint.
Jitter is applied to fixed anchors (the job's own start, the end of a spacing gap, the end of quiet
hours), so a job's send time is stable from one pass to the next rather than receding forever.
"""

import hashlib
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

Jitter = Callable[[str], timedelta]


def no_jitter(key: str) -> timedelta:
    return timedelta(0)


def seeded_jitter(seed: str, max_minutes: int) -> Jitter:
    """A deterministic offset in [0, max_minutes] for each key."""
    span = max_minutes * 60 + 1

    def jitter(key: str) -> timedelta:
        if max_minutes <= 0:
            return timedelta(0)
        digest = hashlib.sha256(f"{seed}:{key}".encode()).digest()
        return timedelta(seconds=int.from_bytes(digest[:8]) % span)

    return jitter


@dataclass(frozen=True)
class QuietWindow:
    """Local wall-clock window during which no prompt is sent. ``start > end`` spans midnight."""

    start: time
    end: time

    def contains(self, local: time) -> bool:
        if self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= local < self.end
        return local >= self.start or local < self.end


@dataclass(frozen=True)
class Candidate:
    key: int
    recipient_id: str
    priority: float
    created_at: datetime
    not_before: datetime | None = None
    expires_at: datetime | None = None
    snoozed_until: datetime | None = None


@dataclass(frozen=True)
class RecipientState:
    tz: ZoneInfo
    quiet: QuietWindow | None = None
    outstanding: int = 0
    recent_prompts: tuple[datetime, ...] = ()  # prompt send times, at least the last ~50 hours
    paused: bool = False

    @property
    def last_prompt(self) -> datetime | None:
        return max(self.recent_prompts, default=None)


@dataclass(frozen=True)
class Policy:
    max_outstanding: int = 1
    min_interval: timedelta = timedelta(0)
    max_per_day: int = 1_000_000
    jitter: Jitter = no_jitter


@dataclass(frozen=True)
class Plan:
    send: list[int] = field(default_factory=list[int])
    expire: list[int] = field(default_factory=list[int])
    wake_at: datetime | None = None  # earliest future time at which the plan could change


def decide(
    now: datetime,
    candidates: Iterable[Candidate],
    recipients: Mapping[str, RecipientState],
    policy: Policy,
) -> Plan:
    send: list[int] = []
    expire: list[int] = []
    wake_times: list[datetime] = []
    by_recipient: defaultdict[str, list[Candidate]] = defaultdict(list)

    for c in candidates:
        if c.expires_at is not None and c.expires_at <= now:
            expire.append(c.key)
        elif c.recipient_id in recipients:
            by_recipient[c.recipient_id].append(c)
            if c.expires_at is not None:
                wake_times.append(c.expires_at)

    for recipient_id, pending in by_recipient.items():
        state = recipients[recipient_id]
        if state.paused:
            continue
        pending.sort(key=lambda c: (-c.priority, c.created_at, c.key))
        while pending and state.outstanding < policy.max_outstanding:
            times = {c.key: earliest_send(now, c, state, policy) for c in pending}
            ready = [c for c in pending if times[c.key] <= now]
            if not ready:
                wake_times.append(min(times.values()))
                break
            chosen = ready[0]  # pending is already in priority order
            send.append(chosen.key)
            pending.remove(chosen)
            # Later picks in this pass must respect spacing and caps as if this one were already sent.
            state = replace(
                state, outstanding=state.outstanding + 1, recent_prompts=(*state.recent_prompts, now)
            )

    return Plan(send=send, expire=expire, wake_at=min(wake_times, default=None))


def earliest_send(now: datetime, c: Candidate, state: RecipientState, policy: Policy) -> datetime:
    anchor = max(t for t in (c.created_at, c.not_before, c.snoozed_until) if t is not None)
    t = max(now, anchor + policy.jitter(f"job:{c.key}:{anchor.isoformat()}"))
    if policy.min_interval and (last := state.last_prompt) is not None:
        t = max(t, last + policy.min_interval + policy.jitter(f"gap:{c.recipient_id}:{last.isoformat()}"))

    for _ in range(14):  # quiet hours and daily caps can push each other forward; this converges quickly
        local = t.astimezone(state.tz)
        if state.quiet is not None and state.quiet.contains(local.time()):
            end = _next_local(t, state.tz, state.quiet.end)
            t = end + policy.jitter(f"quiet:{c.recipient_id}:{end.astimezone(state.tz).date().isoformat()}")
            continue
        if _sent_on(state, local.date()) >= policy.max_per_day:
            t = _next_local(t, state.tz, time(0))
            continue
        return t
    return t


def _sent_on(state: RecipientState, day: date) -> int:
    return sum(1 for sent in state.recent_prompts if sent.astimezone(state.tz).date() == day)


def _next_local(after: datetime, tz: ZoneInfo, wall: time) -> datetime:
    """The first instant strictly after ``after`` whose local wall-clock time is ``wall``."""
    day = after.astimezone(tz).date()
    for offset in range(3):
        candidate = datetime.combine(day + timedelta(days=offset), wall, tzinfo=tz)
        if candidate > after:
            return candidate
    raise AssertionError("unreachable")
