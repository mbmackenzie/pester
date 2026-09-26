from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from pester.config import PesterConfig, Settings
from pester.core.clock import FakeClock
from pester.main import create_app


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def app(tmp_path: Path, clock: FakeClock) -> FastAPI:
    settings = Settings(database_path=tmp_path / "pester.sqlite", dev_mode=True)
    return create_app(settings=settings, config=PesterConfig(), clock=clock)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
