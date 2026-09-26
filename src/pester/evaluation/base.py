"""Evaluator interface (spec §10)."""

from collections.abc import Mapping
from typing import Any, Protocol

from pester.core.models import EvaluationOutcome, HumanResponse, InteractionJob

__all__ = ["EvaluationFailedError", "EvaluationOutcome", "Evaluator", "EvaluatorRegistry"]


class EvaluationFailedError(Exception):
    """An evaluator gave up. Carries whatever was attempted so the failure is auditable."""

    def __init__(
        self,
        error: str,
        *,
        evaluator: str | None = None,
        attempts: int = 1,
        request: dict[str, Any] | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(error)
        self.error = error
        self.evaluator = evaluator
        self.attempts = attempts
        self.request = request
        self.raw = raw


class Evaluator(Protocol):
    @property
    def name(self) -> str: ...

    async def evaluate(self, job: InteractionJob, response: HumanResponse) -> EvaluationOutcome: ...


class EvaluatorRegistry:
    """Maps ``evaluation.evaluator`` names to implementations. Unregistered names are rejected at submit."""

    def __init__(self, by_name: Mapping[str, Evaluator]) -> None:
        self._by_name = dict(by_name)

    def get(self, name: str) -> Evaluator | None:
        return self._by_name.get(name)

    def names(self) -> list[str]:
        return sorted(self._by_name)
