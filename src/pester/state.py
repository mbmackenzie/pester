from dataclasses import dataclass

from pester.config import PesterConfig, Settings
from pester.core.clock import Clock
from pester.evaluation.base import EvaluatorRegistry
from pester.personality.registry import PersonalityRegistry


@dataclass(frozen=True)
class AppState:
    """Everything built from settings and config at startup, before any request is served."""

    settings: Settings
    config: PesterConfig
    clock: Clock
    evaluators: EvaluatorRegistry
    personalities: PersonalityRegistry
