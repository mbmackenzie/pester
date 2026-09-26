import asyncio
from pathlib import Path

import httpx

from pester.config import PesterConfig
from pester.core.clock import FakeClock
from pester.core.messages import OutboundMessage, SentReceipt
from pester.delivery.memory import InMemoryChannel
from pester.main import create_app
from tests.conftest import TOKEN_A, auth, job_payload, make_settings


class SlowChannel(InMemoryChannel):
    """A send that takes a while, so shutdown happens mid-send."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.sending = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, address: str, message: OutboundMessage) -> SentReceipt:
        self.sending.set()
        await self.release.wait()
        return await super().send(address, message)


async def test_shutdown_lets_an_in_flight_send_finish(tmp_path: Path, config: PesterConfig) -> None:
    clock = FakeClock()
    channel = SlowChannel(clock)
    settings = make_settings(tmp_path, run_workers=True)
    app = create_app(settings=settings, config=config, clock=clock, channels=[channel])
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/v1/jobs", json=job_payload(id="j1"), headers=auth(TOKEN_A))
    await asyncio.wait_for(channel.sending.wait(), 2)

    shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
    await asyncio.sleep(0.05)
    assert not shutdown.done()  # waiting for the send, not cancelling it
    channel.release.set()
    await asyncio.wait_for(shutdown, 2)

    # The send was recorded, so a restart has nothing ambiguous to resolve.
    app = create_app(
        settings=make_settings(tmp_path), config=config, clock=clock, channels=[InMemoryChannel(clock)]
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        job = (await client.get("/api/v1/jobs/j1", headers=auth(TOKEN_A))).json()
    assert job["status"] == "AWAITING"
