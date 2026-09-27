"""All persistence. Every state change and its event are written in one transaction."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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

log = logging.getLogger(__name__)

_JOB_COLUMNS = (
    "pk, client_id, job_id, batch_id, recipient_id, status, content_hash, spec, created_at, updated_at, "
    "snoozed_until"
)
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
    attempts: int  # including the one about to be made


@dataclass(frozen=True)
class SchedulingSnapshot:
    queued: list[JobRecord]
    outstanding: dict[str, int]
    recent_prompts: dict[str, list[datetime]]  # recipient -> prompt claim times since the snapshot cutoff
    paused: set[str]


@dataclass(frozen=True)
class AwaitingJob:
    job: JobRecord
    sent_at: datetime
    has_response: bool  # an answer is being collected (debounce window open)


@dataclass(frozen=True)
class RecipientStatus:
    paused: bool
    awaiting: list[AwaitingJob]
    queued: int


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

    async def ping(self) -> None:
        """Raises unless the database accepts a write transaction."""
        async with self._db.transaction() as conn:
            await conn.execute("SELECT 1")

    # ---- Scheduling ---------------------------------------------------------------------------------

    async def scheduling_snapshot(self, prompts_since: datetime) -> SchedulingSnapshot:
        placeholders = ",".join("?" * len(OUTSTANDING))
        claimed = (DeliveryStatus.PENDING, DeliveryStatus.SENDING, DeliveryStatus.SENT)
        async with self._db.read() as conn:
            queued = await conn.execute_fetchall(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE status = ? ORDER BY pk", (JobStatus.QUEUED,)
            )
            outstanding = await conn.execute_fetchall(
                f"SELECT recipient_id, COUNT(*) FROM jobs WHERE status IN ({placeholders}) "
                "GROUP BY recipient_id",
                tuple(OUTSTANDING),
            )
            prompts = await conn.execute_fetchall(
                "SELECT j.recipient_id, d.created_at FROM deliveries d JOIN jobs j ON j.pk = d.job_pk "
                "WHERE d.kind = ? AND d.status IN (?, ?, ?) AND d.created_at >= ?",
                (DeliveryKind.PROMPT, *claimed, to_db(prompts_since)),
            )
            paused = await conn.execute_fetchall("SELECT recipient_id FROM recipient_state WHERE paused = 1")
        recent: dict[str, list[datetime]] = {}
        for row in prompts:
            recent.setdefault(row[0], []).append(from_db(row[1]))
        return SchedulingSnapshot(
            queued=[_to_record(row) for row in queued],
            outstanding={row[0]: int(row[1]) for row in outstanding},
            recent_prompts=recent,
            paused={row[0] for row in paused},
        )

    async def awaiting_jobs(self, recipient_id: str | None = None) -> list[AwaitingJob]:
        """AWAITING jobs with the time their latest prompt was sent."""
        where = "j.status = ?" + (" AND j.recipient_id = ?" if recipient_id else "")
        params: tuple[Any, ...] = (
            (JobStatus.AWAITING, recipient_id) if recipient_id else (JobStatus.AWAITING,)
        )
        async with self._db.read() as conn:
            rows = await conn.execute_fetchall(
                f"SELECT {_J_JOB_COLUMNS}, "
                "(SELECT MAX(d.sent_at) FROM deliveries d WHERE d.job_pk = j.pk AND d.kind = 'PROMPT' "
                " AND d.status = 'SENT') AS prompt_sent_at, "
                "EXISTS (SELECT 1 FROM responses r WHERE r.job_pk = j.pk) AS has_response "
                f"FROM jobs j WHERE {where} ORDER BY j.pk",
                params,
            )
        return [
            AwaitingJob(_to_record(row), from_db(row["prompt_sent_at"]), bool(row["has_response"]))
            for row in rows
            if row["prompt_sent_at"] is not None
        ]

    async def mark_unanswered(self, job_pk: int, sent_at: datetime, deadline: datetime) -> bool:
        """AWAITING -> UNANSWERED, unless an answer arrived (or is being collected) in the meantime."""
        async with self._db.transaction() as conn:
            row = await _fetch_job_by_pk(conn, job_pk)
            if row is None or row["status"] != JobStatus.AWAITING:
                return False
            if await _fetch_one(conn, "SELECT 1 FROM responses WHERE job_pk = ?", (job_pk,)):
                return False
            payload = {"delivery": {"sent_at": _iso(sent_at), "deadline": _iso(deadline)}}
            await self._apply(conn, row, JobStatus.UNANSWERED, payload)
            return True

    async def claim_for_send(self, job_pk: int, channel: str, address: str, message: OutboundMessage) -> bool:
        """QUEUED -> SENDING and create the pending prompt delivery. False if the job is no longer QUEUED."""
        async with self._db.transaction() as conn:
            row = await _fetch_job_by_pk(conn, job_pk)
            if row is None or row["status"] != JobStatus.QUEUED:
                return False
            await self._apply(conn, row, JobStatus.SENDING)
            await self._insert_delivery(conn, job_pk, DeliveryKind.PROMPT, channel, address, message)
            return True

    async def queued_for(self, recipient_id: str) -> list[JobRecord]:
        """A recipient's queued jobs, highest priority first, then oldest."""
        async with self._db.read() as conn:
            rows = await conn.execute_fetchall(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE recipient_id = ? AND status = ? "
                "ORDER BY priority DESC, created_at, pk",
                (recipient_id, JobStatus.QUEUED),
            )
        return [_to_record(row) for row in rows]

    async def outstanding_for(self, recipient_id: str) -> JobRecord | None:
        """One of the recipient's outstanding jobs (sent and not yet closed), if any; AWAITING first."""
        placeholders = ",".join("?" * len(OUTSTANDING))
        async with self._db.read() as conn:
            row = await _fetch_one(
                conn,
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE recipient_id = ? AND status IN ({placeholders}) "
                "ORDER BY status = ? DESC, pk LIMIT 1",
                (recipient_id, *OUTSTANDING, JobStatus.AWAITING),
            )
        return _to_record(row) if row is not None else None

    async def expire(self, job_pk: int) -> bool:
        async with self._db.transaction() as conn:
            row = await _fetch_job_by_pk(conn, job_pk)
            if row is None or row["status"] != JobStatus.QUEUED:
                return False
            await self._apply(conn, row, JobStatus.EXPIRED)
            return True

    # ---- Delivery -----------------------------------------------------------------------------------

    async def claim_next_delivery(self) -> PendingDelivery | None:
        """Mark the oldest due pending delivery SENDING and return it.

        Deliveries whose job has moved on (e.g. was cancelled) are marked CANCELLED and skipped. Deliveries
        waiting to be retried are not due until their ``next_attempt_at``.
        """
        async with self._db.transaction() as conn:
            while True:
                row = await _fetch_one(
                    conn,
                    f"SELECT d.pk AS d_pk, d.kind, d.channel, d.address, d.message, d.attempts, "
                    f"{_J_JOB_COLUMNS} FROM deliveries d JOIN jobs j ON j.pk = d.job_pk "
                    "WHERE d.status = ? AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= ?) "
                    "ORDER BY d.pk LIMIT 1",
                    (DeliveryStatus.PENDING, to_db(self._clock.now())),
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
                    attempts=int(row["attempts"]) + 1,
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
                payload = {"delivery": {"channel": delivery.channel, "sent_at": _iso(sent_at)}}
                await self._apply(conn, row, JobStatus.AWAITING, payload)
            else:
                payload = {"feedback": {"text": delivery.message.text, "sent_at": _iso(sent_at)}}
                await self._apply(conn, row, JobStatus.COMPLETED, payload)

    async def delivery_retry(self, delivery: PendingDelivery, error: str, next_attempt_at: datetime) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE deliveries SET status = ?, error = ?, next_attempt_at = ?, updated_at = ? "
                "WHERE pk = ?",
                (
                    DeliveryStatus.PENDING,
                    error,
                    to_db(next_attempt_at),
                    to_db(self._clock.now()),
                    delivery.pk,
                ),
            )

    async def delivery_failed(self, delivery: PendingDelivery, error: str, reason: str | None = None) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE deliveries SET status = ?, error = ?, updated_at = ? WHERE pk = ?",
                (DeliveryStatus.FAILED, error, to_db(self._clock.now()), delivery.pk),
            )
            row = await _fetch_job_by_pk(conn, delivery.job.pk)
            assert row is not None
            if row["status"] == _EXPECTED_JOB_STATUS[delivery.kind]:
                payload = {
                    "reason": reason or f"{delivery.kind.lower()}_delivery_failed",
                    "error": error,
                    "attempts": delivery.attempts,
                }
                await self._apply(conn, row, JobStatus.FAILED, payload)

    async def recover_interrupted_sends(self) -> int:
        """Resolve deliveries left SENDING by a crash (spec §9.1). They are never resent.

        A person may or may not have received the message, and a duplicate is worse than a lost message. An
        interrupted prompt fails its job (``ambiguous_send``). An interrupted feedback message completes its
        job, flagged ``delivery_uncertain``: the evaluation is intact either way.
        """
        async with self._db.transaction() as conn:
            rows = list(
                await conn.execute_fetchall(
                    f"SELECT d.pk AS d_pk, d.kind, d.message, {_J_JOB_COLUMNS} "
                    "FROM deliveries d JOIN jobs j ON j.pk = d.job_pk WHERE d.status = ?",
                    (DeliveryStatus.SENDING,),
                )
            )
            now = to_db(self._clock.now())
            for row in rows:
                await conn.execute(
                    "UPDATE deliveries SET status = ?, error = ?, updated_at = ? WHERE pk = ?",
                    (DeliveryStatus.FAILED, "interrupted mid-send; not retried", now, row["d_pk"]),
                )
                kind = DeliveryKind(row["kind"])
                if row["status"] != _EXPECTED_JOB_STATUS[kind]:
                    continue
                if kind is DeliveryKind.PROMPT:
                    payload: dict[str, Any] = {"reason": "ambiguous_send", "error": "interrupted mid-send"}
                    await self._apply(conn, row, JobStatus.FAILED, payload)
                else:
                    text = OutboundMessage.model_validate_json(row["message"]).text
                    payload = {"feedback": {"text": text, "sent_at": None, "delivery_uncertain": True}}
                    await self._apply(conn, row, JobStatus.COMPLETED, payload)
            return len(rows)

    # ---- Inbound ------------------------------------------------------------------------------------

    async def record_inbound(self, message: InboundMessage, outcome: IngestOutcome) -> bool:
        """Record an inbound message for dedupe. False if it was already seen."""
        async with self._db.transaction() as conn:
            return await self._record_inbound(conn, message, outcome)

    async def ingest_response(
        self, recipient_id: str, message: InboundMessage, debounce: timedelta = timedelta(0)
    ) -> IngestResult:
        """Route an inbound message to a job and record it, all in one transaction (spec §9.2).

        With a debounce window, the answer is collected (follow-up messages are joined onto it) and the job
        moves to ANSWERED only once the window closes, via ``close_due_responses``. A button press closes it
        immediately.
        """
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

            text = message.text or message.selected_option or ""
            if outcome is IngestOutcome.LATE:
                late = {
                    "text": text,
                    "selected_option": message.selected_option,
                    "received_at": _iso(message.received_at),
                }
                await self._append_event(
                    conn, job_row, EventType.INTERACTION_LATE_RESPONSE, {"response": late}
                )
                return IngestResult(outcome, _to_record(job_row))

            close_now = debounce <= timedelta(0) or message.selected_option is not None
            closes_at = None if close_now else to_db(message.received_at + debounce)
            existing = await _fetch_one(conn, "SELECT pk FROM responses WHERE job_pk = ?", (job_row["pk"],))
            if existing is None:
                await conn.execute(
                    "INSERT INTO responses (job_pk, text, selected_option, channel, address, external_id, "
                    "received_at, raw, closes_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_row["pk"],
                        text,
                        message.selected_option,
                        message.channel,
                        message.sender_address,
                        message.external_id,
                        to_db(message.received_at),
                        json.dumps(message.raw) if message.raw is not None else None,
                        closes_at,
                    ),
                )
            else:
                # Join onto the answer being collected. Feedback threads onto the latest message.
                await conn.execute(
                    "UPDATE responses SET text = text || char(10) || ?, "
                    "selected_option = COALESCE(?, selected_option), external_id = ?, closes_at = ? "
                    "WHERE pk = ?",
                    (text, message.selected_option, message.external_id, closes_at, existing["pk"]),
                )
            if not close_now:
                return IngestResult(outcome, _to_record(job_row))
            return IngestResult(outcome, await self._close_response(conn, job_row))

    async def close_due_responses(self, now: datetime) -> int:
        """Close debounce windows that have elapsed, moving their jobs to ANSWERED."""
        async with self._db.transaction() as conn:
            rows = list(
                await conn.execute_fetchall(
                    f"SELECT {_J_JOB_COLUMNS} FROM responses r JOIN jobs j ON j.pk = r.job_pk "
                    "WHERE r.closes_at IS NOT NULL AND r.closes_at <= ? AND j.status = ?",
                    (to_db(now), JobStatus.AWAITING),
                )
            )
            for row in rows:
                await self._close_response(conn, row)
            return len(rows)

    async def _close_response(self, conn: aiosqlite.Connection, job_row: aiosqlite.Row) -> JobRecord:
        await conn.execute("UPDATE responses SET closes_at = NULL WHERE job_pk = ?", (job_row["pk"],))
        response = await _fetch_response(conn, job_row["pk"], job_row["job_id"])
        assert response is not None
        return await self._apply(conn, job_row, JobStatus.ANSWERED, {"response": _response_payload(response)})

    # ---- Recipient commands -------------------------------------------------------------------------

    async def command_target(self, recipient_id: str, message: InboundMessage) -> JobRecord | IngestOutcome:
        """The AWAITING job a command refers to: the replied-to prompt, else the single open question."""
        async with self._db.read() as conn:
            row = await self._route(conn, recipient_id, message)
        if isinstance(row, IngestOutcome):
            return row
        if row["status"] != JobStatus.AWAITING:
            return IngestOutcome.NOTHING_PENDING
        return _to_record(row)

    async def skip(self, job_pk: int) -> bool:
        return await self._close_by_recipient(job_pk, JobStatus.SKIPPED, {"by": "recipient"})

    async def snooze(self, job_pk: int, until: datetime) -> bool:
        return await self._close_by_recipient(job_pk, JobStatus.QUEUED, {"until": _iso(until)}, until)

    async def _close_by_recipient(
        self, job_pk: int, target: JobStatus, payload: dict[str, Any], snoozed_until: datetime | None = None
    ) -> bool:
        async with self._db.transaction() as conn:
            row = await _fetch_job_by_pk(conn, job_pk)
            if row is None or row["status"] != JobStatus.AWAITING:
                return False
            await conn.execute("DELETE FROM responses WHERE job_pk = ?", (job_pk,))  # drop a partial answer
            if snoozed_until is not None:
                await conn.execute(
                    "UPDATE jobs SET snoozed_until = ? WHERE pk = ?", (to_db(snoozed_until), job_pk)
                )
            await self._apply(conn, row, target, payload)
            return True

    async def set_paused(self, recipient_id: str, paused: bool) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO recipient_state (recipient_id, paused, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (recipient_id) DO UPDATE "
                "SET paused = excluded.paused, updated_at = excluded.updated_at",
                (recipient_id, int(paused), to_db(self._clock.now())),
            )

    async def recipient_status(self, recipient_id: str) -> RecipientStatus:
        awaiting = await self.awaiting_jobs(recipient_id)
        async with self._db.read() as conn:
            paused = await _fetch_one(
                conn, "SELECT paused FROM recipient_state WHERE recipient_id = ?", (recipient_id,)
            )
            queued = await _fetch_one(
                conn,
                "SELECT COUNT(*) AS n FROM jobs WHERE recipient_id = ? AND status = ?",
                (recipient_id, JobStatus.QUEUED),
            )
        return RecipientStatus(
            paused=bool(paused and paused["paused"]),
            awaiting=awaiting,
            queued=int(queued["n"]) if queued else 0,
        )

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
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE status = ? ORDER BY updated_at, pk LIMIT 1",
                (JobStatus.ANSWERED,),
            )
            if row is None:
                return None
            response = await _fetch_response(conn, row["pk"], row["job_id"])
        assert response is not None
        return AnsweredJob(job=_to_record(row), response=response)

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
                    "sent_at": _iso(sent_at),
                    "answered_at": _iso(answered_at),
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
            await self._append_event(conn, row, event_type, payload or {})
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
        row = await _fetch_job(conn, client_id, job_id)
        assert row is not None
        await self._append_event(conn, row, EventType.INTERACTION_QUEUED, {})
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
        job_row: aiosqlite.Row,
        event_type: EventType,
        payload: dict[str, Any],
    ) -> None:
        await conn.execute(
            "INSERT INTO events (event_id, client_id, job_pk, type, payload, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                new_id(),
                job_row["client_id"],
                job_row["pk"],
                event_type,
                json.dumps(payload),
                to_db(self._clock.now()),
            ),
        )
        # One log line per event, written where the event is, so the two can't disagree.
        log.info(
            "%s %s",
            event_type,
            job_row["job_id"],
            extra={
                "event": event_type,
                "interaction_id": job_row["job_id"],
                "batch_id": job_row["batch_id"],
                "client_id": job_row["client_id"],
                "recipient_id": job_row["recipient_id"],
            },
        )


def _iso(dt: datetime) -> str:
    """Timestamps in event payloads match the event envelope's ISO format."""
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_or_none(value: object) -> str | None:
    return json.dumps(value) if value is not None else None


def _response_payload(response: HumanResponse) -> dict[str, Any]:
    return {
        "text": response.text,
        "selected_option": response.selected_option,
        "received_at": _iso(response.received_at),
    }


async def _fetch_one(conn: aiosqlite.Connection, sql: str, params: tuple[Any, ...]) -> aiosqlite.Row | None:
    async with conn.execute(sql, params) as cursor:
        return await cursor.fetchone()


async def _fetch_job(conn: aiosqlite.Connection, client_id: str, job_id: str) -> aiosqlite.Row | None:
    return await _fetch_one(
        conn, f"SELECT {_JOB_COLUMNS} FROM jobs WHERE client_id = ? AND job_id = ?", (client_id, job_id)
    )


async def _fetch_response(conn: aiosqlite.Connection, job_pk: int, job_id: str) -> HumanResponse | None:
    row = await _fetch_one(conn, "SELECT * FROM responses WHERE job_pk = ?", (job_pk,))
    if row is None:
        return None
    return HumanResponse(
        interaction_id=job_id,
        text=row["text"],
        selected_option=row["selected_option"],
        channel=row["channel"],
        address=row["address"],
        external_id=row["external_id"],
        received_at=from_db(row["received_at"]),
        raw=json.loads(row["raw"]) if row["raw"] else None,
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
        snoozed_until=from_db(row["snoozed_until"]) if row["snoozed_until"] else None,
        spec=InteractionJob.model_validate_json(row["spec"]),
    )
