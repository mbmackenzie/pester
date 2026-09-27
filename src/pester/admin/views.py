"""Admin UI pages (docs/admin-ui.md). Server-rendered Jinja + htmx, behind the admin session."""

import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup

from pester.admin.auth import SESSION_LIFETIME, AdminAuth
from pester.admin.forms import FLASH_COOKIE, take_flash
from pester.admin.queries import AdminQueries
from pester.api.health import readiness_checks
from pester.core.errors import IllegalTransitionError, JobNotFoundError
from pester.core.states import JobStatus
from pester.delivery.base import ChannelError
from pester.delivery.memory import ChatMessage, InMemoryChannel, mock_channels
from pester.evaluation.llm import LLMEvaluator
from pester.runtime import Runtime
from pester.state import AppState
from pester.storage.repository import Repository

router = APIRouter(prefix="/admin", include_in_schema=False)

COOKIE = "pester_admin"


# ---- Templates ------------------------------------------------------------------------------------------


def _datetime(value: datetime | None) -> Markup:
    """A UTC timestamp that admin.js rewrites into the browser's local time."""
    if value is None:
        return Markup("")
    iso = value.isoformat().replace("+00:00", "Z")
    return Markup('<time datetime="{}">{} UTC</time>').format(iso, value.strftime("%Y-%m-%d %H:%M"))


def _pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _truncate(value: str, length: int = 80) -> str:
    return value if len(value) <= length else value[: length - 1] + "…"


_env = Environment(loader=PackageLoader("pester.admin", "templates"), autoescape=select_autoescape())
_env.filters.update(dt=_datetime, pretty_json=_pretty_json, truncate_text=_truncate)
templates = Jinja2Templates(env=_env)


# ---- Session and CSRF -----------------------------------------------------------------------------------


class AdminRedirect(Exception):
    def __init__(self, location: str) -> None:
        self.location = location


def redirect(request: Request, location: str) -> Response:
    """303 for normal requests; htmx requests get HX-Redirect so the whole page navigates."""
    if request.headers.get("hx-request"):
        return Response(status_code=status.HTTP_200_OK, headers={"HX-Redirect": location})
    return RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)


@dataclass(frozen=True)
class Admin:
    session_id: str
    csrf_token: str


def get_auth(request: Request) -> AdminAuth:
    return request.app.state.admin


AuthDep = Annotated[AdminAuth, Depends(get_auth)]


async def current_admin(request: Request, auth: AuthDep) -> Admin:
    if not await auth.is_set_up():
        raise AdminRedirect("/admin/setup")
    session_id = request.cookies.get(COOKIE)
    session = await auth.session(session_id) if session_id else None
    if session_id is None or session is None:
        raise AdminRedirect("/admin/login")
    return Admin(session_id, session.csrf_token)


async def checked_admin(request: Request, admin: Annotated[Admin, Depends(current_admin)]) -> Admin:
    """The admin, for state-changing requests: requires the session's CSRF token (header or form field)."""
    token = request.headers.get("x-csrf-token")
    if token is None:
        field = (await request.form()).get("csrf")
        token = field if isinstance(field, str) else None
    if token is None or not hmac.compare_digest(token, admin.csrf_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing or invalid CSRF token; reload the page")
    return admin


AdminDep = Annotated[Admin, Depends(current_admin)]
CheckedAdminDep = Annotated[Admin, Depends(checked_admin)]


def set_session_cookie(request: Request, response: Response, session_id: str) -> None:
    response.set_cookie(
        COOKIE,
        session_id,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path="/admin",
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


# ---- Shared context -------------------------------------------------------------------------------------


def app_state(request: Request) -> AppState:
    return request.app.state.pester


def runtime_of(request: Request) -> Runtime:
    return request.app.state.runtime


def repo_of(request: Request) -> Repository:
    return request.app.state.repo


def _queries(request: Request) -> AdminQueries:
    return request.app.state.admin_queries


async def render(
    request: Request, name: str, admin: Admin | None, status_code: int = 200, **context: Any
) -> HTMLResponse:
    runtime = runtime_of(request)
    channels_ok = bool(runtime.channels) and set(runtime.channels) <= runtime.started_channels
    pending = len(await request.app.state.service.pending_pairings()) if admin else 0
    notice = take_flash(request)
    response = templates.TemplateResponse(
        request,
        name,
        {
            "admin": admin,
            "path": request.url.path,
            "channels_ok": channels_ok,
            "pending_pairings": pending,
            "notice": notice,
            **context,
        },
        status_code=status_code,
    )
    if notice:
        response.delete_cookie(FLASH_COOKIE, path="/admin")
    return response


def _mock_channel(request: Request, name: str | None) -> InMemoryChannel | None:
    """The mock channel called ``name``, else the first one."""
    mocks = mock_channels(runtime_of(request).channels)
    return mocks.get(name) if name else next(iter(mocks.values()), None)


# ---- First run, login, logout ---------------------------------------------------------------------------


@router.get("/setup")
async def setup_page(request: Request, auth: AuthDep) -> Response:
    if await auth.is_set_up():
        return redirect(request, "/admin/login")
    return await render(request, "setup.html", None)


@router.post("/setup")
async def setup(
    request: Request,
    auth: AuthDep,
    code: Annotated[str, Form()],
    password: Annotated[str, Form()],
    confirm: Annotated[str, Form()],
) -> Response:
    if await auth.is_set_up():
        return redirect(request, "/admin/login")
    error = None
    if not auth.check_setup_code(code):
        error = "That setup code is wrong. Check the container logs for the current one."
    elif password != confirm:
        error = "The passwords don't match."
    else:
        try:
            await auth.set_password(password)
        except ValueError as exc:
            error = str(exc).capitalize() + "."
    if error:
        return await render(request, "setup.html", None, status.HTTP_400_BAD_REQUEST, error=error)
    response = redirect(request, "/admin")
    set_session_cookie(request, response, await auth.create_session())
    return response


@router.get("/login")
async def login_page(request: Request, auth: AuthDep) -> Response:
    if not await auth.is_set_up():
        return redirect(request, "/admin/setup")
    return await render(request, "login.html", None)


@router.post("/login")
async def login(request: Request, auth: AuthDep, password: Annotated[str, Form()]) -> Response:
    if not await auth.check_password(password):
        return await render(
            request, "login.html", None, status.HTTP_401_UNAUTHORIZED, error="Wrong password."
        )
    response = redirect(request, "/admin")
    set_session_cookie(request, response, await auth.create_session())
    return response


@router.post("/logout")
async def logout(request: Request, admin: CheckedAdminDep, auth: AuthDep) -> Response:
    await auth.end_session(admin.session_id)
    response = redirect(request, "/admin/login")
    response.delete_cookie(COOKIE, path="/admin")
    return response


# ---- Dashboard ------------------------------------------------------------------------------------------


async def _overview(request: Request) -> dict[str, Any]:
    state, runtime = app_state(request), runtime_of(request)
    queries = _queries(request)
    checks = await readiness_checks(state, repo_of(request), runtime)
    return {
        "counts": await queries.counts(since=state.clock.now() - timedelta(hours=24)),
        "checks": checks,
        "activity": await queries.recent_activity(),
        "llm_live": isinstance(state.evaluators.get("llm"), LLMEvaluator),
        "llm_model": state.config.llm.model,
    }


@router.get("")
async def dashboard(request: Request, admin: AdminDep) -> Response:
    state, runtime = app_state(request), runtime_of(request)
    steps = [
        ("Set the admin password", True, "/admin/settings"),
        ("Add a channel", bool(runtime.started_channels), "/admin/channels"),
        ("Pair a recipient", bool(state.config.recipients), "/admin/recipients"),
        ("Create a client key", bool(state.config.clients), "/admin/clients"),
        ("Add an LLM key (optional)", state.live.current.llm_key_source is not None, "/admin/settings"),
    ]
    return await render(request, "dashboard.html", admin, steps=steps, **await _overview(request))


@router.get("/partials/overview")
async def overview(request: Request, admin: AdminDep) -> Response:
    return await render(request, "partials/overview.html", admin, **await _overview(request))


# ---- Jobs -----------------------------------------------------------------------------------------------


@router.get("/jobs")
async def jobs(
    request: Request,
    admin: AdminDep,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    recipient: str | None = None,
    client: str | None = None,
    before: int | None = None,
) -> Response:
    rows, next_before = await _queries(request).list_jobs(
        status=status_filter, recipient_id=recipient or None, client_id=client or None, before=before
    )
    config = app_state(request).config
    return await render(
        request,
        "jobs.html",
        admin,
        jobs=rows,
        next_before=next_before,
        filters={"status": status_filter or "", "recipient": recipient or "", "client": client or ""},
        statuses=list(JobStatus),
        recipients=sorted(config.recipients),
        clients=sorted(config.clients),
    )


@router.get("/jobs/{client_id}/{job_id}")
async def job_detail(request: Request, admin: AdminDep, client_id: str, job_id: str) -> Response:
    detail = await _queries(request).job_detail(client_id, job_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return await render(request, "job.html", admin, job=detail)


@router.post("/jobs/{client_id}/{job_id}/cancel")
async def cancel_job(request: Request, admin: CheckedAdminDep, client_id: str, job_id: str) -> Response:
    try:
        await repo_of(request).cancel(client_id, job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found") from exc
    except IllegalTransitionError:
        pass  # finished in the meantime; the page shows its final state
    runtime_of(request).nudge()
    return redirect(request, f"/admin/jobs/{client_id}/{job_id}")


# ---- Mock messenger -------------------------------------------------------------------------------------


def _mock_addresses(state: AppState, channel: InMemoryChannel) -> list[tuple[str, str | None]]:
    """(address, recipient id or None) for recipients on this channel, then other active conversations."""
    found: list[tuple[str, str | None]] = []
    for recipient_id, recipient in sorted(state.config.recipients.items()):
        address = recipient.channels.get(channel.name, {}).get("address")
        if isinstance(address, str) and address:
            found.append((address, recipient_id))
    known = {address for address, _ in found}
    found += [(address, None) for address in channel.addresses() if address not in known]
    return found


def _conversation(messages: list[ChatMessage]) -> dict[str, Any]:
    return {
        "messages": messages,
        "last_id": messages[-1].id if messages else 0,
        "quotes": {m.id: m.text or m.selected_option or "" for m in messages},  # for replies
    }


async def _chat_log(request: Request, admin: Admin, channel: InMemoryChannel, address: str) -> Response:
    return await render(
        request,
        "partials/chat_log.html",
        admin,
        channel=channel,
        address=address,
        **_conversation(channel.conversation(address)),
    )


@router.get("/chat")
async def chat(
    request: Request, admin: AdminDep, address: str | None = None, channel: str | None = None
) -> Response:
    state = app_state(request)
    mock = _mock_channel(request, channel)
    known = _mock_addresses(state, mock) if mock else []
    address = address or (known[0][0] if known else None)
    messages = mock.conversation(address) if mock and address else []
    return await render(
        request,
        "chat.html",
        admin,
        channel=mock,
        mocks=list(mock_channels(runtime_of(request).channels)),
        address=address,
        known=known,
        recipient=dict(known).get(address or ""),
        **_conversation(messages),
    )


def _require_mock(request: Request, name: str | None) -> InMemoryChannel:
    mock = _mock_channel(request, name)
    if mock is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no mock channel is enabled")
    return mock


@router.get("/chat/{address}/log")
async def chat_log(
    request: Request, admin: AdminDep, address: str, after: int = 0, channel: str | None = None
) -> Response:
    mock = _require_mock(request, channel)
    if not mock.conversation(address, after):
        return Response(status_code=status.HTTP_204_NO_CONTENT)  # htmx leaves the log alone
    return await _chat_log(request, admin, mock, address)


@router.post("/chat/{address}")
async def chat_send(
    request: Request,
    admin: CheckedAdminDep,
    address: str,
    text: Annotated[str | None, Form()] = None,
    selected_option: Annotated[str | None, Form()] = None,
    reply_to: Annotated[str | None, Form()] = None,
    channel: str | None = None,
) -> Response:
    mock = _require_mock(request, channel)
    text = (text or "").strip() or None
    if text is not None or selected_option:
        try:
            await mock.inject(
                address,
                text,
                reply_to=int(reply_to) if reply_to and reply_to.isdigit() else None,
                selected_option=selected_option or None,
            )
        except ChannelError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return await _chat_log(request, admin, mock, address)
