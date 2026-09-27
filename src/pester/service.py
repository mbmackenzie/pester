"""The admin service: every change to deployment config goes through here (UI, CLI, and pairing).

Each operation reads the latest stored config, edits a copy, validates it (as pydantic models and by
building everything from it, e.g. personalities), and saves it as a new version only if nobody else saved
in between. Messages in ``AdminError`` are written for the person making the change.
"""

import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import ValidationError

from pester.config import Permission, PesterConfig
from pester.configstore import ConfigConflictError, ConfigStore, StoredConfig
from pester.core.tokens import generate_token, hash_token
from pester.delivery.adapters import ChannelConfigError, resolve_adapter, secret_fields, secret_name
from pester.live import LLM_API_KEY
from pester.pairing import Invite, Pairing, PairingStatus, PairingStore

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
IDENTIFIER_HELP = "letters, digits, '.', '_', ':' or '-', starting with a letter or digit"
_UNSET: Any = object()  # "leave unchanged", distinct from None ("clear")


class AdminError(ValueError):
    """A change was refused. The message says why, for the person making it."""


Validator = Callable[[PesterConfig, Mapping[str, str]], None]
Edit = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class CreatedToken:
    client_id: str
    token: str  # shown once; only its hash is stored


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'config'}: {err['msg']}" for err in exc.errors()
    )


def parse_quiet_hours(value: str) -> dict[str, str]:
    """``22:00-09:00`` → ``{"start": "22:00", "end": "09:00"}``."""
    start, sep, end = value.partition("-")
    try:
        if not sep:
            raise ValueError
        return {
            "start": time.fromisoformat(start.strip()).isoformat("minutes"),
            "end": time.fromisoformat(end.strip()).isoformat("minutes"),
        }
    except ValueError as exc:
        raise AdminError(f"quiet hours {value!r} should look like 22:00-09:00") from exc


def check_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AdminError(f"unknown timezone {value!r} (use a name like America/New_York)") from exc
    return value


class AdminService:
    def __init__(
        self,
        store: ConfigStore,
        pairings: PairingStore,
        validate: Validator,
        on_saved: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self._store = store
        self._pairings = pairings
        self._validate = validate
        self._on_saved = on_saved  # the server reloads at once; the CLI relies on the server's watcher

    # ---- Core -----------------------------------------------------------------------------------------

    async def current(self) -> StoredConfig:
        latest = await self._store.latest()
        if latest is None:
            raise AdminError("there is no config yet; start Pester once, or run `pester import`")
        return latest

    async def update(
        self, comment: str, edit: Edit | None = None, secrets: Mapping[str, str | None] | None = None
    ) -> int:
        """Apply ``edit`` to a copy of the latest config, set or clear secrets, and save a new version."""
        for _ in range(3):
            latest = await self._store.latest()
            base = latest.config if latest else PesterConfig()
            data = base.model_dump(mode="json")
            if edit is not None:
                edit(data)
            try:
                config = PesterConfig.model_validate(data)
            except ValidationError as exc:
                raise AdminError(_validation_message(exc)) from exc
            merged = {**await self._store.secrets()}
            for name, value in (secrets or {}).items():
                if value is None:
                    merged.pop(name, None)
                else:
                    merged[name] = value
            try:
                self._validate(config, merged)
            except AdminError:
                raise
            except Exception as exc:
                raise AdminError(str(exc)) from exc
            try:
                version = await self._store.save(
                    config,
                    comment,
                    expected_version=latest.version if latest else 0,
                    secrets=dict(secrets or {}),
                )
            except ConfigConflictError:
                continue  # someone else saved first; redo the edit on top of theirs
            if self._on_saved is not None:
                await self._on_saved()
            return version
        raise AdminError("config kept changing while saving; try again")

    # ---- Clients --------------------------------------------------------------------------------------

    async def create_client(
        self, client_id: str, permissions: Iterable[str], recipients: Iterable[str] = ()
    ) -> CreatedToken:
        token = generate_token()

        def edit(data: dict[str, Any]) -> None:
            if client_id in data["clients"]:
                raise AdminError(f"client {client_id!r} already exists")
            data["clients"][client_id] = {
                "token_hash": hash_token(token),
                "permissions": _permissions(permissions),
                "recipients": sorted(set(recipients)),
            }

        _check_identifier("client", client_id)
        await self.update(f"create client {client_id}", edit)
        return CreatedToken(client_id, token)

    async def update_client(
        self,
        client_id: str,
        *,
        permissions: Iterable[str] | None = None,
        recipients: Iterable[str] | None = None,
    ) -> None:
        def edit(data: dict[str, Any]) -> None:
            client = _existing(data["clients"], "client", client_id)
            if permissions is not None:
                client["permissions"] = _permissions(permissions)
            if recipients is not None:
                client["recipients"] = sorted(set(recipients))

        await self.update(f"update client {client_id}", edit)

    async def rotate_client_token(self, client_id: str) -> CreatedToken:
        token = generate_token()

        def edit(data: dict[str, Any]) -> None:
            _existing(data["clients"], "client", client_id)["token_hash"] = hash_token(token)

        await self.update(f"new token for client {client_id}", edit)
        return CreatedToken(client_id, token)

    async def delete_client(self, client_id: str) -> None:
        def edit(data: dict[str, Any]) -> None:
            _existing(data["clients"], "client", client_id)
            del data["clients"][client_id]

        await self.update(f"revoke client {client_id}", edit)

    # ---- Recipients -----------------------------------------------------------------------------------

    async def create_recipient(
        self,
        recipient_id: str,
        *,
        timezone: str = "UTC",
        quiet_hours: str | None = None,
        channels: Mapping[str, Mapping[str, Any]] | None = None,
        clients: Iterable[str] = (),
    ) -> None:
        _check_identifier("recipient", recipient_id)
        check_timezone(timezone)
        quiet = parse_quiet_hours(quiet_hours) if quiet_hours else None

        def edit(data: dict[str, Any]) -> None:
            if recipient_id in data["recipients"]:
                raise AdminError(f"recipient {recipient_id!r} already exists")
            data["recipients"][recipient_id] = {
                "timezone": timezone,
                "quiet_hours": quiet,
                "channels": {k: dict(v) for k, v in (channels or {}).items()},
            }
            _allow(data, recipient_id, clients)

        await self.update(f"add recipient {recipient_id}", edit)

    async def update_recipient(
        self, recipient_id: str, *, timezone: str | None = None, quiet_hours: str | None = _UNSET
    ) -> None:
        """``quiet_hours``: ``"22:00-09:00"``, ``None`` to use the global setting, or omit to leave it."""
        if timezone is not None:
            check_timezone(timezone)
        quiet = _UNSET if quiet_hours is _UNSET else (parse_quiet_hours(quiet_hours) if quiet_hours else None)

        def edit(data: dict[str, Any]) -> None:
            recipient = _existing(data["recipients"], "recipient", recipient_id)
            if timezone is not None:
                recipient["timezone"] = timezone
            if quiet is not _UNSET:
                recipient["quiet_hours"] = quiet

        await self.update(f"update recipient {recipient_id}", edit)

    async def link_channel(
        self, recipient_id: str, channel: str, recipient_config: Mapping[str, Any]
    ) -> None:
        def edit(data: dict[str, Any]) -> None:
            _existing(data["recipients"], "recipient", recipient_id)["channels"][channel] = dict(
                recipient_config
            )

        await self.update(f"link {recipient_id} on {channel}", edit)

    async def unlink_channel(self, recipient_id: str, channel: str) -> None:
        def edit(data: dict[str, Any]) -> None:
            channels = _existing(data["recipients"], "recipient", recipient_id)["channels"]
            if channel not in channels:
                raise AdminError(f"recipient {recipient_id!r} has no {channel!r} channel")
            del channels[channel]

        await self.update(f"unlink {recipient_id} from {channel}", edit)

    async def delete_recipient(self, recipient_id: str) -> None:
        def edit(data: dict[str, Any]) -> None:
            _existing(data["recipients"], "recipient", recipient_id)
            del data["recipients"][recipient_id]
            for client in data["clients"].values():
                client["recipients"] = [r for r in client["recipients"] if r != recipient_id]

        await self.update(f"remove recipient {recipient_id}", edit)

    # ---- Channels -------------------------------------------------------------------------------------

    async def add_channel(
        self,
        name: str,
        channel_type: str,
        options: Mapping[str, Any] | None = None,
        *,
        description: str = "",
        accept_pairing: bool = True,
    ) -> None:
        options = dict(options or {})
        adapter_secrets = self._check_channel(name, channel_type, options)

        def edit(data: dict[str, Any]) -> None:
            if name in data["channels"]:
                raise AdminError(f"channel {name!r} already exists")
            data["channels"][name] = {
                "type": channel_type,
                "description": description,
                "accept_pairing": accept_pairing,
                **options,
            }

        await self.update(f"add channel {name}", edit, secrets=adapter_secrets)

    async def update_channel(
        self,
        name: str,
        *,
        options: Mapping[str, Any] | None = None,
        enabled: bool | None = None,
        description: str | None = None,
        accept_pairing: bool | None = None,
    ) -> None:
        latest = await self.current()
        existing = latest.config.channels.get(name)
        if existing is None:
            raise AdminError(f"there is no channel {name!r}")
        new_options = dict(options) if options is not None else existing.options
        adapter_secrets = self._check_channel(name, existing.type, new_options)

        def edit(data: dict[str, Any]) -> None:
            current = _existing(data["channels"], "channel", name)
            base = {
                k: current[k] for k in ("type", "enabled", "description", "accept_pairing") if k in current
            }
            if enabled is not None:
                base["enabled"] = enabled
            if description is not None:
                base["description"] = description
            if accept_pairing is not None:
                base["accept_pairing"] = accept_pairing
            data["channels"][name] = {**base, **new_options}

        await self.update(f"update channel {name}", edit, secrets=adapter_secrets)

    async def remove_channel(self, name: str) -> None:
        def edit(data: dict[str, Any]) -> None:
            _existing(data["channels"], "channel", name)
            del data["channels"][name]

        latest = await self.current()
        secrets = {}
        if (config := latest.config.channels.get(name)) is not None:
            try:
                fields = secret_fields(resolve_adapter(config.type).options_model)
            except ChannelConfigError:
                fields = []
            secrets = {secret_name(name, f.name): None for f in fields}
        await self.update(f"remove channel {name}", edit, secrets=secrets)

    async def set_channel_secret(self, name: str, field: str, value: str | None) -> None:
        latest = await self.current()
        config = latest.config.channels.get(name)
        if config is None:
            raise AdminError(f"there is no channel {name!r}")
        fields = {f.name for f in secret_fields(_adapter(config.type).options_model)}
        if field not in fields:
            raise AdminError(
                f"{field!r} isn't a secret of {config.type} channels (secrets: {sorted(fields) or 'none'})"
            )
        await self.update(
            f"{'set' if value else 'clear'} secret {field} of channel {name}",
            secrets={secret_name(name, field): value},
        )

    def _check_channel(self, name: str, channel_type: str, options: dict[str, Any]) -> dict[str, str | None]:
        """Validate a channel's options. Secret fields given with the options are moved out to secrets."""
        adapter = _adapter(channel_type)
        fields = secret_fields(adapter.options_model)
        secrets: dict[str, str | None] = {}
        for field in fields:
            if field.name in options:
                value = options.pop(field.name)
                secrets[secret_name(name, field.name)] = str(value) if value not in (None, "") else None
        placeholders = {f.name: "placeholder" for f in fields}  # a missing secret isn't a config error
        try:
            adapter.options_model.model_validate({**placeholders, **options})
        except ValidationError as exc:
            raise AdminError(f"channel {name!r}: {_validation_message(exc)}") from exc
        return secrets

    # ---- Personalities --------------------------------------------------------------------------------

    async def set_personality(
        self,
        personality_id: str,
        personality_type: str,
        options: Mapping[str, Any] | None = None,
        *,
        description: str = "",
    ) -> None:
        _check_identifier("personality", personality_id)

        def edit(data: dict[str, Any]) -> None:
            data["personalities"][personality_id] = {
                "type": personality_type,
                "description": description,
                **dict(options or {}),
            }

        await self.update(f"set personality {personality_id}", edit)

    async def delete_personality(self, personality_id: str) -> None:
        def edit(data: dict[str, Any]) -> None:
            _existing(data["personalities"], "personality", personality_id)
            if data["default_personality"] == personality_id:
                raise AdminError(
                    f"{personality_id!r} is the default personality; choose another default first"
                )
            del data["personalities"][personality_id]

        await self.update(f"remove personality {personality_id}", edit)

    async def set_default_personality(self, personality_id: str) -> None:
        def edit(data: dict[str, Any]) -> None:
            _existing(data["personalities"], "personality", personality_id)
            data["default_personality"] = personality_id

        await self.update(f"default personality {personality_id}", edit)

    # ---- Settings -------------------------------------------------------------------------------------

    async def update_settings(self, section: str, values: Mapping[str, Any]) -> None:
        """Change fields of ``scheduler``, ``llm``, or ``delivery``."""
        if section not in ("scheduler", "llm", "delivery"):
            raise AdminError(f"unknown settings section {section!r}")

        def edit(data: dict[str, Any]) -> None:
            data[section] = {**data[section], **values}

        await self.update(f"update {section} settings", edit)

    async def set_llm_key(self, value: str | None) -> None:
        await self.update("set LLM API key" if value else "clear LLM API key", secrets={LLM_API_KEY: value})

    # ---- Import / export ------------------------------------------------------------------------------

    async def import_yaml(self, text: str, source: str) -> int:
        """Replace the whole config. Secret channel options in the file are moved out to secrets."""
        try:
            raw: object = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise AdminError(f"{source} isn't valid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise AdminError(f"{source} should be a YAML mapping")
        try:
            config = PesterConfig.model_validate(raw)
        except ValidationError as exc:
            raise AdminError(_validation_message(exc)) from exc
        data = config.model_dump(mode="json")
        secrets: dict[str, str | None] = {}
        for name, channel in config.channels.items():
            options = channel.options
            secrets |= self._check_channel(name, channel.type, options)  # pops secret fields from options
            data["channels"][name] = {
                k: v for k, v in data["channels"][name].items() if k in options or k not in channel.options
            }

        def replace(current: dict[str, Any]) -> None:
            current.clear()
            current.update(data)

        return await self.update(f"imported from {source}", replace, secrets=secrets)

    async def export_yaml(self) -> str:
        """The current config as YAML. It never contains secrets: they aren't part of config."""
        latest = await self.current()
        data = json.loads(latest.config.model_dump_json())
        header = f"# Pester config, version {latest.version}. Secrets are not included.\n"
        return header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

    # ---- Pairing --------------------------------------------------------------------------------------

    async def approve_pairing(
        self,
        pk: int,
        recipient_id: str,
        *,
        timezone: str = "UTC",
        quiet_hours: str | None = None,
        clients: Iterable[str] = (),
    ) -> None:
        """Approve a request: link the address to ``recipient_id``, creating the recipient if it's new."""
        pairing = await self._pairings.get(pk)
        if pairing is None or pairing.status is not PairingStatus.PENDING:
            raise AdminError("that pairing request isn't pending")
        await self._link_or_create(
            pairing.channel, pairing.recipient_config, recipient_id, timezone, quiet_hours, clients
        )
        await self._pairings.record_approved(
            pairing.channel, pairing.address, pairing.recipient_config, recipient_id
        )

    async def reject_pairing(self, pk: int) -> None:
        if not await self._pairings.reject(pk):
            raise AdminError("that pairing request isn't pending")

    async def pending_pairings(self) -> list[Pairing]:
        return await self._pairings.requests(PairingStatus.PENDING)

    async def create_invite(
        self, recipient_id: str, *, timezone: str | None = None, valid_for: timedelta = timedelta(days=7)
    ) -> str:
        _check_identifier("recipient", recipient_id)
        if timezone is not None:
            check_timezone(timezone)
        return await self._pairings.create_invite(recipient_id, timezone, valid_for)

    async def invites(self) -> list[Invite]:
        return await self._pairings.invites()

    async def redeem_invite(
        self, code: str, channel: str, address: str, recipient_config: Mapping[str, Any]
    ) -> str | None:
        """Pair an address with an invite code. Returns the recipient id, or None for a bad code."""
        invite = await self._pairings.redeem(code, channel, address)
        if invite is None:
            return None
        await self._link_or_create(
            channel, recipient_config, invite.recipient_id, invite.timezone or "UTC", None, ()
        )
        await self._pairings.record_approved(channel, address, dict(recipient_config), invite.recipient_id)
        return invite.recipient_id

    async def _link_or_create(
        self,
        channel: str,
        recipient_config: Mapping[str, Any],
        recipient_id: str,
        timezone: str,
        quiet_hours: str | None,
        clients: Iterable[str],
    ) -> None:
        _check_identifier("recipient", recipient_id)
        check_timezone(timezone)
        quiet = parse_quiet_hours(quiet_hours) if quiet_hours else None

        def edit(data: dict[str, Any]) -> None:
            recipient: dict[str, Any] | None = data["recipients"].get(recipient_id)
            if recipient is None:
                recipient = {"timezone": timezone, "quiet_hours": quiet, "channels": {}}
                data["recipients"][recipient_id] = recipient
            recipient["channels"][channel] = dict(recipient_config)
            _allow(data, recipient_id, clients)

        await self.update(f"pair {recipient_id} on {channel}", edit)


# ---- Helpers --------------------------------------------------------------------------------------------


def _adapter(channel_type: str) -> Any:
    try:
        return resolve_adapter(channel_type)
    except ChannelConfigError as exc:
        raise AdminError(str(exc)) from exc


def _existing(section: dict[str, Any], kind: str, key: str) -> dict[str, Any]:
    if key not in section:
        raise AdminError(f"there is no {kind} {key!r}")
    value: dict[str, Any] = section[key]
    return value


def _check_identifier(kind: str, value: str) -> None:
    if not IDENTIFIER.fullmatch(value):  # the same rule as job and batch ids
        raise AdminError(f"{kind} id {value!r} must be {IDENTIFIER_HELP}")


def _permissions(values: Iterable[str]) -> list[str]:
    valid = {p.value for p in Permission}
    chosen = sorted(set(values))
    if unknown := [v for v in chosen if v not in valid]:
        raise AdminError(f"unknown permission(s) {unknown}; use {sorted(valid)}")
    return chosen


def _allow(data: dict[str, Any], recipient_id: str, clients: Iterable[str]) -> None:
    for client_id in clients:
        client = _existing(data["clients"], "client", client_id)
        client["recipients"] = sorted({*client["recipients"], recipient_id})
