from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel

from pester.api.deps import ConfigDep, Principal, ReadPrincipal, RepoDep, RuntimeDep, SubmitPrincipal
from pester.config import PesterConfig
from pester.core.models import InteractionJob, JobRecord, UtcDatetime
from pester.core.states import JobStatus

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


def check_submission(
    config: PesterConfig, principal: Principal, job: InteractionJob, where: str = ""
) -> None:
    """Validate a job against deployment config. ``where`` prefixes messages, e.g. ``jobs[3]: ``."""
    if job.recipient_id not in principal.client.recipients:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"{where}client may not target recipient {job.recipient_id!r}"
        )
    recipient = config.recipients[job.recipient_id]
    if job.personality_id is not None and job.personality_id not in config.personalities:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{where}unknown personality {job.personality_id!r}"
        )
    if job.delivery.channel is not None and job.delivery.channel not in recipient.channels:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{where}recipient {job.recipient_id!r} has no {job.delivery.channel!r} channel",
        )


@router.post("", status_code=status.HTTP_201_CREATED)
async def submit_job(
    job: InteractionJob,
    principal: SubmitPrincipal,
    config: ConfigDep,
    repo: RepoDep,
    runtime: RuntimeDep,
    response: Response,
) -> JobAccepted:
    check_submission(config, principal, job)
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
