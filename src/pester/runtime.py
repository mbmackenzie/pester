"""Wires workers, channels, and the router together, and runs the background loops."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence

from pester.config import PesterConfig
from pester.core.clock import Clock
from pester.core.messages import InboundMessage
from pester.delivery.base import DeliveryChannel
from pester.delivery.router import ResponseRouter
from pester.delivery.worker import DeliveryWorker
from pester.evaluation.base import EvaluatorRegistry
from pester.evaluation.worker import EvaluationWorker
from pester.scheduler.worker import SchedulerWorker
from pester.storage.repository import Repository

log = logging.getLogger(__name__)

POLL_SECONDS = 5.0


class Runtime:
    def __init__(
        self,
        repo: Repository,
        config: PesterConfig,
        clock: Clock,
        channels: Sequence[DeliveryChannel],
        evaluators: EvaluatorRegistry,
    ) -> None:
        self.channels: Mapping[str, DeliveryChannel] = {c.name: c for c in channels}
        self.router = ResponseRouter(repo, config, self.channels)
        self.scheduler = SchedulerWorker(repo, config, self.channels, clock)
        self.delivery = DeliveryWorker(repo, self.channels)
        self.evaluation = EvaluationWorker(repo, evaluators)
        self._steps: dict[str, Callable[[], Awaitable[int]]] = {
            "scheduler": self.scheduler.run_once,
            "delivery": self.delivery.run_once,
            "evaluation": self.evaluation.run_once,
        }
        self._wake = {name: asyncio.Event() for name in self._steps}
        self._tasks: list[asyncio.Task[None]] = []

    async def start_channels(self) -> None:
        for channel in self.channels.values():
            await channel.start(self._on_inbound)

    async def stop_channels(self) -> None:
        for channel in self.channels.values():
            await channel.stop()

    def start_workers(self) -> None:
        for name, step in self._steps.items():
            self._tasks.append(asyncio.create_task(self._loop(name, step), name=f"pester-{name}"))

    async def stop_workers(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    def nudge(self) -> None:
        """Wake every worker loop; called whenever something happened that may create work."""
        for event in self._wake.values():
            event.set()

    async def run_until_idle(self, max_rounds: int = 1000) -> None:
        """Run every worker until none has work. Tests use this instead of the background loops."""
        for _ in range(max_rounds):
            done = 0
            for step in self._steps.values():
                done += await step()
            if done == 0:
                return
        raise RuntimeError(f"workers still busy after {max_rounds} rounds")

    async def _on_inbound(self, message: InboundMessage) -> None:
        await self.router.handle(message)
        self.nudge()

    async def _loop(self, name: str, step: Callable[[], Awaitable[int]]) -> None:
        wake = self._wake[name]
        while True:
            wake.clear()
            try:
                done = await step()
            except Exception:
                log.exception("%s worker failed", name)
                done = 0
            if done:
                self.nudge()
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=POLL_SECONDS)
