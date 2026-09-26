from collections.abc import Callable

from pester.core.models import HumanResponse, InteractionJob
from pester.evaluation.base import EvaluationOutcome

Script = Callable[[InteractionJob, HumanResponse], EvaluationOutcome]


def _always_correct(job: InteractionJob, response: HumanResponse) -> EvaluationOutcome:
    return EvaluationOutcome(result={"correct": True}, feedback_facts="Correct.")


class ScriptedEvaluator:
    """Test evaluator. Returns queued outcomes (or raises queued exceptions) in order.

    With nothing queued it falls back to ``default``, which by default grades every response correct.
    """

    name = "scripted"

    def __init__(self, default: Script | None = None) -> None:
        self._queue: list[EvaluationOutcome | Exception] = []
        self._default: Script = default or _always_correct
        self.calls: list[tuple[InteractionJob, HumanResponse]] = []

    def enqueue(self, *items: EvaluationOutcome | Exception) -> None:
        self._queue.extend(items)

    async def evaluate(self, job: InteractionJob, response: HumanResponse) -> EvaluationOutcome:
        self.calls.append((job, response))
        if not self._queue:
            return self._default(job, response)
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
