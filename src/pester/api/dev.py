"""Dev-only routes backing ``pester chat``. Mounted only when ``PESTER_DEV_MODE`` is on; unauthenticated."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, model_validator

from pester.api.deps import RuntimeDep
from pester.delivery.memory import ChatMessage, InMemoryChannel

router = APIRouter(prefix="/dev", tags=["dev"])


def get_fake_channel(runtime: RuntimeDep) -> InMemoryChannel:
    channel = runtime.channels.get("fake")
    if not isinstance(channel, InMemoryChannel):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "fake channel is not enabled")
    return channel


FakeChannelDep = Annotated[InMemoryChannel, Depends(get_fake_channel)]


class ChatInput(BaseModel):
    text: str | None = None
    reply_to: int | None = None
    selected_option: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "ChatInput":
        if self.text is None and self.selected_option is None:
            raise ValueError("text or selected_option is required")
        return self


@router.get("/chat/{address}")
async def read_chat(
    address: str, channel: FakeChannelDep, after: Annotated[int, Query(ge=0)] = 0
) -> list[ChatMessage]:
    return channel.conversation(address, after)


@router.post("/chat/{address}", status_code=status.HTTP_201_CREATED)
async def send_chat(address: str, body: ChatInput, channel: FakeChannelDep) -> ChatMessage:
    inbound = await channel.inject(
        address, body.text, reply_to=body.reply_to, selected_option=body.selected_option
    )
    return channel.conversation(address, int(inbound.external_id) - 1)[0]
