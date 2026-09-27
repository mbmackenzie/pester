"""Wires workers, channels, and the router together, and runs the background loops."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from pester.configstore import ConfigStore
from pester.core.clock import Clock
from pester.core.messages import InboundMessage
from pester.delivery.manager import ChannelManager
from pester.delivery.pairing import PairingDesk
from pester.delivery.router import ResponseRouter
from pester.delivery.worker import DeliveryWorker
from pester.evaluation.worker import EvaluationWorker
from pester.live import LiveConfig, Snapshot, SnapshotBuilder
from pester.pairing import PairingStore
from pester.scheduler.worker import SchedulerWorker
from pester.service import AdminService
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
        live: LiveConfig,
        clock: Clock,
        manager: ChannelManager,
        *,
        store: ConfigStore | None = None,
        builder: SnapshotBuilder | None = None,
        pairings: PairingStore | None = None,
        service: AdminService | None = None,
    ) -> None:
        self._repo = repo
        self.live = live
        self._store = store
        self._builder = builder
        self._failed_version = 0
        self._reload_lock = asyncio.Lock()  # the config step and the admin service both reload
        self.manager = manager
        self.channels = manager.channels  # updated in place as channels start and stop
        self.pairing = PairingDesk(pairings, service, manager, live) if pairings and service else None
        self.scheduler = SchedulerWorker(repo, live, self.channels, clock)
        self.router = ResponseRouter(
            repo,
            live,
            self.channels,
            clock,
            self.pairing.handle_unknown if self.pairing else None,
            send_now=self.scheduler.send_now,
        )
        self.delivery = DeliveryWorker(repo, self.channels, clock, live)
        self.evaluation = EvaluationWorker(repo, live, clock)
        self._steps: dict[str, Callable[[], Awaitable[int]]] = {
            "config": self._config_step,
            "scheduler": self.scheduler.run_once,
            "delivery": self.delivery.run_once,
            "evaluation": self.evaluation.run_once,
        }
        self._wake = {name: asyncio.Event() for name in self._steps}
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self._clock = clock
        self._last_ok: dict[str, datetime] = {}
        self._last_error: dict[str, str] = {}

    async def _config_step(self) -> int:
        done = await self.reload_config()
        if self.pairing is not None:
            done += await self.pairing.send_welcomes()
        return done

    async def reload_config(self) -> int:
        """Put the latest stored config into effect if it's newer. Returns 1 if it changed, else 0.

        Picks up changes from the admin UI and from the CLI running in another process. A version that
        can't be built (e.g. a personality whose import path no longer resolves) is logged once and skipped;
        the current config stays in effect.
        """
        if self._store is None or self._builder is None:
            return 0
        async with self._reload_lock:
            return await self._reload()

    async def _reload(self) -> int:
        assert self._store is not None and self._builder is not None
        version = await self._store.latest_version()
        if version <= self.live.current.version or version == self._failed_version:
            return 0
        stored = await self._store.latest()
        assert stored is not None
        try:
            snapshot = self._builder.build(stored.version, stored.config, await self._store.secrets())
        except Exception:
            log.exception(
                "config version %d can't be put into effect; keeping version %d",
                version,
                self.live.current.version,
            )
            self._failed_version = version
            return 0
        await self.live.publish(snapshot)
        log.info("config version %d is in effect (%s)", version, stored.comment)
        return 1

    async def recover(self) -> None:
        """Resolve work a crash left in flight. Everything else resumes from its persisted state."""
        if recovered := await self._repo.recover_interrupted_sends():
            log.warning("resolved %d delivery(ies) interrupted mid-send; they were not resent", recovered)

    async def start_channels(self) -> None:
        """Start every channel, and keep configured channels in step with config from now on."""
        await self.manager.start_injected(self._on_inbound)
        await self.manager.sync(self.live.current, self._on_inbound)
        self.live.add_listener(self._sync_channels)

    async def stop_channels(self) -> None:
        await self.manager.stop_all()

    async def restart_channel(self, name: str) -> None:
        await self.manager.restart(name, self.live.current, self._on_inbound)
        self.nudge()

    async def _sync_channels(self, snapshot: Snapshot) -> None:
        await self.manager.sync(snapshot, self._on_inbound)

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
        return self.manager.started

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
