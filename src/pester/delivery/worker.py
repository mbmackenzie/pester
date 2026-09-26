import logging
from collections.abc import Mapping

from pester.delivery.base import DeliveryChannel
from pester.storage.repository import Repository

log = logging.getLogger(__name__)


class DeliveryWorker:
    """Sends pending deliveries using the outbox pattern (spec §9.1).

    The delivery is committed as SENDING before the channel call, so a crash mid-send leaves evidence
    of a possibly-sent message instead of silently resending it.
    """

    def __init__(self, repo: Repository, channels: Mapping[str, DeliveryChannel]) -> None:
        self._repo = repo
        self._channels = channels

    async def run_once(self) -> int:
        """Send the oldest pending delivery. Returns the number of deliveries attempted (0 or 1)."""
        delivery = await self._repo.claim_next_delivery()
        if delivery is None:
            return 0
        channel = self._channels.get(delivery.channel)
        if channel is None:
            await self._repo.delivery_failed(delivery, f"channel {delivery.channel!r} is not enabled")
            return 1
        try:
            receipt = await channel.send(delivery.address, delivery.message)
        except Exception as exc:
            # Retry with backoff arrives in M5; for now a failed send fails the job.
            log.warning("sending delivery %s for %s failed: %r", delivery.pk, delivery.job.id, exc)
            await self._repo.delivery_failed(delivery, repr(exc))
            return 1
        await self._repo.delivery_sent(delivery, receipt.external_id, receipt.sent_at)
        return 1
