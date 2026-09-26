"""Scheduling policy as a pure function (spec §8). No I/O, no clock: everything is an argument."""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Candidate:
    key: int
    recipient_id: str
    priority: float
    created_at: datetime
    not_before: datetime | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class Plan:
    send: list[int] = field(default_factory=list[int])
    expire: list[int] = field(default_factory=list[int])
    wake_at: datetime | None = None  # earliest future time at which the plan could change


def decide(
    now: datetime,
    candidates: Iterable[Candidate],
    outstanding: Mapping[str, int],
    max_outstanding: int,
) -> Plan:
    send: list[int] = []
    expire: list[int] = []
    wake_times: list[datetime] = []
    eligible: defaultdict[str, list[Candidate]] = defaultdict(list)

    for c in candidates:
        if c.expires_at is not None and c.expires_at <= now:
            expire.append(c.key)
            continue
        if c.not_before is not None and c.not_before > now:
            wake_times.append(c.not_before)
            continue
        eligible[c.recipient_id].append(c)
        if c.expires_at is not None:
            wake_times.append(c.expires_at)

    for recipient_id, jobs in eligible.items():
        slots = max(0, max_outstanding - outstanding.get(recipient_id, 0))
        jobs.sort(key=lambda c: (-c.priority, c.created_at, c.key))
        send.extend(c.key for c in jobs[:slots])

    return Plan(send=send, expire=expire, wake_at=min(wake_times, default=None))
