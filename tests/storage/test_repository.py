from datetime import timedelta

import pytest

from pester.core.clock import FakeClock
from pester.core.errors import IdempotencyConflictError, IllegalTransitionError, JobNotFoundError
from pester.core.models import BatchSubmission, InteractionJob
from pester.core.states import EventType, JobStatus
from pester.storage.repository import Repository


def make(**overrides: object) -> InteractionJob:
    data: dict[str, object] = {"recipient_id": "kate", "prompt": "p", "evaluation": {"prompt": "e"}}
    data.update(overrides)
    return InteractionJob.model_validate(data)


def batch(batch_id: str, *jobs: InteractionJob) -> BatchSubmission:
    return BatchSubmission(batch_id=batch_id, jobs=list(jobs))


async def test_submit_creates_queued_job_and_event(repo: Repository) -> None:
    record, created = await repo.submit_job("a", make(id="j1", metadata={"k": "v"}))
    assert created
    assert record.status is JobStatus.QUEUED
    assert record.spec.id == "j1"
    events = await repo.list_events("a", after=0, limit=10)
    assert [(e.type, e.interaction_id, e.metadata) for e in events] == [
        (EventType.INTERACTION_QUEUED, "j1", {"k": "v"})
    ]


async def test_server_generates_id_when_missing(repo: Repository) -> None:
    first, _ = await repo.submit_job("a", make())
    second, _ = await repo.submit_job("a", make())
    assert first.id != second.id  # no producer id means no dedupe


async def test_identical_replay_returns_existing(repo: Repository, clock: FakeClock) -> None:
    first, _ = await repo.submit_job("a", make(id="j1"))
    clock.advance(timedelta(minutes=5))
    again, created = await repo.submit_job("a", make(id="j1"))
    assert not created
    assert again == first
    assert len(await repo.list_events("a", 0, 10)) == 1


async def test_replay_reflects_current_state(repo: Repository) -> None:
    await repo.submit_job("a", make(id="j1"))
    await repo.cancel("a", "j1")
    again, created = await repo.submit_job("a", make(id="j1"))
    assert not created
    assert again.status is JobStatus.CANCELLED


async def test_same_id_different_content_conflicts(repo: Repository) -> None:
    await repo.submit_job("a", make(id="j1"))
    with pytest.raises(IdempotencyConflictError):
        await repo.submit_job("a", make(id="j1", prompt="different"))


async def test_job_ids_are_scoped_per_client(repo: Repository) -> None:
    _, created_a = await repo.submit_job("a", make(id="j1"))
    _, created_b = await repo.submit_job("b", make(id="j1", prompt="unrelated"))
    assert created_a and created_b


async def test_batch_is_atomic_and_ordered(repo: Repository) -> None:
    records, created = await repo.submit_batch("a", batch("b1", make(id="x"), make(), make(id="y")))
    assert created
    assert [r.id for r in records][::2] == ["x", "y"]
    assert all(r.batch_id == "b1" for r in records)
    events = await repo.list_events("a", 0, 10)
    assert [e.interaction_id for e in events] == [r.id for r in records]
    assert all(e.batch_id == "b1" for e in events)


async def test_batch_replay_returns_same_jobs_even_with_generated_ids(repo: Repository) -> None:
    first, _ = await repo.submit_batch("a", batch("b1", make(), make(prompt="q2")))
    again, created = await repo.submit_batch("a", batch("b1", make(), make(prompt="q2")))
    assert not created
    assert [r.id for r in again] == [r.id for r in first]
    assert len(await repo.list_events("a", 0, 10)) == 2


async def test_batch_with_different_content_conflicts(repo: Repository) -> None:
    await repo.submit_batch("a", batch("b1", make(id="x")))
    with pytest.raises(IdempotencyConflictError):
        await repo.submit_batch("a", batch("b1", make(id="x"), make(id="y")))


async def test_batch_colliding_with_existing_job_writes_nothing(repo: Repository) -> None:
    await repo.submit_job("a", make(id="taken"))
    with pytest.raises(IdempotencyConflictError, match="taken"):
        await repo.submit_batch("a", batch("b1", make(id="fresh"), make(id="taken")))
    with pytest.raises(JobNotFoundError):
        await repo.get_job("a", "fresh")
    # The failed batch id is still available.
    _, created = await repo.submit_batch("a", batch("b1", make(id="fresh")))
    assert created


async def test_single_submit_of_batched_id_conflicts(repo: Repository) -> None:
    await repo.submit_batch("a", batch("b1", make(id="x")))
    with pytest.raises(IdempotencyConflictError):
        await repo.submit_job("a", make(id="x"))


async def test_transition_emits_mapped_events(repo: Repository) -> None:
    await repo.submit_job("a", make(id="j1"))
    await repo.transition("a", "j1", JobStatus.SENDING)
    await repo.transition("a", "j1", JobStatus.AWAITING, {"sent_at": "now"})
    events = await repo.list_events("a", 0, 10)
    assert [e.type for e in events] == [EventType.INTERACTION_QUEUED, EventType.INTERACTION_DELIVERED]
    assert events[1].payload == {"sent_at": "now"}


async def test_illegal_transition_changes_nothing(repo: Repository) -> None:
    await repo.submit_job("a", make(id="j1"))
    with pytest.raises(IllegalTransitionError):
        await repo.transition("a", "j1", JobStatus.COMPLETED)
    assert (await repo.get_job("a", "j1")).status is JobStatus.QUEUED
    assert len(await repo.list_events("a", 0, 10)) == 1


async def test_cancel_is_idempotent_but_not_from_other_terminal_states(repo: Repository) -> None:
    await repo.submit_job("a", make(id="j1"))
    await repo.cancel("a", "j1")
    await repo.cancel("a", "j1")
    assert [e.type for e in await repo.list_events("a", 0, 10)][-1] is EventType.INTERACTION_CANCELLED
    assert len(await repo.list_events("a", 0, 10)) == 2

    await repo.submit_job("a", make(id="j2"))
    await repo.transition("a", "j2", JobStatus.EXPIRED)
    with pytest.raises(IllegalTransitionError):
        await repo.cancel("a", "j2")


async def test_unknown_or_foreign_job_not_found(repo: Repository) -> None:
    await repo.submit_job("a", make(id="j1"))
    with pytest.raises(JobNotFoundError):
        await repo.get_job("b", "j1")
    with pytest.raises(JobNotFoundError):
        await repo.cancel("b", "j1")


async def test_event_cursors_are_gap_free_and_client_scoped(repo: Repository) -> None:
    for i in range(5):
        await repo.submit_job("a", make(id=f"a{i}"))
        await repo.submit_job("b", make(id=f"b{i}"))
    a_events = await repo.list_events("a", 0, 100)
    b_events = await repo.list_events("b", 0, 100)
    assert all(e.interaction_id.startswith("a") for e in a_events)
    assert all(e.interaction_id.startswith("b") for e in b_events)
    all_cursors = sorted(e.cursor for e in a_events + b_events)
    assert all_cursors == list(range(all_cursors[0], all_cursors[0] + 10))


async def test_event_paging(repo: Repository) -> None:
    for i in range(7):
        await repo.submit_job("a", make(id=f"j{i}"))
    seen: list[str] = []
    cursor = 0
    while page := await repo.list_events("a", cursor, 3):
        seen += [e.interaction_id for e in page]
        cursor = page[-1].cursor
    assert seen == [f"j{i}" for i in range(7)]
