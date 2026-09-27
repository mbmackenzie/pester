"""Handles messages from unknown addresses (pairing requests, invite codes) and welcomes approved people."""

import logging

from pester.core.messages import InboundMessage, OutboundMessage
from pester.delivery.manager import ChannelManager
from pester.live import LiveConfig
from pester.pairing import PairingStore
from pester.service import AdminService

log = logging.getLogger(__name__)

ASKED = "Hi! I've asked the admin to approve you. You'll hear from me here once they do."
BAD_CODE = "That invite code isn't valid. It may have been used already or expired."
WELCOME = (
    "You're all set! I'll send you questions here. Reply to answer them.\n"
    "Commands: /send (a question now), /status, /skip, /snooze 2h, /pause, /resume"
)


class PairingDesk:
    def __init__(
        self, pairings: PairingStore, service: AdminService, manager: ChannelManager, live: LiveConfig
    ) -> None:
        self._pairings = pairings
        self._service = service
        self._manager = manager
        self._live = live

    async def handle_unknown(self, message: InboundMessage) -> None:
        """A message from an address no recipient uses."""
        config = self._manager.effective(self._live.current).get(message.channel)
        channel = self._manager.channels.get(message.channel)
        context = {"channel": message.channel}
        if config is None or channel is None or not config.accept_pairing:
            log.warning("dropping message from unknown sender %s", message.sender_address, extra=context)
            return
        recipient_config = channel.recipient_config_for(message.sender_address)
        command = message.command
        if command is not None and command.name == "start" and command.args:
            recipient_id = await self._service.redeem_invite(
                command.args, message.channel, message.sender_address, recipient_config
            )
            if recipient_id is None:
                await self._reply(message, BAD_CODE)
            else:
                log.info(
                    "paired %s by invite code", recipient_id, extra={**context, "recipient_id": recipient_id}
                )
            return  # the welcome is sent by send_welcomes, like any approval
        pairing, created = await self._pairings.request(
            message.channel, message.sender_address, recipient_config, message.text
        )
        if pairing is None:
            log.warning(
                "too many pending pairing requests; ignoring %s", message.sender_address, extra=context
            )
        elif created:
            log.info("pairing request from %s", message.sender_address, extra=context)
            await self._reply(message, ASKED)

    async def send_welcomes(self) -> int:
        """Welcome approved people once their recipient is in effect. Returns how many were welcomed."""
        done = 0
        for pairing in await self._pairings.unwelcomed():
            if pairing.recipient_id not in self._live.current.config.recipients:
                continue  # approved, but the config with them isn't in effect yet
            channel = self._manager.channels.get(pairing.channel)
            if channel is None or pairing.channel not in self._manager.started:
                continue
            try:
                await channel.send(pairing.address, OutboundMessage(text=WELCOME))
            except Exception as exc:
                log.warning(
                    "could not welcome %s: %r", pairing.recipient_id, exc, extra={"channel": pairing.channel}
                )
                continue
            await self._pairings.mark_welcomed(pairing.pk)
            done += 1
        return done

    async def _reply(self, message: InboundMessage, text: str) -> None:
        channel = self._manager.channels.get(message.channel)
        if channel is None:
            return
        try:
            await channel.send(
                message.sender_address, OutboundMessage(text=text, reply_to_external_id=message.external_id)
            )
        except Exception as exc:
            log.warning(
                "could not reply to %s: %r", message.sender_address, exc, extra={"channel": message.channel}
            )
