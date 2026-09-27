"""Admin UI forms: every change goes through the admin service and applies without a restart."""

import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from pester.config import PesterConfig
from pester.core.clock import FakeClock
from pester.delivery.mock import MockChannel
from pester.delivery.pairing import WELCOME
from pester.main import create_app
from pester.runtime import Runtime
from tests.admin.conftest import set_up
from tests.conftest import TOKEN_A, auth, job_payload, make_settings
from tests.llm_fakes import FakeLLM, completion, connection_error, evaluation

TOKEN_TYPE = "tests.e2e.test_channels:TokenAdapter"


def config_of(app: FastAPI) -> PesterConfig:
    return app.state.pester.config


def runtime_of(app: FastAPI) -> Runtime:
    return app.state.runtime


def mock_of(app: FastAPI) -> MockChannel:
    channel = runtime_of(app).channels["fake"]
    assert isinstance(channel, MockChannel)
    return channel


def token_on(page: str) -> str:
    match = re.search(r'<div class="once" id="token">([^<]+)</div>', page)
    assert match, "no token on the page"
    return match.group(1)


class Admin:
    """A signed-in browser: posts forms with the CSRF token."""

    def __init__(self, client: httpx.AsyncClient, csrf: str) -> None:
        self.client = client
        self.csrf = csrf

    async def post(self, path: str, **data: str | list[str]) -> httpx.Response:
        return await self.client.post(path, data={"csrf": self.csrf, **data})

    async def get(self, path: str) -> httpx.Response:
        return await self.client.get(path)


@pytest.fixture
async def browser(client: httpx.AsyncClient, app: FastAPI) -> Admin:
    return Admin(client, await set_up(client, app))


# ---- Clients --------------------------------------------------------------------------------------------


async def test_create_a_client_and_use_its_token(browser: Admin, app: FastAPI) -> None:
    resp = await browser.post(
        "/admin/clients", id="study", permissions=["submit_jobs", "read_events"], recipients=["kate"]
    )
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    token = token_on(resp.text)
    assert "/api/v1/jobs" in resp.text and token in resp.text  # a ready-to-run curl snippet
    assert config_of(app).clients["study"].recipients == {"kate"}

    submitted = await browser.client.post("/api/v1/jobs", json=job_payload(), headers=auth(token))
    assert submitted.status_code == 201


async def test_client_form_errors_keep_what_was_typed(browser: Admin) -> None:
    resp = await browser.post("/admin/clients", id="producer-a", permissions=["preview"], recipients=["sam"])
    assert resp.status_code == 400
    assert "already exists" in resp.text
    assert 'value="producer-a"' in resp.text
    assert 'value="sam" checked' in resp.text


async def test_edit_rotate_and_revoke(browser: Admin, app: FastAPI) -> None:
    resp = await browser.post("/admin/clients/producer-a", permissions=["read_events"], recipients=["kate"])
    assert resp.status_code == 303
    assert config_of(app).clients["producer-a"].recipients == {"kate"}
    assert "Saved client producer-a." in (await browser.get("/admin/clients")).text  # flash
    assert "Saved client producer-a." not in (await browser.get("/admin/clients")).text  # shown once

    rotated = await browser.post("/admin/clients/producer-a/rotate")
    new_token = token_on(rotated.text)
    events = "/api/v1/events"
    assert (await browser.client.get(events, headers=auth(TOKEN_A))).status_code == 401
    assert (await browser.client.get(events, headers=auth(new_token))).status_code == 200

    await browser.post("/admin/clients/producer-a/revoke")
    assert (await browser.client.get(events, headers=auth(new_token))).status_code == 401


async def test_forms_need_the_csrf_token(browser: Admin) -> None:
    resp = await browser.client.post("/admin/clients", data={"id": "sneaky", "permissions": "submit_jobs"})
    assert resp.status_code == 403


# ---- Recipients, pairing, invites -----------------------------------------------------------------------


async def test_add_and_edit_a_recipient(browser: Admin, app: FastAPI) -> None:
    resp = await browser.post(
        "/admin/recipients", id="zoe", timezone="Europe/Paris", quiet="23:00-07:00", clients=["producer-a"]
    )
    assert resp.headers["location"] == "/admin/recipients/zoe"
    zoe = config_of(app).recipients["zoe"]
    assert zoe.timezone == "Europe/Paris"
    assert "zoe" in config_of(app).clients["producer-a"].recipients
    page = (await browser.get("/admin/recipients/zoe")).text
    assert 'value="23:00-07:00"' in page
    assert "Pester can't reach zoe yet" in page

    await browser.post("/admin/recipients/zoe", timezone="Asia/Tokyo", quiet="")
    zoe = config_of(app).recipients["zoe"]
    assert (zoe.timezone, zoe.quiet_hours) == ("Asia/Tokyo", None)

    bad = await browser.post("/admin/recipients/zoe", timezone="Mars/Olympus")
    assert bad.status_code == 400
    assert "unknown timezone" in bad.text


async def test_link_unlink_and_remove(browser: Admin, app: FastAPI) -> None:
    await browser.post("/admin/recipients/sam/link", channel="fake", settings="address: sam")
    assert config_of(app).recipients["sam"].channels == {"fake": {"address": "sam"}}
    bad = await browser.post("/admin/recipients/sam/link", channel="fake", settings="[not, a, mapping]")
    assert bad.status_code == 400
    assert "should be a mapping" in bad.text

    await browser.post("/admin/recipients/sam/unlink", channel="fake")
    assert config_of(app).recipients["sam"].channels == {}
    await browser.post("/admin/recipients/sam/delete")
    assert "sam" not in config_of(app).recipients


async def test_approve_a_pairing_request_in_the_browser(browser: Admin, app: FastAPI) -> None:
    await browser.post("/admin/chat/zoe", text="/start")
    page = (await browser.get("/admin/recipients")).text
    assert "Pairing requests" in page
    pk = re.search(r'action="/admin/pairings/(\d+)/approve"', page)
    assert pk
    assert 'value="zoe"' in page  # suggested id

    resp = await browser.post(
        f"/admin/pairings/{pk.group(1)}/approve",
        recipient_id="zoe",
        timezone="Europe/Paris",
        clients=["producer-a"],
    )
    assert resp.status_code == 303
    await runtime_of(app).run_until_idle()
    assert mock_of(app).sent("zoe")[-1].text == WELCOME
    assert config_of(app).recipients["zoe"].channels == {"fake": {"address": "zoe"}}
    assert "Pairing requests" not in (await browser.get("/admin/recipients")).text


async def test_reject_a_pairing_request(browser: Admin, app: FastAPI) -> None:
    await browser.post("/admin/chat/spam", text="buy now")
    page = (await browser.get("/admin/recipients")).text
    pk = re.search(r'action="/admin/pairings/(\d+)/reject"', page)
    assert pk
    await browser.post(f"/admin/pairings/{pk.group(1)}/reject")
    assert await app.state.service.pending_pairings() == []


async def test_invite_code_shown_once_and_redeemable(browser: Admin, app: FastAPI) -> None:
    resp = await browser.post("/admin/invites", recipient_id="zoe", timezone="Asia/Tokyo", days="3")
    assert resp.headers["cache-control"] == "no-store"
    code = re.search(r"/start ([A-Z0-9]{4}-[A-Z0-9]{4})", resp.text)
    assert code
    await mock_of(app).inject("zoe-phone", f"/start {code.group(1)}")
    assert config_of(app).recipients["zoe"].timezone == "Asia/Tokyo"
    assert "used on fake by zoe-phone" in (await browser.get("/admin/recipients")).text


# ---- Channels -------------------------------------------------------------------------------------------


async def test_add_a_mock_channel(browser: Admin, app: FastAPI) -> None:
    form = (await browser.get("/admin/channels/new?type=mock")).text
    assert "Accept pairing requests" in form
    resp = await browser.post(
        "/admin/channels", type="mock", name="phone", description="Pretend phone", accept_pairing="true"
    )
    assert resp.status_code == 303
    assert isinstance(runtime_of(app).channels["phone"], MockChannel)
    listing = (await browser.get("/admin/channels")).text
    assert "Pretend phone" in listing and "running" in listing


async def test_channel_secrets_are_write_only(browser: Admin, app: FastAPI) -> None:
    await browser.post("/admin/channels", type=TOKEN_TYPE, name="tg", opt_greeting="hello")
    listing = (await browser.get("/admin/channels")).text
    assert "token: Field required" in listing  # saved, but can't run without its secret

    await browser.post("/admin/channels/tg", opt_greeting="hello", opt_token="s3cret", enabled="true")
    assert "tg" in runtime_of(app).started_channels
    page = (await browser.get("/admin/channels/tg")).text
    assert "s3cret" not in page
    assert "saved; type to replace" in page

    await browser.post("/admin/channels/tg", opt_greeting="hi", enabled="true")  # blank secret: unchanged
    assert "tg" in runtime_of(app).started_channels
    await browser.post("/admin/channels/tg", opt_greeting="hi", opt_token__clear="true", enabled="true")
    assert "tg" not in runtime_of(app).started_channels


async def test_channel_form_errors(browser: Admin) -> None:
    bad = await browser.post("/admin/channels", type="mock", name="Bad Name")
    assert bad.status_code == 400
    assert "channel name" in bad.text
    unknown = await browser.get("/admin/channels/new?type=telegraph")
    assert unknown.status_code == 303  # back to the list with the reason
    assert "unknown channel type" in (await browser.get("/admin/channels")).text


async def test_disable_restart_and_remove(browser: Admin, app: FastAPI) -> None:
    await browser.post("/admin/channels", type="mock", name="phone", accept_pairing="true")
    first = runtime_of(app).channels["phone"]
    await browser.post("/admin/channels/phone/restart")
    assert runtime_of(app).channels["phone"] is not first
    await browser.post("/admin/channels/phone", accept_pairing="true")  # "enabled" unticked
    assert "phone" not in runtime_of(app).channels
    assert "disabled" in (await browser.get("/admin/channels")).text
    await browser.post("/admin/channels/phone/remove")
    assert "phone" not in config_of(app).channels


# ---- Personalities --------------------------------------------------------------------------------------


async def test_create_edit_and_preview_a_personality(browser: Admin, app: FastAPI) -> None:
    form = (await browser.get("/admin/personalities/new?type=template")).text
    assert '<textarea name="opt_feedback"' in form  # multiline field from the options model
    resp = await browser.post(
        "/admin/personalities",
        type="template",
        id="fern",
        description="A fern",
        opt_feedback="🌿 {{ feedback_facts }}",
    )
    assert resp.status_code == 303
    assert "fern" in app.state.pester.personalities

    preview = await browser.post(
        "/admin/personalities/preview",
        personality_id="fern",
        prompt="Water?",
        evaluator="echo",
        evaluation_prompt="x",
        reply="yes",
    )
    assert "🌿" in preview.text

    bad = await browser.post("/admin/personalities/fern", type="template", opt_feedback="{{ unclosed")
    assert bad.status_code == 400
    assert "fern" in bad.text


async def test_custom_personality_types_take_yaml_options(browser: Admin) -> None:
    form = (await browser.get("/admin/personalities/new?type=my_pkg.voices:Pirate")).text
    assert 'name="options_yaml"' in form
    resp = await browser.post(
        "/admin/personalities", type="no_such_module:Pirate", id="pirate", options_yaml="accent: heavy"
    )
    assert resp.status_code == 400
    assert "cannot import" in resp.text


async def test_default_and_delete(browser: Admin, app: FastAPI) -> None:
    await browser.post("/admin/personalities/houseplant/default")
    assert config_of(app).default_personality == "houseplant"
    refused = await browser.post("/admin/personalities/houseplant/delete")  # it's the default
    assert refused.status_code == 303
    assert "is the default personality" in (await browser.get("/admin/personalities")).text
    assert "houseplant" in config_of(app).personalities

    await browser.post("/admin/personalities/default/delete")  # the built-in neutral one stays
    assert "always available" in (await browser.get("/admin/personalities")).text
    await browser.post("/admin/personalities/weather-goblin/delete")
    assert "weather-goblin" not in config_of(app).personalities


# ---- Settings -------------------------------------------------------------------------------------------


async def test_pacing_settings(browser: Admin, app: FastAPI) -> None:
    resp = await browser.post(
        "/admin/settings/section/scheduler",
        quiet="22:00-07:00",
        opt_max_messages_per_day="6",
        opt_min_interval_minutes="30",
    )
    assert resp.status_code == 303
    scheduler = config_of(app).scheduler
    assert scheduler.max_messages_per_day == 6
    assert scheduler.quiet_hours is not None and f"{scheduler.quiet_hours.start:%H:%M}" == "22:00"

    await browser.post("/admin/settings/section/scheduler", quiet="")
    assert config_of(app).scheduler.quiet_hours is None

    bad = await browser.post("/admin/settings/section/scheduler", opt_max_messages_per_day="0")
    assert bad.status_code == 400
    assert "max_messages_per_day" in bad.text


async def test_llm_settings_and_key(browser: Admin, app: FastAPI) -> None:
    await browser.post(
        "/admin/settings/section/llm", opt_model="gpt-test", opt_base_url="https://llm.example/v1"
    )
    assert config_of(app).llm.base_url == "https://llm.example/v1"
    await browser.post("/admin/settings/section/llm", opt_model="gpt-test", opt_base_url="")
    assert config_of(app).llm.base_url is None  # an emptied optional field is cleared

    no_key = await browser.post("/admin/settings/test-llm")
    assert "No API key is set." in no_key.text

    await browser.post("/admin/settings/llm-key", key="sk-saved")
    assert runtime_of(app).live.current.llm_key_source == "database"
    assert "sk-saved" not in (await browser.get("/admin/settings")).text
    await browser.post("/admin/settings/llm-key", clear="true")
    assert runtime_of(app).live.current.llm_key_source is None


async def test_llm_connection_test(tmp_path: Path, config: PesterConfig, clock: FakeClock) -> None:
    llm = FakeLLM()
    app = create_app(settings=make_settings(tmp_path), config=config, clock=clock, llm_client=llm.client())
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        admin = Admin(c, await set_up(c, app))
        llm.queue(completion("ok"))
        good = await admin.post("/admin/settings/test-llm")
        assert "answered in" in good.text and "ok" in good.text
        assert llm.last["model"] == config.llm.model
        llm.queue(connection_error())
        bad = await admin.post("/admin/settings/test-llm")
        assert 'class="error"' in bad.text


async def test_export_and_history(browser: Admin) -> None:
    await browser.post("/admin/recipients", id="zoe")
    export = await browser.get("/admin/settings/export.yaml")
    assert export.headers["content-disposition"] == 'attachment; filename="pester-config.yaml"'
    assert "zoe" in export.text
    assert "add recipient zoe" in (await browser.get("/admin/settings")).text


async def test_every_management_page_renders(browser: Admin) -> None:
    for path in (
        "/admin/clients/producer-a",
        "/admin/recipients/kate",
        "/admin/channels/new?type=mock",
        f"/admin/channels/new?type={TOKEN_TYPE}",
        "/admin/personalities/new?type=llm",
        "/admin/personalities/new?type=neutral",
        "/admin/personalities/weather-goblin",
    ):
        resp = await browser.get(path)
        assert resp.status_code == 200, (path, resp.text[:300])


async def test_telegram_channel_form(browser: Admin) -> None:
    form = (await browser.get("/admin/channels/new?type=telegram")).text
    assert 'name="opt_bot_token"' in form and 'type="password"' in form
    assert "TELEGRAM_BOT_TOKEN" in form
    advanced = form.split('<details class="advanced">', 1)[1]
    assert 'name="opt_api_base"' in advanced  # rarely changed options are collapsed
    assert 'name="opt_bot_token"' not in advanced


async def test_recipients_page_says_when_the_next_question_comes(browser: Admin, app: FastAPI) -> None:
    await app.state.service.update_recipient("kate", quiet_hours="00:00-23:59")
    resp = await browser.client.post("/api/v1/jobs", json=job_payload(), headers=auth(TOKEN_A))
    assert resp.status_code == 201
    page = (await browser.get("/admin/recipients")).text
    assert "next: " in page and "quiet hours until 11:59 PM" in page


async def test_preview_shows_every_llm_call(tmp_path: Path, config: PesterConfig, clock: FakeClock) -> None:
    llm = FakeLLM()
    app = create_app(settings=make_settings(tmp_path), config=config, clock=clock, llm_client=llm.client())
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        admin = Admin(c, await set_up(c, app))
        llm.queue(
            evaluation({"score": 1}, "They watered the plants."), completion("Fine. The plants live. Barely.")
        )
        page = (
            await admin.post(
                "/admin/personalities/preview",
                personality_id="weather-goblin",
                prompt="Did you water the plants?",
                evaluator="llm",
                evaluation_prompt="Score 1 if yes",
                reply="yes",
            )
        ).text
    assert "What was sent to the LLM" in page
    assert "evaluation" in page and "personality: feedback" in page
    assert "Be a goblin." in page  # the personality's prompt, in its system message
    assert "Facts to convey: They watered the plants." in page  # its user message
    assert "Fine. The plants live. Barely." in page  # its reply
    assert "didn't work" not in page


async def test_preview_explains_a_personality_fallback(browser: Admin) -> None:
    listing = (await browser.get("/admin/personalities")).text
    assert "weather-goblin uses an LLM, but no LLM API key" in listing
    page = (
        await browser.post(
            "/admin/personalities/preview",
            personality_id="weather-goblin",
            prompt="Water?",
            evaluator="echo",
            evaluation_prompt="x",
            reply="yes",
        )
    ).text
    assert "personality didn't work, so the plain text was used" in page
    assert "no LLM API key is set" in page


async def test_api_preview_does_not_return_llm_calls(browser: Admin) -> None:
    resp = await browser.client.post(
        "/api/v1/jobs:preview",
        headers=auth(TOKEN_A),
        json={
            "job": {
                "recipient_id": "kate",
                "prompt": "Water?",
                "personality_id": "weather-goblin",
                "evaluation": {"evaluator": "echo", "prompt": "x"},
            },
            "response": {"text": "yes"},
        },
    )
    body = resp.json()
    assert "llm_calls" not in body
    assert body["personality_fallback"] is True
    assert "no LLM API key" in body["personality_error"]
