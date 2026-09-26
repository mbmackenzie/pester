from itertools import pairwise

import pytest

from pester.core.errors import IllegalTransitionError
from pester.core.states import TERMINAL, TRANSITIONS, EventType, JobStatus, check_transition, event_for

S = JobStatus

HAPPY_PATH = [S.QUEUED, S.SENDING, S.AWAITING, S.ANSWERED, S.EVALUATED, S.COMPLETED]


def test_happy_path_is_legal() -> None:
    for current, target in pairwise(HAPPY_PATH):
        check_transition(current, target)


@pytest.mark.parametrize("status", sorted(TERMINAL))
def test_terminal_states_have_no_exits(status: JobStatus) -> None:
    assert TRANSITIONS[status] == frozenset()
    for target in JobStatus:
        with pytest.raises(IllegalTransitionError):
            check_transition(status, target)


@pytest.mark.parametrize("status", sorted(set(JobStatus) - TERMINAL))
def test_cancel_allowed_from_every_non_terminal_state(status: JobStatus) -> None:
    check_transition(status, S.CANCELLED)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.QUEUED, S.AWAITING),  # must go through SENDING
        (S.QUEUED, S.ANSWERED),
        (S.AWAITING, S.EVALUATED),
        (S.ANSWERED, S.COMPLETED),
        (S.SENDING, S.EXPIRED),  # only undelivered jobs expire
        (S.EVALUATED, S.QUEUED),
    ],
)
def test_illegal_transitions_rejected(current: JobStatus, target: JobStatus) -> None:
    with pytest.raises(IllegalTransitionError):
        check_transition(current, target)


def test_every_legal_transition_has_a_defined_event_mapping() -> None:
    for current, targets in TRANSITIONS.items():
        for target in targets:
            event_for(current, target)  # must not raise


def test_event_mapping() -> None:
    assert event_for(None, S.QUEUED) is EventType.INTERACTION_QUEUED
    assert event_for(S.QUEUED, S.SENDING) is None
    assert event_for(S.SENDING, S.QUEUED) is None
    assert event_for(S.SENDING, S.AWAITING) is EventType.INTERACTION_DELIVERED
    assert event_for(S.AWAITING, S.QUEUED) is EventType.INTERACTION_SNOOZED
    assert event_for(S.AWAITING, S.UNANSWERED) is EventType.INTERACTION_UNANSWERED
    assert event_for(S.QUEUED, S.CANCELLED) is EventType.INTERACTION_CANCELLED
