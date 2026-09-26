from pathlib import Path

import httpx
from fastapi import FastAPI

from pester.config import PesterConfig
from pester.core.clock import FakeClock
from pester.devchat import ChatLine, ChatSession
from pester.main import create_app
from pester.runtime import Runtime
from tests.conftest import TOKEN_A, auth, job_payload, make_settings


async def test_chat_session_full_loop(client: httpx.AsyncClient, app: FastAPI) -> None:
    runtime: Runtime = app.state.runtime
    session = ChatSession(client, "kate")
    await client.post("/api/v1/jobs", json=job_payload(response_options=["Yes", "No"]), headers=auth(TOKEN_A))
    await runtime.run_until_idle()

    (prompt,) = await session.poll()
    assert prompt == "[#1] pester: Did you water the plants?\n      1) Yes   2) No"
    assert await session.poll() == []  # nothing new

    assert await session.send(ChatLine(press=(1, 3))) == "message #1 has no option 3"
    assert await session.send(ChatLine(press=(1, 1))) is None
    await runtime.run_until_idle()
    assert await session.poll() == ["[#3] pester (re #2): Got it: Yes"]  # default echo evaluator


async def test_chat_read_raw(client: httpx.AsyncClient) -> None:
    resp = await client.post("/dev/chat/kate", json={"text": "hi"})
    assert resp.status_code == 201
    assert resp.json()["direction"] == "in"
    messages = (await client.get("/dev/chat/kate")).json()
    assert [(m["direction"], m["text"]) for m in messages] == [
        ("in", "hi"),
        ("out", "Nothing pending right now."),
    ]
    assert (await client.get("/dev/chat/kate", params={"after": 2})).json() == []


async def test_chat_requires_text_or_option(client: httpx.AsyncClient) -> None:
    assert (await client.post("/dev/chat/kate", json={})).status_code == 422


async def test_dev_routes_absent_outside_dev_mode(tmp_path: Path, config: PesterConfig) -> None:
    settings = make_settings(tmp_path, dev_mode=False)
    app = create_app(settings=settings, config=config, clock=FakeClock())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/dev/chat/kate")).status_code == 404
        runtime: Runtime = app.state.runtime
        assert dict(runtime.channels) == {}
