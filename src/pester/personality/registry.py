"""Builds the deployment's personalities from config and renders through them safely."""

import importlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from pydantic import BaseModel

from pester.config import PesterConfig
from pester.personality.base import (
    FeedbackContext,
    Personality,
    PersonalityConfigError,
    PersonalityFactory,
    PersonalityServices,
    PromptContext,
)
from pester.personality.llm import LLMPersonalityOptions, llm
from pester.personality.neutral import NeutralOptions, neutral
from pester.personality.template import TemplateOptions, template

log = logging.getLogger(__name__)

BUILTINS: Mapping[str, PersonalityFactory] = {"neutral": neutral, "template": template, "llm": llm}
# Options models of the built-in types, for admin UI forms.
BUILTIN_OPTIONS: Mapping[str, type[BaseModel]] = {
    "neutral": NeutralOptions,
    "template": TemplateOptions,
    "llm": LLMPersonalityOptions,
}


@dataclass(frozen=True)
class Rendered:
    text: str
    fallback: bool  # True when the personality failed and the neutral text was used instead


@dataclass(frozen=True)
class RegisteredPersonality:
    id: str
    type: str
    description: str
    personality: Personality

    async def prompt(self, ctx: PromptContext) -> Rendered:
        return await self._render(ctx.job.prompt, lambda: self.personality.render_prompt(ctx), ctx.job.id)

    async def feedback(self, ctx: FeedbackContext) -> Rendered:
        return await self._render(
            ctx.feedback_facts, lambda: self.personality.render_feedback(ctx), ctx.job.id
        )

    async def _render(
        self, neutral_text: str, render: Callable[[], Awaitable[str]], job_id: str | None
    ) -> Rendered:
        # A broken personality must never block delivery: fall back to the neutral text.
        try:
            text = await render()
            if text.strip():
                return Rendered(text.strip(), fallback=False)
            log.warning("personality %s returned no text for %s; using neutral text", self.id, job_id)
        except Exception as exc:
            log.warning("personality %s failed for %s: %r; using neutral text", self.id, job_id, exc)
        return Rendered(neutral_text, fallback=True)


class PersonalityRegistry:
    def __init__(self, entries: Mapping[str, RegisteredPersonality], default_id: str) -> None:
        if default_id not in entries:
            raise PersonalityConfigError(f"default personality {default_id!r} is not registered")
        self._entries = dict(entries)
        self.default_id = default_id

    def __contains__(self, personality_id: object) -> bool:
        return personality_id in self._entries

    def resolve(self, personality_id: str | None) -> RegisteredPersonality:
        """The named personality. One removed since the job was submitted falls back to the default."""
        if personality_id is not None and personality_id not in self._entries:
            log.warning("personality %s is no longer registered; using %s", personality_id, self.default_id)
            personality_id = None
        return self._entries[personality_id or self.default_id]

    def all(self) -> list[RegisteredPersonality]:
        return [self._entries[k] for k in sorted(self._entries)]


def build_registry(config: PesterConfig, services: PersonalityServices) -> PersonalityRegistry:
    entries: dict[str, RegisteredPersonality] = {}
    for personality_id, entry in config.personalities.items():
        try:
            personality = resolve_factory(entry.type)(entry.options, services)
        except PersonalityConfigError as exc:
            raise PersonalityConfigError(f"personality {personality_id!r}: {exc}") from exc
        entries[personality_id] = RegisteredPersonality(
            personality_id, entry.type, entry.description, personality
        )
        if entry.type == "llm" and services.llm_client is None:
            log.warning(
                "personality %s uses an LLM but no LLM API key is set; it will fall back to neutral text",
                personality_id,
            )
    return PersonalityRegistry(entries, config.default_personality)


def resolve_factory(type_name: str) -> PersonalityFactory:
    if type_name in BUILTINS:
        return BUILTINS[type_name]
    module_name, sep, attr = type_name.partition(":")
    if not sep or not module_name or not attr:
        raise PersonalityConfigError(
            f"unknown type {type_name!r}: use one of {sorted(BUILTINS)} "
            "or an import path 'package.module:factory'"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise PersonalityConfigError(f"cannot import {module_name!r}: {exc}") from exc
    factory = getattr(module, attr, None)
    if not callable(factory):
        raise PersonalityConfigError(f"{type_name!r} is not a callable personality factory")
    return factory  # type: ignore[no-any-return]
