"""/send: a person asks for their next question now, skipping pacing."""

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from pester.config import PesterConfig
from pester.core.clock import FakeClock
from pester.delivery.memory import InMemoryChannel
from pester.evaluation.base import EvaluatorRegistry
from pester.evaluation.scripted import ScriptedEvaluator
from pester.main import create_app
from tests.conftest import TOKEN_A, auth, job_payload, make_settings
from tests.e2e.conftest import Loop

NY = ZoneInfo("America/New_York")
QUIET_ALL_DAY = {"recipients": {"kate": {"quiet_hours": {"start": "00:00", "end": "23:59"}}}}


def ny(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 25, hour, minute, tzinfo=NY)


@pytest.mark.pester_config(QUIET_ALL_DAY)
async def test_send_skips_quiet_hours(loop: Loop) -> None:
    job_id = await loop.submit()
    await loop.settle()
    assert await loop.status(job_id) == "QUEUED"
    assert loop.seen() == []

    await loop.chat.inject("kate", "/send")
    await loop.settle()
    assert await loop.status(job_id) == "AWAITING"
    assert [m.text for m in loop.seen()] == ["Did you water the plants?"]  # the question is the reply


@pytest.mark.pester_config({"scheduler": {"max_messages_per_day": 1, "min_interval_minutes": 120}})
async def test_send_skips_the_daily_cap_and_spacing(loop: Loop) -> None:
    loop.clock.set(ny(12))
    await loop.submit(id="first")
    second = await loop.submit(id="second")
    await loop.settle()
    await loop.chat.inject("kate", "yes", reply_to=loop.last_seen().id)
    await loop.settle()
    assert await loop.status(second) == "QUEUED"  # capped for today

    await loop.chat.inject("kate", "/send")
    await loop.settle()
    assert await loop.status(second) == "AWAITING"


async def test_send_with_a_question_already_open(loop: Loop) -> None:
    first = await loop.submit(id="first", delivery={"priority": 0.9})
    second = await loop.submit(id="second")
    await loop.settle()
    await loop.chat.inject("kate", "/send")
    await loop.settle()
    reply = loop.last_seen().text or ""
    assert reply.startswith('You still have an open question: "Did you water the plants?".')
    assert "Answer it, /skip it, or /snooze it first." in reply
    assert await loop.status(first) == "AWAITING"
    assert await loop.status(second) == "QUEUED"


async def test_send_with_nothing_queued(loop: Loop) -> None:
    await loop.chat.inject("kate", "/send")
    assert loop.last_seen().text == "Nothing is queued for you right now."


@pytest.mark.pester_config(QUIET_ALL_DAY)
async def test_send_respects_the_producers_window(loop: Loop) -> None:
    now = loop.clock.now()
    await loop.submit(id="later", delivery={"not_before": (now + timedelta(hours=1)).isoformat()})
    await loop.chat.inject("kate", "/send")
    await loop.settle()
    assert loop.last_seen().text == "Nothing is queued for you right now."
    assert await loop.status("later") == "QUEUED"


@pytest.mark.pester_config(QUIET_ALL_DAY)
async def test_send_picks_the_highest_priority(loop: Loop) -> None:
    await loop.submit(id="low", prompt="low", delivery={"priority": 0.1})
    await loop.submit(id="high", prompt="high", delivery={"priority": 0.9})
    await loop.chat.inject("kate", "/send")
    await loop.settle()
    assert loop.last_seen().text == "high"


@pytest.mark.pester_config(QUIET_ALL_DAY)
async def test_send_while_paused_sends_one_and_says_so(loop: Loop) -> None:
    job_id = await loop.submit()
    await loop.chat.inject("kate", "/pause")
    await loop.chat.inject("kate", "/send")
    await loop.settle()
    texts = [m.text for m in loop.seen()]
    assert "Here's one. You're still paused" in (texts[-2] or "")
    assert texts[-1] == "Did you water the plants?"
    assert await loop.status(job_id) == "AWAITING"


@pytest.mark.pester_config({"scheduler": {"min_interval_minutes": 120}})
async def test_a_requested_send_counts_toward_pacing(loop: Loop) -> None:
    await loop.submit(id="first")
    second = await loop.submit(id="second")
    await loop.chat.inject("kate", "/send")  # claims "first" before the scheduler runs
    await loop.settle()
    await loop.chat.inject("kate", "yes", reply_to=loop.last_seen().id)
    await loop.settle()
    loop.clock.advance(timedelta(minutes=5))
    await loop.settle()
    assert await loop.status(second) == "QUEUED"  # spaced 2h from the requested send
    loop.clock.advance(timedelta(hours=2))
    await loop.settle()
    assert await loop.status(second) == "AWAITING"


async def test_send_answers_on_the_channel_it_was_asked_on(
    tmp_path: Path, config: PesterConfig, clock: FakeClock
) -> None:
    data = config.model_dump(mode="json")
    data["recipients"]["kate"]["channels"] = {"fake": {"address": "kate"}, "other": {"address": "kate"}}
    data["recipients"]["kate"]["quiet_hours"] = {"start": "00:00", "end": "23:59"}
    fake, other = InMemoryChannel(clock, "fake"), InMemoryChannel(clock, "other")
    app = create_app(
        settings=make_settings(tmp_path),
        config=PesterConfig.model_validate(data),
        clock=clock,
        channels=[fake, other],
        evaluators=EvaluatorRegistry({"llm": ScriptedEvaluator()}),
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://t") as client,
    ):
        await client.post("/api/v1/jobs", json=job_payload(), headers=auth(TOKEN_A))
        await other.inject("kate", "/send")  # "fake" comes first for kate, but she asked on "other"
        await app.state.runtime.run_until_idle()
    assert [m.text for m in other.sent("kate")] == ["Did you water the plants?"]
    assert fake.sent("kate") == []


@pytest.mark.pester_config({"scheduler": {"shuffle_jobs": True}})
async def test_shuffle_preview_scheduler_and_send_agree(loop: Loop) -> None:
    for i in range(1, 11):
        await loop.submit(id=f"q{i}", prompt=f"Question {i}")
    order: list[str] = []
    for i in range(10):
        preview = await loop.runtime.scheduler.next_send("kate")
        assert preview is not None
        if i % 2:
            result = await loop.runtime.scheduler.send_now("kate")
            assert result.job is not None and result.job.pk == preview.key
        await loop.settle()
        order.append(loop.last_seen().text or "")
        await loop.chat.inject("kate", "/skip")
    assert sorted(order) == sorted(f"Question {i}" for i in range(1, 11))
    assert order != [f"Question {i}" for i in range(1, 11)]
