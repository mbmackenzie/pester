"""Maps inbound messages to jobs (spec §9.2)."""

import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta

from pester.core.clock import Clock
from pester.core.messages import InboundMessage, OutboundMessage
from pester.delivery.base import DeliveryChannel
from pester.delivery.commands import CommandHandler
from pester.live import LiveConfig
from pester.scheduler.worker import SchedulerWorker
from pester.storage.repository import IngestOutcome, Repository

log = logging.getLogger(__name__)

NOTICES: dict[IngestOutcome, str] = {
    IngestOutcome.NOTHING_PENDING: "Nothing pending right now.",
    IngestOutcome.AMBIGUOUS: (
        "You have more than one open question. Reply directly to the one you're answering."
    ),
}


class ResponseRouter:
    def __init__(
        self,
        repo: Repository,
        live: LiveConfig,
        channels: Mapping[str, DeliveryChannel],
        clock: Clock,
        on_unknown: Callable[[InboundMessage], Awaitable[None]] | None = None,
        scheduler: SchedulerWorker | None = None,
    ) -> None:
        self._repo = repo
        self._live = live
        self._channels = channels
        self._commands = CommandHandler(repo, live, clock, scheduler)
        self._on_unknown = on_unknown

    def recipient_for(self, channel: str, address: str) -> str | None:
        """The recipient with this address on this channel. Recipients are the sender allowlist."""
        if (instance := self._channels.get(channel)) is None:
            return None
        for recipient_id, recipient in self._live.current.config.recipients.items():
            channel_config = recipient.channels.get(channel)
            if channel_config is None:
                continue
            try:
                if instance.address_of(channel_config) == address:
                    return recipient_id
            except ValueError as exc:  # one malformed entry mustn't stop routing for everyone
                log.warning("recipient %s: bad %s config: %s", recipient_id, channel, exc)
        return None

    async def handle(self, message: InboundMessage) -> IngestOutcome | None:
        """Route one inbound message. Returns the outcome, or None if the sender is unknown."""
        recipient_id = self.recipient_for(message.channel, message.sender_address)
        context = {"recipient_id": recipient_id, "channel": message.channel}
        if recipient_id is not None:
            kind = f"command /{message.command.name}" if message.command else "message"
            log.info("%s from %s", kind, recipient_id, extra=context)
        if recipient_id is None:
            if self._on_unknown is not None:
                await self._on_unknown(message)  # pairing
            else:
                log.warning(
                    "dropping message from unknown sender %s on %s", message.sender_address, message.channel
                )
            return None

        if message.command is not None:
            if not await self._repo.record_inbound(message, IngestOutcome.COMMAND):
                return IngestOutcome.DUPLICATE
            if (reply := await self._commands.handle(recipient_id, message)) is not None:
                await self._notify(message, reply)
            return IngestOutcome.COMMAND

        debounce = timedelta(seconds=self._live.current.config.scheduler.debounce_seconds)
        outcome = (await self._repo.ingest_response(recipient_id, message, debounce)).outcome
        if (notice := NOTICES.get(outcome)) is not None:
            await self._notify(message, notice)
        return outcome

    async def _notify(self, message: InboundMessage, text: str) -> None:
        channel = self._channels[message.channel]
        try:
            await channel.send(
                message.sender_address, OutboundMessage(text=text, reply_to_external_id=message.external_id)
            )
        except Exception as exc:
            log.warning("could not send notice to %s: %r", message.sender_address, exc)
