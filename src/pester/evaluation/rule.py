from pester.core.models import EvaluationOutcome, HumanResponse, InteractionJob


class RuleEvaluator:
    """Deterministic matching against ``response_options``. No LLM, no cost.

    A button press or a case-insensitive exact text match selects an option. Anything else is recorded as
    unmatched rather than failing, so the producer still sees what the person said.
    """

    name = "rule"

    async def evaluate(self, job: InteractionJob, response: HumanResponse) -> EvaluationOutcome:
        options = job.response_options or []
        answer = (response.selected_option or response.text).strip().casefold()
        choice = next((opt for opt in options if opt.strip().casefold() == answer), None)
        if choice is None:
            facts = f"That didn't match any of the options ({', '.join(options)}); recorded as-is."
            return EvaluationOutcome(result={"matched": False, "choice": None}, feedback_facts=facts)
        return EvaluationOutcome(
            result={"matched": True, "choice": choice}, feedback_facts=f"Recorded: {choice}."
        )
