"""The management CLI, run against a real database file."""

import io
import sqlite3
from pathlib import Path

import pytest
import yaml

from pester.cli import main

TOKEN_TYPE = "tests.e2e.test_channels:TokenAdapter"


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "pester.sqlite"
    monkeypatch.setenv("PESTER_DATABASE_PATH", str(path))
    monkeypatch.chdir(tmp_path)  # no stray .env
    return path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    main(list(argv))
    return capsys.readouterr().out


def test_set_up_a_deployment_with_no_yaml(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert "Added channel mock" in run(capsys, "channel", "add", "mock", "--type", "mock")
    run(capsys, "recipient", "add", "kate", "--timezone", "America/New_York", "--quiet", "22:00-09:00")
    run(capsys, "recipient", "link", "kate", "mock", "address=kate")
    out = run(capsys, "client", "create", "study", "--recipient", "kate")
    token = out.split("\n\n")[1].strip()
    assert len(token) > 20
    run(capsys, "settings", "set", "scheduler.max_messages_per_day=6", "scheduler.quiet_hours=null")

    listing = run(capsys, "recipient", "list")
    assert "kate" in listing and "America/New_York" in listing and "mock: {'address': 'kate'}" in listing
    assert "study" in run(capsys, "client", "list")
    assert "max_messages_per_day: 6" in run(capsys, "settings", "show")
    history = run(capsys, "history")
    assert "create client study" in history and "add channel mock" in history
    assert token not in run(capsys, "export")


def test_errors_exit_nonzero_with_a_message(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "channel", "add", "mock", "--type", "mock")
    with pytest.raises(SystemExit) as exc:
        main(["client", "create", "study", "--recipient", "nobody"])
    assert exc.value.code == 1
    assert "unknown recipients" in capsys.readouterr().err


def test_secrets_are_read_from_stdin_not_arguments(
    db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    out = run(capsys, "channel", "add", "tg", "--type", TOKEN_TYPE)
    assert "It needs a secret: pester channel secret tg token (or set TEST_CHANNEL_TOKEN)" in out
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret\n"))
    run(capsys, "channel", "secret", "tg", "token")
    assert "token: database" in run(capsys, "channel", "list")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT value FROM secrets WHERE name = 'channel.tg.token'").fetchone() == (
            "s3cret",
        )

    monkeypatch.setattr("sys.stdin", io.StringIO("sk-abc\n"))
    run(capsys, "llm", "key")
    assert "llm api key: database" in run(capsys, "settings", "show")


def test_import_and_export(db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "config.yaml"
    source.write_text(
        yaml.safe_dump(
            {
                "recipients": {"kate": {"channels": {"mock": {"address": "kate"}}}},
                "channels": {"mock": {"type": "mock"}},
                "personalities": {"fern": {"type": "template", "feedback": "🌿 {{ feedback_facts }}"}},
            }
        )
    )
    assert "as config version 1" in run(capsys, "import", str(source))
    out = tmp_path / "backup.yaml"
    run(capsys, "export", "-o", str(out))
    exported = yaml.safe_load(out.read_text())
    assert exported["channels"]["mock"]["type"] == "mock"
    assert "fern" in exported["personalities"]
    assert "fern" in run(capsys, "personality", "list")


def test_pairing_and_invites(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "channel", "add", "mock", "--type", "mock")
    assert "(none)" in run(capsys, "pairing", "list")
    out = run(capsys, "invite", "create", "zoe", "--timezone", "Asia/Tokyo")
    assert "/start " in out
    assert "zoe" in run(capsys, "invite", "list")
