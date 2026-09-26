"""Behavior every DeliveryChannel must provide.

Telegram (M6) joins the parametrization with its own harness.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

import pytest

from pester.core.clock import FakeClock
from pester.core.messages import InboundMessage, OutboundMessage
from pester.delivery.base import DeliveryChannel
from pester.delivery.memory import InMemoryChannel


class Harness(Protocol):
    """Drives the far side of a channel: what the person sees and sends."""

    @property
    def channel(self) -> DeliveryChannel: ...

    @property
    def address(self) -> str: ...

    async def person_sends(self, text: str, reply_to: str | None = None) -> None: ...

    def person_sees(self) -> list[str]: ...


@dataclass
class InMemoryHarness:
    channel: InMemoryChannel
    address: str = "kate"

    async def person_sends(self, text: str, reply_to: str | None = None) -> None:
        await self.channel.inject(self.address, text, reply_to=int(reply_to) if reply_to else None)

    def person_sees(self) -> list[str]:
        return [m.text or "" for m in self.channel.sent(self.address)]


@pytest.fixture(params=["memory"])
async def harness(request: pytest.FixtureRequest) -> AsyncIterator[Harness]:
    assert request.param == "memory"
    yield InMemoryHarness(InMemoryChannel(FakeClock()))


@pytest.fixture
async def received(harness: Harness) -> AsyncIterator[list[InboundMessage]]:
    inbox: list[InboundMessage] = []

    async def handler(message: InboundMessage) -> None:
        inbox.append(message)

    await harness.channel.start(handler)
    yield inbox
    await harness.channel.stop()


async def test_send_reaches_person_with_unique_ids(harness: Harness, received: list[InboundMessage]) -> None:
    first = await harness.channel.send(harness.address, OutboundMessage(text="one"))
    second = await harness.channel.send(harness.address, OutboundMessage(text="two", options=["a", "b"]))
    assert first.external_id != second.external_id
    assert harness.person_sees() == ["one", "two"]
    assert first.sent_at.tzinfo is not None


async def test_inbound_carries_channel_sender_and_text(
    harness: Harness, received: list[InboundMessage]
) -> None:
    await harness.person_sends("hello")
    (message,) = received
    assert message.channel == harness.channel.name
    assert message.sender_address == harness.address
    assert message.text == "hello"
    assert message.command is None


async def test_reply_threading_round_trips(harness: Harness, received: list[InboundMessage]) -> None:
    receipt = await harness.channel.send(harness.address, OutboundMessage(text="question"))
    await harness.person_sends("answer", reply_to=receipt.external_id)
    assert received[-1].reply_to_external_id == receipt.external_id


async def test_commands_are_parsed(harness: Harness, received: list[InboundMessage]) -> None:
    await harness.person_sends("/snooze 2h")
    command = received[-1].command
    assert command is not None and (command.name, command.args) == ("snooze", "2h")


async def test_inbound_ids_unique_within_conversation(
    harness: Harness, received: list[InboundMessage]
) -> None:
    await harness.person_sends("a")
    await harness.person_sends("b")
    assert len({m.external_id for m in received}) == 2


def test_address_of_rejects_missing_config(harness: Harness) -> None:
    with pytest.raises(ValueError):
        harness.channel.address_of({})
