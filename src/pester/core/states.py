"""Job lifecycle (spec §5). Every legal transition is defined here and nowhere else."""

from enum import StrEnum

from pester.core.errors import IllegalTransitionError


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    SENDING = "SENDING"
    AWAITING = "AWAITING"
    ANSWERED = "ANSWERED"
    EVALUATED = "EVALUATED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    UNANSWERED = "UNANSWERED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class EventType(StrEnum):
    INTERACTION_QUEUED = "INTERACTION_QUEUED"
    INTERACTION_DELIVERED = "INTERACTION_DELIVERED"
    INTERACTION_ANSWERED = "INTERACTION_ANSWERED"
    INTERACTION_EVALUATED = "INTERACTION_EVALUATED"
    INTERACTION_COMPLETED = "INTERACTION_COMPLETED"
    INTERACTION_SNOOZED = "INTERACTION_SNOOZED"
    INTERACTION_SKIPPED = "INTERACTION_SKIPPED"
    INTERACTION_UNANSWERED = "INTERACTION_UNANSWERED"
    INTERACTION_EXPIRED = "INTERACTION_EXPIRED"
    INTERACTION_CANCELLED = "INTERACTION_CANCELLED"
    INTERACTION_FAILED = "INTERACTION_FAILED"
    INTERACTION_LATE_RESPONSE = "INTERACTION_LATE_RESPONSE"


S = JobStatus

TERMINAL: frozenset[JobStatus] = frozenset(
    {S.COMPLETED, S.CANCELLED, S.EXPIRED, S.UNANSWERED, S.SKIPPED, S.FAILED}
)

# Cancellation is allowed from every non-terminal state and is added below.
_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    S.QUEUED: frozenset({S.SENDING, S.EXPIRED}),
    S.SENDING: frozenset({S.AWAITING, S.QUEUED, S.FAILED}),  # SENDING -> QUEUED: retry after transient error
    S.AWAITING: frozenset({S.ANSWERED, S.UNANSWERED, S.SKIPPED, S.QUEUED}),  # AWAITING -> QUEUED: snooze
    S.ANSWERED: frozenset({S.EVALUATED, S.FAILED}),
    S.EVALUATED: frozenset({S.COMPLETED}),
}

TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    status: (_TRANSITIONS.get(status, frozenset()) | {S.CANCELLED}) if status not in TERMINAL else frozenset()
    for status in JobStatus
}

_EVENT_FOR_TARGET: dict[JobStatus, EventType] = {
    S.AWAITING: EventType.INTERACTION_DELIVERED,
    S.ANSWERED: EventType.INTERACTION_ANSWERED,
    S.EVALUATED: EventType.INTERACTION_EVALUATED,
    S.COMPLETED: EventType.INTERACTION_COMPLETED,
    S.SKIPPED: EventType.INTERACTION_SKIPPED,
    S.UNANSWERED: EventType.INTERACTION_UNANSWERED,
    S.EXPIRED: EventType.INTERACTION_EXPIRED,
    S.CANCELLED: EventType.INTERACTION_CANCELLED,
    S.FAILED: EventType.INTERACTION_FAILED,
}


def check_transition(current: JobStatus, target: JobStatus) -> None:
    if target not in TRANSITIONS[current]:
        raise IllegalTransitionError(current, target)


def event_for(current: JobStatus | None, target: JobStatus) -> EventType | None:
    """The event emitted by a transition, or None for internal-only transitions."""
    if current is None:
        return EventType.INTERACTION_QUEUED if target is S.QUEUED else None
    if current is S.AWAITING and target is S.QUEUED:
        return EventType.INTERACTION_SNOOZED
    if target in (S.QUEUED, S.SENDING):
        return None
    return _EVENT_FOR_TARGET[target]
