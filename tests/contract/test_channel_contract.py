"""Behavior every DeliveryChannel must provide. Each channel joins the parametrization with a harness that
plays the person on the far side."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

import pytest

from pester.core.clock import FakeClock
from pester.core.messages import InboundMessage, OutboundMessage
from pester.delivery.base import DeliveryChannel
from pester.delivery.memory import InMemoryChannel
from pester.delivery.telegram import TelegramChannel
from tests.fake_telegram import FakeBotAPI


class Harness(Protocol):
    """Drives the far side of a channel: what the person sees and sends."""

    @property
    def channel(self) -> DeliveryChannel: ...

    @property
    def address(self) -> str: ...

    async def person_sends(self, text: str, reply_to: str | None = None) -> None: ...

    async def person_presses(self, external_id: str, index: int) -> None: ...

    def person_sees(self) -> list[str]: ...

    async def close(self) -> None: ...


@dataclass
class InMemoryHarness:
    channel: InMemoryChannel
    address: str = "kate"

    async def person_sends(self, text: str, reply_to: str | None = None) -> None:
        await self.channel.inject(self.address, text, reply_to=int(reply_to) if reply_to else None)

    async def person_presses(self, external_id: str, index: int) -> None:
        message = next(m for m in self.channel.sent(self.address) if str(m.id) == external_id)
        assert message.options is not None
        await self.channel.inject(self.address, selected_option=message.options[index], reply_to=message.id)

    def person_sees(self) -> list[str]:
        return [m.text or "" for m in self.channel.sent(self.address)]

    async def close(self) -> None:
        pass


@dataclass
class TelegramHarness:
    """The channel against a fake Bot API; the person's actions arrive through long polling."""

    channel: TelegramChannel
    api: FakeBotAPI
    chat_id: int = 424242

    @property
    def address(self) -> str:
        return str(self.chat_id)

    async def person_sends(self, text: str, reply_to: str | None = None) -> None:
        update = self.api.person_sends(self.chat_id, text, reply_to=int(reply_to) if reply_to else None)
        await self.api.acknowledged(update)

    async def person_presses(self, external_id: str, index: int) -> None:
        await self.api.acknowledged(self.api.person_presses(self.chat_id, int(external_id), index))

    def person_sees(self) -> list[str]:
        return self.api.texts(self.chat_id)

    async def close(self) -> None:
        pass


@pytest.fixture(params=["memory", "telegram"])
async def harness(request: pytest.FixtureRequest) -> AsyncIterator[Harness]:
    if request.param == "memory":
        yield InMemoryHarness(InMemoryChannel(FakeClock()))
        return
    api = FakeBotAPI()
    yield TelegramHarness(
        TelegramChannel("telegram", api.token, poll_seconds=1, transport=api.transport(), retry_seconds=0.01),
        api,
    )


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


async def test_buttons_select_an_option(harness: Harness, received: list[InboundMessage]) -> None:
    receipt = await harness.channel.send(
        harness.address, OutboundMessage(text="water?", options=["Yes", "No"])
    )
    await harness.person_presses(receipt.external_id, 1)
    message = received[-1]
    assert message.selected_option == "No"
    assert message.reply_to_external_id == receipt.external_id  # routes to the prompt's job
    assert message.sender_address == harness.address


def test_recipient_config_round_trips(harness: Harness) -> None:
    """Pairing stores recipient_config_for(address); it must reach the same address."""
    channel = harness.channel
    assert channel.address_of(channel.recipient_config_for(harness.address)) == harness.address
