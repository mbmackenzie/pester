from datetime import UTC, datetime, timedelta

from pester.scheduler.policy import Candidate, decide

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
H = timedelta(hours=1)


def cand(key: int, recipient: str = "kate", priority: float = 0.5, age: int = 0, **kw: datetime) -> Candidate:
    return Candidate(key=key, recipient_id=recipient, priority=priority, created_at=NOW - age * H, **kw)


def test_nothing_to_do() -> None:
    plan = decide(NOW, [], {}, max_outstanding=1)
    assert plan.send == [] and plan.expire == [] and plan.wake_at is None


def test_highest_priority_then_oldest_first() -> None:
    jobs = [cand(1, priority=0.5, age=5), cand(2, priority=0.9, age=0), cand(3, priority=0.5, age=9)]
    assert decide(NOW, jobs, {}, max_outstanding=1).send == [2]
    assert decide(NOW, jobs, {}, max_outstanding=3).send == [2, 3, 1]


def test_ties_broken_by_key() -> None:
    assert decide(NOW, [cand(7), cand(3)], {}, max_outstanding=1).send == [3]


def test_outstanding_limits_per_recipient() -> None:
    jobs = [cand(1, "kate"), cand(2, "kate"), cand(3, "sam")]
    plan = decide(NOW, jobs, {"kate": 1}, max_outstanding=1)
    assert plan.send == [3]
    plan = decide(NOW, jobs, {"kate": 1}, max_outstanding=2)
    assert sorted(plan.send) == [1, 3]


def test_outstanding_above_limit_sends_nothing() -> None:
    assert decide(NOW, [cand(1)], {"kate": 5}, max_outstanding=1).send == []


def test_not_before_in_future_waits_and_sets_wake() -> None:
    plan = decide(NOW, [cand(1, not_before=NOW + 2 * H), cand(2, not_before=NOW + H)], {}, 1)
    assert plan.send == []
    assert plan.wake_at == NOW + H


def test_not_before_now_is_eligible() -> None:
    assert decide(NOW, [cand(1, not_before=NOW)], {}, 1).send == [1]


def test_expired_jobs_expire_even_when_blocked() -> None:
    jobs = [cand(1, expires_at=NOW), cand(2, expires_at=NOW - H, not_before=NOW + H)]
    plan = decide(NOW, jobs, {"kate": 1}, max_outstanding=1)
    assert sorted(plan.expire) == [1, 2]
    assert plan.send == []


def test_future_expiry_of_waiting_job_sets_wake() -> None:
    plan = decide(NOW, [cand(1, expires_at=NOW + 3 * H)], {"kate": 1}, max_outstanding=1)
    assert plan.send == [] and plan.expire == []
    assert plan.wake_at == NOW + 3 * H
