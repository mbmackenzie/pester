from pester.core.models import HumanResponse, InteractionJob
from pester.evaluation.base import EvaluationOutcome


class EchoEvaluator:
    """Development evaluator: accepts any response and repeats it back. Needs no API key."""

    name = "echo"

    async def evaluate(self, job: InteractionJob, response: HumanResponse) -> EvaluationOutcome:
        return EvaluationOutcome(
            result={"echo": response.text},
            feedback_facts=f"Got it: {response.text}",
        )
