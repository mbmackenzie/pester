"""End-to-end harness: the real app, an in-memory channel, a scripted evaluator, and a fake clock."""

import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI

from pester.config import PesterConfig
from pester.core.clock import FakeClock
from pester.delivery.memory import ChatMessage, InMemoryChannel
from pester.evaluation.base import EvaluatorRegistry
from pester.evaluation.scripted import ScriptedEvaluator
from pester.main import create_app
from pester.runtime import Runtime
from tests.conftest import TOKEN_A, auth, job_payload, make_settings
from tests.llm_fakes import FakeLLM


@dataclass
class Loop:
    client: httpx.AsyncClient
    runtime: Runtime
    chat: InMemoryChannel
    evaluator: ScriptedEvaluator
    clock: FakeClock
    llm: FakeLLM
    db_path: Path

    def audit(self) -> list[dict[str, Any]]:
        """The evaluations table, read directly from the SQLite file."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("SELECT * FROM evaluations ORDER BY pk")]

    async def submit(self, token: str = TOKEN_A, **job: Any) -> str:
        resp = await self.client.post("/api/v1/jobs", json=job_payload(**job), headers=auth(token))
        assert resp.status_code in (200, 201), resp.text
        return resp.json()["id"]

    async def cancel(self, job_id: str, token: str = TOKEN_A) -> httpx.Response:
        return await self.client.post(f"/api/v1/jobs/{job_id}/cancel", headers=auth(token))

    async def status(self, job_id: str, token: str = TOKEN_A) -> str:
        return (await self.client.get(f"/api/v1/jobs/{job_id}", headers=auth(token))).json()["status"]

    async def events(self, job_id: str | None = None, token: str = TOKEN_A) -> list[dict[str, Any]]:
        resp = await self.client.get("/api/v1/events", params={"limit": 1000}, headers=auth(token))
        events: list[dict[str, Any]] = resp.json()["events"]
        return [e for e in events if job_id is None or e["interaction_id"] == job_id]

    async def event_types(self, job_id: str, token: str = TOKEN_A) -> list[str]:
        return [e["type"] for e in await self.events(job_id, token)]

    async def settle(self) -> None:
        await self.runtime.run_until_idle()

    def seen(self, address: str = "kate") -> list[ChatMessage]:
        return self.chat.sent(address)

    def last_seen(self, address: str = "kate") -> ChatMessage:
        return self.seen(address)[-1]


@pytest.fixture
def config(request: pytest.FixtureRequest, config: PesterConfig) -> PesterConfig:
    """The base test config, deep-merged with any ``@pytest.mark.pester_config({...})`` overrides."""
    node = cast(pytest.Item, request.node)  # pyright: ignore[reportUnknownMemberType]
    marker = node.get_closest_marker("pester_config")
    if marker is None:
        return config
    overrides: dict[str, Any] = marker.args[0]
    return PesterConfig.model_validate(_merge(config.model_dump(), overrides))


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)  # pyright: ignore[reportUnknownArgumentType]
        else:
            merged[key] = value
    return merged


@pytest.fixture
def evaluator() -> ScriptedEvaluator:
    return ScriptedEvaluator()


@pytest.fixture
def chat(clock: FakeClock) -> InMemoryChannel:
    return InMemoryChannel(clock, name="fake")


@pytest.fixture
def llm() -> FakeLLM:
    """Every e2e app talks to this fake. Unqueued requests fail loudly: no test calls an LLM by accident."""
    return FakeLLM()


@pytest.fixture
def evaluator_registry(evaluator: ScriptedEvaluator) -> EvaluatorRegistry | None:
    """Scripted by default. Override to return None to use the real defaults (LLM on the fake, rule, echo)."""
    return EvaluatorRegistry({"llm": evaluator, "rule": evaluator})


@pytest.fixture
def app(
    tmp_path: Path,
    config: PesterConfig,
    clock: FakeClock,
    chat: InMemoryChannel,
    evaluator_registry: EvaluatorRegistry | None,
    llm: FakeLLM,
) -> FastAPI:
    return create_app(
        settings=make_settings(tmp_path),
        config=config,
        clock=clock,
        channels=[chat],
        evaluators=evaluator_registry,
        llm_client=llm.client(),
    )


@pytest.fixture
async def loop(
    client: httpx.AsyncClient,
    app: FastAPI,
    chat: InMemoryChannel,
    evaluator: ScriptedEvaluator,
    clock: FakeClock,
    llm: FakeLLM,
    tmp_path: Path,
) -> AsyncIterator[Loop]:
    yield Loop(client, app.state.runtime, chat, evaluator, clock, llm, tmp_path / "pester.sqlite")
