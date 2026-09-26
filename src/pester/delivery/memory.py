"""In-memory channel: used directly by tests, and as the ``fake`` channel for local development."""

from collections import defaultdict
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from pester.core.clock import Clock
from pester.core.messages import InboundMessage, OutboundMessage, SentReceipt, parse_command
from pester.core.models import UtcDatetime
from pester.delivery.base import ChannelError, InboundHandler


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    direction: Literal["in", "out"]
    text: str | None
    options: list[str] | None = None
    selected_option: str | None = None
    reply_to: int | None = None
    at: UtcDatetime


class InMemoryChannel:
    """Holds one conversation per address. Message ids are sequential per conversation, like Telegram."""

    def __init__(self, clock: Clock, name: str = "fake") -> None:
        self._name = name
        self._clock = clock
        self._handler: InboundHandler | None = None
        self._chats: defaultdict[str, list[ChatMessage]] = defaultdict(list)
        self._send_failures: list[Exception] = []

    @property
    def name(self) -> str:
        return self._name

    def address_of(self, recipient_config: Mapping[str, Any]) -> str:
        address = recipient_config.get("address")
        if not isinstance(address, str) or not address:
            raise ValueError(f"{self._name} channel config requires a non-empty 'address'")
        return address

    async def start(self, on_inbound: InboundHandler) -> None:
        self._handler = on_inbound

    async def stop(self) -> None:
        self._handler = None

    async def send(self, address: str, message: OutboundMessage) -> SentReceipt:
        if self._send_failures:
            raise self._send_failures.pop(0)
        sent = self._append(
            address,
            direction="out",
            text=message.text,
            options=message.options,
            reply_to=int(message.reply_to_external_id) if message.reply_to_external_id else None,
        )
        return SentReceipt(external_id=str(sent.id), sent_at=sent.at)

    # ---- Test and dev helpers -----------------------------------------------------------------------

    async def inject(
        self,
        address: str,
        text: str | None = None,
        *,
        reply_to: int | None = None,
        selected_option: str | None = None,
    ) -> InboundMessage:
        """Simulate the person at ``address`` sending a message, and hand it to the inbound handler."""
        if self._handler is None:
            raise ChannelError(f"{self._name} channel is not started")
        if text is None and selected_option is None:
            raise ValueError("inject needs text or selected_option")
        chat = self._append(
            address, direction="in", text=text, selected_option=selected_option, reply_to=reply_to
        )
        inbound = self.inbound_for(address, chat)
        await self._handler(inbound)
        return inbound

    async def redeliver(self, message: InboundMessage) -> None:
        """Hand an already-delivered message to the handler again, as a flaky provider might."""
        if self._handler is None:
            raise ChannelError(f"{self._name} channel is not started")
        await self._handler(message)

    def inbound_for(self, address: str, chat: ChatMessage) -> InboundMessage:
        return InboundMessage(
            channel=self._name,
            external_id=str(chat.id),
            sender_address=address,
            text=chat.text,
            command=parse_command(chat.text) if chat.text else None,
            selected_option=chat.selected_option,
            reply_to_external_id=str(chat.reply_to) if chat.reply_to is not None else None,
            received_at=chat.at,
        )

    def fail_next_send(self, error: Exception | None = None) -> None:
        self._send_failures.append(error or ChannelError("simulated send failure"))

    def conversation(self, address: str, after: int = 0) -> list[ChatMessage]:
        return [m for m in self._chats[address] if m.id > after]

    def sent(self, address: str) -> list[ChatMessage]:
        return [m for m in self._chats[address] if m.direction == "out"]

    def _append(
        self,
        address: str,
        *,
        direction: Literal["in", "out"],
        text: str | None,
        options: list[str] | None = None,
        selected_option: str | None = None,
        reply_to: int | None = None,
    ) -> ChatMessage:
        chat = self._chats[address]
        message = ChatMessage(
            id=len(chat) + 1,
            direction=direction,
            text=text,
            options=options,
            selected_option=selected_option,
            reply_to=reply_to,
            at=self._clock.now(),
        )
        chat.append(message)
        return message
