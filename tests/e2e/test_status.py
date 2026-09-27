"""/status says when the next question comes, and why it's waiting."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.e2e.conftest import Loop

NY = ZoneInfo("America/New_York")
QUIET = {"recipients": {"kate": {"quiet_hours": {"start": "21:00", "end": "08:30"}}}}


def ny(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 25, hour, minute, tzinfo=NY)


async def status(loop: Loop) -> str:
    await loop.chat.inject("kate", "/status")
    return loop.last_seen().text or ""


@pytest.mark.pester_config(QUIET)
async def test_quiet_hours(loop: Loop) -> None:
    loop.clock.set(ny(23, 6))
    for i in range(5):
        await loop.submit(id=f"q{i}")
    await loop.settle()
    assert await status(loop) == (
        "5 questions queued. The next comes around tomorrow 8:30 AM (quiet hours until 8:30 AM). "
        "Send /send for one now."
    )


@pytest.mark.pester_config({"scheduler": {"min_interval_minutes": 120}})
async def test_spacing(loop: Loop) -> None:
    loop.clock.set(ny(12))
    await loop.submit(id="first")
    await loop.submit(id="second")
    await loop.settle()
    await loop.chat.inject("kate", "yes", reply_to=loop.last_seen().id)
    await loop.settle()
    assert await status(loop) == (
        "1 question queued. The next comes around 2:00 PM (at most one every 2 hours). "
        "Send /send for one now."
    )


@pytest.mark.pester_config({"scheduler": {"max_messages_per_day": 1}})
async def test_daily_cap(loop: Loop) -> None:
    loop.clock.set(ny(12))
    await loop.submit(id="first")
    await loop.submit(id="second")
    await loop.settle()
    await loop.chat.inject("kate", "yes", reply_to=loop.last_seen().id)
    await loop.settle()
    assert await status(loop) == (
        "1 question queued. The next comes around tomorrow 12:00 AM (you've had today's 1). "
        "Send /send for one now."
    )


async def test_waiting_on_an_answer(loop: Loop) -> None:
    loop.clock.set(ny(9, 5))
    await loop.submit(id="first")
    await loop.submit(id="second")
    await loop.settle()
    assert await status(loop) == (
        'Waiting on your answer: "Did you water the plants?" (asked 9:05 AM).\n'
        "1 more question queued. The next comes after you answer (or /skip) the open one."
    )


async def test_snoozed(loop: Loop) -> None:
    loop.clock.set(ny(9))
    await loop.submit()
    await loop.settle()
    await loop.chat.inject("kate", "/snooze 3h")
    assert await status(loop) == (
        "1 question queued. The next comes around 12:00 PM (you snoozed it). Send /send for one now."
    )


async def test_scheduled_by_the_producer(loop: Loop) -> None:
    loop.clock.set(ny(9))
    await loop.submit(delivery={"not_before": (loop.clock.now() + timedelta(days=3)).isoformat()})
    assert await status(loop) == (
        "1 question queued. The next comes around Mon 9:00 AM (it's scheduled for later). "
        "Send /send for one now."
    )


async def test_paused_keeps_it_short(loop: Loop) -> None:
    await loop.chat.inject("kate", "/pause")
    await loop.submit()
    assert (
        await status(loop) == "Paused: send /resume to start again, or /send for one now.\n1 question queued."
    )
