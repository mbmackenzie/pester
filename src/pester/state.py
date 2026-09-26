from dataclasses import dataclass

from pester.config import PesterConfig, Settings
from pester.core.clock import Clock
from pester.evaluation.base import EvaluatorRegistry
from pester.live import LiveConfig
from pester.personality.registry import PersonalityRegistry


@dataclass(frozen=True)
class AppState:
    """Process-wide state. Config and what's built from it come from ``live``, which changes at runtime."""

    settings: Settings
    clock: Clock
    live: LiveConfig

    @property
    def config(self) -> PesterConfig:
        return self.live.current.config

    @property
    def evaluators(self) -> EvaluatorRegistry:
        return self.live.current.evaluators

    @property
    def personalities(self) -> PersonalityRegistry:
        return self.live.current.personalities
