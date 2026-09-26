import logging
from collections.abc import Mapping
from datetime import timedelta

from pester.config import DeliveryConfig
from pester.core.clock import Clock
from pester.delivery.base import ChannelError, DeliveryChannel, PermanentChannelError
from pester.live import LiveConfig
from pester.storage.repository import PendingDelivery, Repository

log = logging.getLogger(__name__)


class DeliveryWorker:
    """Sends pending deliveries using the outbox pattern (spec §9.1).

    The delivery is committed as SENDING before the channel call, so a crash mid-send leaves evidence
    of a possibly-sent message instead of silently resending it.
    """

    def __init__(
        self,
        repo: Repository,
        channels: Mapping[str, DeliveryChannel],
        clock: Clock,
        live: LiveConfig,
    ) -> None:
        self._repo = repo
        self._channels = channels
        self._clock = clock
        self._live = live

    @property
    def _config(self) -> DeliveryConfig:
        return self._live.current.config.delivery

    async def run_once(self) -> int:
        """Send the oldest due delivery. Returns the number of deliveries attempted (0 or 1)."""
        delivery = await self._repo.claim_next_delivery()
        if delivery is None:
            return 0
        channel = self._channels.get(delivery.channel)
        if channel is None:
            await self._repo.delivery_failed(delivery, f"channel {delivery.channel!r} is not enabled")
            return 1
        try:
            receipt = await channel.send(delivery.address, delivery.message)
        except PermanentChannelError as exc:
            self._log_failure(delivery, exc, "failed permanently")
            await self._repo.delivery_failed(delivery, repr(exc))
        except ChannelError as exc:
            if delivery.attempts >= self._config.max_attempts:
                self._log_failure(delivery, exc, "failed; out of attempts")
                await self._repo.delivery_failed(delivery, repr(exc))
            else:
                retry_at = self._clock.now() + self.backoff(delivery.attempts)
                self._log_failure(delivery, exc, f"failed; retrying at {retry_at.isoformat()}")
                await self._repo.delivery_retry(delivery, repr(exc), retry_at)
        except Exception as exc:
            # We can't tell whether the person got it. Never risk a duplicate.
            self._log_failure(delivery, exc, "outcome unknown; not retrying")
            await self._repo.delivery_failed(delivery, repr(exc), reason="ambiguous_send")
        else:
            await self._repo.delivery_sent(delivery, receipt.external_id, receipt.sent_at)
        return 1

    def backoff(self, attempts: int) -> timedelta:
        seconds = self._config.backoff_seconds * 2 ** (attempts - 1)
        return timedelta(seconds=min(seconds, self._config.max_backoff_seconds))

    def _log_failure(self, delivery: PendingDelivery, exc: Exception, what: str) -> None:
        log.warning(
            "%s delivery %s (attempt %s) %s: %r",
            delivery.kind.lower(),
            delivery.pk,
            delivery.attempts,
            what,
            exc,
            extra={
                "interaction_id": delivery.job.id,
                "client_id": delivery.job.client_id,
                "recipient_id": delivery.job.spec.recipient_id,
                "channel": delivery.channel,
            },
        )
