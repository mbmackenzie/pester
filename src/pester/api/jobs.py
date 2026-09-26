from typing import NoReturn

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel

from pester.api.deps import Principal, ReadPrincipal, RepoDep, RuntimeDep, StateDep, SubmitPrincipal
from pester.core.models import InteractionJob, JobRecord, UtcDatetime
from pester.core.states import JobStatus
from pester.evaluation.schema import schema_error
from pester.state import AppState

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


class JobAccepted(BaseModel):
    id: str
    batch_id: str | None
    status: JobStatus

    @classmethod
    def of(cls, record: JobRecord) -> "JobAccepted":
        return cls(id=record.id, batch_id=record.batch_id, status=record.status)


class JobView(BaseModel):
    id: str
    batch_id: str | None
    status: JobStatus
    created_at: UtcDatetime
    updated_at: UtcDatetime
    job: InteractionJob


def check_submission(state: AppState, principal: Principal, job: InteractionJob, where: str = "") -> None:
    """Validate a job against this deployment. ``where`` prefixes messages, e.g. ``jobs[3]: ``."""

    def reject(code: int, message: str) -> NoReturn:
        raise HTTPException(code, f"{where}{message}")

    if job.recipient_id not in principal.client.recipients:
        reject(status.HTTP_403_FORBIDDEN, f"client may not target recipient {job.recipient_id!r}")
    recipient = state.config.recipients[job.recipient_id]
    if job.personality_id is not None and job.personality_id not in state.personalities:
        reject(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown personality {job.personality_id!r}")
    if job.delivery.channel is not None and job.delivery.channel not in recipient.channels:
        reject(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"recipient {job.recipient_id!r} has no {job.delivery.channel!r} channel",
        )
    evaluation = job.evaluation
    if state.evaluators.get(evaluation.evaluator) is None:
        available = ", ".join(state.evaluators.names())
        reject(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"evaluator {evaluation.evaluator!r} is not configured on this server (available: {available})",
        )
    if evaluation.evaluator == "rule" and not job.response_options:
        reject(status.HTTP_422_UNPROCESSABLE_CONTENT, "the rule evaluator requires response_options")
    if evaluation.output_schema is not None and (error := schema_error(evaluation.output_schema)):
        reject(status.HTTP_422_UNPROCESSABLE_CONTENT, f"output_schema is not a valid JSON Schema: {error}")


@router.post("", status_code=status.HTTP_201_CREATED)
async def submit_job(
    job: InteractionJob,
    principal: SubmitPrincipal,
    state: StateDep,
    repo: RepoDep,
    runtime: RuntimeDep,
    response: Response,
) -> JobAccepted:
    check_submission(state, principal, job)
    record, created = await repo.submit_job(principal.client_id, job)
    runtime.nudge()
    if not created:
        response.status_code = status.HTTP_200_OK
    return JobAccepted.of(record)


@router.get("/{job_id}")
async def get_job(job_id: str, principal: ReadPrincipal, repo: RepoDep) -> JobView:
    record = await repo.get_job(principal.client_id, job_id)
    return JobView(
        id=record.id,
        batch_id=record.batch_id,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
        job=record.spec,
    )


@router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: str, principal: SubmitPrincipal, repo: RepoDep, runtime: RuntimeDep
) -> JobAccepted:
    record = await repo.cancel(principal.client_id, job_id)
    runtime.nudge()
    return JobAccepted.of(record)
