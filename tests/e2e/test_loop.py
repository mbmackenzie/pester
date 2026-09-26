from datetime import timedelta

import pytest

from pester.core.messages import OutboundMessage
from pester.evaluation.base import EvaluationOutcome
from tests.conftest import TOKEN_B
from tests.e2e.conftest import Loop

HAPPY = [
    "INTERACTION_QUEUED",
    "INTERACTION_DELIVERED",
    "INTERACTION_ANSWERED",
    "INTERACTION_EVALUATED",
    "INTERACTION_COMPLETED",
]


async def test_happy_path(loop: Loop) -> None:
    loop.evaluator.enqueue(EvaluationOutcome(result={"answer": "YES"}, feedback_facts="Logged: watered."))
    job_id = await loop.submit(id="plants", prompt="Did you water the plants?", metadata={"plant": "fern"})
    await loop.settle()

    prompt = loop.last_seen()
    assert prompt.text == "Did you water the plants?"
    assert await loop.status(job_id) == "AWAITING"

    loop.clock.advance(timedelta(minutes=3))
    await loop.chat.inject("kate", "yep, this morning", reply_to=prompt.id)
    await loop.settle()

    feedback = loop.last_seen()
    assert feedback.text == "Logged: watered."
    assert feedback.reply_to == prompt.id + 1  # threads onto the person's answer
    assert await loop.status(job_id) == "COMPLETED"

    events = await loop.events(job_id)
    assert [e["type"] for e in events] == HAPPY
    assert all(e["metadata"] == {"plant": "fern"} for e in events)
    evaluated = events[3]["payload"]
    assert evaluated["response"]["text"] == "yep, this morning"
    assert evaluated["evaluation"] == {"result": {"answer": "YES"}, "evaluator": "scripted", "model": None}
    assert evaluated["delivery"]["response_latency_s"] == 180.0
    assert events[4]["payload"]["feedback"]["text"] == "Logged: watered."

    job, response = loop.evaluator.calls[0]
    assert job.id == "plants" and response.text == "yep, this morning"


async def test_plain_message_routes_to_single_open_question(loop: Loop) -> None:
    job_id = await loop.submit()
    await loop.settle()
    await loop.chat.inject("kate", "no reply-to, still counts")
    await loop.settle()
    assert await loop.status(job_id) == "COMPLETED"


async def test_button_press(loop: Loop) -> None:
    job_id = await loop.submit(response_options=["Yes", "No"])
    await loop.settle()
    prompt = loop.last_seen()
    assert prompt.options == ["Yes", "No"]
    await loop.chat.inject("kate", selected_option="No", reply_to=prompt.id)
    await loop.settle()
    _, response = loop.evaluator.calls[0]
    assert (response.text, response.selected_option) == ("No", "No")
    assert await loop.status(job_id) == "COMPLETED"


async def test_one_outstanding_question_at_a_time(loop: Loop) -> None:
    first = await loop.submit(id="first", delivery={"priority": 0.9})
    second = await loop.submit(id="second")
    await loop.settle()
    assert [m.text for m in loop.seen()] == ["Did you water the plants?"]
    assert await loop.status(second) == "QUEUED"

    await loop.chat.inject("kate", "done")
    await loop.settle()
    assert await loop.status(first) == "COMPLETED"
    assert await loop.status(second) == "AWAITING"  # slot freed once feedback was delivered


@pytest.mark.pester_config({"recipients": {"sam": {"channels": {"fake": {"address": "sam"}}}}})
async def test_recipients_are_independent(loop: Loop) -> None:
    kate = await loop.submit(recipient_id="kate")
    sam = await loop.submit(recipient_id="sam")
    await loop.settle()
    assert await loop.status(kate) == "AWAITING"
    assert await loop.status(sam) == "AWAITING"


async def test_not_before_respected(loop: Loop) -> None:
    job_id = await loop.submit(delivery={"not_before": (loop.clock.now() + timedelta(hours=2)).isoformat()})
    await loop.settle()
    assert loop.seen() == []
    loop.clock.advance(timedelta(hours=2))
    await loop.settle()
    assert await loop.status(job_id) == "AWAITING"


async def test_undelivered_job_expires(loop: Loop) -> None:
    blocker = await loop.submit(id="blocker")
    expiring = await loop.submit(
        id="exp", delivery={"expires_at": (loop.clock.now() + timedelta(hours=1)).isoformat()}
    )
    await loop.settle()
    loop.clock.advance(timedelta(hours=1))
    await loop.settle()
    assert await loop.status(expiring) == "EXPIRED"
    assert await loop.event_types(expiring) == ["INTERACTION_QUEUED", "INTERACTION_EXPIRED"]
    assert await loop.status(blocker) == "AWAITING"


async def test_recipient_without_enabled_channel_stays_queued(loop: Loop) -> None:
    job_id = await loop.submit(recipient_id="sam")  # sam has no channels in the base config
    await loop.settle()
    assert await loop.status(job_id) == "QUEUED"


async def test_cancel_while_awaiting_makes_reply_late(loop: Loop) -> None:
    job_id = await loop.submit()
    await loop.settle()
    assert (await loop.cancel(job_id)).json()["status"] == "CANCELLED"
    await loop.chat.inject("kate", "answer after cancel")
    await loop.settle()
    assert loop.evaluator.calls == []
    assert loop.last_seen().text == "Nothing pending right now."
    assert await loop.event_types(job_id) == [
        "INTERACTION_QUEUED",
        "INTERACTION_DELIVERED",
        "INTERACTION_CANCELLED",
    ]


async def test_reply_to_cancelled_prompt_is_late_response(loop: Loop) -> None:
    job_id = await loop.submit()
    await loop.settle()
    prompt = loop.last_seen()
    await loop.cancel(job_id)
    await loop.chat.inject("kate", "too late", reply_to=prompt.id)
    await loop.settle()
    events = await loop.events(job_id)
    assert events[-1]["type"] == "INTERACTION_LATE_RESPONSE"
    assert events[-1]["payload"]["response"]["text"] == "too late"


async def test_cancel_between_claim_and_send_sends_nothing(loop: Loop) -> None:
    job_id = await loop.submit()
    await loop.runtime.scheduler.run_once()  # QUEUED -> SENDING, delivery pending
    assert await loop.status(job_id) == "SENDING"
    await loop.cancel(job_id)
    await loop.settle()
    assert loop.seen() == []
    assert await loop.status(job_id) == "CANCELLED"


async def test_cancel_after_evaluation_suppresses_feedback(loop: Loop) -> None:
    job_id = await loop.submit()
    await loop.settle()
    await loop.chat.inject("kate", "answer")
    await loop.runtime.evaluation.run_once()
    assert await loop.status(job_id) == "EVALUATED"
    await loop.cancel(job_id)
    await loop.settle()
    assert [m.text for m in loop.seen()] == ["Did you water the plants?"]
    assert (await loop.event_types(job_id))[-1] == "INTERACTION_CANCELLED"


async def test_late_reply_after_completion(loop: Loop) -> None:
    job_id = await loop.submit()
    await loop.settle()
    prompt = loop.last_seen()
    await loop.chat.inject("kate", "first answer", reply_to=prompt.id)
    await loop.settle()
    await loop.chat.inject("kate", "actually, second thoughts", reply_to=prompt.id)
    await loop.settle()
    assert len(loop.evaluator.calls) == 1
    assert (await loop.event_types(job_id))[-1] == "INTERACTION_LATE_RESPONSE"


async def test_unknown_sender_is_dropped(loop: Loop) -> None:
    job_id = await loop.submit()
    await loop.settle()
    await loop.chat.inject("mallory", "let me answer kate's question")
    await loop.settle()
    assert await loop.status(job_id) == "AWAITING"
    assert loop.seen("mallory") == []  # no reply at all to strangers


async def test_duplicate_inbound_is_ignored(loop: Loop) -> None:
    job_id = await loop.submit()
    await loop.submit(id="next")
    await loop.settle()
    inbound = await loop.chat.inject("kate", "answer")
    await loop.settle()
    assert await loop.status(job_id) == "COMPLETED"

    await loop.chat.redeliver(inbound)  # provider retries the same message
    await loop.settle()
    assert len(loop.evaluator.calls) == 1
    assert await loop.status("next") == "AWAITING"  # not answered by the redelivered message
    assert "INTERACTION_LATE_RESPONSE" not in await loop.event_types(job_id)


async def test_nothing_pending_notice(loop: Loop) -> None:
    await loop.chat.inject("kate", "hello?")
    assert loop.last_seen().text == "Nothing pending right now."


async def test_commands_get_not_supported_notice(loop: Loop) -> None:
    job_id = await loop.submit()
    await loop.settle()
    await loop.chat.inject("kate", "/skip")
    await loop.settle()
    assert loop.last_seen().text == "Commands aren't supported yet."
    assert await loop.status(job_id) == "AWAITING"


async def test_evaluation_failure_fails_job_but_keeps_response(loop: Loop) -> None:
    loop.evaluator.enqueue(RuntimeError("model exploded"))
    job_id = await loop.submit()
    await loop.settle()
    await loop.chat.inject("kate", "my answer")
    await loop.settle()
    events = await loop.events(job_id)
    assert [e["type"] for e in events][-2:] == ["INTERACTION_ANSWERED", "INTERACTION_FAILED"]
    assert events[-2]["payload"]["response"]["text"] == "my answer"
    assert events[-1]["payload"]["reason"] == "evaluation_failed"


async def test_prompt_send_failure_fails_job(loop: Loop) -> None:
    loop.chat.fail_next_send()
    job_id = await loop.submit()
    await loop.settle()
    events = await loop.events(job_id)
    assert events[-1]["type"] == "INTERACTION_FAILED"
    assert events[-1]["payload"]["reason"] == "prompt_delivery_failed"


async def test_feedback_send_failure_fails_job(loop: Loop) -> None:
    job_id = await loop.submit()
    await loop.settle()
    await loop.chat.inject("kate", "answer")
    await loop.runtime.evaluation.run_once()
    loop.chat.fail_next_send()
    await loop.settle()
    events = await loop.events(job_id)
    assert [e["type"] for e in events][-2:] == ["INTERACTION_EVALUATED", "INTERACTION_FAILED"]
    assert events[-1]["payload"]["reason"] == "feedback_delivery_failed"


async def test_events_stay_client_scoped_through_the_loop(loop: Loop) -> None:
    a = await loop.submit(id="a-job")
    await loop.settle()
    await loop.chat.inject("kate", "answer")
    await loop.settle()
    assert await loop.event_types(a) == HAPPY
    assert await loop.events(token=TOKEN_B) == []


async def test_notice_send_failure_does_not_break_routing(loop: Loop) -> None:
    loop.chat.fail_next_send()
    await loop.chat.inject("kate", "hello?")  # notice fails to send; must not raise
    job_id = await loop.submit()
    await loop.settle()
    assert await loop.status(job_id) == "AWAITING"


@pytest.mark.pester_config({"scheduler": {"max_outstanding": 2}})
async def test_ambiguous_plain_reply_asks_for_threading(loop: Loop) -> None:
    first = await loop.submit(id="first")
    second = await loop.submit(id="second")
    await loop.settle()
    assert await loop.status(first) == await loop.status(second) == "AWAITING"
    await loop.chat.inject("kate", "which one?")
    assert (loop.last_seen().text or "").startswith("You have more than one open question")

    second_prompt = loop.seen()[1]
    await loop.chat.inject("kate", "this one", reply_to=second_prompt.id)
    await loop.settle()
    assert await loop.status(second) == "COMPLETED"
    assert await loop.status(first) == "AWAITING"


async def test_direct_send_is_not_routed_as_prompt(loop: Loop) -> None:
    # A message Pester sent outside a delivery (a notice) is not a valid reply target.
    receipt = await loop.chat.send("kate", OutboundMessage(text="notice"))
    job_id = await loop.submit()
    await loop.settle()
    await loop.chat.inject("kate", "reply to the notice", reply_to=int(receipt.external_id))
    await loop.settle()
    assert await loop.status(job_id) == "COMPLETED"  # fell back to the single open question
