"""Delivery channel interface (spec §9). Core logic depends only on this."""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from pester.core.messages import InboundMessage, OutboundMessage, SentReceipt

InboundHandler = Callable[[InboundMessage], Awaitable[None]]


class ChannelError(Exception):
    """The message was definitely NOT delivered, and trying again later may work. Pester retries these.

    Channels must only raise this when they are sure nothing reached the person. Any other exception from
    ``send`` means the outcome is unknown, and Pester fails the delivery rather than risk a duplicate.
    """


class PermanentChannelError(ChannelError):
    """The message was not delivered and never will be (e.g. the person blocked the bot). Not retried."""


class DeliveryChannel(Protocol):
    @property
    def name(self) -> str: ...

    def address_of(self, recipient_config: Mapping[str, Any]) -> str:
        """The address for a recipient, from their ``recipients.<id>.channels.<name>`` config."""
        ...

    async def start(self, on_inbound: InboundHandler) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, address: str, message: OutboundMessage) -> SentReceipt: ...
