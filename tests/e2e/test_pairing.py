"""Pairing: unknown addresses ask to be approved, or pair themselves with an invite code."""

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from pester.config import PesterConfig
from pester.core.clock import FakeClock
from pester.delivery.mock import MockChannel
from pester.delivery.pairing import ASKED, BAD_CODE, WELCOME
from pester.main import create_app
from pester.pairing import MAX_PENDING, PairingStatus, PairingStore
from pester.runtime import Runtime
from pester.service import AdminError, AdminService
from tests.conftest import TOKEN_A, auth, job_payload, make_settings


@pytest.fixture
def app(tmp_path: Path, config: PesterConfig, clock: FakeClock) -> FastAPI:
    """Dev mode, so the implicit mock channel "fake" (which accepts pairing) is running."""
    return create_app(settings=make_settings(tmp_path), config=config, clock=clock)


def runtime_of(app: FastAPI) -> Runtime:
    return app.state.runtime


def mock_of(app: FastAPI) -> MockChannel:
    channel = runtime_of(app).channels["fake"]
    assert isinstance(channel, MockChannel)
    return channel


def service_of(app: FastAPI) -> AdminService:
    return app.state.service


def pairings_of(app: FastAPI) -> PairingStore:
    return app.state.pairings


def texts(app: FastAPI, address: str) -> list[str | None]:
    return [m.text for m in mock_of(app).sent(address)]


async def test_unknown_address_asks_once_and_waits(client: httpx.AsyncClient, app: FastAPI) -> None:
    await mock_of(app).inject("zoe", "/start")
    await mock_of(app).inject("zoe", "hello??")
    assert texts(app, "zoe") == [ASKED]  # no reply spam
    [request] = await service_of(app).pending_pairings()
    assert (request.channel, request.address, request.first_text) == ("fake", "zoe", "/start")
    assert request.recipient_config == {"address": "zoe"}


async def test_approval_creates_the_recipient_and_welcomes_them(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    await mock_of(app).inject("zoe", "hi")
    [request] = await service_of(app).pending_pairings()
    await service_of(app).approve_pairing(request.pk, "zoe", timezone="Europe/Paris", clients=["producer-a"])
    await runtime_of(app).run_until_idle()
    assert texts(app, "zoe")[-1] == WELCOME

    config = app.state.pester.config
    assert config.recipients["zoe"].timezone == "Europe/Paris"
    assert config.recipients["zoe"].channels == {"fake": {"address": "zoe"}}
    assert "zoe" in config.clients["producer-a"].recipients

    # They're a normal recipient now.
    resp = await client.post("/api/v1/jobs", json=job_payload(recipient_id="zoe"), headers=auth(TOKEN_A))
    assert resp.status_code == 201
    await runtime_of(app).run_until_idle()
    assert texts(app, "zoe")[-1] == "Did you water the plants?"
    await runtime_of(app).run_until_idle()
    assert texts(app, "zoe").count(WELCOME) == 1  # welcomed exactly once


async def test_approving_as_an_existing_recipient_links_the_address(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    await mock_of(app).inject("kate-phone-2", "it's me, kate")
    [request] = await service_of(app).pending_pairings()
    await service_of(app).approve_pairing(request.pk, "sam")  # sam exists, with no channels yet
    assert app.state.pester.config.recipients["sam"].channels == {"fake": {"address": "kate-phone-2"}}
    assert app.state.pester.config.recipients["sam"].timezone == "Europe/London"  # unchanged


async def test_rejected_addresses_are_ignored(client: httpx.AsyncClient, app: FastAPI) -> None:
    await mock_of(app).inject("spammer", "buy now")
    [request] = await service_of(app).pending_pairings()
    await service_of(app).reject_pairing(request.pk)
    await mock_of(app).inject("spammer", "/start")
    assert texts(app, "spammer") == [ASKED]
    assert await service_of(app).pending_pairings() == []
    with pytest.raises(AdminError, match="isn't pending"):
        await service_of(app).approve_pairing(request.pk, "spammer")


async def test_invite_code_pairs_directly(client: httpx.AsyncClient, app: FastAPI) -> None:
    code = await service_of(app).create_invite("zoe", timezone="Asia/Tokyo")
    await mock_of(app).inject("zoe-phone", f"/start {code.lower()}")  # case and dashes don't matter
    await runtime_of(app).run_until_idle()
    assert texts(app, "zoe-phone") == [WELCOME]
    assert app.state.pester.config.recipients["zoe"].timezone == "Asia/Tokyo"
    assert await service_of(app).pending_pairings() == []

    # One use only.
    await mock_of(app).inject("someone-else", f"/start {code}")
    assert texts(app, "someone-else") == [BAD_CODE]
    [invite] = await service_of(app).invites()
    assert (invite.used_channel, invite.used_address) == ("fake", "zoe-phone")


async def test_expired_invite_is_refused(client: httpx.AsyncClient, app: FastAPI, clock: FakeClock) -> None:
    from datetime import timedelta

    code = await service_of(app).create_invite("zoe", valid_for=timedelta(hours=1))
    clock.advance(timedelta(hours=2))
    await mock_of(app).inject("zoe-phone", f"/start {code}")
    assert texts(app, "zoe-phone") == [BAD_CODE]


async def test_known_recipients_sending_start_get_help(client: httpx.AsyncClient, app: FastAPI) -> None:
    await mock_of(app).inject("kate", "/start")
    assert "already set up" in (texts(app, "kate")[-1] or "")


async def test_channels_can_refuse_pairing(client: httpx.AsyncClient, app: FastAPI) -> None:
    await service_of(app).add_channel("mock", "mock")
    await service_of(app).update_channel("mock", accept_pairing=False)
    channel = runtime_of(app).channels["mock"]
    assert isinstance(channel, MockChannel)
    await channel.inject("zoe", "/start")
    assert channel.sent("zoe") == []
    assert await service_of(app).pending_pairings() == []


async def test_removed_recipient_can_ask_again(client: httpx.AsyncClient, app: FastAPI) -> None:
    await mock_of(app).inject("zoe", "hi")
    [request] = await service_of(app).pending_pairings()
    await service_of(app).approve_pairing(request.pk, "zoe")
    await service_of(app).delete_recipient("zoe")
    await mock_of(app).inject("zoe", "hello again")
    [again] = await service_of(app).pending_pairings()
    assert again.pk == request.pk
    assert again.status is PairingStatus.PENDING


async def test_pending_requests_are_capped(client: httpx.AsyncClient, app: FastAPI) -> None:
    for i in range(MAX_PENDING):
        await mock_of(app).inject(f"bot-{i}", "hi")
    await mock_of(app).inject("one-too-many", "hi")
    assert texts(app, "one-too-many") == []
    assert len(await pairings_of(app).requests()) == MAX_PENDING
