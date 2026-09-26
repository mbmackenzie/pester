"""Deterministic personalities from Jinja templates, rendered in a sandbox."""

from collections.abc import Mapping
from typing import Any

from jinja2 import StrictUndefined, Template, TemplateError
from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, ConfigDict

from pester.personality.base import (
    FeedbackContext,
    PersonalityConfigError,
    PersonalityServices,
    PromptContext,
    parse_options,
)


class TemplateOptions(BaseModel):
    """Available variables: ``prompt``, ``response_options``, ``metadata``, ``recipient_id``; feedback
    templates also get ``feedback_facts``, ``result``, and ``response`` (the person's reply text)."""

    model_config = ConfigDict(extra="forbid")

    feedback: str = "{{ feedback_facts }}"
    prompt: str = "{{ prompt }}"


class TemplatePersonality:
    def __init__(self, feedback: str, prompt: str) -> None:
        env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
        try:
            self._feedback: Template = env.from_string(feedback)
            self._prompt: Template = env.from_string(prompt)
        except TemplateError as exc:
            raise PersonalityConfigError(f"invalid template: {exc}") from exc

    async def render_prompt(self, ctx: PromptContext) -> str:
        return self._prompt.render(_job_vars(ctx.job)).strip()

    async def render_feedback(self, ctx: FeedbackContext) -> str:
        return self._feedback.render(
            _job_vars(ctx.job),
            feedback_facts=ctx.feedback_facts,
            result=ctx.result,
            response=ctx.response.text,
        ).strip()


def _job_vars(job: Any) -> dict[str, Any]:
    return {
        "prompt": job.prompt,
        "response_options": job.response_options,
        "metadata": job.metadata,
        "recipient_id": job.recipient_id,
    }


def template(options: Mapping[str, Any], services: PersonalityServices) -> TemplatePersonality:
    opts = parse_options(TemplateOptions, options)
    return TemplatePersonality(feedback=opts.feedback, prompt=opts.prompt)
