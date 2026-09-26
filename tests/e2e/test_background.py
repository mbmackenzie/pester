"""The real background loops, woken by nudges rather than driven by run_until_idle()."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from pester.config import PesterConfig
from pester.core.clock import FakeClock
from pester.delivery.memory import InMemoryChannel
from pester.main import create_app
from tests.conftest import TOKEN_A, auth, job_payload, make_settings


async def wait_for(condition: Callable[[], Awaitable[bool]]) -> None:
    while not await condition():  # noqa: ASYNC110 - polling external state; callers bound it with a timeout
        await asyncio.sleep(0.01)


async def test_background_workers_complete_the_loop(tmp_path: Path, config: PesterConfig) -> None:
    clock = FakeClock()
    chat = InMemoryChannel(clock)
    settings = make_settings(tmp_path, run_workers=True)
    app = create_app(settings=settings, config=config, clock=clock, channels=[chat])
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):

        async def status() -> str:
            return (await client.get("/api/v1/jobs/bg", headers=auth(TOKEN_A))).json()["status"]

        async def sent(n: int) -> bool:
            return len(chat.sent("kate")) == n

        async def completed() -> bool:
            return await status() == "COMPLETED"

        # Well under the 5s poll interval, so this passes only if the nudges wake the workers.
        async with asyncio.timeout(2):
            await client.post("/api/v1/jobs", json=job_payload(id="bg"), headers=auth(TOKEN_A))
            await wait_for(lambda: sent(1))
            await chat.inject("kate", "done")
            await wait_for(completed)
        assert [m.text for m in chat.sent("kate")] == ["Did you water the plants?", "Got it: done"]
