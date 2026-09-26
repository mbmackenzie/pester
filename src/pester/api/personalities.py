from fastapi import APIRouter
from pydantic import BaseModel

from pester.api.deps import StateDep, SubmitPrincipal

router = APIRouter(prefix="/api/v1/personalities", tags=["personalities"])


class PersonalityInfo(BaseModel):
    id: str
    description: str


class PersonalityList(BaseModel):
    default: str
    personalities: list[PersonalityInfo]


@router.get("")
async def list_personalities(principal: SubmitPrincipal, state: StateDep) -> PersonalityList:
    """The personalities this deployment offers; pass one as a job's ``personality_id``."""
    registry = state.personalities
    return PersonalityList(
        default=registry.default_id,
        personalities=[PersonalityInfo(id=p.id, description=p.description) for p in registry.all()],
    )
