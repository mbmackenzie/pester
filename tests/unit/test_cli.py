import asyncio
from pathlib import Path

import pytest

from pester.admin.auth import AdminAuth
from pester.cli import main
from pester.core.clock import FakeClock
from pester.core.tokens import hash_token, verify_token
from pester.storage.db import Database


def test_hash_token_given(capsys: pytest.CaptureFixture[str]) -> None:
    main(["hash-token", "secret"])
    assert capsys.readouterr().out.strip() == hash_token("secret")


def test_hash_token_generated(capsys: pytest.CaptureFixture[str]) -> None:
    main(["hash-token"])
    lines = dict(line.split(":", 1) for line in capsys.readouterr().out.splitlines())
    assert verify_token(lines["token"].strip(), lines["hash"].strip())


async def _is_set_up(path: Path, password: str | None = None) -> bool:
    db = await Database.open(path)
    try:
        auth = AdminAuth(db, FakeClock())
        if password:
            await auth.set_password(password)
        return await auth.is_set_up()
    finally:
        await db.close()


def test_admin_reset_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "pester.sqlite"
    assert asyncio.run(_is_set_up(path, "a fine password"))
    monkeypatch.setenv("PESTER_DATABASE_PATH", str(path))
    main(["admin", "reset-password"])
    assert "/admin/setup" in capsys.readouterr().out
    assert not asyncio.run(_is_set_up(path))
