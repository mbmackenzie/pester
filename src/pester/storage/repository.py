"""All persistence. Every state change and its event are written in one transaction."""

import json
from datetime import datetime
from typing import Any

import aiosqlite

from pester.core.clock import Clock
from pester.core.errors import IdempotencyConflictError, JobNotFoundError
from pester.core.ids import new_id
from pester.core.models import BatchSubmission, Event, InteractionJob, JobRecord
from pester.core.states import EventType, JobStatus, check_transition, event_for
from pester.storage.db import Database, from_db, to_db

_JOB_COLUMNS = "pk, client_id, job_id, batch_id, status, content_hash, spec, created_at, updated_at"


class Repository:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

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
            return await self._transition(conn, client_id, job_id, target, payload, noop_if_current=False)

    async def cancel(self, client_id: str, job_id: str) -> JobRecord:
        """Cancel a job. Cancelling an already-cancelled job is a no-op."""
        async with self._db.transaction() as conn:
            return await self._transition(
                conn, client_id, job_id, JobStatus.CANCELLED, None, noop_if_current=True
            )

    async def _transition(
        self,
        conn: aiosqlite.Connection,
        client_id: str,
        job_id: str,
        target: JobStatus,
        payload: dict[str, Any] | None,
        *,
        noop_if_current: bool,
    ) -> JobRecord:
        row = await _fetch_job(conn, client_id, job_id)
        if row is None:
            raise JobNotFoundError(job_id)
        current = JobStatus(row["status"])
        if noop_if_current and current is target:
            return _to_record(row)
        check_transition(current, target)
        now = self._clock.now()
        await conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE pk = ?",
            (target, to_db(now), row["pk"]),
        )
        if (event_type := event_for(current, target)) is not None:
            await self._append_event(conn, client_id, row["pk"], event_type, payload or {}, now)
        row = await _fetch_job(conn, client_id, job_id)
        assert row is not None
        return _to_record(row)

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
        now = self._clock.now()
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
                to_db(now),
                to_db(now),
            ),
        )
        assert cursor.lastrowid is not None
        await self._append_event(conn, client_id, cursor.lastrowid, EventType.INTERACTION_QUEUED, {}, now)
        row = await _fetch_job(conn, client_id, job_id)
        assert row is not None
        return row

    async def _append_event(
        self,
        conn: aiosqlite.Connection,
        client_id: str,
        job_pk: int,
        event_type: EventType,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        await conn.execute(
            "INSERT INTO events (event_id, client_id, job_pk, type, payload, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_id(), client_id, job_pk, event_type, json.dumps(payload), to_db(occurred_at)),
        )


async def _fetch_one(conn: aiosqlite.Connection, sql: str, params: tuple[Any, ...]) -> aiosqlite.Row | None:
    async with conn.execute(sql, params) as cursor:
        return await cursor.fetchone()


async def _fetch_job(conn: aiosqlite.Connection, client_id: str, job_id: str) -> aiosqlite.Row | None:
    return await _fetch_one(
        conn, f"SELECT {_JOB_COLUMNS} FROM jobs WHERE client_id = ? AND job_id = ?", (client_id, job_id)
    )


def _to_record(row: aiosqlite.Row) -> JobRecord:
    return JobRecord(
        client_id=row["client_id"],
        id=row["job_id"],
        batch_id=row["batch_id"],
        status=JobStatus(row["status"]),
        created_at=from_db(row["created_at"]),
        updated_at=from_db(row["updated_at"]),
        spec=InteractionJob.model_validate_json(row["spec"]),
    )
