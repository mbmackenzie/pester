import logging

from pester.core.messages import OutboundMessage
from pester.evaluation.base import EvaluatorRegistry
from pester.storage.repository import Repository

log = logging.getLogger(__name__)


class EvaluationWorker:
    def __init__(self, repo: Repository, evaluators: EvaluatorRegistry) -> None:
        self._repo = repo
        self._evaluators = evaluators

    async def run_once(self) -> int:
        """Evaluate the oldest answered job. Returns the number of jobs processed (0 or 1)."""
        answered = await self._repo.next_answered()
        if answered is None:
            return 0
        evaluator = self._evaluators.get(answered.job.spec.evaluation.evaluator)
        try:
            outcome = await evaluator.evaluate(answered.job.spec, answered.response)
        except Exception as exc:
            log.warning("evaluation of %s failed: %r", answered.job.id, exc)
            await self._repo.evaluation_failed(answered, evaluator=evaluator.name, error=repr(exc))
            return 1
        # Personality rendering arrives in M3; until then feedback is the neutral facts verbatim.
        feedback = OutboundMessage(
            text=outcome.feedback_facts, reply_to_external_id=answered.response.external_id
        )
        await self._repo.evaluation_succeeded(
            answered,
            evaluator=evaluator.name,
            model=outcome.model,
            result=outcome.result,
            feedback_facts=outcome.feedback_facts,
            raw=outcome.raw,
            feedback=feedback,
        )
        return 1
