from datetime import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from pester.config import Permission, PesterConfig, Settings, load_config

EXAMPLE = Path(__file__).parents[2] / "config.example.yaml"
HASH = "sha256:" + "0" * 64


def test_example_config_is_valid() -> None:
    config = load_config(EXAMPLE)
    client = config.clients["example-producer"]
    assert Permission.SUBMIT_JOBS in client.permissions
    assert config.recipients["kate"].tz.key == "America/New_York"
    assert config.recipients["kate"].quiet_hours is not None
    assert config.recipients["kate"].quiet_hours.start == time(22, 0)
    assert config.personalities["weather-goblin"].type == "llm"


def test_no_path_gives_defaults() -> None:
    config = load_config(None)
    assert config.clients == {}
    assert config.personalities["default"].type == "neutral"
    assert config.scheduler.max_outstanding == 1


def test_empty_file_gives_defaults(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("")
    assert load_config(path) == PesterConfig()


def test_unknown_timezone_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown timezone"):
        PesterConfig.model_validate({"recipients": {"kate": {"timezone": "Mars/Olympus"}}})


def test_client_with_unknown_recipient_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown recipients"):
        PesterConfig.model_validate({"clients": {"p": {"token_hash": HASH, "recipients": ["ghost"]}}})


def test_plaintext_token_rejected() -> None:
    with pytest.raises(ValidationError, match="hash-token"):
        PesterConfig.model_validate({"clients": {"p": {"token_hash": "hunter2"}}})


def test_unknown_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        PesterConfig.model_validate({"scheduler": {"max_outstandnig": 2}})


def test_default_personality_always_exists() -> None:
    config = PesterConfig.model_validate(
        {"personalities": {"pirate": {"type": "template", "feedback": "Arr"}}}
    )
    assert set(config.personalities) == {"default", "pirate"}
    assert config.personalities["pirate"].options == {"feedback": "Arr"}


def test_default_personality_must_be_registered() -> None:
    with pytest.raises(ValidationError, match="default_personality"):
        PesterConfig.model_validate({"default_personality": "ghost"})


def test_api_key_read_from_env_not_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == "sk-test"
    assert "sk-test" not in repr(settings)
    with pytest.raises(ValidationError):
        PesterConfig.model_validate({"llm": {"api_key": "sk-nope"}})
