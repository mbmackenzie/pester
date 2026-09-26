"""Evaluator interface (spec §10)."""

from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from pester.core.models import HumanResponse, InteractionJob


class EvaluationOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    result: dict[str, Any]
    feedback_facts: str  # neutral statement of what the person should be told
    model: str | None = None
    raw: dict[str, Any] | None = Field(default=None, repr=False)


class Evaluator(Protocol):
    @property
    def name(self) -> str: ...

    async def evaluate(self, job: InteractionJob, response: HumanResponse) -> EvaluationOutcome: ...


class EvaluatorRegistry:
    """Maps ``evaluation.evaluator`` names to implementations, with a fallback for unregistered names."""

    def __init__(self, default: Evaluator, by_name: Mapping[str, Evaluator] | None = None) -> None:
        self._default = default
        self._by_name = dict(by_name or {})

    def get(self, name: str) -> Evaluator:
        return self._by_name.get(name, self._default)
