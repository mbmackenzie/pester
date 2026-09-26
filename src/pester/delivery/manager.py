"""Runs the configured channel instances and keeps them in step with config.

When config changes, only channels whose effective config changed are restarted; the rest keep running.
Workers hold ``channels``, a dict this manager updates in place, so they always see the current instances.
"""

import contextlib
import hashlib
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pester.config import ChannelConfig
from pester.delivery.adapters import (
    ChannelConfigError,
    ChannelServices,
    resolve_adapter,
    resolve_options,
    secret_fields,
    secret_name,
)
from pester.delivery.base import DeliveryChannel, InboundHandler
from pester.live import Snapshot

log = logging.getLogger(__name__)

DEV_MOCK = "fake"  # dev mode's implicit mock channel, kept under its historical name


@dataclass(frozen=True)
class ChannelStatus:
    name: str
    type: str
    running: bool
    error: str | None
    injected: bool  # supplied by code (tests), not config


@dataclass
class _Running:
    fingerprint: str
    channel: DeliveryChannel
    type: str


class ChannelManager:
    def __init__(
        self,
        services: ChannelServices,
        injected: Sequence[DeliveryChannel] = (),
        *,
        dev_mock: bool = False,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._services = services
        self._injected = {c.name: c for c in injected}
        self._dev_mock = dev_mock
        self._environ = os.environ if environ is None else environ
        self._running: dict[str, _Running] = {}
        self._errors: dict[str, str] = {}
        self._configured: dict[str, ChannelConfig] = {}
        self._started: set[str] = set()
        self.channels: dict[str, DeliveryChannel] = dict(self._injected)

    @property
    def started(self) -> frozenset[str]:
        return frozenset(self._started)

    def enabled(self) -> list[str]:
        """Every channel that should be running."""
        return sorted(set(self._injected) | set(self._configured))

    def status(self) -> list[ChannelStatus]:
        rows = [
            ChannelStatus(name, "injected", name in self._started, self._errors.get(name), injected=True)
            for name in self._injected
        ]
        rows += [
            ChannelStatus(name, config.type, name in self._started, self._errors.get(name), injected=False)
            for name, config in self._configured.items()
        ]
        return sorted(rows, key=lambda r: r.name)

    def effective(self, snapshot: Snapshot) -> dict[str, ChannelConfig]:
        """The enabled channel configs, plus dev mode's implicit mock channel."""
        configs = {name: c for name, c in snapshot.config.channels.items() if c.enabled}
        has_mock = any(c.type == "mock" for c in configs.values())
        if self._dev_mock and DEV_MOCK not in snapshot.config.channels and not has_mock:
            configs[DEV_MOCK] = ChannelConfig(type="mock", description="Dev mode's built-in mock channel")
        for name in set(configs) & set(self._injected):
            log.warning("channel %s is supplied by code; its config is ignored", name)
            del configs[name]
        return configs

    async def start_injected(self, on_inbound: InboundHandler) -> None:
        for name, channel in self._injected.items():
            await self._start(name, channel, on_inbound)

    async def sync(self, snapshot: Snapshot, on_inbound: InboundHandler) -> None:
        """Start, restart, and stop configured channels to match ``snapshot``."""
        wanted = self.effective(snapshot)
        self._configured = wanted
        for name in list(self._running):
            if name not in wanted:
                await self._stop(name)
                self.channels.pop(name, None)
                self._errors.pop(name, None)
                log.info("channel %s stopped (removed or disabled)", name, extra={"channel": name})
        for name, config in wanted.items():
            fingerprint = self._fingerprint(name, config, snapshot)
            current = self._running.get(name)
            if current is not None and current.fingerprint == fingerprint:
                continue
            await self._replace(name, config, snapshot, fingerprint, on_inbound)

    async def restart(self, name: str, snapshot: Snapshot, on_inbound: InboundHandler) -> None:
        if name in self._injected:
            channel = self._injected[name]
            await self._stop_channel(name, channel)
            await self._start(name, channel, on_inbound)
            return
        config = self.effective(snapshot).get(name)
        if config is None:
            raise KeyError(name)
        await self._replace(name, config, snapshot, self._fingerprint(name, config, snapshot), on_inbound)

    async def stop_all(self) -> None:
        for name in list(self._started):
            channel = self.channels.get(name)
            if channel is not None:
                await self._stop_channel(name, channel)

    async def _replace(
        self,
        name: str,
        config: ChannelConfig,
        snapshot: Snapshot,
        fingerprint: str,
        on_inbound: InboundHandler,
    ) -> None:
        # Stop the old instance first: two instances polling one provider account would fight over updates.
        await self._stop(name)
        try:
            adapter = resolve_adapter(config.type)
            options = resolve_options(name, config, adapter, snapshot.secrets, self._environ)
            channel = adapter.create(name, options, self._services)
        except ChannelConfigError as exc:
            self._fail(name, str(exc))
            self.channels.pop(name, None)
            self._running.pop(name, None)
            return
        except Exception as exc:
            log.exception("channel %s could not be created", name, extra={"channel": name})
            self._fail(name, repr(exc))
            self.channels.pop(name, None)
            self._running.pop(name, None)
            return
        self._running[name] = _Running(fingerprint, channel, config.type)
        self.channels[name] = channel  # kept even if it fails to start, so sends fail and are retried
        await self._start(name, channel, on_inbound)

    async def _start(self, name: str, channel: DeliveryChannel, on_inbound: InboundHandler) -> None:
        try:
            await channel.start(on_inbound)
        except Exception as exc:
            log.exception("channel %s failed to start", name, extra={"channel": name})
            self._fail(name, repr(exc))
            return
        self._errors.pop(name, None)
        self._started.add(name)
        log.info("channel %s started", name, extra={"channel": name})

    async def _stop(self, name: str) -> None:
        running = self._running.get(name)
        if running is not None:
            await self._stop_channel(name, running.channel)

    async def _stop_channel(self, name: str, channel: DeliveryChannel) -> None:
        if name in self._started:
            with contextlib.suppress(Exception):
                await channel.stop()
            self._started.discard(name)

    def _fail(self, name: str, error: str) -> None:
        self._errors[name] = error
        self._started.discard(name)
        log.warning("channel %s is not running: %s", name, error, extra={"channel": name})

    def _fingerprint(self, name: str, config: ChannelConfig, snapshot: Snapshot) -> str:
        """Changes whenever anything the channel is built from changes, including its secrets."""
        try:
            fields = secret_fields(resolve_adapter(config.type).options_model)
        except ChannelConfigError:
            fields = []
        secrets = {
            f.name: [
                self._environ.get(f.env) if f.env else None,
                snapshot.secrets.get(secret_name(name, f.name)),
            ]
            for f in fields
        }
        blob = json.dumps({"config": config.model_dump(mode="json"), "secrets": secrets}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()
