"""Personality interface (spec §11).

Deployers register personalities in config; producers pick one per job with ``personality_id``. To add your
own, write a factory (a function or a class) taking ``(options, services)`` and returning an object with
``render_prompt`` and ``render_feedback``, then register it by import path::

    personalities:
      pirate:
        type: my_package.voices:PiratePersonality
        description: Answers everything like a pirate
        swagger: 11          # any other keys are passed to the factory as options

A personality changes how things are said, never what was concluded: it receives a read-only copy of the
evaluation and can only return text.
"""

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from pester.config import LLMConfig
from pester.core.models import HumanResponse, InteractionJob


class PersonalityConfigError(ValueError):
    """A personality's config is invalid. Raised at startup so misconfiguration never reaches a person."""


@dataclass(frozen=True)
class PromptContext:
    job: InteractionJob


@dataclass(frozen=True)
class FeedbackContext:
    job: InteractionJob
    response: HumanResponse
    feedback_facts: str
    result: Mapping[str, Any] = field(default_factory=dict[str, Any])

    def __post_init__(self) -> None:
        # A private, read-only copy: nothing a personality does can reach the stored evaluation.
        object.__setattr__(self, "result", MappingProxyType(copy.deepcopy(dict(self.result))))


class Personality(Protocol):
    async def render_prompt(self, ctx: PromptContext) -> str: ...

    async def render_feedback(self, ctx: FeedbackContext) -> str: ...


class BasePersonality:
    """Convenience base: prompts verbatim, feedback facts as-is. Override what you need."""

    async def render_prompt(self, ctx: PromptContext) -> str:
        return ctx.job.prompt

    async def render_feedback(self, ctx: FeedbackContext) -> str:
        return ctx.feedback_facts


@dataclass(frozen=True)
class PersonalityServices:
    """Shared resources handed to every personality factory."""

    llm_config: LLMConfig
    llm_client: AsyncOpenAI | None = None
    base_dir: Path = Path()  # relative paths in options resolve against the config file's directory


PersonalityFactory = Callable[[Mapping[str, Any], PersonalityServices], Personality]


def parse_options[T: BaseModel](model: type[T], options: Mapping[str, Any]) -> T:
    """Validate a personality's options with a pydantic model, as a PersonalityConfigError on failure."""
    try:
        return model.model_validate(dict(options))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'options'}: {err['msg']}" for err in exc.errors()
        )
        raise PersonalityConfigError(problems) from exc
