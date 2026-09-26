"""Send retries, ambiguous sends, and recovery after a crash."""

from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from pester.config import PesterConfig
from pester.core.clock import FakeClock
from pester.core.messages import OutboundMessage, SentReceipt
from pester.delivery.base import ChannelError, PermanentChannelError
from pester.delivery.memory import InMemoryChannel
from pester.delivery.mock import MockChannel
from pester.evaluation.base import EvaluatorRegistry
from pester.evaluation.scripted import ScriptedEvaluator
from pester.main import create_app
from pester.runtime import Runtime
from tests.conftest import TOKEN_A, auth, job_payload, make_settings
from tests.e2e.conftest import Loop

S = timedelta(seconds=1)


# ---- Retries ----------------------------------------------------------------------------------------


async def test_transient_failures_retry_with_backoff(loop: Loop) -> None:
    for _ in range(3):
        loop.chat.fail_next_send()
    job_id = await loop.submit()
    await loop.settle()
    assert await loop.status(job_id) == "SENDING"  # waiting to retry
    assert loop.seen() == []

    for wait in (5, 10):  # backoff doubles: 5s, 10s, (20s)
        loop.clock.advance(wait * S - S)
        await loop.settle()
        assert loop.seen() == [], f"retried before the {wait}s backoff elapsed"
        loop.clock.advance(S)
        await loop.settle()
    assert loop.seen() == []
    loop.clock.advance(20 * S)
    await loop.settle()
    assert await loop.status(job_id) == "AWAITING"  # fourth attempt went through
    assert len(loop.seen()) == 1


@pytest.mark.pester_config({"delivery": {"max_attempts": 3, "backoff_seconds": 1}})
async def test_retries_exhausted_fail_the_job(loop: Loop) -> None:
    for _ in range(3):
        loop.chat.fail_next_send()
    job_id = await loop.submit()
    for _ in range(3):
        await loop.settle()
        loop.clock.advance(timedelta(minutes=1))
    await loop.settle()
    failed = (await loop.events(job_id))[-1]
    assert failed["type"] == "INTERACTION_FAILED"
    assert failed["payload"]["reason"] == "prompt_delivery_failed"
    assert failed["payload"]["attempts"] == 3


async def test_permanent_failure_is_not_retried(loop: Loop) -> None:
    loop.chat.fail_next_send(PermanentChannelError("blocked by user"))
    job_id = await loop.submit()
    await loop.settle()
    failed = (await loop.events(job_id))[-1]
    assert failed["payload"]["reason"] == "prompt_delivery_failed"
    assert failed["payload"]["attempts"] == 1


async def test_unknown_send_outcome_is_never_retried(loop: Loop) -> None:
    loop.chat.fail_next_send(TimeoutError("did it arrive? nobody knows"))
    job_id = await loop.submit()
    await loop.settle()
    loop.clock.advance(timedelta(hours=1))
    await loop.settle()
    assert loop.seen() == []
    assert (await loop.events(job_id))[-1]["payload"]["reason"] == "ambiguous_send"


async def test_feedback_retries_too(loop: Loop) -> None:
    job_id = await loop.submit()
    await loop.settle()
    await loop.chat.inject("kate", "answer")
    await loop.runtime.evaluation.run_once()
    loop.chat.fail_next_send()
    await loop.settle()
    assert await loop.status(job_id) == "EVALUATED"
    loop.clock.advance(5 * S)
    await loop.settle()
    assert await loop.status(job_id) == "COMPLETED"


async def test_cancel_during_backoff_stops_retries(loop: Loop) -> None:
    loop.chat.fail_next_send()
    job_id = await loop.submit()
    await loop.settle()
    await loop.cancel(job_id)
    loop.clock.advance(timedelta(minutes=1))
    await loop.settle()
    assert loop.seen() == []
    assert await loop.status(job_id) == "CANCELLED"


# ---- Crash recovery ---------------------------------------------------------------------------------


class Crash(BaseException):
    """Stands in for the process dying: not an Exception, so nothing catches it."""


class CrashingChannel(InMemoryChannel):
    """Dies mid-send, after the delivery was committed as SENDING, on the chosen send."""

    def __init__(self, clock: FakeClock, crash_on_send: int) -> None:
        super().__init__(clock)
        self._sends = 0
        self._crash_on = crash_on_send

    async def send(self, address: str, message: OutboundMessage) -> SentReceipt:
        self._sends += 1
        if self._sends == self._crash_on:
            raise Crash
        return await super().send(address, message)


async def boot(
    tmp_path: Path, config: PesterConfig, clock: FakeClock, channel: InMemoryChannel
) -> tuple[FastAPI, httpx.AsyncClient]:
    app = create_app(
        settings=make_settings(tmp_path),
        config=config,
        clock=clock,
        channels=[channel],
        evaluators=EvaluatorRegistry({"llm": ScriptedEvaluator()}),
    )
    return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def runtime_of(app: FastAPI) -> Runtime:
    runtime: Runtime = app.state.runtime
    return runtime


async def test_crash_mid_prompt_send_fails_ambiguously_and_never_resends(
    tmp_path: Path, config: PesterConfig, clock: FakeClock
) -> None:
    first = CrashingChannel(clock, crash_on_send=1)
    app, client = await boot(tmp_path, config, clock, first)
    async with app.router.lifespan_context(app), client:
        await client.post("/api/v1/jobs", json=job_payload(id="j1"), headers=auth(TOKEN_A))
        await client.post("/api/v1/jobs", json=job_payload(id="j2"), headers=auth(TOKEN_A))
        with pytest.raises(Crash):
            await runtime_of(app).run_until_idle()

    # Restart on the same database file with a healthy channel.
    second = InMemoryChannel(clock)
    app, client = await boot(tmp_path, config, clock, second)
    async with app.router.lifespan_context(app), client:
        await runtime_of(app).run_until_idle()
        j1 = (await client.get("/api/v1/jobs/j1", headers=auth(TOKEN_A))).json()
        j2 = (await client.get("/api/v1/jobs/j2", headers=auth(TOKEN_A))).json()
        events = (await client.get("/api/v1/events", headers=auth(TOKEN_A))).json()["events"]
    assert j1["status"] == "FAILED"
    assert [e["payload"].get("reason") for e in events if e["interaction_id"] == "j1"][-1] == "ambiguous_send"
    assert j2["status"] == "AWAITING"  # the queue moved on
    assert [m.text for m in second.sent("kate")] == ["Did you water the plants?"]  # j2 only; j1 never resent


async def test_crash_mid_feedback_send_completes_without_resending(
    tmp_path: Path, config: PesterConfig, clock: FakeClock
) -> None:
    first = CrashingChannel(clock, crash_on_send=2)  # the prompt succeeds, the feedback dies
    app, client = await boot(tmp_path, config, clock, first)
    async with app.router.lifespan_context(app), client:
        await client.post("/api/v1/jobs", json=job_payload(id="j1"), headers=auth(TOKEN_A))
        await runtime_of(app).run_until_idle()
        await first.inject("kate", "answer")
        with pytest.raises(Crash):
            await runtime_of(app).run_until_idle()

    second = InMemoryChannel(clock)
    app, client = await boot(tmp_path, config, clock, second)
    async with app.router.lifespan_context(app), client:
        await runtime_of(app).run_until_idle()
        events = (await client.get("/api/v1/events", headers=auth(TOKEN_A))).json()["events"]
    completed = events[-1]
    assert completed["type"] == "INTERACTION_COMPLETED"
    assert completed["payload"]["feedback"]["delivery_uncertain"] is True
    assert events[-2]["type"] == "INTERACTION_EVALUATED"  # the evaluation survived
    assert second.sent("kate") == []


async def test_work_in_every_other_state_resumes_after_restart(
    tmp_path: Path, config: PesterConfig, clock: FakeClock
) -> None:
    channel = InMemoryChannel(clock)
    app, client = await boot(tmp_path, config, clock, channel)
    async with app.router.lifespan_context(app), client:
        for job_id in ("queued", "claimed"):
            await client.post("/api/v1/jobs", json=job_payload(id=job_id), headers=auth(TOKEN_A))
        await runtime_of(app).scheduler.run_once()  # claims one job; its delivery is PENDING, never attempted
    # Stop here, as if crashed. A claimed-but-unattempted delivery is safe to send after restart.

    app, client = await boot(tmp_path, config, clock, channel)
    async with app.router.lifespan_context(app), client:
        runtime = runtime_of(app)
        await runtime.run_until_idle()
        assert len(channel.sent("kate")) == 1
        await channel.inject("kate", "first answer")  # now ANSWERED; stop before evaluating it
        statuses = {
            job_id: (await client.get(f"/api/v1/jobs/{job_id}", headers=auth(TOKEN_A))).json()["status"]
            for job_id in ("queued", "claimed")
        }
    assert sorted(statuses.values()) == ["ANSWERED", "QUEUED"]

    # Restart with an answered job: evaluation, feedback, and the next question all resume.
    app, client = await boot(tmp_path, config, clock, channel)
    async with app.router.lifespan_context(app), client:
        await runtime_of(app).run_until_idle()
        statuses = {
            job_id: (await client.get(f"/api/v1/jobs/{job_id}", headers=auth(TOKEN_A))).json()["status"]
            for job_id in ("queued", "claimed")
        }
    assert sorted(statuses.values()) == ["AWAITING", "COMPLETED"]
    assert len(channel.sent("kate")) == 3  # prompt, feedback, next prompt


async def test_retry_backoff_survives_restart(tmp_path: Path, config: PesterConfig, clock: FakeClock) -> None:
    channel = InMemoryChannel(clock)
    channel.fail_next_send(ChannelError("offline"))
    app, client = await boot(tmp_path, config, clock, channel)
    async with app.router.lifespan_context(app), client:
        await client.post("/api/v1/jobs", json=job_payload(id="j1"), headers=auth(TOKEN_A))
        await runtime_of(app).run_until_idle()

    app, client = await boot(tmp_path, config, clock, channel)
    async with app.router.lifespan_context(app), client:
        runtime = runtime_of(app)
        await runtime.run_until_idle()
        assert channel.sent("kate") == []  # still backing off
        clock.advance(5 * S)
        await runtime.run_until_idle()
        assert len(channel.sent("kate")) == 1


async def test_mock_channel_conversations_survive_restart(
    tmp_path: Path, config: PesterConfig, clock: FakeClock
) -> None:
    """The mock channel keeps its transcript, so ids are never reused and replies route to the right job."""

    def boot_dev() -> tuple[FastAPI, httpx.AsyncClient]:
        app = create_app(
            settings=make_settings(tmp_path),  # dev mode: the implicit mock channel "fake"
            config=config,
            clock=clock,
            evaluators=EvaluatorRegistry({"llm": ScriptedEvaluator()}),
        )
        return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    app, client = boot_dev()
    async with app.router.lifespan_context(app), client:
        mock = runtime_of(app).channels["fake"]
        assert isinstance(mock, MockChannel)
        await client.post("/api/v1/jobs", json=job_payload(id="old"), headers=auth(TOKEN_A))
        await runtime_of(app).run_until_idle()
        await mock.inject("kate", "old answer", reply_to=1)
        await runtime_of(app).run_until_idle()
        before = [m.text for m in mock.conversation("kate")]

    app, client = boot_dev()
    async with app.router.lifespan_context(app), client:
        mock = runtime_of(app).channels["fake"]
        assert isinstance(mock, MockChannel)
        assert [m.text for m in mock.conversation("kate")] == before  # restored
        await client.post("/api/v1/jobs", json=job_payload(id="new"), headers=auth(TOKEN_A))
        await runtime_of(app).run_until_idle()
        prompt = mock.sent("kate")[-1]
        assert prompt.id == 4  # after the old prompt, answer, and feedback
        await mock.inject("kate", "new answer", reply_to=prompt.id)
        await runtime_of(app).run_until_idle()
        status = (await client.get("/api/v1/jobs/new", headers=auth(TOKEN_A))).json()["status"]
    assert status == "COMPLETED"
