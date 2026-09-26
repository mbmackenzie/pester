"""Pairing requests and invite codes (docs/admin-ui.md, Recipients)."""

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pester.core.clock import Clock
from pester.storage.db import Database, from_db, to_db

MAX_PENDING = 50  # beyond this, new requests are dropped: a public bot shouldn't fill the database
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class PairingStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class Pairing:
    pk: int
    channel: str
    address: str
    recipient_config: dict[str, Any]
    first_text: str | None
    status: PairingStatus
    recipient_id: str | None
    created_at: datetime
    welcomed_at: datetime | None


@dataclass(frozen=True)
class Invite:
    code_hash: str
    recipient_id: str
    timezone: str | None
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None
    used_channel: str | None
    used_address: str | None


def _hash_code(code: str) -> str:
    normalized = "".join(ch for ch in code.upper() if ch.isalnum())
    return hashlib.sha256(normalized.encode()).hexdigest()


class PairingStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    # ---- Requests -------------------------------------------------------------------------------------

    async def request(
        self, channel: str, address: str, recipient_config: dict[str, Any], text: str | None
    ) -> tuple[Pairing | None, bool]:
        """Record a request from an unknown address. Returns (pairing, created).

        An address that already asked keeps its request (and a rejected one stays rejected). Returns
        (None, False) when too many requests are pending.
        """
        now = to_db(self._clock.now())
        async with self._db.transaction() as conn:
            existing = list(
                await conn.execute_fetchall(
                    "SELECT * FROM pairings WHERE channel = ? AND address = ?", (channel, address)
                )
            )
            if existing:
                row = existing[0]
                if row["status"] != PairingStatus.APPROVED:
                    return _pairing(row), False
                # Approved, yet unknown again: the recipient was removed since. Ask again.
                await conn.execute(
                    "UPDATE pairings SET status = ?, recipient_id = NULL, welcomed_at = NULL, "
                    "first_text = ?, recipient_config = ?, updated_at = ? WHERE pk = ?",
                    (PairingStatus.PENDING, text, json.dumps(recipient_config), now, row["pk"]),
                )
                pk = row["pk"]
            else:
                pending = list(
                    await conn.execute_fetchall(
                        "SELECT COUNT(*) FROM pairings WHERE status = ?", (PairingStatus.PENDING,)
                    )
                )
                if int(pending[0][0]) >= MAX_PENDING:
                    return None, False
                cursor = await conn.execute(
                    "INSERT INTO pairings "
                    "(channel, address, recipient_config, first_text, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (channel, address, json.dumps(recipient_config), text, PairingStatus.PENDING, now, now),
                )
                pk = cursor.lastrowid
            rows = list(await conn.execute_fetchall("SELECT * FROM pairings WHERE pk = ?", (pk,)))
        return _pairing(rows[0]), True

    async def get(self, pk: int) -> Pairing | None:
        async with self._db.read() as conn:
            rows = list(await conn.execute_fetchall("SELECT * FROM pairings WHERE pk = ?", (pk,)))
        return _pairing(rows[0]) if rows else None

    async def requests(self, status: PairingStatus | None = PairingStatus.PENDING) -> list[Pairing]:
        async with self._db.read() as conn:
            if status is None:
                rows = await conn.execute_fetchall("SELECT * FROM pairings ORDER BY pk DESC")
            else:
                rows = await conn.execute_fetchall(
                    "SELECT * FROM pairings WHERE status = ? ORDER BY pk DESC", (status,)
                )
        return [_pairing(row) for row in rows]

    async def record_approved(
        self, channel: str, address: str, recipient_config: dict[str, Any], recipient_id: str
    ) -> None:
        """Mark (or create) the pairing for this address as approved; the welcome is sent later."""
        now = to_db(self._clock.now())
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO pairings (channel, address, recipient_config, status, recipient_id, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (channel, address) DO UPDATE SET "
                "status = excluded.status, recipient_id = excluded.recipient_id, welcomed_at = NULL, "
                "updated_at = excluded.updated_at",
                (
                    channel,
                    address,
                    json.dumps(recipient_config),
                    PairingStatus.APPROVED,
                    recipient_id,
                    now,
                    now,
                ),
            )

    async def reject(self, pk: int) -> bool:
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE pairings SET status = ?, updated_at = ? WHERE pk = ? AND status = ?",
                (PairingStatus.REJECTED, to_db(self._clock.now()), pk, PairingStatus.PENDING),
            )
        return cursor.rowcount == 1

    async def forget(self, pk: int) -> None:
        """Delete a request, so the address may ask again (e.g. to undo a rejection)."""
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM pairings WHERE pk = ?", (pk,))

    async def unwelcomed(self) -> list[Pairing]:
        async with self._db.read() as conn:
            rows = await conn.execute_fetchall(
                "SELECT * FROM pairings WHERE status = ? AND welcomed_at IS NULL ORDER BY pk",
                (PairingStatus.APPROVED,),
            )
        return [_pairing(row) for row in rows]

    async def mark_welcomed(self, pk: int) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE pairings SET welcomed_at = ? WHERE pk = ?", (to_db(self._clock.now()), pk)
            )

    # ---- Invites --------------------------------------------------------------------------------------

    async def create_invite(self, recipient_id: str, timezone: str | None, valid_for: timedelta) -> str:
        """A one-time code. Only its hash is stored, so this is the only time it's shown."""
        raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
        code = f"{raw[:4]}-{raw[4:]}"
        now = self._clock.now()
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO invites (code_hash, recipient_id, timezone, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (_hash_code(code), recipient_id, timezone, to_db(now), to_db(now + valid_for)),
            )
        return code

    async def redeem(self, code: str, channel: str, address: str) -> Invite | None:
        """Use up a valid invite. Returns it, or None if the code is unknown, used, or expired."""
        now = to_db(self._clock.now())
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE invites SET used_at = ?, used_channel = ?, used_address = ? "
                "WHERE code_hash = ? AND used_at IS NULL AND expires_at > ?",
                (now, channel, address, _hash_code(code), now),
            )
            if cursor.rowcount != 1:
                return None
            rows = list(
                await conn.execute_fetchall("SELECT * FROM invites WHERE code_hash = ?", (_hash_code(code),))
            )
        return _invite(rows[0])

    async def invites(self) -> list[Invite]:
        async with self._db.read() as conn:
            rows = await conn.execute_fetchall("SELECT * FROM invites ORDER BY created_at DESC")
        return [_invite(row) for row in rows]

    async def revoke_invite(self, code_hash: str) -> bool:
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM invites WHERE code_hash = ? AND used_at IS NULL", (code_hash,)
            )
        return cursor.rowcount == 1


def _dt(value: str | None) -> datetime | None:
    return from_db(value) if value else None


def _pairing(row: Any) -> Pairing:
    return Pairing(
        pk=row["pk"],
        channel=row["channel"],
        address=row["address"],
        recipient_config=json.loads(row["recipient_config"]),
        first_text=row["first_text"],
        status=PairingStatus(row["status"]),
        recipient_id=row["recipient_id"],
        created_at=from_db(row["created_at"]),
        welcomed_at=_dt(row["welcomed_at"]),
    )


def _invite(row: Any) -> Invite:
    return Invite(
        code_hash=row["code_hash"],
        recipient_id=row["recipient_id"],
        timezone=row["timezone"],
        created_at=from_db(row["created_at"]),
        expires_at=from_db(row["expires_at"]),
        used_at=_dt(row["used_at"]),
        used_channel=row["used_channel"],
        used_address=row["used_address"],
    )
