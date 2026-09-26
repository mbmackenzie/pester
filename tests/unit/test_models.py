import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from pester.core.models import BatchSubmission, InteractionJob

FIXTURE = Path(__file__).parents[2] / "fixtures" / "example_batch.json"


def make(**overrides: object) -> InteractionJob:
    data: dict[str, object] = {
        "recipient_id": "kate",
        "prompt": "Did you water the plants?",
        "evaluation": {"prompt": "Classify."},
    }
    data.update(overrides)
    return InteractionJob.model_validate(data)


def test_example_batch_fixture_is_valid() -> None:
    batch = BatchSubmission.model_validate_json(FIXTURE.read_text())
    assert [job.id for job in batch.jobs] == ["study-001", "plants-001"]


def test_datetimes_normalized_to_utc() -> None:
    job = make(delivery={"not_before": "2026-09-26T09:00:00-04:00"})
    assert job.delivery.not_before == datetime(2026, 9, 26, 13, tzinfo=UTC)


def test_naive_datetimes_rejected() -> None:
    with pytest.raises(ValidationError):
        make(delivery={"not_before": "2026-09-26T09:00:00"})


def test_expiry_must_follow_not_before() -> None:
    with pytest.raises(ValidationError, match="expires_at must be after not_before"):
        make(delivery={"not_before": "2026-09-26T09:00:00Z", "expires_at": "2026-09-26T08:00:00Z"})


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        make(promtp="typo")


@pytest.mark.parametrize("bad_id", ["", "has space", "-leading-dash", "x" * 129, "slash/y"])
def test_invalid_ids_rejected(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        make(id=bad_id)


def test_content_hash_ignores_id_and_formatting() -> None:
    a = make(id="one", delivery={"not_before": "2026-09-26T09:00:00-04:00", "priority": 0.5})
    b = make(id="two", delivery={"not_before": "2026-09-26T13:00:00Z"})
    assert a.content_hash() == b.content_hash()


def test_content_hash_ignores_key_order() -> None:
    a = make(metadata={"x": 1, "y": {"a": 1, "b": 2}})
    b = make(metadata=json.loads('{"y": {"b": 2, "a": 1}, "x": 1}'))
    assert a.content_hash() == b.content_hash()


def test_content_hash_detects_changes() -> None:
    assert make().content_hash() != make(prompt="Did you feed the cat?").content_hash()
    assert make().content_hash() != make(metadata={"k": "v"}).content_hash()


def test_batch_rejects_duplicate_job_ids() -> None:
    job = {"id": "dup", "recipient_id": "kate", "prompt": "p", "evaluation": {"prompt": "e"}}
    with pytest.raises(ValidationError, match="unique"):
        BatchSubmission.model_validate({"batch_id": "b", "jobs": [job, job]})


def test_batch_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        BatchSubmission.model_validate({"batch_id": "b", "jobs": []})
