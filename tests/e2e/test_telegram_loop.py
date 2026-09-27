"""The full loop through a configured Telegram channel (against a fake Bot API)."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from pester.config import PesterConfig
from pester.core.clock import FakeClock
from pester.delivery.adapters import ChannelServices
from pester.delivery.pairing import ASKED, WELCOME
from pester.delivery.telegram import TelegramAdapter, TelegramChannel, TelegramOptions
from pester.main import create_app
from pester.runtime import Runtime
from pester.service import AdminService
from tests.conftest import TOKEN_A, auth, job_payload, make_settings
from tests.fake_telegram import FakeBotAPI

CHAT = 777


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeBotAPI:
    """Channels built by the telegram adapter talk to this fake instead of api.telegram.org."""
    fake = FakeBotAPI()

    def create(self: TelegramAdapter, name: str, options: Any, services: ChannelServices) -> TelegramChannel:
        assert isinstance(options, TelegramOptions)
        return TelegramChannel(
            name,
            options.bot_token.get_secret_value(),
            poll_seconds=1,
            transport=fake.transport(),
            retry_seconds=0.01,
        )

    monkeypatch.setattr(TelegramAdapter, "create", create)
    return fake


@pytest.fixture
async def app(
    tmp_path: Path, config: PesterConfig, clock: FakeClock, api: FakeBotAPI
) -> AsyncIterator[FastAPI]:
    application = create_app(settings=make_settings(tmp_path, dev_mode=False), config=config, clock=clock)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def runtime_of(app: FastAPI) -> Runtime:
    return app.state.runtime


def service_of(app: FastAPI) -> AdminService:
    return app.state.service


async def test_pair_on_telegram_and_answer_with_a_button(
    app: FastAPI, client: httpx.AsyncClient, api: FakeBotAPI
) -> None:
    # The admin adds the bot (the token goes to secrets, never into config).
    await service_of(app).add_channel("telegram", "telegram", {"bot_token": api.token})
    assert "telegram" in runtime_of(app).started_channels
    assert api.token not in app.state.pester.config.model_dump_json()
    [status] = runtime_of(app).manager.status()
    assert status.note == "connected as @PesterTestBot"

    # Someone presses Start in Telegram.
    await api.acknowledged(api.person_sends(CHAT, "/start", first_name="Zoe", username="zoe_q"))
    assert api.texts(CHAT) == [ASKED]
    [request] = await service_of(app).pending_pairings()
    assert (request.address, request.sender_name, request.recipient_config) == (
        str(CHAT),
        "Zoe (@zoe_q)",
        {"chat_id": CHAT},
    )

    # The admin approves; Pester welcomes them.
    await service_of(app).approve_pairing(request.pk, "zoe", timezone="Europe/Paris", clients=["producer-a"])
    await runtime_of(app).run_until_idle()
    assert api.texts(CHAT)[-1] == WELCOME

    # A producer asks them something; they answer with a button.
    job = job_payload(
        recipient_id="zoe",
        response_options=["Yes", "No"],
        evaluation={"evaluator": "rule", "prompt": "match"},
    )
    job_id = (await client.post("/api/v1/jobs", json=job, headers=auth(TOKEN_A))).json()["id"]
    await runtime_of(app).run_until_idle()
    prompt = api.sent[CHAT][-1]
    assert prompt["text"] == "Did you water the plants?"
    assert [row[0]["text"] for row in prompt["reply_markup"]["inline_keyboard"]] == ["Yes", "No"]

    await api.acknowledged(api.person_presses(CHAT, prompt["message_id"], 0))
    await runtime_of(app).run_until_idle()
    assert api.texts(CHAT)[-1] == "Recorded: Yes."
    status_body = (await client.get(f"/api/v1/jobs/{job_id}", headers=auth(TOKEN_A))).json()
    assert status_body["status"] == "COMPLETED"


async def test_invite_links_use_the_bot(app: FastAPI, api: FakeBotAPI) -> None:
    await service_of(app).add_channel("telegram", "telegram", {"bot_token": api.token})
    code = await service_of(app).create_invite("zoe")
    links = runtime_of(app).manager.invite_links(code)
    assert links == {"telegram": f"https://t.me/PesterTestBot?start={code}"}

    await api.acknowledged(api.person_sends(CHAT, f"/start {code}"))
    await runtime_of(app).run_until_idle()
    assert app.state.pester.config.recipients["zoe"].channels == {"telegram": {"chat_id": CHAT}}
    assert api.texts(CHAT) == [WELCOME]


async def test_a_bad_token_shows_as_a_channel_error(app: FastAPI, client: httpx.AsyncClient) -> None:
    await service_of(app).add_channel("telegram", "telegram", {"bot_token": "000:NOT-THE-TOKEN"})
    checks = (await client.get("/ready")).json()["checks"]["channels"]
    assert checks["errors"]["telegram"].startswith("Telegram rejected the bot token")  # a message, not a repr
    assert "NOT-THE-TOKEN" not in str(checks)


async def test_the_token_can_come_from_the_environment(
    app: FastAPI, api: FakeBotAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", api.token)
    await service_of(app).add_channel("telegram", "telegram")
    assert "telegram" in runtime_of(app).started_channels
    assert await app.state.config_store.secrets() == {}
