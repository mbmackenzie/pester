from pathlib import Path

import httpx

from pester.config import PesterConfig
from pester.core.clock import FakeClock
from pester.delivery.memory import InMemoryChannel
from pester.main import create_app
from tests.conftest import make_settings


async def test_ready_when_everything_is_up(client: httpx.AsyncClient) -> None:
    resp = await client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == {"ok": True}
    assert body["checks"]["channels"] == {"ok": True, "enabled": ["fake"], "started": ["fake"]}
    assert "workers" not in body["checks"]  # disabled in tests
    assert body["checks"]["evaluators"]["registered"] == ["echo", "llm", "rule"]


async def test_not_ready_without_channels(tmp_path: Path, config: PesterConfig) -> None:
    app = create_app(settings=make_settings(tmp_path, dev_mode=False), config=config, clock=FakeClock())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        resp = await client.get("/ready")
        assert (await client.get("/health")).status_code == 200  # alive, just not ready
    assert resp.status_code == 503
    assert resp.json()["checks"]["channels"]["error"] == "no delivery channels are enabled"


class BrokenChannel(InMemoryChannel):
    async def start(self, on_inbound: object) -> None:  # type: ignore[override]
        raise ConnectionError("provider unreachable")


async def test_not_ready_when_a_channel_fails_to_start(tmp_path: Path, config: PesterConfig) -> None:
    clock = FakeClock()
    app = create_app(
        settings=make_settings(tmp_path), config=config, clock=clock, channels=[BrokenChannel(clock)]
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        resp = await client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["channels"] == {
        "ok": False,
        "enabled": ["fake"],
        "started": [],
        "errors": {"fake": "ConnectionError('provider unreachable')"},
    }


async def test_workers_reported_when_running(tmp_path: Path, config: PesterConfig) -> None:
    app = create_app(settings=make_settings(tmp_path, run_workers=True), config=config, clock=FakeClock())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        body = (await client.get("/ready")).json()
    workers = body["checks"]["workers"]
    assert workers["ok"] is True
    assert {workers[name]["running"] for name in ("scheduler", "delivery", "evaluation")} == {True}
