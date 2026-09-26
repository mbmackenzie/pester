"""All persistence. Every state change and its event are written in one transaction."""

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

import aiosqlite

from pester.core.clock import Clock
from pester.core.errors import IdempotencyConflictError, JobNotFoundError
from pester.core.ids import new_id
from pester.core.messages import InboundMessage, OutboundMessage
from pester.core.models import (
    BatchSubmission,
    DeliveryKind,
    DeliveryStatus,
    EvaluationOutcome,
    Event,
    HumanResponse,
    InteractionJob,
    JobRecord,
)
from pester.core.states import EventType, JobStatus, check_transition, event_for
from pester.storage.db import Database, from_db, to_db

_JOB_COLUMNS = "pk, client_id, job_id, batch_id, status, content_hash, spec, created_at, updated_at"
_J_JOB_COLUMNS = ", ".join(f"j.{c.strip()}" for c in _JOB_COLUMNS.split(","))

# Jobs that occupy one of a recipient's outstanding slots.
OUTSTANDING: frozenset[JobStatus] = frozenset(
    {JobStatus.SENDING, JobStatus.AWAITING, JobStatus.ANSWERED, JobStatus.EVALUATED}
)

# The job status a delivery of each kind requires at send time.
_EXPECTED_JOB_STATUS = {DeliveryKind.PROMPT: JobStatus.SENDING, DeliveryKind.FEEDBACK: JobStatus.EVALUATED}


class IngestOutcome(StrEnum):
    DUPLICATE = "DUPLICATE"
    ANSWERED = "ANSWERED"
    LATE = "LATE"
    AMBIGUOUS = "AMBIGUOUS"
    NOTHING_PENDING = "NOTHING_PENDING"
    COMMAND = "COMMAND"


@dataclass(frozen=True)
class IngestResult:
    outcome: IngestOutcome
    job: JobRecord | None = None


@dataclass(frozen=True)
class PendingDelivery:
    pk: int
    kind: DeliveryKind
    channel: str
    address: str
    message: OutboundMessage
    job: JobRecord


@dataclass(frozen=True)
class AnsweredJob:
    job: JobRecord
    response: HumanResponse


class Repository:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    # ---- Producer operations ------------------------------------------------------------------------

    async def submit_job(self, client_id: str, job: InteractionJob) -> tuple[JobRecord, bool]:
        """Store a job. Returns ``(record, created)``; a replay of identical content is not created."""
        content_hash = job.content_hash()
        async with self._db.transaction() as conn:
            if job.id is not None:
                row = await _fetch_job(conn, client_id, job.id)
                if row is not None:
                    if row["content_hash"] != content_hash or row["batch_id"] is not None:
                        raise IdempotencyConflictError(
                            f"job {job.id!r} already exists with different content"
                        )
                    return _to_record(row), False
            row = await self._insert_job(conn, client_id, job, content_hash, batch_id=None, position=None)
            return _to_record(row), True

    async def submit_batch(self, client_id: str, batch: BatchSubmission) -> tuple[list[JobRecord], bool]:
        """Store a batch atomically. Returns ``(records, created)``; an identical replay is not created."""
        content_hash = batch.content_hash()
        async with self._db.transaction() as conn:
            existing = await _fetch_one(
                conn,
                "SELECT content_hash FROM batches WHERE client_id = ? AND batch_id = ?",
                (client_id, batch.batch_id),
            )
            if existing is not None:
                if existing["content_hash"] != content_hash:
                    raise IdempotencyConflictError(
                        f"batch {batch.batch_id!r} already exists with different content"
                    )
                rows = await conn.execute_fetchall(
                    f"SELECT {_JOB_COLUMNS} FROM jobs WHERE client_id = ? AND batch_id = ? "
                    "ORDER BY batch_position",
                    (client_id, batch.batch_id),
                )
                return [_to_record(row) for row in rows], False

            ids = [job.id for job in batch.jobs if job.id is not None]
            if ids:
                placeholders = ",".join("?" * len(ids))
                taken = await conn.execute_fetchall(
                    f"SELECT job_id FROM jobs WHERE client_id = ? AND job_id IN ({placeholders})",
                    (client_id, *ids),
                )
                if taken:
                    raise IdempotencyConflictError(
                        f"job ids already exist outside this batch: {sorted(row[0] for row in taken)}"
                    )

            await conn.execute(
                "INSERT INTO batches (client_id, batch_id, content_hash, created_at) VALUES (?, ?, ?, ?)",
                (client_id, batch.batch_id, content_hash, to_db(self._clock.now())),
            )
            records: list[JobRecord] = []
            for position, job in enumerate(batch.jobs):
                row = await self._insert_job(
                    conn, client_id, job, job.content_hash(), batch_id=batch.batch_id, position=position
                )
                records.append(_to_record(row))
            return records, True

    async def get_job(self, client_id: str, job_id: str) -> JobRecord:
        async with self._db.read() as conn:
            row = await _fetch_job(conn, client_id, job_id)
        if row is None:
            raise JobNotFoundError(job_id)
        return _to_record(row)

    async def transition(
        self,
        client_id: str,
        job_id: str,
        target: JobStatus,
        payload: dict[str, Any] | None = None,
    ) -> JobRecord:
        async with self._db.transaction() as conn:
            row = await _fetch_job(conn, client_id, job_id)
            if row is None:
                raise JobNotFoundError(job_id)
            return await self._apply(conn, row, target, payload)

    async def cancel(self, client_id: str, job_id: str) -> JobRecord:
        """Cancel a job. Cancelling an already-cancelled job is a no-op."""
        async with self._db.transaction() as conn:
            row = await _fetch_job(conn, client_id, job_id)
            if row is None:
                raise JobNotFoundError(job_id)
            if row["status"] == JobStatus.CANCELLED:
                return _to_record(row)
            return await self._apply(conn, row, JobStatus.CANCELLED)

    async def list_events(self, client_id: str, after: int, limit: int) -> list[Event]:
        async with self._db.read() as conn:
            rows = await conn.execute_fetchall(
                """
                SELECT e.cursor, e.event_id, e.type, e.payload, e.occurred_at,
                       j.job_id, j.batch_id, json_extract(j.spec, '$.metadata') AS metadata
                FROM events e JOIN jobs j ON j.pk = e.job_pk
                WHERE e.client_id = ? AND e.cursor > ?
                ORDER BY e.cursor
                LIMIT ?
                """,
                (client_id, after, limit),
            )
        return [
            Event(
                cursor=row["cursor"],
                event_id=row["event_id"],
                type=EventType(row["type"]),
                interaction_id=row["job_id"],
                batch_id=row["batch_id"],
                occurred_at=from_db(row["occurred_at"]),
                payload=json.loads(row["payload"]),
                metadata=json.loads(row["metadata"]),
            )
            for row in rows
        ]

    # ---- Scheduling ---------------------------------------------------------------------------------

    async def queued_jobs(self) -> list[JobRecord]:
        async with self._db.read() as conn:
            rows = await conn.execute_fetchall(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE status = ? ORDER BY pk", (JobStatus.QUEUED,)
            )
        return [_to_record(row) for row in rows]

    async def outstanding_counts(self) -> dict[str, int]:
        placeholders = ",".join("?" * len(OUTSTANDING))
        async with self._db.read() as conn:
            rows = await conn.execute_fetchall(
                f"SELECT recipient_id, COUNT(*) FROM jobs WHERE status IN ({placeholders}) "
                "GROUP BY recipient_id",
                tuple(OUTSTANDING),
            )
        return {row[0]: int(row[1]) for row in rows}

    async def claim_for_send(self, job_pk: int, channel: str, address: str, message: OutboundMessage) -> bool:
        """QUEUED -> SENDING and create the pending prompt delivery. False if the job is no longer QUEUED."""
        async with self._db.transaction() as conn:
            row = await _fetch_job_by_pk(conn, job_pk)
            if row is None or row["status"] != JobStatus.QUEUED:
                return False
            await self._apply(conn, row, JobStatus.SENDING)
            await self._insert_delivery(conn, job_pk, DeliveryKind.PROMPT, channel, address, message)
            return True

    async def expire(self, job_pk: int) -> bool:
        async with self._db.transaction() as conn:
            row = await _fetch_job_by_pk(conn, job_pk)
            if row is None or row["status"] != JobStatus.QUEUED:
                return False
            await self._apply(conn, row, JobStatus.EXPIRED)
            return True

    # ---- Delivery -----------------------------------------------------------------------------------

    async def claim_next_delivery(self) -> PendingDelivery | None:
        """Mark the oldest pending delivery SENDING and return it.

        Deliveries whose job has moved on (e.g. was cancelled) are marked CANCELLED and skipped.
        """
        async with self._db.transaction() as conn:
            while True:
                row = await _fetch_one(
                    conn,
                    f"SELECT d.pk AS d_pk, d.kind, d.channel, d.address, d.message, {_J_JOB_COLUMNS} "
                    "FROM deliveries d JOIN jobs j ON j.pk = d.job_pk "
                    "WHERE d.status = ? ORDER BY d.pk LIMIT 1",
                    (DeliveryStatus.PENDING,),
                )
                if row is None:
                    return None
                kind = DeliveryKind(row["kind"])
                now = to_db(self._clock.now())
                if row["status"] != _EXPECTED_JOB_STATUS[kind]:
                    await conn.execute(
                        "UPDATE deliveries SET status = ?, updated_at = ? WHERE pk = ?",
                        (DeliveryStatus.CANCELLED, now, row["d_pk"]),
                    )
                    continue
                await conn.execute(
                    "UPDATE deliveries SET status = ?, attempts = attempts + 1, updated_at = ? WHERE pk = ?",
                    (DeliveryStatus.SENDING, now, row["d_pk"]),
                )
                return PendingDelivery(
                    pk=row["d_pk"],
                    kind=kind,
                    channel=row["channel"],
                    address=row["address"],
                    message=OutboundMessage.model_validate_json(row["message"]),
                    job=_to_record(row),
                )

    async def delivery_sent(self, delivery: PendingDelivery, external_id: str, sent_at: datetime) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE deliveries SET status = ?, external_id = ?, sent_at = ?, updated_at = ? WHERE pk = ?",
                (DeliveryStatus.SENT, external_id, to_db(sent_at), to_db(self._clock.now()), delivery.pk),
            )
            row = await _fetch_job_by_pk(conn, delivery.job.pk)
            assert row is not None
            if row["status"] != _EXPECTED_JOB_STATUS[delivery.kind]:
                return  # the job was cancelled while the message was in flight
            if delivery.kind is DeliveryKind.PROMPT:
                payload = {"delivery": {"channel": delivery.channel, "sent_at": to_db(sent_at)}}
                await self._apply(conn, row, JobStatus.AWAITING, payload)
            else:
                payload = {"feedback": {"text": delivery.message.text, "sent_at": to_db(sent_at)}}
                await self._apply(conn, row, JobStatus.COMPLETED, payload)

    async def delivery_failed(self, delivery: PendingDelivery, error: str) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE deliveries SET status = ?, error = ?, updated_at = ? WHERE pk = ?",
                (DeliveryStatus.FAILED, error, to_db(self._clock.now()), delivery.pk),
            )
            row = await _fetch_job_by_pk(conn, delivery.job.pk)
            assert row is not None
            if row["status"] == _EXPECTED_JOB_STATUS[delivery.kind]:
                reason = f"{delivery.kind.lower()}_delivery_failed"
                await self._apply(conn, row, JobStatus.FAILED, {"reason": reason, "error": error})

    # ---- Inbound ------------------------------------------------------------------------------------

    async def record_inbound(self, message: InboundMessage, outcome: IngestOutcome) -> bool:
        """Record an inbound message for dedupe. False if it was already seen."""
        async with self._db.transaction() as conn:
            return await self._record_inbound(conn, message, outcome)

    async def ingest_response(self, recipient_id: str, message: InboundMessage) -> IngestResult:
        """Route an inbound message to a job and record it, all in one transaction (spec §9.2)."""
        async with self._db.transaction() as conn:
            job_row = await self._route(conn, recipient_id, message)
            if isinstance(job_row, IngestOutcome):
                outcome = job_row
                if not await self._record_inbound(conn, message, outcome):
                    return IngestResult(IngestOutcome.DUPLICATE)
                return IngestResult(outcome)

            status = JobStatus(job_row["status"])
            outcome = IngestOutcome.ANSWERED if status is JobStatus.AWAITING else IngestOutcome.LATE
            if not await self._record_inbound(conn, message, outcome):
                return IngestResult(IngestOutcome.DUPLICATE)

            response = HumanResponse(
                interaction_id=job_row["job_id"],
                text=message.text or message.selected_option or "",
                selected_option=message.selected_option,
                channel=message.channel,
                address=message.sender_address,
                external_id=message.external_id,
                received_at=message.received_at,
                raw=message.raw,
            )
            payload = {"response": _response_payload(response)}
            if outcome is IngestOutcome.LATE:
                await self._append_event(
                    conn, job_row["client_id"], job_row["pk"], EventType.INTERACTION_LATE_RESPONSE, payload
                )
                return IngestResult(outcome, _to_record(job_row))

            await conn.execute(
                "INSERT INTO responses (job_pk, text, selected_option, channel, address, external_id, "
                "received_at, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_row["pk"],
                    response.text,
                    response.selected_option,
                    response.channel,
                    response.address,
                    response.external_id,
                    to_db(response.received_at),
                    json.dumps(response.raw) if response.raw is not None else None,
                ),
            )
            return IngestResult(outcome, await self._apply(conn, job_row, JobStatus.ANSWERED, payload))

    async def _route(
        self, conn: aiosqlite.Connection, recipient_id: str, message: InboundMessage
    ) -> aiosqlite.Row | IngestOutcome:
        if message.reply_to_external_id is not None:
            row = await _fetch_one(
                conn,
                f"SELECT {_J_JOB_COLUMNS} FROM deliveries d JOIN jobs j ON j.pk = d.job_pk "
                "WHERE d.channel = ? AND d.address = ? AND d.external_id = ? AND j.recipient_id = ?",
                (message.channel, message.sender_address, message.reply_to_external_id, recipient_id),
            )
            if row is not None:
                return row
        rows = list(
            await conn.execute_fetchall(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE recipient_id = ? AND status = ?",
                (recipient_id, JobStatus.AWAITING),
            )
        )
        if len(rows) == 1:
            return rows[0]
        return IngestOutcome.AMBIGUOUS if rows else IngestOutcome.NOTHING_PENDING

    # ---- Evaluation ---------------------------------------------------------------------------------

    async def next_answered(self) -> AnsweredJob | None:
        async with self._db.read() as conn:
            row = await _fetch_one(
                conn,
                f"SELECT {_J_JOB_COLUMNS}, r.text, r.selected_option, r.channel AS r_channel, "
                "r.address AS r_address, r.external_id AS r_external_id, r.received_at, r.raw "
                "FROM jobs j JOIN responses r ON r.job_pk = j.pk "
                "WHERE j.status = ? ORDER BY j.updated_at, j.pk LIMIT 1",
                (JobStatus.ANSWERED,),
            )
        if row is None:
            return None
        return AnsweredJob(
            job=_to_record(row),
            response=HumanResponse(
                interaction_id=row["job_id"],
                text=row["text"],
                selected_option=row["selected_option"],
                channel=row["r_channel"],
                address=row["r_address"],
                external_id=row["r_external_id"],
                received_at=from_db(row["received_at"]),
                raw=json.loads(row["raw"]) if row["raw"] else None,
            ),
        )

    async def evaluation_succeeded(
        self,
        answered: AnsweredJob,
        *,
        evaluator: str,
        outcome: EvaluationOutcome,
        personality_id: str,
        personality_fallback: bool,
        feedback: OutboundMessage,
    ) -> None:
        """Store the evaluation, move the job to EVALUATED, and queue the feedback message."""
        async with self._db.transaction() as conn:
            job_pk = answered.job.pk
            await conn.execute(
                "INSERT INTO evaluations (job_pk, status, evaluator, model, result, feedback_facts, raw, "
                "request, usage, latency_ms, attempts, personality_id, personality_fallback, feedback_text, "
                "created_at) VALUES (?, 'SUCCESS', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_pk,
                    evaluator,
                    outcome.model,
                    json.dumps(outcome.result),
                    outcome.feedback_facts,
                    _json_or_none(outcome.raw),
                    _json_or_none(outcome.request),
                    _json_or_none(outcome.usage),
                    outcome.latency_ms,
                    outcome.attempts,
                    personality_id,
                    int(personality_fallback),
                    feedback.text,
                    to_db(self._clock.now()),
                ),
            )
            row = await _fetch_job_by_pk(conn, job_pk)
            assert row is not None
            if row["status"] != JobStatus.ANSWERED:
                return  # cancelled during evaluation; keep the evaluation for audit only
            prompt = await _fetch_one(
                conn,
                "SELECT channel, address, sent_at FROM deliveries "
                "WHERE job_pk = ? AND kind = ? AND status = ? ORDER BY pk DESC LIMIT 1",
                (job_pk, DeliveryKind.PROMPT, DeliveryStatus.SENT),
            )
            assert prompt is not None
            sent_at = from_db(prompt["sent_at"])
            answered_at = answered.response.received_at
            payload = {
                "response": _response_payload(answered.response),
                "evaluation": {
                    "result": outcome.result,
                    "evaluator": evaluator,
                    "model": outcome.model,
                    "usage": outcome.usage,
                    "latency_ms": outcome.latency_ms,
                    "attempts": outcome.attempts,
                },
                "feedback": {
                    "text": feedback.text,
                    "personality": personality_id,
                    "personality_fallback": personality_fallback,
                },
                "delivery": {
                    "sent_at": to_db(sent_at),
                    "answered_at": to_db(answered_at),
                    "response_latency_s": round((answered_at - sent_at).total_seconds(), 3),
                },
            }
            await self._apply(conn, row, JobStatus.EVALUATED, payload)
            await self._insert_delivery(
                conn, job_pk, DeliveryKind.FEEDBACK, prompt["channel"], prompt["address"], feedback
            )

    async def evaluation_failed(
        self,
        answered: AnsweredJob,
        *,
        evaluator: str,
        error: str,
        attempts: int = 1,
        request: dict[str, Any] | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        async with self._db.transaction() as conn:
            job_pk = answered.job.pk
            await conn.execute(
                "INSERT INTO evaluations (job_pk, status, evaluator, error, attempts, request, raw, "
                "created_at) VALUES (?, 'FAILED', ?, ?, ?, ?, ?, ?)",
                (
                    job_pk,
                    evaluator,
                    error,
                    attempts,
                    _json_or_none(request),
                    _json_or_none(raw),
                    to_db(self._clock.now()),
                ),
            )
            row = await _fetch_job_by_pk(conn, job_pk)
            assert row is not None
            if row["status"] == JobStatus.ANSWERED:
                payload = {
                    "reason": "evaluation_failed",
                    "error": error,
                    "evaluator": evaluator,
                    "attempts": attempts,
                }
                await self._apply(conn, row, JobStatus.FAILED, payload)

    # ---- Internals ----------------------------------------------------------------------------------

    async def _apply(
        self,
        conn: aiosqlite.Connection,
        row: aiosqlite.Row,
        target: JobStatus,
        payload: dict[str, Any] | None = None,
    ) -> JobRecord:
        current = JobStatus(row["status"])
        check_transition(current, target)
        await conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE pk = ?",
            (target, to_db(self._clock.now()), row["pk"]),
        )
        if (event_type := event_for(current, target)) is not None:
            await self._append_event(conn, row["client_id"], row["pk"], event_type, payload or {})
        updated = await _fetch_job_by_pk(conn, row["pk"])
        assert updated is not None
        return _to_record(updated)

    async def _insert_job(
        self,
        conn: aiosqlite.Connection,
        client_id: str,
        job: InteractionJob,
        content_hash: str,
        *,
        batch_id: str | None,
        position: int | None,
    ) -> aiosqlite.Row:
        job_id = job.id or new_id()
        stored = job.model_copy(update={"id": job_id})
        now = to_db(self._clock.now())
        cursor = await conn.execute(
            """
            INSERT INTO jobs (client_id, job_id, batch_id, batch_position, recipient_id, status, priority,
                              not_before, expires_at, content_hash, spec, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                client_id,
                job_id,
                batch_id,
                position,
                job.recipient_id,
                JobStatus.QUEUED,
                job.delivery.priority,
                to_db(job.delivery.not_before) if job.delivery.not_before else None,
                to_db(job.delivery.expires_at) if job.delivery.expires_at else None,
                content_hash,
                stored.model_dump_json(),
                now,
                now,
            ),
        )
        assert cursor.lastrowid is not None
        await self._append_event(conn, client_id, cursor.lastrowid, EventType.INTERACTION_QUEUED, {})
        row = await _fetch_job(conn, client_id, job_id)
        assert row is not None
        return row

    async def _insert_delivery(
        self,
        conn: aiosqlite.Connection,
        job_pk: int,
        kind: DeliveryKind,
        channel: str,
        address: str,
        message: OutboundMessage,
    ) -> None:
        now = to_db(self._clock.now())
        await conn.execute(
            "INSERT INTO deliveries (job_pk, kind, channel, address, message, status, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_pk, kind, channel, address, message.model_dump_json(), DeliveryStatus.PENDING, now, now),
        )

    async def _record_inbound(
        self, conn: aiosqlite.Connection, message: InboundMessage, outcome: IngestOutcome
    ) -> bool:
        cursor = await conn.execute(
            "INSERT INTO inbound_messages (channel, address, external_id, outcome, received_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (
                message.channel,
                message.sender_address,
                message.external_id,
                outcome,
                to_db(message.received_at),
            ),
        )
        return cursor.rowcount == 1

    async def _append_event(
        self,
        conn: aiosqlite.Connection,
        client_id: str,
        job_pk: int,
        event_type: EventType,
        payload: dict[str, Any],
    ) -> None:
        await conn.execute(
            "INSERT INTO events (event_id, client_id, job_pk, type, payload, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_id(), client_id, job_pk, event_type, json.dumps(payload), to_db(self._clock.now())),
        )


def _json_or_none(value: object) -> str | None:
    return json.dumps(value) if value is not None else None


def _response_payload(response: HumanResponse) -> dict[str, Any]:
    return {
        "text": response.text,
        "selected_option": response.selected_option,
        "received_at": to_db(response.received_at),
    }


async def _fetch_one(conn: aiosqlite.Connection, sql: str, params: tuple[Any, ...]) -> aiosqlite.Row | None:
    async with conn.execute(sql, params) as cursor:
        return await cursor.fetchone()


async def _fetch_job(conn: aiosqlite.Connection, client_id: str, job_id: str) -> aiosqlite.Row | None:
    return await _fetch_one(
        conn, f"SELECT {_JOB_COLUMNS} FROM jobs WHERE client_id = ? AND job_id = ?", (client_id, job_id)
    )


async def _fetch_job_by_pk(conn: aiosqlite.Connection, pk: int) -> aiosqlite.Row | None:
    return await _fetch_one(conn, f"SELECT {_JOB_COLUMNS} FROM jobs WHERE pk = ?", (pk,))


def _to_record(row: aiosqlite.Row) -> JobRecord:
    return JobRecord(
        pk=row["pk"],
        client_id=row["client_id"],
        id=row["job_id"],
        batch_id=row["batch_id"],
        status=JobStatus(row["status"]),
        created_at=from_db(row["created_at"]),
        updated_at=from_db(row["updated_at"]),
        spec=InteractionJob.model_validate_json(row["spec"]),
    )
