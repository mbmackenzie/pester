import logging

from pester.core.clock import Clock
from pester.core.messages import OutboundMessage
from pester.evaluation.base import EvaluationFailedError
from pester.evaluation.pipeline import assess
from pester.live import LiveConfig
from pester.storage.repository import Repository

log = logging.getLogger(__name__)


class EvaluationWorker:
    def __init__(
        self,
        repo: Repository,
        live: LiveConfig,
        clock: Clock,
    ) -> None:
        self._repo = repo
        self._clock = clock
        self._live = live

    async def run_once(self) -> int:
        """Close elapsed debounce windows, then evaluate the oldest answered job. Returns work done."""
        closed = await self._repo.close_due_responses(self._clock.now())
        answered = await self._repo.next_answered()
        if answered is None:
            return closed
        job = answered.job.spec
        snapshot = self._live.current
        try:
            assessment = await assess(job, answered.response, snapshot.evaluators, snapshot.personalities)
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
            return closed + 1
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
        return closed + 1
