"""Admin authentication: one password, first-run setup code, database-backed sessions, CSRF tokens.

The admin UI is LAN-only by design (docs/admin-ui.md §3). This is enough to keep other people on the
network out; it is not hardened for the internet.
"""

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

from pester.core.clock import Clock
from pester.storage.db import Database, to_db

log = logging.getLogger(__name__)

SESSION_LIFETIME = timedelta(days=30)
MIN_PASSWORD_LENGTH = 8
_PASSWORD_KEY = "password_hash"
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    return hmac.compare_digest(candidate.hex(), digest)


def _session_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def _normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


@dataclass(frozen=True)
class Session:
    csrf_token: str


class AdminAuth:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock
        # Changes on every start until setup is done, and is never stored: reading the logs proves you run
        # this deployment.
        raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
        self.setup_code = f"{raw[:4]}-{raw[4:]}"

    async def is_set_up(self) -> bool:
        async with self._db.read() as conn:
            rows = list(
                await conn.execute_fetchall("SELECT 1 FROM admin_settings WHERE key = ?", (_PASSWORD_KEY,))
            )
        return bool(rows)

    async def announce(self) -> None:
        """Log the setup code at startup while no admin password exists."""
        if not await self.is_set_up():
            log.warning("Admin UI is not set up. Open /admin/setup and enter code %s", self.setup_code)

    def check_setup_code(self, code: str) -> bool:
        return hmac.compare_digest(_normalize_code(code), _normalize_code(self.setup_code))

    async def set_password(self, password: str) -> None:
        """Set (or replace) the admin password. Every existing session is signed out."""
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO admin_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (_PASSWORD_KEY, hash_password(password)),
            )
            await conn.execute("DELETE FROM admin_sessions")

    async def check_password(self, password: str) -> bool:
        async with self._db.read() as conn:
            rows = list(
                await conn.execute_fetchall(
                    "SELECT value FROM admin_settings WHERE key = ?", (_PASSWORD_KEY,)
                )
            )
        return bool(rows) and verify_password(password, rows[0]["value"])

    async def create_session(self) -> str:
        """Start a session and return the cookie value."""
        session_id = secrets.token_urlsafe(32)
        now = self._clock.now()
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (to_db(now),))
            await conn.execute(
                "INSERT INTO admin_sessions (id_hash, csrf_token, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    _session_key(session_id),
                    secrets.token_urlsafe(32),
                    to_db(now),
                    to_db(now + SESSION_LIFETIME),
                ),
            )
        return session_id

    async def session(self, session_id: str) -> Session | None:
        """The live session for a cookie value, extending its expiry; None if unknown or expired."""
        now = self._clock.now()
        key = _session_key(session_id)
        async with self._db.transaction() as conn:
            rows = list(
                await conn.execute_fetchall(
                    "SELECT csrf_token FROM admin_sessions WHERE id_hash = ? AND expires_at >= ?",
                    (key, to_db(now)),
                )
            )
            if not rows:
                return None
            await conn.execute(
                "UPDATE admin_sessions SET expires_at = ? WHERE id_hash = ?",
                (to_db(now + SESSION_LIFETIME), key),
            )
        return Session(csrf_token=rows[0]["csrf_token"])

    async def end_session(self, session_id: str) -> None:
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM admin_sessions WHERE id_hash = ?", (_session_key(session_id),))

    async def end_all_sessions(self) -> None:
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM admin_sessions")

    async def reset(self) -> None:
        """Forget the password and every session, so the next visit goes through first-run setup."""
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM admin_settings WHERE key = ?", (_PASSWORD_KEY,))
            await conn.execute("DELETE FROM admin_sessions")
