"""Config lives in the database and changes take effect without a restart."""

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI

from pester.config import PesterConfig
from pester.configstore import ConfigStore
from pester.core.clock import FakeClock
from pester.core.models import EvaluationOutcome
from pester.delivery.memory import InMemoryChannel
from pester.main import create_app
from tests.conftest import TOKEN_A, auth, job_payload, make_settings
from tests.e2e.conftest import Loop


def store_of(app: FastAPI) -> ConfigStore:
    return app.state.config_store


async def change(app: FastAPI, edit: dict[str, Any], comment: str = "test change") -> None:
    """Save an edited copy of the current config, as the admin UI or CLI would, and reload."""
    store = store_of(app)
    data = _merge(app.state.pester.config.model_dump(mode="json"), edit)  # based on the config in effect
    await store.save(
        PesterConfig.model_validate(data), comment, expected_version=await store.latest_version()
    )
    assert await app.state.runtime.reload_config() == 1


def _merge(base: dict[str, Any], edit: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in edit.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)  # pyright: ignore[reportUnknownArgumentType]
        else:
            merged[key] = value
    return merged


async def boot(
    tmp_path: Path, clock: FakeClock, pester_config: PesterConfig | None, **settings: Any
) -> FastAPI:
    return create_app(
        settings=make_settings(tmp_path, **settings),
        config=pester_config,
        clock=clock,
        channels=[InMemoryChannel(clock)],
    )


# ---- Seeding --------------------------------------------------------------------------------------------


async def test_explicit_config_is_saved_once(tmp_path: Path, clock: FakeClock, config: PesterConfig) -> None:
    app = await boot(tmp_path, clock, config)
    async with app.router.lifespan_context(app):
        assert await store_of(app).latest_version() == 1
    app = await boot(tmp_path, clock, config)  # restart with the same config: no new version
    async with app.router.lifespan_context(app):
        assert await store_of(app).latest_version() == 1
        assert app.state.pester.config == config


async def test_yaml_seeds_an_empty_database_only(
    tmp_path: Path, clock: FakeClock, config: PesterConfig, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    app = await boot(tmp_path, clock, None, config=path)
    async with app.router.lifespan_context(app):
        latest = await store_of(app).latest()
        assert latest is not None
        assert latest.comment == f"imported from {path}"
        assert set(app.state.pester.config.recipients) == {"kate", "sam", "alex"}

    path.write_text(yaml.safe_dump({"recipients": {"someone-else": {}}}))
    app = await boot(tmp_path, clock, None, config=path)
    with caplog.at_level(logging.WARNING):
        async with app.router.lifespan_context(app):
            assert set(app.state.pester.config.recipients) == {"kate", "sam", "alex"}  # the database wins
    assert "differs from the config in the database" in caplog.text


async def test_no_config_at_all_starts_empty(tmp_path: Path, clock: FakeClock) -> None:
    app = await boot(tmp_path, clock, None)
    async with app.router.lifespan_context(app):
        assert app.state.pester.config == PesterConfig()
        assert await store_of(app).latest_version() == 1


# ---- Live changes ---------------------------------------------------------------------------------------


async def test_a_new_recipient_works_without_a_restart(loop: Loop, app: FastAPI) -> None:
    assert await loop.chat.inject("zoe", "hello?") is not None
    assert loop.seen("zoe") == []  # unknown sender: dropped

    await change(
        app,
        {
            "recipients": {"zoe": {"channels": {"fake": {"address": "zoe"}}}},
            "clients": {"producer-a": {"recipients": ["kate", "sam", "zoe"]}},
        },
    )
    job_id = await loop.submit(recipient_id="zoe")
    await loop.settle()
    assert loop.last_seen("zoe").text == "Did you water the plants?"
    assert await loop.status(job_id) == "AWAITING"


async def test_pacing_changes_apply_to_the_next_pass(loop: Loop, app: FastAPI) -> None:
    await change(app, {"scheduler": {"quiet_hours": {"start": "00:00", "end": "23:59"}}})
    job_id = await loop.submit()
    await loop.settle()
    assert await loop.status(job_id) == "QUEUED"  # quiet hours all day

    await change(app, {"scheduler": {"quiet_hours": None}})
    await loop.settle()
    assert await loop.status(job_id) == "AWAITING"


async def test_revoked_client_is_rejected_immediately(
    loop: Loop, app: FastAPI, client: httpx.AsyncClient
) -> None:
    assert (await client.get("/api/v1/events", headers=auth(TOKEN_A))).status_code == 200
    store = store_of(app)
    latest = await store.latest()
    assert latest is not None
    data = latest.config.model_dump(mode="json")
    del data["clients"]["producer-a"]
    await store.save(PesterConfig.model_validate(data), "revoke", expected_version=latest.version)
    await app.state.runtime.reload_config()
    assert (await client.get("/api/v1/events", headers=auth(TOKEN_A))).status_code == 401


async def test_a_version_that_cannot_be_built_is_skipped(
    loop: Loop, app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    store = store_of(app)
    before = app.state.runtime.live.current.version
    data = app.state.pester.config.model_dump(mode="json")
    data["personalities"]["broken"] = {"type": "no_such_module:factory"}
    await store.save(PesterConfig.model_validate(data), "broken personality")
    with caplog.at_level(logging.ERROR):
        assert await app.state.runtime.reload_config() == 0
        assert await app.state.runtime.reload_config() == 0  # logged once, not every poll
    assert caplog.text.count("can't be put into effect") == 1
    assert app.state.runtime.live.current.version == before

    await change(app, {"scheduler": {"max_outstanding": 2}})  # a later good version applies
    assert app.state.pester.config.scheduler.max_outstanding == 2


async def test_removed_personality_falls_back_to_default(loop: Loop, app: FastAPI) -> None:
    loop.evaluator.enqueue(EvaluationOutcome(result={"answer": "YES"}, feedback_facts="Plants watered."))
    await loop.submit(personality_id="houseplant")
    await loop.settle()
    data = app.state.pester.config.model_dump(mode="json")
    del data["personalities"]["houseplant"]
    store = store_of(app)
    await store.save(PesterConfig.model_validate(data), "remove houseplant")
    await app.state.runtime.reload_config()

    await loop.chat.inject("kate", "yes", reply_to=1)
    await loop.settle()
    assert loop.last_seen().text == "Plants watered."  # neutral, not "🌿 ..."


async def test_submitting_after_the_job_request_is_validated_against_live_config(
    loop: Loop, app: FastAPI, client: httpx.AsyncClient
) -> None:
    resp = await client.post("/api/v1/jobs", json=job_payload(personality_id="pirate"), headers=auth(TOKEN_A))
    assert resp.status_code == 422
    await change(
        app, {"personalities": {"pirate": {"type": "template", "feedback": "Arr, {{ feedback_facts }}"}}}
    )
    resp = await client.post("/api/v1/jobs", json=job_payload(personality_id="pirate"), headers=auth(TOKEN_A))
    assert resp.status_code == 201
