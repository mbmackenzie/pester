"""Delivery channel interface (spec §9). Core logic depends only on this."""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from pester.core.messages import InboundMessage, OutboundMessage, SentReceipt

InboundHandler = Callable[[InboundMessage], Awaitable[None]]


class ChannelError(Exception):
    """Raised by channels when a message could not be sent."""


class DeliveryChannel(Protocol):
    @property
    def name(self) -> str: ...

    def address_of(self, recipient_config: Mapping[str, Any]) -> str:
        """The address for a recipient, from their ``recipients.<id>.channels.<name>`` config."""
        ...

    async def start(self, on_inbound: InboundHandler) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, address: str, message: OutboundMessage) -> SentReceipt: ...
