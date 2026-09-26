import json
import logging
from pathlib import Path

import httpx
import pytest

from pester.config import PesterConfig
from pester.core.clock import FakeClock
from pester.logs import JsonFormatter, TextFormatter
from pester.main import create_app
from tests.conftest import TOKEN_A, auth, job_payload, make_settings


def record(**extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("pester.test", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


def test_json_formatter_includes_structured_fields() -> None:
    line = JsonFormatter().format(record(interaction_id="j1", client_id="c", unrelated="x"))
    entry = json.loads(line)
    assert entry["message"] == "hello world"
    assert entry["level"] == "INFO" and entry["logger"] == "pester.test"
    assert entry["interaction_id"] == "j1" and entry["client_id"] == "c"
    assert "unrelated" not in entry
    assert entry["ts"].endswith("Z")


def test_text_formatter_appends_fields() -> None:
    line = TextFormatter().format(record(event="INTERACTION_QUEUED", interaction_id="j1"))
    assert line.endswith("pester.test: hello world [event=INTERACTION_QUEUED interaction_id=j1]")
    assert TextFormatter().format(record()).endswith("hello world")


async def test_every_event_is_logged_with_context_and_no_secrets(
    tmp_path: Path, config: PesterConfig, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "sk-very-secret-key-123"
    app = create_app(
        settings=make_settings(tmp_path, openai_api_key=secret), config=config, clock=FakeClock()
    )
    caplog.set_level(logging.DEBUG)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        await client.post(
            "/api/v1/jobs",
            json=job_payload(
                id="j1", evaluation={"evaluator": "rule", "prompt": "x"}, response_options=["Yes"]
            ),
            headers=auth(TOKEN_A),
        )
        await app.state.runtime.run_until_idle()
        await app.state.runtime.channels["fake"].inject("kate", selected_option="Yes")
        await app.state.runtime.run_until_idle()

    events = [r for r in caplog.records if getattr(r, "event", None)]
    assert [r.event for r in events] == [  # type: ignore[attr-defined]
        "INTERACTION_QUEUED",
        "INTERACTION_DELIVERED",
        "INTERACTION_ANSWERED",
        "INTERACTION_EVALUATED",
        "INTERACTION_COMPLETED",
    ]
    for r in events:
        assert (r.interaction_id, r.client_id, r.recipient_id) == ("j1", "producer-a", "kate")  # type: ignore[attr-defined]
    everything = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    assert secret not in everything
    assert TOKEN_A not in everything
