"""The full loop with the real evaluators (LLM against a fake endpoint, rule) and personalities."""

import json
from typing import Any

import pytest

from pester.evaluation.base import EvaluatorRegistry
from tests.e2e.conftest import Loop
from tests.llm_fakes import completion, error, evaluation

SCORE = {"type": "object", "properties": {"score": {"type": "number"}}, "required": ["score"]}


@pytest.fixture
def evaluator_registry() -> EvaluatorRegistry | None:
    return None  # use the app's real defaults


async def answer(loop: Loop, text: str, **job: Any) -> str:
    job_id = await loop.submit(**job)
    await loop.settle()
    await loop.chat.inject("kate", text, reply_to=loop.last_seen().id)
    await loop.settle()
    return job_id


async def test_llm_graded_and_goblin_voiced(loop: Loop) -> None:
    loop.llm.queue(
        evaluation({"score": 1}, "Correct: writers append to the WAL.", model="gpt-test"),
        completion("Hmph. Yes, the WAL. Don't let it go to your head."),
    )
    job_id = await answer(
        loop,
        "writers append to a separate WAL file",
        prompt="Why can readers proceed during a WAL write?",
        personality_id="weather-goblin",
        evaluation={"prompt": "Score 0 or 1.", "output_schema": SCORE},
    )

    assert loop.last_seen().text == "Hmph. Yes, the WAL. Don't let it go to your head."
    assert await loop.status(job_id) == "COMPLETED"
    grading, voicing = loop.llm.requests
    assert "Score 0 or 1." in grading["messages"][0]["content"]
    assert voicing["messages"][0]["content"].startswith("Be a goblin.")
    assert "Correct: writers append to the WAL." in voicing["messages"][1]["content"]

    evaluated = (await loop.events(job_id))[3]["payload"]
    assert evaluated["evaluation"]["result"] == {"score": 1}
    assert evaluated["evaluation"]["model"] == "gpt-test"
    assert evaluated["evaluation"]["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert evaluated["evaluation"]["attempts"] == 1
    assert evaluated["feedback"] == {
        "text": "Hmph. Yes, the WAL. Don't let it go to your head.",
        "personality": "weather-goblin",
        "personality_fallback": False,
    }

    (row,) = loop.audit()
    assert row["status"] == "SUCCESS"
    assert json.loads(row["request"]) == grading  # exactly what was sent
    assert json.loads(row["raw"])["model"] == "gpt-test"
    assert row["personality_id"] == "weather-goblin" and row["personality_fallback"] == 0
    assert row["feedback_facts"] == "Correct: writers append to the WAL."


async def test_llm_failure_fails_job_and_keeps_audit(loop: Loop) -> None:
    loop.llm.default = completion("I refuse to answer in JSON")
    job_id = await answer(loop, "some answer")

    events = await loop.events(job_id)
    assert events[-1]["type"] == "INTERACTION_FAILED"
    assert events[-1]["payload"]["reason"] == "evaluation_failed"
    assert events[-1]["payload"]["evaluator"] == "llm"
    assert events[-1]["payload"]["attempts"] == 3
    assert events[-2]["payload"]["response"]["text"] == "some answer"  # the answer is never lost

    (row,) = loop.audit()
    assert row["status"] == "FAILED" and row["attempts"] == 3
    assert len(json.loads(row["request"])["messages"]) == 6  # includes the correction turns
    assert row["raw"] is not None


async def test_personality_failure_still_delivers_neutral_feedback(loop: Loop) -> None:
    loop.llm.queue(evaluation({"score": 0}, "Not quite: readers use a snapshot."), error(500))
    job_id = await answer(loop, "no idea", personality_id="weather-goblin")
    assert loop.last_seen().text == "Not quite: readers use a snapshot."
    assert await loop.status(job_id) == "COMPLETED"
    assert (await loop.events(job_id))[3]["payload"]["feedback"]["personality_fallback"] is True


async def test_rule_evaluator_needs_no_llm(loop: Loop) -> None:
    job_id = await loop.submit(
        response_options=["Yes", "No"],
        evaluation={"evaluator": "rule", "prompt": "yes/no"},
        personality_id="houseplant",
    )
    await loop.settle()
    await loop.chat.inject("kate", selected_option="Yes", reply_to=loop.last_seen().id)
    await loop.settle()
    assert loop.last_seen().text == "🌿 Recorded: Yes."
    assert loop.llm.requests == []
    assert (await loop.events(job_id))[3]["payload"]["evaluation"]["result"] == {
        "matched": True,
        "choice": "Yes",
    }


async def test_prompt_rendered_by_personality_when_requested(loop: Loop) -> None:
    await loop.submit(
        id="voiced",
        personality_id="houseplant",
        response_options=["Yes", "No"],
        delivery={"prompt_rendering": "personality"},
    )
    await loop.submit(id="verbatim", personality_id="houseplant", delivery={"priority": 0.1})
    await loop.settle()
    prompt = loop.last_seen()
    assert (prompt.text, prompt.options) == ("🌱 Did you water the plants?", ["Yes", "No"])
