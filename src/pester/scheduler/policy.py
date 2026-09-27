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
from enum import StrEnum
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


class Hold(StrEnum):
    """Why a job can't be sent yet (beyond its own jitter)."""

    NOT_BEFORE = "not_before"  # the producer scheduled it for later
    SNOOZED = "snoozed"
    SPACING = "spacing"  # too soon after the last question
    QUIET_HOURS = "quiet_hours"
    DAILY_CAP = "daily_cap"


def earliest_send(now: datetime, c: Candidate, state: RecipientState, policy: Policy) -> datetime:
    return send_time(now, c, state, policy)[0]


def send_time(
    now: datetime, c: Candidate, state: RecipientState, policy: Policy
) -> tuple[datetime, frozenset[Hold]]:
    """When ``c`` may be sent, and which rules push it past now."""
    holds: set[Hold] = set()
    anchor = max(t for t in (c.created_at, c.not_before, c.snoozed_until) if t is not None)
    if anchor > now:
        holds.add(Hold.SNOOZED if anchor == c.snoozed_until else Hold.NOT_BEFORE)
    t = max(now, anchor + policy.jitter(f"job:{c.key}:{anchor.isoformat()}"))
    if policy.min_interval and (last := state.last_prompt) is not None:
        spaced = last + policy.min_interval + policy.jitter(f"gap:{c.recipient_id}:{last.isoformat()}")
        if spaced > t:
            holds.add(Hold.SPACING)
            t = spaced

    for _ in range(14):  # quiet hours and daily caps can push each other forward; this converges quickly
        local = t.astimezone(state.tz)
        if state.quiet is not None and (released := _quiet_release(t, c.recipient_id, state, policy)) > t:
            holds.add(Hold.QUIET_HOURS)
            t = released
            continue
        if _sent_on(state, local.date()) >= policy.max_per_day:
            holds.add(Hold.DAILY_CAP)
            t = _next_local(t, state.tz, time(0))
            continue
        break
    return t, frozenset(holds)


@dataclass(frozen=True)
class NextSend:
    """The next of a recipient's queued jobs to go out, and what's holding it."""

    key: int
    at: datetime  # when the time-based rules allow it (it may still wait on the rules below)
    holds: frozenset[Hold]
    waiting_on_answer: bool  # the recipient has as many questions open as they may
    paused: bool


def next_send(
    now: datetime, pending: Iterable[Candidate], state: RecipientState, policy: Policy
) -> NextSend | None:
    """Explain what happens next for one recipient, consistently with ``decide``."""
    live = sorted(
        (c for c in pending if c.expires_at is None or c.expires_at > now),
        key=lambda c: (-c.priority, c.created_at, c.key),
    )
    if not live:
        return None
    timed = [(send_time(now, c, state, policy), c) for c in live]
    (at, holds), chosen = min(timed, key=lambda item: item[0][0])  # ties keep priority order
    return NextSend(
        key=chosen.key,
        at=at,
        holds=holds,
        waiting_on_answer=state.outstanding >= policy.max_outstanding,
        paused=state.paused,
    )


def _quiet_release(t: datetime, recipient_id: str, state: RecipientState, policy: Policy) -> datetime:
    """When quiet hours affecting ``t`` end, including that night's jitter; ``t`` itself if unaffected.

    Each night's window effectively runs until ``end + jitter(that day)``. That must hold whether or not ``t``
    is inside the nominal window, or re-planning just after ``end`` would skip the jitter.
    """
    assert state.quiet is not None

    def jittered(end: datetime) -> datetime:
        day = end.astimezone(state.tz).date().isoformat()
        return end + policy.jitter(f"quiet:{recipient_id}:{day}")

    if state.quiet.contains(t.astimezone(state.tz).time()):
        return jittered(_next_local(t, state.tz, state.quiet.end))
    today = t.astimezone(state.tz).date()
    for day in (today - timedelta(days=1), today):
        end = datetime.combine(day, state.quiet.end, tzinfo=state.tz)
        if end <= t < (release := jittered(end)):
            return release
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
