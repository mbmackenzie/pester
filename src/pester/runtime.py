"""Wires workers, channels, and the router together, and runs the background loops."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from pester.config import PesterConfig
from pester.core.clock import Clock
from pester.core.messages import InboundMessage
from pester.delivery.base import DeliveryChannel
from pester.delivery.router import ResponseRouter
from pester.delivery.worker import DeliveryWorker
from pester.evaluation.base import EvaluatorRegistry
from pester.evaluation.worker import EvaluationWorker
from pester.personality.registry import PersonalityRegistry
from pester.scheduler.worker import SchedulerWorker
from pester.storage.repository import Repository

log = logging.getLogger(__name__)

POLL_SECONDS = 5.0


@dataclass(frozen=True)
class WorkerHealth:
    running: bool
    last_ok: datetime | None
    last_error: str | None


class Runtime:
    def __init__(
        self,
        repo: Repository,
        config: PesterConfig,
        clock: Clock,
        channels: Sequence[DeliveryChannel],
        evaluators: EvaluatorRegistry,
        personalities: PersonalityRegistry,
    ) -> None:
        self._repo = repo
        self.channels: Mapping[str, DeliveryChannel] = {c.name: c for c in channels}
        self.evaluators = evaluators
        self.personalities = personalities
        self.router = ResponseRouter(repo, config, self.channels, clock)
        self.scheduler = SchedulerWorker(repo, config, self.channels, clock, personalities)
        self.delivery = DeliveryWorker(repo, self.channels, clock, config.delivery)
        self.evaluation = EvaluationWorker(repo, evaluators, personalities, clock)
        self._steps: dict[str, Callable[[], Awaitable[int]]] = {
            "scheduler": self.scheduler.run_once,
            "delivery": self.delivery.run_once,
            "evaluation": self.evaluation.run_once,
        }
        self._wake = {name: asyncio.Event() for name in self._steps}
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self._started: set[str] = set()
        self._clock = clock
        self._last_ok: dict[str, datetime] = {}
        self._last_error: dict[str, str] = {}

    async def recover(self) -> None:
        """Resolve work a crash left in flight. Everything else resumes from its persisted state."""
        if recovered := await self._repo.recover_interrupted_sends():
            log.warning("resolved %d delivery(ies) interrupted mid-send; they were not resent", recovered)

    async def start_channels(self) -> None:
        for name, channel in self.channels.items():
            try:
                await channel.start(self._on_inbound)
            except Exception:
                log.exception("channel %s failed to start", name, extra={"channel": name})
            else:
                self._started.add(name)

    async def stop_channels(self) -> None:
        for name in list(self._started):
            with contextlib.suppress(Exception):
                await self.channels[name].stop()
            self._started.discard(name)

    def start_workers(self) -> None:
        for name, step in self._steps.items():
            self._tasks.append(asyncio.create_task(self._loop(name, step), name=f"pester-{name}"))

    async def stop_workers(self, grace_seconds: float = 10.0) -> None:
        """Let each loop finish the step it is in (so an in-flight send is recorded), then stop.

        Loops still busy after the grace period are cancelled; any send they leave SENDING is resolved as
        ambiguous on the next start.
        """
        if not self._tasks:
            return
        self._stopping = True
        self.nudge()
        _, pending = await asyncio.wait(self._tasks, timeout=grace_seconds)
        for task in pending:
            log.warning("worker %s did not stop within %ss; cancelling", task.get_name(), grace_seconds)
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        self._stopping = False

    def health(self) -> dict[str, WorkerHealth]:
        running = {task.get_name().removeprefix("pester-"): not task.done() for task in self._tasks}
        return {
            name: WorkerHealth(
                running=running.get(name, False),
                last_ok=self._last_ok.get(name),
                last_error=self._last_error.get(name),
            )
            for name in self._steps
        }

    @property
    def started_channels(self) -> frozenset[str]:
        return frozenset(self._started)

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
        while not self._stopping:
            wake.clear()
            try:
                done = await step()
            except Exception as exc:
                log.exception("%s worker failed", name)
                self._last_error[name] = repr(exc)
                done = 0
            else:
                self._last_ok[name] = self._clock.now()
            if done:
                self.nudge()
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=POLL_SECONDS)
