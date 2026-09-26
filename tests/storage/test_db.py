from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from pester.storage.db import Database, from_db, load_migrations, to_db


async def test_migrations_apply_to_empty_database(db: Database) -> None:
    assert await db.schema_version() == len(load_migrations())
    async with db.read() as conn:
        rows = await conn.execute_fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
    assert {"batches", "jobs", "events", "schema_version"} <= {row[0] for row in rows}


async def test_reopening_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "pester.sqlite"
    for _ in range(2):
        db = await Database.open(path)
        assert await db.schema_version() == len(load_migrations())
        await db.close()


async def test_wal_and_foreign_keys_enabled(db: Database) -> None:
    async with db.read() as conn:
        (mode,) = next(iter(await conn.execute_fetchall("PRAGMA journal_mode")))
        (fk,) = next(iter(await conn.execute_fetchall("PRAGMA foreign_keys")))
    assert mode == "wal"
    assert fk == 1


async def test_transaction_rolls_back_on_error(db: Database) -> None:
    with pytest.raises(RuntimeError):
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO batches (client_id, batch_id, content_hash, created_at) VALUES ('c','b','h','t')"
            )
            raise RuntimeError("boom")
    async with db.read() as conn:
        rows = await conn.execute_fetchall("SELECT COUNT(*) FROM batches")
    assert next(iter(rows))[0] == 0


def test_timestamps_round_trip_and_sort() -> None:
    est = timezone(timedelta(hours=-5))
    a = datetime(2026, 1, 1, 7, 0, 0, tzinfo=est)
    b = datetime(2026, 1, 1, 12, 0, 0, 1, tzinfo=UTC)
    assert from_db(to_db(a)) == a
    assert to_db(a) < to_db(b)  # fixed width: lexical order is chronological
