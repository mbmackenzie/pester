"""SQLite access: one connection, one writer, explicit transactions (spec §12)."""

import asyncio
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import aiosqlite

_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_MIGRATION_NAME = re.compile(r"^(\d+)_.*\.sql$")


def to_db(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(_TS_FORMAT)


def from_db(value: str) -> datetime:
    return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=UTC)


def load_migrations() -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for entry in resources.files("pester.storage.migrations").iterdir():
        if match := _MIGRATION_NAME.match(entry.name):
            found.append((int(match.group(1)), entry.read_text()))
    found.sort()
    versions = [v for v, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"migrations must be numbered 1..n without gaps, found {versions}")
    return found


class Database:
    """Serializes all access through one connection.

    Pester's scale does not need concurrent writers, and a single writer is what keeps event cursors
    gap-free and in commit order.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path | str) -> "Database":
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        db = cls(conn)
        await db._migrate()
        return db

    async def close(self) -> None:
        await self._conn.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[aiosqlite.Connection]:
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                await self._conn.execute("ROLLBACK")
                raise
            await self._conn.execute("COMMIT")

    @asynccontextmanager
    async def read(self) -> AsyncGenerator[aiosqlite.Connection]:
        async with self._lock:
            yield self._conn

    async def schema_version(self) -> int:
        async with self.read() as conn:
            rows = await conn.execute_fetchall("SELECT COALESCE(MAX(version), 0) FROM schema_version")
            return int(next(iter(rows))[0])

    async def _migrate(self) -> None:
        conn = self._conn
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {int(row[0]) for row in await conn.execute_fetchall("SELECT version FROM schema_version")}
        for version, sql in load_migrations():
            if version in applied:
                continue
            script = (
                f"BEGIN IMMEDIATE;\n{sql}\n"
                f"INSERT INTO schema_version (version, applied_at) "
                f"VALUES ({version}, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\nCOMMIT;"
            )
            try:
                await conn.executescript(script)
            except BaseException:
                if conn.in_transaction:
                    await conn.execute("ROLLBACK")
                raise
