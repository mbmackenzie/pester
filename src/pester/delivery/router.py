"""Maps inbound messages to jobs (spec §9.2)."""

import logging
from collections.abc import Mapping

from pester.config import PesterConfig
from pester.core.messages import InboundMessage, OutboundMessage
from pester.delivery.base import DeliveryChannel
from pester.storage.repository import IngestOutcome, Repository

log = logging.getLogger(__name__)

NOTICES: dict[IngestOutcome, str] = {
    IngestOutcome.NOTHING_PENDING: "Nothing pending right now.",
    IngestOutcome.AMBIGUOUS: (
        "You have more than one open question. Reply directly to the one you're answering."
    ),
    IngestOutcome.COMMAND: "Commands aren't supported yet.",
}


class ResponseRouter:
    def __init__(
        self,
        repo: Repository,
        config: PesterConfig,
        channels: Mapping[str, DeliveryChannel],
    ) -> None:
        self._repo = repo
        self._channels = channels
        # channel name -> address -> recipient id; this is also the sender allowlist.
        self._recipients: dict[str, dict[str, str]] = {name: {} for name in channels}
        for recipient_id, recipient in config.recipients.items():
            for name, channel_config in recipient.channels.items():
                if name in channels:
                    address = channels[name].address_of(channel_config)
                    self._recipients[name][address] = recipient_id

    def recipient_for(self, channel: str, address: str) -> str | None:
        return self._recipients.get(channel, {}).get(address)

    async def handle(self, message: InboundMessage) -> IngestOutcome | None:
        """Route one inbound message. Returns the outcome, or None if the sender is unknown."""
        recipient_id = self.recipient_for(message.channel, message.sender_address)
        if recipient_id is None:
            log.warning(
                "dropping message from unknown sender %s on %s", message.sender_address, message.channel
            )
            return None

        if message.command is not None:
            if not await self._repo.record_inbound(message, IngestOutcome.COMMAND):
                return IngestOutcome.DUPLICATE
            outcome = IngestOutcome.COMMAND
        else:
            outcome = (await self._repo.ingest_response(recipient_id, message)).outcome

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
