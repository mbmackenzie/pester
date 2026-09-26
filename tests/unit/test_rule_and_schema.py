from datetime import UTC, datetime

import pytest

from pester.core.models import HumanResponse, InteractionJob
from pester.evaluation.rule import RuleEvaluator
from pester.evaluation.schema import result_error, schema_error

JOB = InteractionJob.model_validate(
    {
        "recipient_id": "kate",
        "prompt": "Water?",
        "response_options": ["Yes", "No"],
        "evaluation": {"prompt": "x"},
    }
)


def reply(text: str, selected: str | None = None) -> HumanResponse:
    return HumanResponse(
        interaction_id="j",
        text=text,
        selected_option=selected,
        channel="fake",
        address="kate",
        external_id="1",
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("text", "selected", "choice"),
    [("Yes", "Yes", "Yes"), ("  no ", None, "No"), ("YES", None, "Yes"), ("maybe", None, None)],
)
async def test_rule_evaluator(text: str, selected: str | None, choice: str | None) -> None:
    outcome = await RuleEvaluator().evaluate(JOB, reply(text, selected))
    assert outcome.result == {"matched": choice is not None, "choice": choice}
    assert outcome.feedback_facts


def test_schema_error() -> None:
    assert schema_error({"type": "object"}) is None
    assert schema_error({"type": "nonsense"}) is not None


def test_result_error() -> None:
    schema = {"type": "object", "properties": {"score": {"type": "number"}}, "required": ["score"]}
    assert result_error(None, {"anything": 1}) is None
    assert result_error(schema, {"score": 1}) is None
    assert result_error(schema, {}) == "<root>: 'score' is a required property"
    assert result_error(schema, {"score": "high"}) == "score: 'high' is not of type 'number'"
