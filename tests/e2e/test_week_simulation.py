"""A week of simulated time under realistic pacing. Every constraint is checked against what Kate saw.

Kate answers most questions after a random delay and ignores some. Everything is seeded, so a failure
reproduces exactly.
"""

import random
from collections import Counter
from datetime import datetime, time, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from tests.e2e.conftest import Loop

NY = ZoneInfo("America/New_York")
START = datetime(2026, 9, 21, 0, 0, tzinfo=NY)  # a Monday
STEP = timedelta(minutes=10)
QUIET_START, QUIET_END = time(21, 0), time(8, 30)
MIN_GAP = timedelta(minutes=120)
PER_DAY = 4
ANSWER_WITHIN = timedelta(hours=3)


@pytest.mark.pester_config(
    {
        "recipients": {"kate": {"quiet_hours": {"start": "21:00", "end": "08:30"}}},
        "scheduler": {
            "min_interval_minutes": 120,
            "max_messages_per_day": PER_DAY,
            "jitter_minutes": 30,
            "default_answer_within_seconds": int(ANSWER_WITHIN.total_seconds()),
            "max_outstanding": 1,
        },
    }
)
async def test_a_week_of_pestering_respects_every_constraint(loop: Loop) -> None:
    rng = random.Random(1234)
    loop.clock.set(START)
    for i in range(40):
        not_before = START + timedelta(hours=rng.randint(0, 120)) if i % 3 == 0 else None
        await loop.submit(
            id=f"q{i}",
            prompt=f"Question {i}?",
            delivery={
                "priority": rng.random(),
                **({"not_before": not_before.isoformat()} if not_before else {}),
            },
        )

    answer_at: dict[int, datetime] = {}  # prompt message id -> when Kate will reply
    ignored: set[str] = set()
    seen_prompts: set[int] = set()
    while loop.clock.now() < START + timedelta(days=7):
        await loop.settle()
        for message in loop.seen():
            if message.text and message.text.startswith("Question") and message.id not in seen_prompts:
                seen_prompts.add(message.id)
                if rng.random() < 0.7:
                    answer_at[message.id] = message.at + timedelta(minutes=rng.randint(1, 170))
                else:
                    ignored.add(message.text)
        for message_id, when in list(answer_at.items()):
            if when <= loop.clock.now():
                await loop.chat.inject("kate", "my answer", reply_to=message_id)
                del answer_at[message_id]
        loop.clock.advance(STEP)
    await loop.settle()

    prompts = [m for m in loop.seen() if m.text and m.text.startswith("Question")]
    assert len(prompts) >= 20, "the simulation should exercise the scheduler a lot"
    local = [m.at.astimezone(NY) for m in prompts]

    # Never during quiet hours, in Kate's timezone.
    assert all(QUIET_END <= t.time() < QUIET_START for t in local), local

    # At most PER_DAY prompts per local day.
    assert max(Counter(t.date() for t in local).values()) <= PER_DAY

    # At least MIN_GAP between prompts.
    gaps = [b - a for a, b in pairwise(local)]
    assert min(gaps) >= MIN_GAP

    # Never more than one open question: each question was resolved before the next was sent.
    by_text = {m.text: m for m in prompts}
    for earlier, later in pairwise(prompts):
        job_id = f"q{earlier.text.removeprefix('Question ').removesuffix('?')}"  # type: ignore[union-attr]
        closing = next(
            e
            for e in await loop.events(job_id)
            if e["type"] in ("INTERACTION_COMPLETED", "INTERACTION_UNANSWERED")
        )
        assert datetime.fromisoformat(closing["occurred_at"]) <= later.at

    # Ignored questions time out exactly ANSWER_WITHIN after they were sent (within one simulation step).
    assert ignored
    for text in ignored:
        job_id = f"q{text.removeprefix('Question ').removesuffix('?')}"
        (event,) = [e for e in await loop.events(job_id) if e["type"] == "INTERACTION_UNANSWERED"]
        sent = by_text[text].at
        assert timedelta(0) <= datetime.fromisoformat(event["occurred_at"]) - (sent + ANSWER_WITHIN) < STEP

    # Jitter actually varies delivery times rather than firing on the minute quiet hours end.
    first_of_day = {t.date(): t for t in reversed(local)}
    assert len({t.time() for t in first_of_day.values()}) > 1
