"""Versioned deployment config and secrets, stored in SQLite (docs/admin-ui.md §6).

Every change saves the whole validated config as a new version. Saving is optimistic: a writer states the
version it read, and the save fails if someone else (the UI, or the CLI in another process) saved first.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pester.config import PesterConfig
from pester.core.clock import Clock
from pester.storage.db import Database, from_db, to_db


class ConfigConflictError(Exception):
    """The config changed since it was read. Re-read and try again."""


@dataclass(frozen=True)
class StoredConfig:
    version: int
    config: PesterConfig
    comment: str
    created_at: datetime


def config_to_json(config: PesterConfig) -> dict[str, Any]:
    return config.model_dump(mode="json")


class ConfigStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    async def latest_version(self) -> int:
        async with self._db.read() as conn:
            rows = list(await conn.execute_fetchall("SELECT COALESCE(MAX(version), 0) FROM config_versions"))
        return int(rows[0][0])

    async def latest(self) -> StoredConfig | None:
        async with self._db.read() as conn:
            rows = list(
                await conn.execute_fetchall("SELECT * FROM config_versions ORDER BY version DESC LIMIT 1")
            )
        return _stored(rows[0]) if rows else None

    async def history(self, limit: int = 50) -> list[StoredConfig]:
        async with self._db.read() as conn:
            rows = await conn.execute_fetchall(
                "SELECT * FROM config_versions ORDER BY version DESC LIMIT ?", (limit,)
            )
        return [_stored(row) for row in rows]

    async def save(
        self,
        config: PesterConfig,
        comment: str,
        *,
        expected_version: int | None = None,
        secrets: dict[str, str | None] | None = None,
    ) -> int:
        """Store a new version (and set or delete secrets; None deletes). Returns the new version.

        With ``expected_version``, raises ConfigConflictError unless that is still the latest version.
        """
        now = to_db(self._clock.now())
        async with self._db.transaction() as conn:
            if expected_version is not None:
                rows = list(
                    await conn.execute_fetchall("SELECT COALESCE(MAX(version), 0) FROM config_versions")
                )
                if int(rows[0][0]) != expected_version:
                    raise ConfigConflictError(
                        f"config changed since version {expected_version} was read; try again"
                    )
            for name, value in (secrets or {}).items():
                if value is None:
                    await conn.execute("DELETE FROM secrets WHERE name = ?", (name,))
                else:
                    await conn.execute(
                        "INSERT INTO secrets (name, value, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT (name) DO UPDATE "
                        "SET value = excluded.value, updated_at = excluded.updated_at",
                        (name, value, now),
                    )
            cursor = await conn.execute(
                "INSERT INTO config_versions (config, comment, created_at) VALUES (?, ?, ?)",
                (json.dumps(config_to_json(config), sort_keys=True), comment, now),
            )
            version = cursor.lastrowid
        assert version is not None
        return version

    async def secrets(self) -> dict[str, str]:
        async with self._db.read() as conn:
            rows = await conn.execute_fetchall("SELECT name, value FROM secrets")
        return {row[0]: row[1] for row in rows}


def _stored(row: Any) -> StoredConfig:
    return StoredConfig(
        version=row["version"],
        config=PesterConfig.model_validate(json.loads(row["config"])),
        comment=row["comment"],
        created_at=from_db(row["created_at"]),
    )
