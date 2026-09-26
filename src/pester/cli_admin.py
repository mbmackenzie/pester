"""CLI for managing a deployment: clients, recipients, channels, pairing, settings, import/export.

Every command goes through ``AdminService`` against the database in ``PESTER_DATABASE_PATH``. It's safe to
run while the server is up (e.g. ``docker compose exec pester pester client list``): the server picks up
changes within a few seconds.
"""

import argparse
import asyncio
import getpass
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from pester.config import Permission, Settings
from pester.configstore import ConfigStore
from pester.core.clock import SystemClock
from pester.delivery.adapters import resolve_adapter, secret_fields, secret_source
from pester.live import LLM_API_KEY, SnapshotBuilder
from pester.pairing import PairingStore
from pester.service import AdminError, AdminService
from pester.storage.db import Database

DEFAULT_PERMISSIONS = [p.value for p in Permission]


@asynccontextmanager
async def _service() -> AsyncGenerator[tuple[AdminService, ConfigStore]]:
    settings = Settings()
    db = await Database.open(settings.database_path)
    clock = SystemClock()
    store = ConfigStore(db, clock)
    builder = SnapshotBuilder(settings, settings.config.parent if settings.config else Path.cwd())

    def validate(config: Any, secrets: Any) -> None:
        builder.build(0, config, secrets)

    try:
        yield AdminService(store, PairingStore(db, clock), validate), store
    finally:
        await db.close()


def _run(action: Callable[[AdminService, ConfigStore], Awaitable[None]]) -> None:
    async def main() -> None:
        async with _service() as (service, store):
            await action(service, store)

    try:
        asyncio.run(main())
    except AdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _pairs(items: list[str]) -> dict[str, Any]:
    """``key=value`` arguments; values are parsed as YAML scalars (numbers, true/false, null)."""
    result: dict[str, Any] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise SystemExit(f"error: expected key=value, got {item!r}")
        result[key] = yaml.safe_load(value) if value else ""
    return result


def _read_secret(what: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(f"{what}: ")
    return sys.stdin.readline().strip()


def _table(rows: list[list[str]], headers: list[str]) -> None:
    if not rows:
        print("(none)")
        return
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    for row in [headers, *rows]:
        print("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())


# ---- Config ---------------------------------------------------------------------------------------------


def _import(args: argparse.Namespace) -> None:
    text = Path(args.file).read_text()

    async def action(service: AdminService, store: ConfigStore) -> None:
        version = await service.import_yaml(text, args.file)
        print(f"Imported {args.file} as config version {version}.")

    _run(action)


def _export(args: argparse.Namespace) -> None:
    exported: list[str] = []

    async def action(service: AdminService, store: ConfigStore) -> None:
        exported.append(await service.export_yaml())

    _run(action)
    if args.output:
        Path(args.output).write_text(exported[0])
        print(f"Wrote {args.output}.")
    else:
        print(exported[0], end="")


def _history(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        rows = [
            [str(c.version), c.created_at.strftime("%Y-%m-%d %H:%M"), c.comment]
            for c in await store.history(args.limit)
        ]
        _table(rows, ["VERSION", "SAVED (UTC)", "CHANGE"])

    _run(action)


# ---- Clients --------------------------------------------------------------------------------------------


def _client_create(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        created = await service.create_client(
            args.id, args.permission or DEFAULT_PERMISSIONS, args.recipient or []
        )
        print(f"Created client {created.client_id}. Its token (shown only now):\n\n  {created.token}\n")

    _run(action)


def _client_list(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        config = (await service.current()).config
        rows = [
            [client_id, ", ".join(sorted(c.permissions)), ", ".join(sorted(c.recipients)) or "-"]
            for client_id, c in sorted(config.clients.items())
        ]
        _table(rows, ["CLIENT", "PERMISSIONS", "RECIPIENTS"])

    _run(action)


def _client_update(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        await service.update_client(args.id, permissions=args.permission, recipients=args.recipient)
        print(f"Updated client {args.id}.")

    _run(action)


def _client_rotate(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        created = await service.rotate_client_token(args.id)
        print(f"New token for {created.client_id} (the old one stops working now):\n\n  {created.token}\n")

    _run(action)


def _client_revoke(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        await service.delete_client(args.id)
        print(f"Revoked client {args.id}.")

    _run(action)


# ---- Recipients -----------------------------------------------------------------------------------------


def _recipient_add(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        await service.create_recipient(
            args.id, timezone=args.timezone, quiet_hours=args.quiet, clients=args.client or []
        )
        print(f"Added recipient {args.id}.")

    _run(action)


def _recipient_list(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        config = (await service.current()).config
        rows = [
            [
                recipient_id,
                r.timezone,
                f"{r.quiet_hours.start:%H:%M}-{r.quiet_hours.end:%H:%M}" if r.quiet_hours else "global",
                ", ".join(f"{name}: {cfg}" for name, cfg in r.channels.items()) or "-",
            ]
            for recipient_id, r in sorted(config.recipients.items())
        ]
        _table(rows, ["RECIPIENT", "TIMEZONE", "QUIET HOURS", "CHANNELS"])

    _run(action)


def _recipient_update(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        if args.no_quiet:
            await service.update_recipient(args.id, timezone=args.timezone, quiet_hours=None)
        elif args.quiet:
            await service.update_recipient(args.id, timezone=args.timezone, quiet_hours=args.quiet)
        else:
            await service.update_recipient(args.id, timezone=args.timezone)
        print(f"Updated recipient {args.id}.")

    _run(action)


def _recipient_link(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        await service.link_channel(args.id, args.channel, _pairs(args.settings))
        print(f"Linked {args.id} on {args.channel}.")

    _run(action)


def _recipient_unlink(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        await service.unlink_channel(args.id, args.channel)
        print(f"Unlinked {args.id} from {args.channel}.")

    _run(action)


def _recipient_remove(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        await service.delete_recipient(args.id)
        print(f"Removed recipient {args.id}.")

    _run(action)


# ---- Channels -------------------------------------------------------------------------------------------


def _channel_add(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        await service.add_channel(args.name, args.type, _pairs(args.options), description=args.description)
        print(f"Added channel {args.name} ({args.type}).")
        await _warn_missing_secrets(service, store, args.name)

    _run(action)


async def _warn_missing_secrets(service: AdminService, store: ConfigStore, name: str) -> None:
    config = (await service.current()).config.channels[name]
    secrets = await store.secrets()
    for field in secret_fields(resolve_adapter(config.type).options_model):
        if secret_source(name, field, secrets) is None:
            hint = f" (or set {field.env})" if field.env else ""
            print(f"It needs a secret: pester channel secret {name} {field.name}{hint}")


def _channel_list(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        config = (await service.current()).config
        secrets = await store.secrets()
        rows: list[list[str]] = []
        for name, c in sorted(config.channels.items()):
            try:
                fields = secret_fields(resolve_adapter(c.type).options_model)
            except Exception:
                fields = []
            secret_info = ", ".join(
                f"{f.name}: {secret_source(name, f, secrets) or 'NOT SET'}" for f in fields
            )
            rows.append(
                [
                    name,
                    c.type,
                    "yes" if c.enabled else "no",
                    "yes" if c.accept_pairing else "no",
                    ", ".join(f"{k}={v}" for k, v in c.options.items()) or "-",
                    secret_info or "-",
                ]
            )
        _table(rows, ["CHANNEL", "TYPE", "ENABLED", "PAIRING", "OPTIONS", "SECRETS"])

    _run(action)


def _channel_update(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        options = None
        if args.options:
            current = (await service.current()).config.channels.get(args.name)
            options = {**(current.options if current else {}), **_pairs(args.options)}
        await service.update_channel(
            args.name,
            options=options,
            enabled=args.enabled,
            accept_pairing=None if args.pairing is None else args.pairing == "on",
            description=args.description,
        )
        print(f"Updated channel {args.name}.")

    _run(action)


def _channel_remove(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        await service.remove_channel(args.name)
        print(f"Removed channel {args.name}.")

    _run(action)


def _channel_secret(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        value = None if args.clear else _read_secret(f"{args.name} {args.field}")
        if not args.clear and not value:
            raise AdminError("no value given")
        await service.set_channel_secret(args.name, args.field, value)
        print(f"{'Cleared' if args.clear else 'Set'} {args.field} for channel {args.name}.")

    _run(action)


# ---- Pairing and invites --------------------------------------------------------------------------------


def _pairing_list(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        rows = [
            [
                str(p.pk),
                p.channel,
                p.address,
                (p.first_text or "")[:40],
                p.created_at.strftime("%Y-%m-%d %H:%M"),
            ]
            for p in await service.pending_pairings()
        ]
        _table(rows, ["ID", "CHANNEL", "ADDRESS", "SAID", "WHEN (UTC)"])

    _run(action)


def _pairing_approve(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        await service.approve_pairing(
            args.pk, args.recipient, timezone=args.timezone, quiet_hours=args.quiet, clients=args.client or []
        )
        print(f"Approved: {args.recipient}. Pester will send them a welcome message.")

    _run(action)


def _pairing_reject(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        await service.reject_pairing(args.pk)
        print("Rejected. Further messages from that address are ignored.")

    _run(action)


def _invite_create(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        code = await service.create_invite(
            args.recipient, timezone=args.timezone, valid_for=timedelta(days=args.days)
        )
        print(
            f"Invite for {args.recipient}, valid for {args.days} days and usable once:\n\n  /start {code}\n\n"
            "They send that to Pester on any channel that accepts pairing."
        )

    _run(action)


def _invite_list(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        rows = [
            [
                i.recipient_id,
                i.created_at.strftime("%Y-%m-%d %H:%M"),
                i.expires_at.strftime("%Y-%m-%d %H:%M"),
                f"used on {i.used_channel} by {i.used_address}" if i.used_at else "unused",
            ]
            for i in await service.invites()
        ]
        _table(rows, ["RECIPIENT", "CREATED (UTC)", "EXPIRES (UTC)", "STATUS"])

    _run(action)


# ---- Settings, LLM, personalities -----------------------------------------------------------------------


def _settings_show(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        config = (await service.current()).config
        secrets = await store.secrets()
        data = {s: getattr(config, s).model_dump(mode="json") for s in ("scheduler", "llm", "delivery")}
        print(yaml.safe_dump(data, sort_keys=False), end="")
        key = (
            "environment"
            if Settings().openai_api_key
            else ("database" if LLM_API_KEY in secrets else "not set")
        )
        print(f"llm api key: {key}")

    _run(action)


def _settings_set(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        by_section: dict[str, dict[str, Any]] = {}
        for key, value in _pairs(args.values).items():
            section, sep, field = key.partition(".")
            if not sep:
                raise AdminError(
                    f"use section.field=value, e.g. scheduler.max_messages_per_day=6 (got {key!r})"
                )
            by_section.setdefault(section, {})[field] = value
        for section, values in by_section.items():
            await service.update_settings(section, values)
        print("Settings updated.")

    _run(action)


def _llm_key(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        value = None if args.clear else _read_secret("LLM API key")
        if not args.clear and not value:
            raise AdminError("no key given")
        await service.set_llm_key(value)
        print(
            "LLM API key cleared."
            if args.clear
            else "LLM API key saved. (OPENAI_API_KEY, if set, takes precedence.)"
        )

    _run(action)


def _personality_list(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        config = (await service.current()).config
        rows = [
            [pid + (" (default)" if pid == config.default_personality else ""), p.type, p.description]
            for pid, p in sorted(config.personalities.items())
        ]
        _table(rows, ["PERSONALITY", "TYPE", "DESCRIPTION"])

    _run(action)


def _personality_set(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        await service.set_personality(args.id, args.type, _pairs(args.options), description=args.description)
        print(f"Saved personality {args.id}.")

    _run(action)


def _personality_remove(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        await service.delete_personality(args.id)
        print(f"Removed personality {args.id}.")

    _run(action)


def _personality_default(args: argparse.Namespace) -> None:
    async def action(service: AdminService, store: ConfigStore) -> None:
        await service.set_default_personality(args.id)
        print(f"Default personality is now {args.id}.")

    _run(action)


# ---- Parser ---------------------------------------------------------------------------------------------


def register(sub: Any) -> None:
    """Add the management commands to the top-level ``pester`` subparsers."""
    p = sub.add_parser("import", help="replace the deployment config with a YAML file")
    p.add_argument("file")
    p.set_defaults(func=_import)

    p = sub.add_parser("export", help="print the deployment config as YAML (no secrets)")
    p.add_argument("-o", "--output")
    p.set_defaults(func=_export)

    p = sub.add_parser("history", help="list config versions")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=_history)

    client = sub.add_parser("client", help="producer clients and their tokens").add_subparsers(
        dest="client_command", required=True
    )
    p = client.add_parser("create", help="create a client and print its token (shown once)")
    p.add_argument("id")
    p.add_argument(
        "--permission", action="append", choices=DEFAULT_PERMISSIONS, help="repeatable (default: all)"
    )
    p.add_argument("--recipient", action="append", help="a recipient it may message (repeatable)")
    p.set_defaults(func=_client_create)
    client.add_parser("list").set_defaults(func=_client_list)
    p = client.add_parser("update", help="replace a client's permissions and/or recipients")
    p.add_argument("id")
    p.add_argument("--permission", action="append", choices=DEFAULT_PERMISSIONS)
    p.add_argument("--recipient", action="append")
    p.set_defaults(func=_client_update)
    p = client.add_parser("rotate", help="issue a new token; the old one stops working")
    p.add_argument("id")
    p.set_defaults(func=_client_rotate)
    p = client.add_parser("revoke", help="delete a client")
    p.add_argument("id")
    p.set_defaults(func=_client_revoke)

    recipient = sub.add_parser("recipient", help="people Pester messages").add_subparsers(
        dest="recipient_command", required=True
    )
    p = recipient.add_parser("add")
    p.add_argument("id")
    p.add_argument("--timezone", default="UTC")
    p.add_argument("--quiet", help="quiet hours override, e.g. 22:00-09:00")
    p.add_argument("--client", action="append", help="let this client message them (repeatable)")
    p.set_defaults(func=_recipient_add)
    recipient.add_parser("list").set_defaults(func=_recipient_list)
    p = recipient.add_parser("update")
    p.add_argument("id")
    p.add_argument("--timezone")
    p.add_argument("--quiet", help="quiet hours override, e.g. 22:00-09:00")
    p.add_argument("--no-quiet", action="store_true", help="use the global quiet hours")
    p.set_defaults(func=_recipient_update)
    p = recipient.add_parser("link", help="reach a recipient on a channel, e.g. link kate mock address=kate")
    p.add_argument("id")
    p.add_argument("channel")
    p.add_argument("settings", nargs="+", metavar="key=value")
    p.set_defaults(func=_recipient_link)
    p = recipient.add_parser("unlink")
    p.add_argument("id")
    p.add_argument("channel")
    p.set_defaults(func=_recipient_unlink)
    p = recipient.add_parser("remove")
    p.add_argument("id")
    p.set_defaults(func=_recipient_remove)

    channel = sub.add_parser("channel", help="delivery channels").add_subparsers(
        dest="channel_command", required=True
    )
    p = channel.add_parser("add", help="add a channel, e.g. add mock --type mock")
    p.add_argument("name")
    p.add_argument("--type", required=True, help="mock, or an import path package.module:Adapter")
    p.add_argument("--description", default="")
    p.add_argument("options", nargs="*", metavar="key=value")
    p.set_defaults(func=_channel_add)
    channel.add_parser("list").set_defaults(func=_channel_list)
    p = channel.add_parser("update")
    p.add_argument("name")
    p.add_argument("options", nargs="*", metavar="key=value")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--enable", dest="enabled", action="store_const", const=True)
    group.add_argument("--disable", dest="enabled", action="store_const", const=False)
    p.add_argument("--pairing", choices=["on", "off"], help="accept pairing requests from unknown addresses")
    p.add_argument("--description")
    p.set_defaults(func=_channel_update)
    p = channel.add_parser("remove")
    p.add_argument("name")
    p.set_defaults(func=_channel_remove)
    p = channel.add_parser("secret", help="set a channel secret (read from a prompt or stdin)")
    p.add_argument("name")
    p.add_argument("field")
    p.add_argument("--clear", action="store_true")
    p.set_defaults(func=_channel_secret)

    pairing = sub.add_parser("pairing", help="requests from unknown addresses").add_subparsers(
        dest="pairing_command", required=True
    )
    pairing.add_parser("list", help="pending requests").set_defaults(func=_pairing_list)
    p = pairing.add_parser("approve")
    p.add_argument("pk", type=int, metavar="id")
    p.add_argument("--as", dest="recipient", required=True, help="recipient id (new, or existing to link)")
    p.add_argument("--timezone", default="UTC")
    p.add_argument("--quiet", help="quiet hours override, e.g. 22:00-09:00")
    p.add_argument("--client", action="append", help="let this client message them (repeatable)")
    p.set_defaults(func=_pairing_approve)
    p = pairing.add_parser("reject")
    p.add_argument("pk", type=int, metavar="id")
    p.set_defaults(func=_pairing_reject)

    invite = sub.add_parser("invite", help="one-time pairing codes").add_subparsers(
        dest="invite_command", required=True
    )
    p = invite.add_parser("create")
    p.add_argument("recipient", help="recipient id the code pairs as (created if new)")
    p.add_argument("--timezone", help="for a new recipient (default UTC)")
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(func=_invite_create)
    invite.add_parser("list").set_defaults(func=_invite_list)

    settings = sub.add_parser("settings", help="pacing, LLM, and delivery settings").add_subparsers(
        dest="settings_command", required=True
    )
    settings.add_parser("show").set_defaults(func=_settings_show)
    p = settings.add_parser("set", help="e.g. set scheduler.max_messages_per_day=6 llm.model=gpt-5-mini")
    p.add_argument("values", nargs="+", metavar="section.field=value")
    p.set_defaults(func=_settings_set)

    llm = sub.add_parser("llm", help="LLM provider").add_subparsers(dest="llm_command", required=True)
    p = llm.add_parser("key", help="store the API key (read from a prompt or stdin)")
    p.add_argument("--clear", action="store_true")
    p.set_defaults(func=_llm_key)

    personality = sub.add_parser("personality", help="feedback personalities").add_subparsers(
        dest="personality_command", required=True
    )
    personality.add_parser("list").set_defaults(func=_personality_list)
    p = personality.add_parser("set", help="create or replace, e.g. set fern --type template feedback='...'")
    p.add_argument("id")
    p.add_argument("--type", required=True, help="neutral, template, llm, or package.module:factory")
    p.add_argument("--description", default="")
    p.add_argument("options", nargs="*", metavar="key=value")
    p.set_defaults(func=_personality_set)
    p = personality.add_parser("remove")
    p.add_argument("id")
    p.set_defaults(func=_personality_remove)
    p = personality.add_parser("default")
    p.add_argument("id")
    p.set_defaults(func=_personality_default)
