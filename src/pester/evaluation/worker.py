import logging

from pester.core.messages import OutboundMessage
from pester.evaluation.base import EvaluationFailedError, EvaluatorRegistry
from pester.evaluation.pipeline import assess
from pester.personality.registry import PersonalityRegistry
from pester.storage.repository import Repository

log = logging.getLogger(__name__)


class EvaluationWorker:
    def __init__(
        self, repo: Repository, evaluators: EvaluatorRegistry, personalities: PersonalityRegistry
    ) -> None:
        self._repo = repo
        self._evaluators = evaluators
        self._personalities = personalities

    async def run_once(self) -> int:
        """Evaluate the oldest answered job. Returns the number of jobs processed (0 or 1)."""
        answered = await self._repo.next_answered()
        if answered is None:
            return 0
        job = answered.job.spec
        try:
            assessment = await assess(job, answered.response, self._evaluators, self._personalities)
        except EvaluationFailedError as exc:
            log.warning("evaluation of %s failed: %s", answered.job.id, exc.error)
            await self._repo.evaluation_failed(
                answered,
                evaluator=exc.evaluator or job.evaluation.evaluator,
                error=exc.error,
                attempts=exc.attempts,
                request=exc.request,
                raw=exc.raw,
            )
            return 1
        feedback = OutboundMessage(
            text=assessment.feedback.text, reply_to_external_id=answered.response.external_id
        )
        await self._repo.evaluation_succeeded(
            answered,
            evaluator=assessment.evaluator,
            outcome=assessment.outcome,
            personality_id=assessment.personality_id,
            personality_fallback=assessment.feedback.fallback,
            feedback=feedback,
        )
        return 1
