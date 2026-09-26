from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from pester.config import PesterConfig, Settings
from pester.core.clock import FakeClock
from pester.core.tokens import hash_token
from pester.main import create_app

TOKEN_A = "token-a"
TOKEN_B = "token-b"
TOKEN_READONLY = "token-readonly"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def job_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "recipient_id": "kate",
        "prompt": "Did you water the plants?",
        "evaluation": {"prompt": "Classify as YES / NO / UNCLEAR."},
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def config() -> PesterConfig:
    return PesterConfig.model_validate(
        {
            "clients": {
                "producer-a": {
                    "token_hash": hash_token(TOKEN_A),
                    "permissions": ["submit_jobs", "read_events", "preview"],
                    "recipients": ["kate", "sam"],
                },
                "producer-b": {
                    "token_hash": hash_token(TOKEN_B),
                    "permissions": ["submit_jobs", "read_events"],
                    "recipients": ["kate"],
                },
                "reader": {
                    "token_hash": hash_token(TOKEN_READONLY),
                    "permissions": ["read_events"],
                    "recipients": ["kate"],
                },
            },
            "recipients": {
                "kate": {"timezone": "America/New_York", "channels": {"fake": {"address": "kate"}}},
                "sam": {"timezone": "Europe/London"},
                "alex": {},
            },
            "personalities": {
                "default": {"type": "neutral"},
                "weather-goblin": {"type": "llm", "prompt": "Be a goblin."},
            },
        }
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def app(tmp_path: Path, config: PesterConfig, clock: FakeClock) -> FastAPI:
    settings = Settings(database_path=tmp_path / "pester.sqlite", dev_mode=True, run_workers=False)
    return create_app(settings=settings, config=config, clock=clock)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        yield c
