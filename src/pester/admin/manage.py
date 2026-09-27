"""Admin UI pages that change deployment config: clients, recipients and pairing, channels, personalities,
settings. Every change goes through ``AdminService``, like the CLI, and applies without a restart."""

import asyncio
import re
import time
import zoneinfo
from datetime import timedelta
from typing import Annotated, Any

from fastapi import Form, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ValidationError
from starlette.datastructures import FormData

from pester.admin.forms import (
    FormError,
    FormField,
    dump_mapping,
    fields_for,
    flash,
    parse_fields,
    parse_yaml_mapping,
)
from pester.admin.views import (
    COOKIE,
    Admin,
    AdminDep,
    AuthDep,
    CheckedAdminDep,
    app_state,
    redirect,
    render,
    repo_of,
    router,
    runtime_of,
    set_session_cookie,
)
from pester.api.preview import SampleResponse, run_preview
from pester.config import DeliveryConfig, LLMConfig, Permission, SchedulerConfig
from pester.configstore import ConfigStore
from pester.core.models import DeliverySpec, EvaluationSpec, InteractionJob
from pester.delivery.adapters import (
    ChannelAdapter,
    ChannelConfigError,
    builtin_adapters,
    resolve_adapter,
    secret_fields,
    secret_source,
)
from pester.delivery.commands import describe_next
from pester.evaluation.llm import LLMEvaluator
from pester.llm import chat, first_text, user
from pester.personality.registry import BUILTIN_OPTIONS, BUILTINS
from pester.service import AdminError, AdminService, CreatedToken, parse_quiet_hours

PERMISSIONS = [p.value for p in Permission]
TIMEZONES = sorted(zoneinfo.available_timezones())
NO_STORE = {"Cache-Control": "no-store"}  # pages that show a token or code once


def service_of(request: Request) -> AdminService:
    return request.app.state.service


def store_of(request: Request) -> ConfigStore:
    return request.app.state.config_store


def text(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key)
    return value.strip() if isinstance(value, str) else default


def many(form: FormData, key: str) -> list[str]:
    return [v for v in form.getlist(key) if isinstance(v, str) and v]


def checked(form: FormData, key: str) -> bool:
    return form.get(key) is not None


def done(request: Request, message: str, location: str) -> Response:
    return flash(redirect(request, location), message)


def _name_part(sender_name: str | None) -> str | None:
    """``Kate Smith (@kates)`` → ``kates``; ``Kate Smith`` → ``kate``."""
    if not sender_name:
        return None
    if match := re.search(r"@([A-Za-z0-9_]+)", sender_name):
        return match.group(1).lower()
    return sender_name.split()[0].lower()


def suggested_id(address: str) -> str:
    """A recipient id suggested for a new pairing, from the address."""
    cleaned = re.sub(r"[^A-Za-z0-9._:-]+", "-", address).strip("-._:")
    return cleaned[:40] or "recipient"


# ---- Clients --------------------------------------------------------------------------------------------


async def _clients_page(
    request: Request, admin: Admin, status_code: int = 200, error: str | None = None, form: Any = None
) -> Response:
    config = app_state(request).config
    return await render(
        request,
        "clients.html",
        admin,
        status_code,
        clients=sorted(config.clients.items()),
        recipients=sorted(config.recipients),
        permissions=PERMISSIONS,
        error=error,
        form=form or {"id": "", "permissions": PERMISSIONS, "recipients": []},
    )


@router.get("/clients")
async def clients(request: Request, admin: AdminDep) -> Response:
    return await _clients_page(request, admin)


@router.post("/clients")
async def create_client(request: Request, admin: CheckedAdminDep) -> Response:
    form = await request.form()
    client_id, permissions, allowed = text(form, "id"), many(form, "permissions"), many(form, "recipients")
    values = {"id": client_id, "permissions": permissions, "recipients": allowed}
    try:
        created = await service_of(request).create_client(client_id, permissions, allowed)
    except AdminError as exc:
        return await _clients_page(request, admin, 400, str(exc), values)
    return await _token_page(request, admin, created, new=True)


async def _token_page(request: Request, admin: Admin, created: CreatedToken, *, new: bool) -> Response:
    client = app_state(request).config.clients.get(created.client_id)
    recipient = min(client.recipients) if client and client.recipients else "RECIPIENT"
    response = await render(
        request,
        "client_token.html",
        admin,
        created=created,
        new=new,
        base_url=str(request.base_url).rstrip("/"),
        recipient=recipient,
    )
    response.headers.update(NO_STORE)
    return response


def _client_or_404(request: Request, client_id: str) -> Any:
    client = app_state(request).config.clients.get(client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such client")
    return client


@router.get("/clients/{client_id}")
async def edit_client_page(request: Request, admin: AdminDep, client_id: str) -> Response:
    client = _client_or_404(request, client_id)
    return await render(
        request,
        "client_edit.html",
        admin,
        client_id=client_id,
        client=client,
        permissions=PERMISSIONS,
        recipients=sorted(app_state(request).config.recipients),
    )


@router.post("/clients/{client_id}")
async def edit_client(request: Request, admin: CheckedAdminDep, client_id: str) -> Response:
    form = await request.form()
    try:
        await service_of(request).update_client(
            client_id, permissions=many(form, "permissions"), recipients=many(form, "recipients")
        )
    except AdminError as exc:
        return await render(
            request,
            "client_edit.html",
            admin,
            400,
            client_id=client_id,
            client=_client_or_404(request, client_id),
            permissions=PERMISSIONS,
            recipients=sorted(app_state(request).config.recipients),
            error=str(exc),
        )
    return done(request, f"Saved client {client_id}.", "/admin/clients")


@router.post("/clients/{client_id}/rotate")
async def rotate_client(request: Request, admin: CheckedAdminDep, client_id: str) -> Response:
    try:
        created = await service_of(request).rotate_client_token(client_id)
    except AdminError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return await _token_page(request, admin, created, new=False)


@router.post("/clients/{client_id}/revoke")
async def revoke_client(request: Request, admin: CheckedAdminDep, client_id: str) -> Response:
    try:
        await service_of(request).delete_client(client_id)
    except AdminError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return done(request, f"Revoked client {client_id}. Its token no longer works.", "/admin/clients")


# ---- Recipients, pairing, invites -----------------------------------------------------------------------


async def _recipients_page(
    request: Request, admin: Admin, status_code: int = 200, error: str | None = None, form: Any = None
) -> Response:
    state, repo, service = app_state(request), repo_of(request), service_of(request)
    scheduler, now = runtime_of(request).scheduler, state.clock.now()
    rows = [
        (recipient_id, recipient, await repo.recipient_status(recipient_id))
        for recipient_id, recipient in sorted(state.config.recipients.items())
    ]
    next_up = {
        recipient_id: describe_next(state.config, recipient_id, await scheduler.next_send(recipient_id), now)
        for recipient_id, _, status_ in rows
        if status_.queued
    }
    pending = await service.pending_pairings()
    return await render(
        request,
        "recipients.html",
        admin,
        status_code,
        recipients=rows,
        next_up=next_up,
        pending=[(p, suggested_id(_name_part(p.sender_name) or p.address)) for p in pending],
        invites=await service.invites(),
        clients=sorted(state.config.clients),
        timezones=TIMEZONES,
        error=error,
        form=form or {},
    )


@router.get("/recipients")
async def recipients(request: Request, admin: AdminDep) -> Response:
    return await _recipients_page(request, admin)


@router.post("/recipients")
async def add_recipient(request: Request, admin: CheckedAdminDep) -> Response:
    form = await request.form()
    values = {
        "id": text(form, "id"),
        "timezone": text(form, "timezone", "UTC") or "UTC",
        "quiet": text(form, "quiet"),
    }
    try:
        await service_of(request).create_recipient(
            values["id"],
            timezone=values["timezone"],
            quiet_hours=values["quiet"] or None,
            clients=many(form, "clients"),
        )
    except AdminError as exc:
        return await _recipients_page(request, admin, 400, str(exc), {"add": values})
    return done(
        request,
        f"Added {values['id']}. Link a channel so Pester can reach them.",
        f"/admin/recipients/{values['id']}",
    )


@router.post("/recipients/{recipient_id}/pause")
async def pause_recipient(request: Request, admin: CheckedAdminDep, recipient_id: str) -> Response:
    return await _set_paused(request, recipient_id, True)


@router.post("/recipients/{recipient_id}/resume")
async def resume_recipient(request: Request, admin: CheckedAdminDep, recipient_id: str) -> Response:
    return await _set_paused(request, recipient_id, False)


async def _set_paused(request: Request, recipient_id: str, paused: bool) -> Response:
    if recipient_id not in app_state(request).config.recipients:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    await repo_of(request).set_paused(recipient_id, paused)
    runtime_of(request).nudge()
    return done(request, f"{'Paused' if paused else 'Resumed'} {recipient_id}.", "/admin/recipients")


async def _recipient_page(
    request: Request, admin: Admin, recipient_id: str, status_code: int = 200, error: str | None = None
) -> Response:
    config = app_state(request).config
    recipient = config.recipients.get(recipient_id)
    if recipient is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such recipient")
    quiet = (
        f"{recipient.quiet_hours.start:%H:%M}-{recipient.quiet_hours.end:%H:%M}"
        if recipient.quiet_hours
        else ""
    )
    return await render(
        request,
        "recipient_edit.html",
        admin,
        status_code,
        recipient_id=recipient_id,
        recipient=recipient,
        quiet=quiet,
        links=[(name, dump_mapping(settings)) for name, settings in recipient.channels.items()],
        channel_names=sorted(set(config.channels) | set(runtime_of(request).channels)),
        allowed_clients=sorted(
            c for c, client in config.clients.items() if recipient_id in client.recipients
        ),
        timezones=TIMEZONES,
        error=error,
    )


@router.get("/recipients/{recipient_id}")
async def recipient_page(request: Request, admin: AdminDep, recipient_id: str) -> Response:
    return await _recipient_page(request, admin, recipient_id)


@router.post("/recipients/{recipient_id}")
async def edit_recipient(request: Request, admin: CheckedAdminDep, recipient_id: str) -> Response:
    form = await request.form()
    try:
        await service_of(request).update_recipient(
            recipient_id, timezone=text(form, "timezone") or None, quiet_hours=text(form, "quiet") or None
        )
    except AdminError as exc:
        return await _recipient_page(request, admin, recipient_id, 400, str(exc))
    return done(request, f"Saved {recipient_id}.", f"/admin/recipients/{recipient_id}")


@router.post("/recipients/{recipient_id}/link")
async def link_recipient(request: Request, admin: CheckedAdminDep, recipient_id: str) -> Response:
    form = await request.form()
    try:
        settings = parse_yaml_mapping(text(form, "settings"), "Settings")
        await service_of(request).link_channel(recipient_id, text(form, "channel"), settings)
    except (AdminError, FormError) as exc:
        return await _recipient_page(request, admin, recipient_id, 400, str(exc))
    return done(
        request, f"Linked {recipient_id} on {text(form, 'channel')}.", f"/admin/recipients/{recipient_id}"
    )


@router.post("/recipients/{recipient_id}/unlink")
async def unlink_recipient(request: Request, admin: CheckedAdminDep, recipient_id: str) -> Response:
    form = await request.form()
    try:
        await service_of(request).unlink_channel(recipient_id, text(form, "channel"))
    except AdminError as exc:
        return await _recipient_page(request, admin, recipient_id, 400, str(exc))
    return done(
        request, f"Unlinked {recipient_id} from {text(form, 'channel')}.", f"/admin/recipients/{recipient_id}"
    )


@router.post("/recipients/{recipient_id}/delete")
async def delete_recipient(request: Request, admin: CheckedAdminDep, recipient_id: str) -> Response:
    try:
        await service_of(request).delete_recipient(recipient_id)
    except AdminError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return done(request, f"Removed {recipient_id}.", "/admin/recipients")


@router.post("/pairings/{pk}/approve")
async def approve_pairing(request: Request, admin: CheckedAdminDep, pk: int) -> Response:
    form = await request.form()
    recipient_id = text(form, "recipient_id")
    try:
        await service_of(request).approve_pairing(
            pk,
            recipient_id,
            timezone=text(form, "timezone") or "UTC",
            quiet_hours=text(form, "quiet") or None,
            clients=many(form, "clients"),
        )
    except AdminError as exc:
        return await _recipients_page(request, admin, 400, str(exc), {"pairing": pk})
    runtime_of(request).nudge()  # send the welcome promptly
    return done(
        request, f"Approved {recipient_id}. Pester is sending them a welcome message.", "/admin/recipients"
    )


@router.post("/pairings/{pk}/reject")
async def reject_pairing(request: Request, admin: CheckedAdminDep, pk: int) -> Response:
    try:
        await service_of(request).reject_pairing(pk)
    except AdminError as exc:
        return await _recipients_page(request, admin, 400, str(exc))
    return done(request, "Rejected. Further messages from that address are ignored.", "/admin/recipients")


@router.post("/invites")
async def create_invite(request: Request, admin: CheckedAdminDep) -> Response:
    form = await request.form()
    recipient_id = text(form, "recipient_id")
    try:
        days = int(text(form, "days") or "7")
        code = await service_of(request).create_invite(
            recipient_id, timezone=text(form, "timezone") or None, valid_for=timedelta(days=days)
        )
    except (AdminError, ValueError) as exc:
        return await _recipients_page(request, admin, 400, str(exc), {"invite": dict(form)})
    response = await render(
        request,
        "invite_code.html",
        admin,
        code=code,
        recipient_id=recipient_id,
        days=days,
        links=runtime_of(request).manager.invite_links(code),
    )
    response.headers.update(NO_STORE)
    return response


# ---- Channels -------------------------------------------------------------------------------------------


@router.get("/channels")
async def channels(request: Request, admin: AdminDep) -> Response:
    state, runtime = app_state(request), runtime_of(request)
    statuses = {s.name: s for s in runtime.manager.status()}
    names = sorted(set(state.config.channels) | set(statuses))
    rows = [
        (
            name,
            state.config.channels.get(name),
            statuses.get(name),
            [r for r, recipient in sorted(state.config.recipients.items()) if name in recipient.channels],
        )
        for name in names
    ]
    adapters = sorted((name, adapter.description) for name, adapter in builtin_adapters().items())
    return await render(request, "channels.html", admin, channels=rows, adapters=adapters)


def _channel_fields(
    request: Request, name: str | None, channel_type: str, options: dict[str, Any]
) -> tuple[ChannelAdapter, list[FormField]]:
    adapter = resolve_adapter(channel_type)
    secrets = app_state(request).live.current.secrets
    fields = secret_fields(adapter.options_model)
    status_by_field = {f.name: secret_source(name, f, secrets) for f in fields} if name else {}
    return adapter, fields_for(adapter.options_model, options, status_by_field)


async def _channel_form(
    request: Request,
    admin: Admin,
    *,
    name: str | None,
    channel_type: str,
    values: dict[str, Any],
    status_code: int = 200,
    error: str | None = None,
) -> Response:
    try:
        adapter, fields = _channel_fields(request, name, channel_type, values.get("options", {}))
    except ChannelConfigError as exc:
        return done(request, str(exc), "/admin/channels")
    return await render(
        request,
        "channel_form.html",
        admin,
        status_code,
        channel_name=name,
        channel_type=channel_type,
        adapter=adapter,
        fields=fields,
        values=values,
        error=error,
    )


@router.get("/channels/new")
async def new_channel_page(request: Request, admin: AdminDep, type: str = "mock") -> Response:
    return await _channel_form(
        request, admin, name=None, channel_type=type, values={"name": type.rsplit(":", 1)[-1].lower()[:32]}
    )


@router.post("/channels")
async def add_channel(request: Request, admin: CheckedAdminDep) -> Response:
    form = await request.form()
    channel_type, name = text(form, "type"), text(form, "name")
    values: dict[str, Any] = {"name": name, "description": text(form, "description")}
    try:
        _, fields = _channel_fields(request, None, channel_type, {})
        parsed = parse_fields(fields, form)
        values["options"] = parsed.options
        await service_of(request).add_channel(
            name,
            channel_type,
            {**parsed.options, **parsed.secrets},  # the service moves secret fields out to secrets
            description=values["description"],
            accept_pairing=checked(form, "accept_pairing"),
        )
    except (AdminError, FormError, ChannelConfigError) as exc:
        return await _channel_form(
            request,
            admin,
            name=None,
            channel_type=channel_type,
            values=values,
            status_code=400,
            error=str(exc),
        )
    return done(request, f"Added channel {name}.", "/admin/channels")


@router.get("/channels/{name}")
async def edit_channel_page(request: Request, admin: AdminDep, name: str) -> Response:
    config = app_state(request).config.channels.get(name)
    if config is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "that channel isn't configured (it may be supplied by code)"
        )
    values = {
        "name": name,
        "description": config.description,
        "enabled": config.enabled,
        "accept_pairing": config.accept_pairing,
        "options": config.options,
    }
    return await _channel_form(request, admin, name=name, channel_type=config.type, values=values)


@router.post("/channels/{name}")
async def edit_channel(request: Request, admin: CheckedAdminDep, name: str) -> Response:
    config = app_state(request).config.channels.get(name)
    if config is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    form = await request.form()
    values: dict[str, Any] = {
        "name": name,
        "description": text(form, "description"),
        "enabled": checked(form, "enabled"),
        "accept_pairing": checked(form, "accept_pairing"),
    }
    try:
        _, fields = _channel_fields(request, name, config.type, config.options)
        parsed = parse_fields(fields, form)
        values["options"] = parsed.options
        await service_of(request).update_channel(
            name,
            options={**parsed.options, **parsed.secrets},  # secrets left blank are unchanged
            enabled=values["enabled"],
            description=values["description"],
            accept_pairing=values["accept_pairing"],
        )
    except (AdminError, FormError) as exc:
        return await _channel_form(
            request,
            admin,
            name=name,
            channel_type=config.type,
            values=values,
            status_code=400,
            error=str(exc),
        )
    return done(request, f"Saved channel {name}.", "/admin/channels")


@router.post("/channels/{name}/restart")
async def restart_channel(request: Request, admin: CheckedAdminDep, name: str) -> Response:
    try:
        await runtime_of(request).restart_channel(name)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "that channel isn't enabled") from exc
    return done(request, f"Restarted channel {name}.", "/admin/channels")


@router.post("/channels/{name}/remove")
async def remove_channel(request: Request, admin: CheckedAdminDep, name: str) -> Response:
    try:
        await service_of(request).remove_channel(name)
    except AdminError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return done(request, f"Removed channel {name} and its secrets.", "/admin/channels")


# ---- Personalities --------------------------------------------------------------------------------------


@router.get("/personalities")
async def personalities(request: Request, admin: AdminDep) -> Response:
    state = app_state(request)
    return await render(
        request,
        "personalities.html",
        admin,
        personalities=sorted(state.config.personalities.items()),
        default_id=state.config.default_personality,
        registered=[p.id for p in state.personalities.all()],
        evaluators=state.evaluators.names(),
        types=sorted(BUILTINS),
    )


@router.post("/personalities/preview")
async def preview(
    request: Request,
    admin: CheckedAdminDep,
    personality_id: Annotated[str, Form()],
    prompt: Annotated[str, Form()],
    evaluator: Annotated[str, Form()],
    evaluation_prompt: Annotated[str, Form()],
    reply: Annotated[str, Form()],
    options: Annotated[str, Form()] = "",
    personality_prompt: Annotated[bool, Form()] = False,
) -> Response:
    state = app_state(request)
    response_options = [o.strip() for o in options.split(",") if o.strip()] or None
    error = None
    if personality_id not in state.personalities:
        error = f"unknown personality {personality_id!r}"
    elif state.evaluators.get(evaluator) is None:
        error = f"evaluator {evaluator!r} is not configured"
    elif evaluator == "rule" and not response_options:
        error = "the rule evaluator needs response options"
    if error:
        return await render(request, "partials/preview_result.html", admin, error=error)
    try:
        job = InteractionJob(
            id="preview",
            recipient_id="preview",
            prompt=prompt,
            response_options=response_options,
            personality_id=personality_id,
            evaluation=EvaluationSpec(evaluator=evaluator, prompt=evaluation_prompt),
            delivery=DeliverySpec(prompt_rendering="personality" if personality_prompt else "verbatim"),
        )
        chosen = reply.strip()
        sample = (
            SampleResponse(selected_option=chosen)
            if response_options and chosen in response_options
            else SampleResponse(text=chosen)
        )
    except ValidationError as exc:
        messages = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return await render(request, "partials/preview_result.html", admin, error=messages)
    result = await run_preview(state, job, sample)
    return await render(request, "partials/preview_result.html", admin, result=result)


async def _personality_form(
    request: Request,
    admin: Admin,
    *,
    personality_id: str | None,
    personality_type: str,
    values: dict[str, Any],
    status_code: int = 200,
    error: str | None = None,
) -> Response:
    model = BUILTIN_OPTIONS.get(personality_type)
    options = values.get("options", {})
    return await render(
        request,
        "personality_form.html",
        admin,
        status_code,
        personality_id=personality_id,
        personality_type=personality_type,
        fields=fields_for(model, options) if model else None,
        options_yaml=values.get("options_yaml", dump_mapping(options)),
        values=values,
        error=error,
    )


@router.get("/personalities/new")
async def new_personality_page(request: Request, admin: AdminDep, type: str = "template") -> Response:
    return await _personality_form(request, admin, personality_id=None, personality_type=type, values={})


def _personality_options(form: FormData, personality_type: str) -> dict[str, Any]:
    model: type[BaseModel] | None = BUILTIN_OPTIONS.get(personality_type)
    if model is None:
        return parse_yaml_mapping(text(form, "options_yaml"), "Options")
    return parse_fields(fields_for(model), form).options


async def _save_personality(request: Request, admin: Admin, personality_id: str | None) -> Response:
    form = await request.form()
    target = personality_id or text(form, "id")
    personality_type = text(form, "type")
    values: dict[str, Any] = {
        "id": target,
        "description": text(form, "description"),
        "options_yaml": text(form, "options_yaml"),
    }
    try:
        options = _personality_options(form, personality_type)
        values["options"] = options
        if personality_id is None and target in app_state(request).config.personalities:
            raise AdminError(f"personality {target!r} already exists")
        await service_of(request).set_personality(
            target, personality_type, options, description=values["description"]
        )
    except (AdminError, FormError) as exc:
        return await _personality_form(
            request,
            admin,
            personality_id=personality_id,
            personality_type=personality_type,
            values=values,
            status_code=400,
            error=str(exc),
        )
    return done(request, f"Saved personality {target}.", "/admin/personalities")


@router.post("/personalities")
async def create_personality(request: Request, admin: CheckedAdminDep) -> Response:
    return await _save_personality(request, admin, None)


@router.get("/personalities/{personality_id}")
async def edit_personality_page(request: Request, admin: AdminDep, personality_id: str) -> Response:
    entry = app_state(request).config.personalities.get(personality_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such personality")
    values = {"id": personality_id, "description": entry.description, "options": entry.options}
    return await _personality_form(
        request, admin, personality_id=personality_id, personality_type=entry.type, values=values
    )


@router.post("/personalities/{personality_id}")
async def edit_personality(request: Request, admin: CheckedAdminDep, personality_id: str) -> Response:
    if personality_id not in app_state(request).config.personalities:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return await _save_personality(request, admin, personality_id)


@router.post("/personalities/{personality_id}/default")
async def default_personality(request: Request, admin: CheckedAdminDep, personality_id: str) -> Response:
    try:
        await service_of(request).set_default_personality(personality_id)
    except AdminError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return done(request, f"{personality_id} is now the default personality.", "/admin/personalities")


@router.post("/personalities/{personality_id}/delete")
async def delete_personality(request: Request, admin: CheckedAdminDep, personality_id: str) -> Response:
    try:
        await service_of(request).delete_personality(personality_id)
    except AdminError as exc:
        return done(request, str(exc).capitalize() + ".", "/admin/personalities")
    return done(request, f"Removed personality {personality_id}.", "/admin/personalities")


# ---- Settings -------------------------------------------------------------------------------------------

_SECTIONS: dict[str, type[BaseModel]] = {
    "scheduler": SchedulerConfig,
    "llm": LLMConfig,
    "delivery": DeliveryConfig,
}


async def _settings_page(
    request: Request,
    admin: Admin,
    status_code: int = 200,
    errors: dict[str, str] | None = None,
    password_error: str | None = None,
) -> Response:
    state = app_state(request)
    config = state.config
    scheduler = config.scheduler
    quiet = (
        f"{scheduler.quiet_hours.start:%H:%M}-{scheduler.quiet_hours.end:%H:%M}"
        if scheduler.quiet_hours
        else ""
    )
    sections = {
        name: [
            f
            for f in fields_for(model, getattr(config, name).model_dump(mode="json"))
            if f.name != "quiet_hours"
        ]
        for name, model in _SECTIONS.items()
    }
    return await render(
        request,
        "settings.html",
        admin,
        status_code,
        sections=sections,
        quiet=quiet,
        llm_key_source=state.live.current.llm_key_source,
        llm_live=isinstance(state.evaluators.get("llm"), LLMEvaluator),
        history=await store_of(request).history(15),
        errors=errors or {},
        password_error=password_error,
    )


@router.get("/settings")
async def settings_page(request: Request, admin: AdminDep) -> Response:
    return await _settings_page(request, admin)


@router.post("/settings/section/{section}")
async def save_settings(request: Request, admin: CheckedAdminDep, section: str) -> Response:
    model = _SECTIONS.get(section)
    if model is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    form = await request.form()
    try:
        fields = [f for f in fields_for(model) if f.name != "quiet_hours"]
        values = parse_fields(fields, form).options
        if section == "scheduler":
            quiet = text(form, "quiet")
            values["quiet_hours"] = parse_quiet_hours(quiet) if quiet else None
        await service_of(request).update_settings(section, values)
    except (AdminError, FormError) as exc:
        return await _settings_page(request, admin, 400, errors={section: str(exc)})
    return done(request, f"Saved {section} settings.", "/admin/settings")


@router.post("/settings/llm-key")
async def save_llm_key(request: Request, admin: CheckedAdminDep) -> Response:
    form = await request.form()
    value = None if checked(form, "clear") else text(form, "key")
    if value == "":
        return await _settings_page(request, admin, 400, errors={"llm_key": "Paste a key, or tick Clear."})
    await service_of(request).set_llm_key(value)
    return done(request, "LLM API key cleared." if value is None else "LLM API key saved.", "/admin/settings")


@router.post("/settings/test-llm")
async def test_llm(request: Request, admin: CheckedAdminDep) -> Response:
    snapshot = app_state(request).live.current
    llm = snapshot.config.llm
    if snapshot.llm_client is None:
        return await render(request, "partials/llm_test.html", admin, ok=False, message="No API key is set.")
    started = time.monotonic()
    try:
        completion = await asyncio.wait_for(
            chat(snapshot.llm_client, model=llm.model, messages=[user("Reply with just the word: ok")]),
            timeout=llm.timeout_seconds,
        )
    except Exception as exc:
        return await render(
            request, "partials/llm_test.html", admin, ok=False, message=f"{type(exc).__name__}: {exc}"
        )
    elapsed = int((time.monotonic() - started) * 1000)
    reply = (first_text(completion) or "").strip()[:80]
    message = f"{llm.model} answered in {elapsed} ms: “{reply}”"
    return await render(request, "partials/llm_test.html", admin, ok=True, message=message)


@router.get("/settings/export.yaml")
async def export_config(request: Request, admin: AdminDep) -> Response:
    text_ = await service_of(request).export_yaml()
    return PlainTextResponse(
        text_,
        media_type="application/yaml",
        headers={"Content-Disposition": 'attachment; filename="pester-config.yaml"'},
    )


@router.post("/settings/password")
async def change_password(
    request: Request,
    admin: CheckedAdminDep,
    auth: AuthDep,
    current: Annotated[str, Form()],
    password: Annotated[str, Form()],
    confirm: Annotated[str, Form()],
) -> Response:
    error = None
    if not await auth.check_password(current):
        error = "The current password is wrong."
    elif password != confirm:
        error = "The new passwords don't match."
    else:
        try:
            await auth.set_password(password)  # signs out every session, including this one
        except ValueError as exc:
            error = str(exc).capitalize() + "."
    if error:
        return await _settings_page(request, admin, 400, password_error=error)
    response = done(request, "Password changed. Other sessions were signed out.", "/admin/settings")
    set_session_cookie(request, response, await auth.create_session())
    return response


@router.post("/settings/sign-out-everywhere")
async def sign_out_everywhere(request: Request, admin: CheckedAdminDep, auth: AuthDep) -> Response:
    await auth.end_all_sessions()
    response = redirect(request, "/admin/login")
    response.delete_cookie(COOKIE, path="/admin")
    return response
