from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from pester.api.deps import ReadPrincipal, RepoDep
from pester.core.models import Event

router = APIRouter(prefix="/api/v1/events", tags=["events"])


class EventsPage(BaseModel):
    events: list[Event]
    next_cursor: int


@router.get("")
async def list_events(
    principal: ReadPrincipal,
    repo: RepoDep,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> EventsPage:
    events = await repo.list_events(principal.client_id, after, limit)
    return EventsPage(events=events, next_cursor=events[-1].cursor if events else after)
