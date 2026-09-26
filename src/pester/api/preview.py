"""``POST /api/v1/jobs:preview``: evaluate a sample reply and render feedback, storing nothing."""

from typing import Any, Literal, Self

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, model_validator

from pester.api.deps import PreviewPrincipal, StateDep
from pester.api.jobs import check_submission
from pester.core.ids import new_id
from pester.core.models import HumanResponse, InteractionJob
from pester.evaluation.base import EvaluationFailedError
from pester.evaluation.pipeline import assess
from pester.personality.base import PromptContext
from pester.state import AppState

router = APIRouter(tags=["jobs"])


class SampleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    selected_option: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.text and not self.selected_option:
            raise ValueError("text or selected_option is required")
        return self


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job: InteractionJob
    response: SampleResponse


class PreviewResult(BaseModel):
    status: Literal["SUCCESS", "FAILED"]
    prompt: str  # as it would be delivered
    personality: str
    evaluator: str | None = None
    result: dict[str, Any] | None = None
    feedback_facts: str | None = None
    feedback: str | None = None  # as it would be delivered
    personality_fallback: bool | None = None
    model: str | None = None
    usage: dict[str, int] | None = None
    latency_ms: int | None = None
    attempts: int | None = None
    error: str | None = None


@router.post("/api/v1/jobs:preview")
async def preview_job(body: PreviewRequest, principal: PreviewPrincipal, state: StateDep) -> PreviewResult:
    job = body.job.model_copy(update={"id": body.job.id or "preview"})
    check_submission(state, principal, job)
    return await run_preview(state, job, body.response)


async def run_preview(state: AppState, job: InteractionJob, sample: SampleResponse) -> PreviewResult:
    """Evaluate ``sample`` as a reply to ``job`` and voice the feedback, storing and sending nothing."""
    personality = state.personalities.resolve(job.personality_id)
    prompt = job.prompt
    if job.delivery.prompt_rendering == "personality":
        prompt = (await personality.prompt(PromptContext(job))).text

    response = HumanResponse(
        interaction_id=job.id or "preview",
        text=sample.text or sample.selected_option or "",
        selected_option=sample.selected_option,
        channel="preview",
        address="preview",
        external_id=new_id(),
        received_at=state.clock.now(),
    )
    try:
        assessment = await assess(job, response, state.evaluators, state.personalities)
    except EvaluationFailedError as exc:
        return PreviewResult(
            status="FAILED",
            prompt=prompt,
            personality=personality.id,
            evaluator=exc.evaluator,
            attempts=exc.attempts,
            error=exc.error,
        )
    outcome = assessment.outcome
    return PreviewResult(
        status="SUCCESS",
        prompt=prompt,
        personality=assessment.personality_id,
        evaluator=assessment.evaluator,
        result=outcome.result,
        feedback_facts=outcome.feedback_facts,
        feedback=assessment.feedback.text,
        personality_fallback=assessment.feedback.fallback,
        model=outcome.model,
        usage=outcome.usage,
        latency_ms=outcome.latency_ms,
        attempts=outcome.attempts,
    )
