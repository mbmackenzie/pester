from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.e2e.conftest import Loop

NY = ZoneInfo("America/New_York")
H = timedelta(hours=1)


def ny(hour: int, minute: int = 0, day: int = 25) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=NY)


async def at_noon(loop: Loop) -> None:
    loop.clock.set(ny(12))


# ---- Answer timeouts --------------------------------------------------------------------------------


@pytest.mark.pester_config({"scheduler": {"default_answer_within_seconds": 3600}})
async def test_ignored_prompt_times_out_and_frees_the_slot(loop: Loop) -> None:
    first = await loop.submit(id="first", delivery={"priority": 0.9})
    second = await loop.submit(id="second")
    await loop.settle()
    prompt = loop.last_seen()

    loop.clock.advance(H - timedelta(seconds=1))
    await loop.settle()
    assert await loop.status(first) == "AWAITING"

    loop.clock.advance(timedelta(seconds=1))
    await loop.settle()
    assert await loop.status(first) == "UNANSWERED"
    assert await loop.status(second) == "AWAITING"  # slot freed
    unanswered = (await loop.events(first))[-1]
    assert unanswered["type"] == "INTERACTION_UNANSWERED"
    assert unanswered["payload"]["delivery"]["deadline"] == unanswered["occurred_at"]

    await loop.chat.inject("kate", "sorry, yes", reply_to=prompt.id)
    await loop.settle()
    assert (await loop.event_types(first))[-1] == "INTERACTION_LATE_RESPONSE"
    assert await loop.status(second) == "AWAITING"  # the late reply didn't answer the new question


async def test_job_answer_window_overrides_default(loop: Loop) -> None:
    job_id = await loop.submit(delivery={"answer_within_seconds": 600})
    await loop.settle()
    loop.clock.advance(timedelta(minutes=10))
    await loop.settle()
    assert await loop.status(job_id) == "UNANSWERED"


# ---- Commands ---------------------------------------------------------------------------------------


async def test_skip(loop: Loop) -> None:
    first = await loop.submit(id="first", delivery={"priority": 0.9})
    second = await loop.submit(id="second")
    await loop.settle()
    await loop.chat.inject("kate", "/skip")
    await loop.settle()
    assert await loop.status(first) == "SKIPPED"
    assert await loop.event_types(first) == [
        "INTERACTION_QUEUED",
        "INTERACTION_DELIVERED",
        "INTERACTION_SKIPPED",
    ]
    assert [m.text for m in loop.seen()][-2:] == ["Skipped.", "Did you water the plants?"]
    assert await loop.status(second) == "AWAITING"


async def test_skip_with_nothing_open(loop: Loop) -> None:
    await loop.chat.inject("kate", "/skip")
    assert loop.last_seen().text == "Nothing to skip."


@pytest.mark.pester_config({"scheduler": {"max_outstanding": 2}})
async def test_skip_targets_the_replied_to_question(loop: Loop) -> None:
    first = await loop.submit(id="first", prompt="First?")
    second = await loop.submit(id="second", prompt="Second?")
    await loop.settle()
    await loop.chat.inject("kate", "/skip")
    assert loop.last_seen().text.startswith("You have more than one open question")  # type: ignore[union-attr]
    second_prompt = next(m for m in loop.seen() if m.text == "Second?")
    await loop.chat.inject("kate", "/skip", reply_to=second_prompt.id)
    await loop.settle()
    assert (await loop.status(first), await loop.status(second)) == ("AWAITING", "SKIPPED")


async def test_snooze(loop: Loop) -> None:
    loop.clock.set(ny(12))
    snoozed = await loop.submit(id="snoozed", prompt="Snooze me?", delivery={"priority": 0.9})
    other = await loop.submit(id="other", prompt="Other?")
    await loop.settle()

    await loop.chat.inject("kate", "/snooze 2h")
    await loop.settle()
    assert loop.seen()[-2].text == "Snoozed until 14:00."  # the recipient's local time
    assert await loop.status(snoozed) == "QUEUED"
    snooze_event = (await loop.events(snoozed))[-1]
    assert snooze_event["type"] == "INTERACTION_SNOOZED"
    assert await loop.status(other) == "AWAITING"  # the freed slot went to the next question

    await loop.chat.inject("kate", "done")
    await loop.settle()
    assert await loop.status(snoozed) == "QUEUED"  # still snoozed

    loop.clock.advance(2 * H)
    await loop.settle()
    assert await loop.status(snoozed) == "AWAITING"
    assert [m.text for m in loop.seen()].count("Snooze me?") == 2
    await loop.chat.inject("kate", "now I know", reply_to=loop.last_seen().id)
    await loop.settle()
    assert await loop.status(snoozed) == "COMPLETED"


@pytest.mark.parametrize(
    ("command", "reply"),
    [
        ("/snooze forever", "Try /snooze 30m, /snooze 2h, or /snooze 1d."),
        ("/snooze 30d", "I can snooze for at most 168 hours."),
    ],
)
async def test_snooze_rejects_bad_durations(loop: Loop, command: str, reply: str) -> None:
    job_id = await loop.submit()
    await loop.settle()
    await loop.chat.inject("kate", command)
    assert loop.last_seen().text == reply
    assert await loop.status(job_id) == "AWAITING"


async def test_snooze_defaults_to_an_hour_and_shows_weekday_if_not_today(loop: Loop) -> None:
    loop.clock.set(ny(23, 30))
    await loop.submit()
    await loop.settle()
    await loop.chat.inject("kate", "/snooze")
    assert loop.last_seen().text == "Snoozed until Sat 00:30."


async def test_pause_and_resume(loop: Loop) -> None:
    await loop.chat.inject("kate", "/pause")
    assert loop.last_seen().text == "Paused. I won't ask you anything until you send /resume."
    job_id = await loop.submit()
    await loop.settle()
    assert await loop.status(job_id) == "QUEUED"

    await loop.chat.inject("kate", "/resume")
    await loop.settle()
    assert await loop.status(job_id) == "AWAITING"


async def test_status(loop: Loop) -> None:
    loop.clock.set(ny(9, 5))
    await loop.chat.inject("kate", "/status")
    assert loop.last_seen().text == "Nothing pending."

    await loop.submit(id="a", prompt="What is the capital of Assyria?")
    await loop.submit(id="b")
    await loop.submit(id="c")
    await loop.settle()
    await loop.chat.inject("kate", "/pause")
    await loop.chat.inject("kate", "/status")
    assert loop.last_seen().text == (
        "Paused (send /resume to start again).\n"
        'Waiting on your answer: "What is the capital of Assyria?" (asked 09:05).\n'
        "2 more queued."
    )


async def test_unknown_command_lists_commands(loop: Loop) -> None:
    await loop.chat.inject("kate", "/help")
    assert loop.last_seen().text == "Commands: /skip, /snooze 2h, /pause, /resume, /status"


async def test_duplicate_command_runs_once(loop: Loop) -> None:
    first = await loop.submit(id="first", delivery={"priority": 0.9})
    second = await loop.submit(id="second")
    await loop.settle()
    inbound = await loop.chat.inject("kate", "/skip")
    await loop.settle()
    await loop.chat.redeliver(inbound)
    await loop.settle()
    assert (await loop.status(first), await loop.status(second)) == ("SKIPPED", "AWAITING")


# ---- Debounce ---------------------------------------------------------------------------------------


@pytest.mark.pester_config({"scheduler": {"debounce_seconds": 20}})
async def test_messages_within_debounce_are_joined(loop: Loop) -> None:
    job_id = await loop.submit()
    await loop.settle()
    await loop.chat.inject("kate", "yes")
    loop.clock.advance(timedelta(seconds=15))
    await loop.chat.inject("kate", "this morning, actually")
    await loop.settle()
    assert await loop.status(job_id) == "AWAITING"  # still collecting

    loop.clock.advance(timedelta(seconds=19))  # 19s after the second message: window slides
    await loop.settle()
    assert await loop.status(job_id) == "AWAITING"

    loop.clock.advance(timedelta(seconds=1))
    await loop.settle()
    assert await loop.status(job_id) == "COMPLETED"
    _, response = loop.evaluator.calls[0]
    assert response.text == "yes\nthis morning, actually"
    answered = (await loop.events(job_id))[2]
    assert answered["type"] == "INTERACTION_ANSWERED"
    assert answered["payload"]["response"]["text"] == "yes\nthis morning, actually"
    feedback = loop.last_seen()
    assert feedback.reply_to == 3  # threads onto the latest message (#3)


@pytest.mark.pester_config({"scheduler": {"debounce_seconds": 20}})
async def test_button_press_closes_debounce_immediately(loop: Loop) -> None:
    job_id = await loop.submit(response_options=["Yes", "No"])
    await loop.settle()
    await loop.chat.inject("kate", "hmm")
    await loop.chat.inject("kate", selected_option="Yes", reply_to=loop.seen()[0].id)
    await loop.settle()
    assert await loop.status(job_id) == "COMPLETED"
    _, response = loop.evaluator.calls[0]
    assert (response.text, response.selected_option) == ("hmm\nYes", "Yes")


@pytest.mark.pester_config({"scheduler": {"debounce_seconds": 60, "default_answer_within_seconds": 600}})
async def test_answer_being_collected_does_not_time_out(loop: Loop) -> None:
    job_id = await loop.submit()
    await loop.settle()
    loop.clock.advance(timedelta(minutes=9, seconds=30))
    await loop.chat.inject("kate", "just in time")
    loop.clock.advance(timedelta(seconds=45))  # past the answer deadline, inside the debounce window
    await loop.settle()
    assert await loop.status(job_id) == "AWAITING"
    loop.clock.advance(timedelta(seconds=15))
    await loop.settle()
    assert await loop.status(job_id) == "COMPLETED"


@pytest.mark.pester_config({"scheduler": {"debounce_seconds": 60}})
async def test_skip_discards_a_partial_answer(loop: Loop) -> None:
    job_id = await loop.submit()
    await loop.settle()
    await loop.chat.inject("kate", "umm")
    await loop.chat.inject("kate", "/skip")
    loop.clock.advance(timedelta(minutes=5))
    await loop.settle()
    assert await loop.status(job_id) == "SKIPPED"
    assert loop.evaluator.calls == []


# ---- Quiet hours through the whole app -------------------------------------------------------------


@pytest.mark.pester_config({"recipients": {"kate": {"quiet_hours": {"start": "21:00", "end": "08:30"}}}})
async def test_quiet_hours_in_the_recipients_timezone(loop: Loop) -> None:
    loop.clock.set(ny(23))
    job_id = await loop.submit()
    await loop.settle()
    assert await loop.status(job_id) == "QUEUED"
    loop.clock.set(ny(8, 29, day=26))
    await loop.settle()
    assert await loop.status(job_id) == "QUEUED"
    loop.clock.set(ny(8, 30, day=26))
    await loop.settle()
    assert await loop.status(job_id) == "AWAITING"
