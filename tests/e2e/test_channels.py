"""Pluggable channel adapters and their lifecycle."""

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from pester.config import ChannelConfig, PesterConfig
from pester.core.clock import FakeClock
from pester.delivery.adapters import (
    ChannelConfigError,
    ChannelServices,
    SecretField,
    resolve_adapter,
    resolve_options,
    secret_fields,
    secret_source,
)
from pester.delivery.memory import InMemoryChannel
from pester.delivery.mock import MockAdapter, MockChannel
from pester.main import create_app
from pester.runtime import Runtime
from tests.conftest import TOKEN_A, auth, job_payload, make_settings

# ---- A test adapter with a secret, loaded by import path -----------------------------------------------


class TokenOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: SecretStr = Field(json_schema_extra={"env": "TEST_CHANNEL_TOKEN"})
    greeting: str = "hi"


class TokenChannel(InMemoryChannel):
    def __init__(self, clock: FakeClock, name: str, options: TokenOptions) -> None:
        super().__init__(clock, name)
        self.options = options


class TokenAdapter:
    description = "test adapter with a secret"
    options_model = TokenOptions

    def create(self, name: str, options: Any, services: ChannelServices) -> TokenChannel:
        assert isinstance(services.clock, FakeClock)
        return TokenChannel(services.clock, name, options)


TOKEN_TYPE = "tests.e2e.test_channels:TokenAdapter"


# ---- Adapter registry and options ----------------------------------------------------------------------


def test_builtin_and_import_path_adapters() -> None:
    assert isinstance(resolve_adapter("mock"), MockAdapter)
    assert isinstance(resolve_adapter(TOKEN_TYPE), TokenAdapter)  # a class is instantiated
    for bad, message in [
        ("telegraph", "unknown channel type"),
        ("no_such_module:X", "cannot import"),
        ("tests.e2e.test_channels:TOKEN_TYPE", "is not a channel adapter"),
    ]:
        with pytest.raises(ChannelConfigError, match=message):
            resolve_adapter(bad)


def test_secret_fields_are_the_secretstr_fields() -> None:
    assert secret_fields(TokenOptions) == [SecretField("token", "TEST_CHANNEL_TOKEN")]
    assert secret_fields(MockAdapter.options_model) == []


def test_options_combine_config_stored_secrets_and_environment() -> None:
    adapter = TokenAdapter()
    config = ChannelConfig.model_validate({"type": TOKEN_TYPE, "greeting": "yo"})
    stored = {"channel.tg.token": "from-db"}

    options = resolve_options("tg", config, adapter, stored, environ={})
    assert isinstance(options, TokenOptions)
    assert options.token.get_secret_value() == "from-db"
    assert options.greeting == "yo"
    assert secret_source("tg", secret_fields(TokenOptions)[0], stored, environ={}) == "database"

    env = {"TEST_CHANNEL_TOKEN": "from-env"}
    options = resolve_options("tg", config, adapter, stored, environ=env)
    assert isinstance(options, TokenOptions)
    assert options.token.get_secret_value() == "from-env"  # the environment wins
    assert secret_source("tg", secret_fields(TokenOptions)[0], stored, environ=env) == "environment"


def test_options_problems_are_reported_readably() -> None:
    adapter = TokenAdapter()
    with pytest.raises(ChannelConfigError, match="token: Field required"):
        resolve_options("tg", ChannelConfig(type=TOKEN_TYPE), adapter, {}, environ={})
    with pytest.raises(ChannelConfigError, match="greting: Extra inputs"):
        resolve_options(
            "tg",
            ChannelConfig.model_validate({"type": TOKEN_TYPE, "greting": "x"}),
            adapter,
            {"channel.tg.token": "t"},
            environ={},
        )
    with pytest.raises(ChannelConfigError, match="token is a secret"):
        resolve_options(
            "tg",
            ChannelConfig.model_validate({"type": TOKEN_TYPE, "token": "plain"}),
            adapter,
            {},
            environ={},
        )


def test_channel_names_are_validated() -> None:
    with pytest.raises(ValueError, match="channel name"):
        PesterConfig.model_validate({"channels": {"Bad Name": {"type": "mock"}}})


# ---- Lifecycle -----------------------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path: Path, config: PesterConfig, clock: FakeClock) -> FastAPI:
    """No injected channels and no dev mode: every channel comes from config."""
    return create_app(settings=make_settings(tmp_path, dev_mode=False), config=config, clock=clock)


def runtime_of(app: FastAPI) -> Runtime:
    return app.state.runtime


async def set_channels(
    app: FastAPI, channels: dict[str, Any], secrets: dict[str, str | None] | None = None
) -> None:
    store = app.state.config_store
    data = app.state.pester.config.model_dump(mode="json")
    data["channels"] = channels
    await store.save(PesterConfig.model_validate(data), "channels", secrets=secrets)
    await runtime_of(app).reload_config()


async def test_adding_a_channel_starts_it_without_a_restart(client: httpx.AsyncClient, app: FastAPI) -> None:
    runtime = runtime_of(app)
    assert runtime.channels == {}
    assert (await client.get("/ready")).status_code == 503

    await set_channels(app, {"fake": {"type": "mock"}})
    assert isinstance(runtime.channels["fake"], MockChannel)
    assert runtime.started_channels == {"fake"}
    assert (await client.get("/ready")).status_code == 200

    # The full loop runs on it straight away.
    job = job_payload(response_options=["Yes", "No"], evaluation={"evaluator": "rule", "prompt": "match"})
    assert (await client.post("/api/v1/jobs", json=job, headers=auth(TOKEN_A))).status_code == 201
    await runtime.run_until_idle()
    mock = runtime.channels["fake"]
    assert isinstance(mock, MockChannel)
    assert mock.sent("kate")[0].text == "Did you water the plants?"


async def test_unchanged_channels_keep_running(client: httpx.AsyncClient, app: FastAPI) -> None:
    await set_channels(app, {"fake": {"type": "mock"}})
    first = runtime_of(app).channels["fake"]
    await set_channels(app, {"fake": {"type": "mock"}, "other": {"type": "mock"}})
    assert runtime_of(app).channels["fake"] is first
    assert runtime_of(app).started_channels == {"fake", "other"}


async def test_changed_channels_are_restarted(client: httpx.AsyncClient, app: FastAPI) -> None:
    await set_channels(app, {"fake": {"type": "mock"}})
    first = runtime_of(app).channels["fake"]
    await set_channels(app, {"fake": {"type": "mock", "description": "renamed"}})
    second = runtime_of(app).channels["fake"]
    assert second is not first
    assert runtime_of(app).started_channels == {"fake"}


async def test_disabled_and_removed_channels_stop(client: httpx.AsyncClient, app: FastAPI) -> None:
    await set_channels(app, {"fake": {"type": "mock"}, "other": {"type": "mock"}})
    await set_channels(app, {"fake": {"type": "mock", "enabled": False}})
    assert runtime_of(app).channels == {}
    assert runtime_of(app).started_channels == frozenset()


async def test_broken_channel_reports_its_error(client: httpx.AsyncClient, app: FastAPI) -> None:
    await set_channels(app, {"tg": {"type": TOKEN_TYPE}})  # no token yet
    assert "tg" not in runtime_of(app).channels
    checks = (await client.get("/ready")).json()["checks"]["channels"]
    assert checks["ok"] is False
    assert "token: Field required" in checks["errors"]["tg"]

    # Setting the secret restarts it with the value; the secret never goes into config.
    await set_channels(app, {"tg": {"type": TOKEN_TYPE}}, secrets={"channel.tg.token": "s3cret"})
    channel = runtime_of(app).channels["tg"]
    assert isinstance(channel, TokenChannel)
    assert channel.options.token.get_secret_value() == "s3cret"
    assert "s3cret" not in app.state.pester.config.model_dump_json()
    assert (await client.get("/ready")).status_code == 200


async def test_a_new_secret_restarts_the_channel(client: httpx.AsyncClient, app: FastAPI) -> None:
    await set_channels(app, {"tg": {"type": TOKEN_TYPE}}, secrets={"channel.tg.token": "one"})
    first = runtime_of(app).channels["tg"]
    await set_channels(app, {"tg": {"type": TOKEN_TYPE}}, secrets={"channel.tg.token": "two"})
    second = runtime_of(app).channels["tg"]
    assert second is not first
    assert isinstance(second, TokenChannel)
    assert second.options.token.get_secret_value() == "two"


async def test_restart_one_channel(client: httpx.AsyncClient, app: FastAPI) -> None:
    await set_channels(app, {"fake": {"type": "mock"}, "other": {"type": "mock"}})
    fake, other = runtime_of(app).channels["fake"], runtime_of(app).channels["other"]
    await runtime_of(app).restart_channel("fake")
    assert runtime_of(app).channels["fake"] is not fake
    assert runtime_of(app).channels["other"] is other


async def test_dev_mode_adds_a_mock_channel_only_when_none_is_configured(
    tmp_path: Path, config: PesterConfig, clock: FakeClock
) -> None:
    app = create_app(settings=make_settings(tmp_path), config=config, clock=clock)  # dev mode
    async with app.router.lifespan_context(app):
        assert set(runtime_of(app).channels) == {"fake"}
        await set_channels(app, {"mock": {"type": "mock"}})
        assert set(runtime_of(app).channels) == {"mock"}  # the configured one replaces the implicit one
