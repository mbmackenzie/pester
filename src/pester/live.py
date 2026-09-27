"""The config in effect, and everything built from it, swapped atomically when config changes.

A ``Snapshot`` is a validated config plus the objects built from it (LLM client, evaluators, personalities).
Workers read ``LiveConfig.current`` at the start of each step and use that one snapshot throughout, so a
change never lands halfway through a step.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from openai import AsyncOpenAI

from pester.config import PesterConfig, Settings
from pester.evaluation.base import Evaluator, EvaluatorRegistry
from pester.evaluation.echo import EchoEvaluator
from pester.evaluation.llm import LLMEvaluator
from pester.evaluation.rule import RuleEvaluator
from pester.llm import make_client
from pester.personality.base import PersonalityServices
from pester.personality.registry import PersonalityRegistry, build_registry

log = logging.getLogger(__name__)

LLM_API_KEY = "llm.api_key"


@dataclass(frozen=True)
class Snapshot:
    version: int
    config: PesterConfig
    secrets: Mapping[str, str] = field(repr=False)  # as stored; environment overrides are applied by users
    llm_client: AsyncOpenAI | None
    llm_key_source: str | None  # "environment", "database", "override", or None when there is no key
    evaluators: EvaluatorRegistry
    personalities: PersonalityRegistry


class SnapshotBuilder:
    """Builds snapshots. Raises (e.g. PersonalityConfigError) if a config can't be put into effect."""

    def __init__(
        self,
        settings: Settings,
        base_dir: Path,
        *,
        evaluators: EvaluatorRegistry | None = None,
        llm_client: AsyncOpenAI | None = None,
    ) -> None:
        self._settings = settings
        self._base_dir = base_dir
        self._evaluators = evaluators  # tests inject these
        self._llm_client = llm_client

    def build(self, version: int, config: PesterConfig, secrets: Mapping[str, str]) -> Snapshot:
        client, source = self._client(config, secrets)
        return Snapshot(
            version=version,
            config=config,
            secrets=dict(secrets),
            llm_client=client,
            llm_key_source=source,
            evaluators=self._evaluators or default_evaluators(self._settings, config, client),
            personalities=build_registry(config, PersonalityServices(config.llm, client, self._base_dir)),
        )

    def _client(
        self, config: PesterConfig, secrets: Mapping[str, str]
    ) -> tuple[AsyncOpenAI | None, str | None]:
        if self._llm_client is not None:
            return self._llm_client, "override"
        if self._settings.openai_api_key is not None:
            return make_client(self._settings.openai_api_key.get_secret_value(), config.llm), "environment"
        if key := secrets.get(LLM_API_KEY):
            return make_client(key, config.llm), "database"
        return None, None


def default_evaluators(
    settings: Settings, config: PesterConfig, llm_client: AsyncOpenAI | None
) -> EvaluatorRegistry:
    by_name: dict[str, Evaluator] = {"rule": RuleEvaluator()}
    if llm_client is not None:
        by_name["llm"] = LLMEvaluator(llm_client, config.llm)
    elif settings.dev_mode:
        log.warning("no LLM API key is set: dev mode answers 'llm' evaluations with the echo evaluator")
        by_name["llm"] = EchoEvaluator()
    if settings.dev_mode:
        by_name["echo"] = EchoEvaluator()
    return EvaluatorRegistry(by_name)


Listener = Callable[[Snapshot], Awaitable[None]]


class LiveConfig:
    def __init__(self, snapshot: Snapshot | None = None) -> None:
        self._current = snapshot
        self._listeners: list[Listener] = []

    @property
    def current(self) -> Snapshot:
        if self._current is None:
            raise RuntimeError("config has not been loaded yet")
        return self._current

    @property
    def loaded(self) -> bool:
        return self._current is not None

    def add_listener(self, listener: Listener) -> None:
        """Called after every swap, e.g. to start and stop channels."""
        self._listeners.append(listener)

    async def publish(self, snapshot: Snapshot) -> None:
        self._current = snapshot
        for listener in self._listeners:
            await listener(snapshot)
