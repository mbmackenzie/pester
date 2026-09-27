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
from pydantic import ValidationError

from pester.admin.auth import SESSION_LIFETIME, AdminAuth
from pester.admin.queries import AdminQueries
from pester.api.health import readiness_checks
from pester.api.preview import SampleResponse, run_preview
from pester.core.errors import IllegalTransitionError, JobNotFoundError
from pester.core.models import DeliverySpec, EvaluationSpec, InteractionJob
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


def _set_session_cookie(request: Request, response: Response, session_id: str) -> None:
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


def _state(request: Request) -> AppState:
    return request.app.state.pester


def _runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def _repo(request: Request) -> Repository:
    return request.app.state.repo


def _queries(request: Request) -> AdminQueries:
    return request.app.state.admin_queries


def render(
    request: Request, name: str, admin: Admin | None, status_code: int = 200, **context: Any
) -> HTMLResponse:
    runtime = _runtime(request)
    channels_ok = bool(runtime.channels) and set(runtime.channels) <= runtime.started_channels
    return templates.TemplateResponse(
        request,
        name,
        {"admin": admin, "path": request.url.path, "channels_ok": channels_ok, **context},
        status_code=status_code,
    )


def _mock_channel(request: Request, name: str | None) -> InMemoryChannel | None:
    """The mock channel called ``name``, else the first one."""
    mocks = mock_channels(_runtime(request).channels)
    return mocks.get(name) if name else next(iter(mocks.values()), None)


# ---- First run, login, logout ---------------------------------------------------------------------------


@router.get("/setup")
async def setup_page(request: Request, auth: AuthDep) -> Response:
    if await auth.is_set_up():
        return redirect(request, "/admin/login")
    return render(request, "setup.html", None)


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
        return render(request, "setup.html", None, status.HTTP_400_BAD_REQUEST, error=error)
    response = redirect(request, "/admin")
    _set_session_cookie(request, response, await auth.create_session())
    return response


@router.get("/login")
async def login_page(request: Request, auth: AuthDep) -> Response:
    if not await auth.is_set_up():
        return redirect(request, "/admin/setup")
    return render(request, "login.html", None)


@router.post("/login")
async def login(request: Request, auth: AuthDep, password: Annotated[str, Form()]) -> Response:
    if not await auth.check_password(password):
        return render(request, "login.html", None, status.HTTP_401_UNAUTHORIZED, error="Wrong password.")
    response = redirect(request, "/admin")
    _set_session_cookie(request, response, await auth.create_session())
    return response


@router.post("/logout")
async def logout(request: Request, admin: CheckedAdminDep, auth: AuthDep) -> Response:
    await auth.end_session(admin.session_id)
    response = redirect(request, "/admin/login")
    response.delete_cookie(COOKIE, path="/admin")
    return response


# ---- Dashboard ------------------------------------------------------------------------------------------


async def _overview(request: Request) -> dict[str, Any]:
    state, runtime = _state(request), _runtime(request)
    queries = _queries(request)
    checks = await readiness_checks(state, _repo(request), runtime)
    return {
        "counts": await queries.counts(since=state.clock.now() - timedelta(hours=24)),
        "checks": checks,
        "activity": await queries.recent_activity(),
        "llm_live": isinstance(state.evaluators.get("llm"), LLMEvaluator),
        "llm_model": state.config.llm.model,
    }


@router.get("")
async def dashboard(request: Request, admin: AdminDep) -> Response:
    state, runtime = _state(request), _runtime(request)
    steps = [
        ("Admin password", True, "/admin/settings"),
        ("Channel", bool(runtime.started_channels), "/admin/channels"),
        ("Recipient", bool(state.config.recipients), "/admin/recipients"),
        ("Client key", bool(state.config.clients), "/admin/clients"),
        ("LLM key (optional)", state.live.current.llm_key_source is not None, "/admin/settings"),
    ]
    return render(request, "dashboard.html", admin, steps=steps, **await _overview(request))


@router.get("/partials/overview")
async def overview(request: Request, admin: AdminDep) -> Response:
    return render(request, "partials/overview.html", admin, **await _overview(request))


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
    config = _state(request).config
    return render(
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
    return render(request, "job.html", admin, job=detail)


@router.post("/jobs/{client_id}/{job_id}/cancel")
async def cancel_job(request: Request, admin: CheckedAdminDep, client_id: str, job_id: str) -> Response:
    try:
        await _repo(request).cancel(client_id, job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found") from exc
    except IllegalTransitionError:
        pass  # finished in the meantime; the page shows its final state
    _runtime(request).nudge()
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


def _chat_log(request: Request, admin: Admin, channel: InMemoryChannel, address: str) -> Response:
    return render(
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
    state = _state(request)
    mock = _mock_channel(request, channel)
    known = _mock_addresses(state, mock) if mock else []
    address = address or (known[0][0] if known else None)
    messages = mock.conversation(address) if mock and address else []
    return render(
        request,
        "chat.html",
        admin,
        channel=mock,
        mocks=list(mock_channels(_runtime(request).channels)),
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
    return _chat_log(request, admin, mock, address)


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
    return _chat_log(request, admin, mock, address)


# ---- Configuration (read-only until M6) -----------------------------------------------------------------


@router.get("/recipients")
async def recipients(request: Request, admin: AdminDep) -> Response:
    state, repo = _state(request), _repo(request)
    rows = [
        (recipient_id, recipient, await repo.recipient_status(recipient_id))
        for recipient_id, recipient in sorted(state.config.recipients.items())
    ]
    pending = await request.app.state.service.pending_pairings()
    return render(request, "recipients.html", admin, recipients=rows, pending=pending)


@router.post("/recipients/{recipient_id}/{action}")
async def set_recipient_paused(
    request: Request, admin: CheckedAdminDep, recipient_id: str, action: str
) -> Response:
    if recipient_id not in _state(request).config.recipients or action not in ("pause", "resume"):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    await _repo(request).set_paused(recipient_id, action == "pause")
    _runtime(request).nudge()
    return redirect(request, "/admin/recipients")


@router.get("/clients")
async def clients(request: Request, admin: AdminDep) -> Response:
    return render(request, "clients.html", admin, clients=sorted(_state(request).config.clients.items()))


@router.get("/channels")
async def channels(request: Request, admin: AdminDep) -> Response:
    state, runtime = _state(request), _runtime(request)
    rows = [
        (
            status_,
            [
                (recipient_id, recipient.channels[status_.name])
                for recipient_id, recipient in sorted(state.config.recipients.items())
                if status_.name in recipient.channels
            ],
        )
        for status_ in runtime.manager.status()
    ]
    return render(request, "channels.html", admin, channels=rows, dev_mode=state.settings.dev_mode)


@router.get("/personalities")
async def personalities(request: Request, admin: AdminDep) -> Response:
    state = _state(request)
    return render(
        request,
        "personalities.html",
        admin,
        personalities=state.personalities.all(),
        default_id=state.personalities.default_id,
        evaluators=state.evaluators.names(),
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
    state = _state(request)
    response_options = [o.strip() for o in options.split(",") if o.strip()] or None
    error = None
    if personality_id not in state.personalities:
        error = f"unknown personality {personality_id!r}"
    elif state.evaluators.get(evaluator) is None:
        error = f"evaluator {evaluator!r} is not configured"
    elif evaluator == "rule" and not response_options:
        error = "the rule evaluator needs response options"
    if error:
        return render(request, "partials/preview_result.html", admin, error=error)
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
        return render(request, "partials/preview_result.html", admin, error=messages)
    result = await run_preview(state, job, sample)
    return render(request, "partials/preview_result.html", admin, result=result)


# ---- Settings -------------------------------------------------------------------------------------------


@router.get("/settings")
async def settings_page(request: Request, admin: AdminDep) -> Response:
    return render(request, "settings.html", admin, **_settings_context(request))


def _settings_context(request: Request) -> dict[str, Any]:
    state = _state(request)
    return {
        "scheduler": state.config.scheduler,
        "llm": state.config.llm,
        "delivery": state.config.delivery,
        "llm_key_source": state.live.current.llm_key_source,
        "llm_live": isinstance(state.evaluators.get("llm"), LLMEvaluator),
    }


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
        return render(
            request,
            "settings.html",
            admin,
            status.HTTP_400_BAD_REQUEST,
            password_error=error,
            **_settings_context(request),
        )
    response = redirect(request, "/admin/settings?password=changed")
    _set_session_cookie(request, response, await auth.create_session())
    return response


@router.post("/settings/sign-out-everywhere")
async def sign_out_everywhere(request: Request, admin: CheckedAdminDep, auth: AuthDep) -> Response:
    await auth.end_all_sessions()
    response = redirect(request, "/admin/login")
    response.delete_cookie(COOKIE, path="/admin")
    return response
