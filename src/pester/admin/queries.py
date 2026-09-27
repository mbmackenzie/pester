"""Read models for the admin UI. Unlike the producer API, these see every client's jobs."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiosqlite

from pester.core.models import InteractionJob
from pester.core.states import TERMINAL, EventType, JobStatus
from pester.storage.db import Database, from_db, to_db

_OUTSTANDING = (JobStatus.SENDING, JobStatus.AWAITING, JobStatus.ANSWERED, JobStatus.EVALUATED)


@dataclass(frozen=True)
class Counts:
    queued: int
    outstanding: int
    completed_since: int
    failed_since: int


@dataclass(frozen=True)
class Activity:
    occurred_at: datetime
    type: EventType
    client_id: str
    job_id: str
    recipient_id: str
    prompt: str

    @property
    def label(self) -> str:
        return self.type.removeprefix("INTERACTION_")


@dataclass(frozen=True)
class JobRow:
    pk: int
    client_id: str
    job_id: str
    recipient_id: str
    status: JobStatus
    prompt: str
    updated_at: datetime


@dataclass(frozen=True)
class TimelineEntry:
    occurred_at: datetime
    type: EventType
    payload: dict[str, Any]

    @property
    def label(self) -> str:
        return self.type.removeprefix("INTERACTION_")


@dataclass(frozen=True)
class JobDetail:
    row: JobRow
    batch_id: str | None
    created_at: datetime
    spec: InteractionJob
    timeline: list[TimelineEntry]
    deliveries: list[dict[str, Any]]
    response: dict[str, Any] | None
    evaluations: list[dict[str, Any]]

    @property
    def cancellable(self) -> bool:
        return self.row.status not in TERMINAL


def _prompt(spec_json: str) -> str:
    return str(json.loads(spec_json).get("prompt", ""))


def _json(value: str | None) -> Any:
    return json.loads(value) if value else None


def _job_row(row: aiosqlite.Row) -> JobRow:
    return JobRow(
        pk=row["pk"],
        client_id=row["client_id"],
        job_id=row["job_id"],
        recipient_id=row["recipient_id"],
        status=JobStatus(row["status"]),
        prompt=_prompt(row["spec"]),
        updated_at=from_db(row["updated_at"]),
    )


class AdminQueries:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def counts(self, since: datetime) -> Counts:
        placeholders = ",".join("?" * len(_OUTSTANDING))
        async with self._db.read() as conn:
            rows = await conn.execute_fetchall(
                f"""
                SELECT
                  SUM(status = ?) AS queued,
                  SUM(status IN ({placeholders})) AS outstanding,
                  SUM(status = ? AND updated_at >= ?) AS completed,
                  SUM(status = ? AND updated_at >= ?) AS failed
                FROM jobs
                """,
                (
                    JobStatus.QUEUED,
                    *_OUTSTANDING,
                    JobStatus.COMPLETED,
                    to_db(since),
                    JobStatus.FAILED,
                    to_db(since),
                ),
            )
        row = next(iter(rows))
        return Counts(
            queued=row["queued"] or 0,
            outstanding=row["outstanding"] or 0,
            completed_since=row["completed"] or 0,
            failed_since=row["failed"] or 0,
        )

    async def recent_activity(self, limit: int = 15) -> list[Activity]:
        async with self._db.read() as conn:
            rows = await conn.execute_fetchall(
                """
                SELECT e.occurred_at, e.type, e.client_id, j.job_id, j.recipient_id, j.spec
                FROM events e JOIN jobs j ON j.pk = e.job_pk
                ORDER BY e.cursor DESC LIMIT ?
                """,
                (limit,),
            )
        return [
            Activity(
                occurred_at=from_db(row["occurred_at"]),
                type=EventType(row["type"]),
                client_id=row["client_id"],
                job_id=row["job_id"],
                recipient_id=row["recipient_id"],
                prompt=_prompt(row["spec"]),
            )
            for row in rows
        ]

    async def list_jobs(
        self,
        *,
        status: JobStatus | None = None,
        recipient_id: str | None = None,
        client_id: str | None = None,
        before: int | None = None,
        limit: int = 50,
        outstanding: bool = False,
    ) -> tuple[list[JobRow], int | None]:
        """Newest first. Returns the page and the ``before`` cursor for the next one (None on the last)."""
        where: list[str] = []
        params: list[Any] = []
        for column, value in (("status", status), ("recipient_id", recipient_id), ("client_id", client_id)):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        if before is not None:
            where.append("pk < ?")
            params.append(before)
        if outstanding:
            where.append(f"status IN ({','.join('?' for _ in _OUTSTANDING)})")
            params.extend(_OUTSTANDING)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        async with self._db.read() as conn:
            rows = await conn.execute_fetchall(
                f"SELECT pk, client_id, job_id, recipient_id, status, spec, updated_at FROM jobs {clause} "
                "ORDER BY pk DESC LIMIT ?",
                (*params, limit + 1),
            )
        jobs = [_job_row(row) for row in rows]
        if len(jobs) > limit:
            return jobs[:limit], jobs[limit - 1].pk
        return jobs, None

    async def job_detail(self, client_id: str, job_id: str) -> JobDetail | None:
        async with self._db.read() as conn:
            rows = list(
                await conn.execute_fetchall(
                    "SELECT * FROM jobs WHERE client_id = ? AND job_id = ?", (client_id, job_id)
                )
            )
            if not rows:
                return None
            job = rows[0]
            pk = job["pk"]
            events = await conn.execute_fetchall(
                "SELECT occurred_at, type, payload FROM events WHERE job_pk = ? ORDER BY cursor", (pk,)
            )
            deliveries = await conn.execute_fetchall(
                "SELECT kind, channel, address, message, status, attempts, error, sent_at, created_at "
                "FROM deliveries WHERE job_pk = ? ORDER BY pk",
                (pk,),
            )
            responses = list(await conn.execute_fetchall("SELECT * FROM responses WHERE job_pk = ?", (pk,)))
            evaluations = await conn.execute_fetchall(
                "SELECT * FROM evaluations WHERE job_pk = ? ORDER BY pk", (pk,)
            )
        return JobDetail(
            row=_job_row(job),
            batch_id=job["batch_id"],
            created_at=from_db(job["created_at"]),
            spec=InteractionJob.model_validate_json(job["spec"]),
            timeline=[
                TimelineEntry(from_db(e["occurred_at"]), EventType(e["type"]), json.loads(e["payload"]))
                for e in events
            ],
            deliveries=[
                {
                    **dict(d),
                    "message": json.loads(d["message"]),
                    "sent_at": from_db(d["sent_at"]) if d["sent_at"] else None,
                    "created_at": from_db(d["created_at"]),
                }
                for d in deliveries
            ],
            response=(
                {**dict(responses[0]), "received_at": from_db(responses[0]["received_at"])}
                if responses
                else None
            ),
            evaluations=[
                {
                    **dict(ev),
                    "result": _json(ev["result"]),
                    "request": _json(ev["request"]),
                    "raw": _json(ev["raw"]),
                    "usage": _json(ev["usage"]),
                    "created_at": from_db(ev["created_at"]),
                }
                for ev in evaluations
            ],
        )
