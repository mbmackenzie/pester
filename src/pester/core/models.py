"""Producer-facing data model (spec §4, §6)."""

import hashlib
import json
from datetime import UTC
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from pester.core.states import EventType, JobStatus

UtcDatetime = Annotated[AwareDatetime, AfterValidator(lambda d: d.astimezone(UTC))]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvaluationSpec(_Model):
    evaluator: str = "llm"
    prompt: str = Field(min_length=1)
    context: dict[str, Any] | list[Any] | str | None = None
    output_schema: dict[str, Any] | None = None
    model: str | None = None
    prompt_version: str | None = None


class DeliverySpec(_Model):
    channel: str | None = None
    not_before: UtcDatetime | None = None
    expires_at: UtcDatetime | None = None
    answer_within_seconds: int | None = Field(default=None, gt=0)
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    prompt_rendering: Literal["verbatim", "personality"] = "verbatim"
    allow_reminder: bool = False
    reminder_after_seconds: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        if self.not_before and self.expires_at and self.expires_at <= self.not_before:
            raise ValueError("expires_at must be after not_before")
        return self


class Provenance(_Model):
    producer: str | None = None
    producer_version: str | None = None
    source_revision: str | None = None
    generator_version: str | None = None
    evaluation_version: str | None = None
    created_by: str | None = None


class InteractionJob(_Model):
    """A job as submitted by a producer. Immutable once accepted."""

    id: Identifier | None = None
    recipient_id: str
    prompt: str = Field(min_length=1)
    response_options: list[str] | None = Field(default=None, min_length=1)
    evaluation: EvaluationSpec
    personality_id: str | None = None
    delivery: DeliverySpec = DeliverySpec()
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance | None = None

    def content_hash(self) -> str:
        """Hash of everything except ``id``; equal content hashes equal regardless of formatting."""
        return _hash(self.model_dump(mode="json", exclude={"id"}))


class BatchSubmission(_Model):
    batch_id: Identifier
    jobs: list[InteractionJob] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _check_unique_ids(self) -> Self:
        ids = [job.id for job in self.jobs if job.id is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("job ids within a batch must be unique")
        return self

    def content_hash(self) -> str:
        return _hash([{"id": job.id, "hash": job.content_hash()} for job in self.jobs])


class JobRecord(_Model):
    """A stored job: the immutable submission plus its current lifecycle state."""

    pk: int
    client_id: str
    id: str
    batch_id: str | None
    status: JobStatus
    created_at: UtcDatetime
    updated_at: UtcDatetime
    spec: InteractionJob


class DeliveryKind(StrEnum):
    PROMPT = "PROMPT"
    FEEDBACK = "FEEDBACK"


class DeliveryStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class HumanResponse(_Model):
    interaction_id: str
    text: str
    selected_option: str | None = None
    channel: str
    address: str
    external_id: str
    received_at: UtcDatetime
    raw: dict[str, Any] | None = None


class Event(_Model):
    cursor: int
    event_id: str
    type: EventType
    interaction_id: str
    batch_id: str | None
    occurred_at: UtcDatetime
    payload: dict[str, Any]
    metadata: dict[str, Any]


def _hash(data: Any) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()
