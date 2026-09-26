"""Channel adapters: how a configured channel instance is built (docs/admin-ui.md, Channels).

An adapter declares a pydantic ``options_model``. Its ``SecretStr`` fields are secrets: they're stored apart
from config (as ``channel.<name>.<field>``) and never shown again after saving. A secret field may name an
environment variable that overrides the stored value, with ``Field(json_schema_extra={"env": "NAME"})``.

``type`` in config is a built-in adapter or an import path ``package.module:Adapter`` for your own.
"""

import importlib
import os
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from pydantic import BaseModel, SecretStr, ValidationError

from pester.config import ChannelConfig
from pester.core.clock import Clock
from pester.delivery.base import DeliveryChannel
from pester.storage.db import Database


class ChannelConfigError(Exception):
    """A channel's config can't be used. The channel doesn't start; its status shows this message."""


@dataclass(frozen=True)
class ChannelServices:
    """Shared resources handed to every adapter."""

    clock: Clock
    db: Database


class ChannelAdapter(Protocol):
    @property
    def description(self) -> str: ...

    @property
    def options_model(self) -> type[BaseModel]: ...

    def create(self, name: str, options: Any, services: ChannelServices) -> DeliveryChannel:
        """Build the channel. ``options`` is a validated instance of ``options_model``."""
        ...


@dataclass(frozen=True)
class SecretField:
    name: str
    env: str | None


def secret_name(channel: str, field: str) -> str:
    return f"channel.{channel}.{field}"


def secret_fields(model: type[BaseModel]) -> list[SecretField]:
    found: list[SecretField] = []
    for name, info in model.model_fields.items():
        annotation = info.annotation
        args = typing.get_args(annotation)
        if annotation is SecretStr or SecretStr in args:
            raw: object = info.json_schema_extra
            env = cast(dict[str, object], raw).get("env") if isinstance(raw, dict) else None
            found.append(SecretField(name, env if isinstance(env, str) else None))
    return found


def resolve_adapter(type_name: str) -> ChannelAdapter:
    from pester.delivery.mock import MockAdapter

    builtins: dict[str, ChannelAdapter] = {"mock": MockAdapter()}
    if type_name in builtins:
        return builtins[type_name]
    module_name, sep, attr = type_name.partition(":")
    if not sep or not module_name or not attr:
        raise ChannelConfigError(
            f"unknown channel type {type_name!r}: use one of {sorted(builtins)} "
            "or an import path 'package.module:Adapter'"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ChannelConfigError(f"cannot import {module_name!r}: {exc}") from exc
    adapter = getattr(module, attr, None)
    if isinstance(adapter, type):
        adapter = adapter()
    if adapter is None or not hasattr(adapter, "create") or not hasattr(adapter, "options_model"):
        raise ChannelConfigError(f"{type_name!r} is not a channel adapter")
    return adapter  # type: ignore[no-any-return]


def resolve_options(
    name: str,
    config: ChannelConfig,
    adapter: ChannelAdapter,
    secrets: Mapping[str, str],
    environ: Mapping[str, str] | None = None,
) -> BaseModel:
    """The adapter's options: config options plus secrets (environment first, then stored)."""
    environ = os.environ if environ is None else environ
    values: dict[str, Any] = dict(config.options)
    for field in secret_fields(adapter.options_model):
        if field.name in values:
            raise ChannelConfigError(f"{field.name} is a secret; set it as a secret, not in config")
        if field.env and environ.get(field.env):
            values[field.name] = environ[field.env]
        elif (stored := secrets.get(secret_name(name, field.name))) is not None:
            values[field.name] = stored
    try:
        return adapter.options_model.model_validate(values)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'options'}: {err['msg']}" for err in exc.errors()
        )
        raise ChannelConfigError(problems) from exc


def secret_source(
    name: str, field: SecretField, secrets: Mapping[str, str], environ: Mapping[str, str] | None = None
) -> str | None:
    """Where a secret's value comes from: "environment", "database", or None when it isn't set."""
    environ = os.environ if environ is None else environ
    if field.env and environ.get(field.env):
        return "environment"
    return "database" if secret_name(name, field.name) in secrets else None
