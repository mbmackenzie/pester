"""Evaluate a response and render feedback. Shared by the evaluation worker and the preview endpoint."""

from dataclasses import dataclass

from pester.core.models import EvaluationOutcome, HumanResponse, InteractionJob
from pester.evaluation.base import EvaluationFailedError, EvaluatorRegistry
from pester.evaluation.schema import result_error
from pester.personality.base import FeedbackContext
from pester.personality.registry import PersonalityRegistry, Rendered


@dataclass(frozen=True)
class Assessment:
    evaluator: str
    outcome: EvaluationOutcome
    personality_id: str
    feedback: Rendered


async def assess(
    job: InteractionJob,
    response: HumanResponse,
    evaluators: EvaluatorRegistry,
    personalities: PersonalityRegistry,
) -> Assessment:
    """Raises EvaluationFailedError when no valid result could be produced."""
    requested = job.evaluation.evaluator
    evaluator = evaluators.get(requested)
    if evaluator is None:
        raise EvaluationFailedError(f"evaluator {requested!r} is not configured", evaluator=requested)
    try:
        outcome = await evaluator.evaluate(job, response)
    except EvaluationFailedError as exc:
        exc.evaluator = evaluator.name
        raise
    except Exception as exc:
        raise EvaluationFailedError(repr(exc), evaluator=evaluator.name) from exc

    # Every evaluator's result must honor the producer's schema, not just the LLM's.
    if error := result_error(job.evaluation.output_schema, outcome.result):
        raise EvaluationFailedError(
            f"result does not match output_schema: {error}",
            evaluator=evaluator.name,
            attempts=outcome.attempts,
            request=outcome.request,
            raw=outcome.raw,
        )

    personality = personalities.resolve(job.personality_id)
    feedback = await personality.feedback(
        FeedbackContext(job, response, outcome.feedback_facts, outcome.result)
    )
    return Assessment(evaluator.name, outcome, personality.id, feedback)
