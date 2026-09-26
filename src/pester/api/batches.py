from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from pester.api.deps import ConfigDep, RepoDep, SubmitPrincipal
from pester.api.jobs import JobAccepted, check_submission
from pester.core.models import BatchSubmission

router = APIRouter(prefix="/api/v1/batches", tags=["batches"])


class BatchAccepted(BaseModel):
    batch_id: str
    jobs: list[JobAccepted]


@router.post("", status_code=status.HTTP_201_CREATED)
async def submit_batch(
    batch: BatchSubmission, principal: SubmitPrincipal, config: ConfigDep, repo: RepoDep, response: Response
) -> BatchAccepted:
    for index, job in enumerate(batch.jobs):
        check_submission(config, principal, job, where=f"jobs[{index}]: ")
    records, created = await repo.submit_batch(principal.client_id, batch)
    if not created:
        response.status_code = status.HTTP_200_OK
    return BatchAccepted(batch_id=batch.batch_id, jobs=[JobAccepted.of(r) for r in records])
