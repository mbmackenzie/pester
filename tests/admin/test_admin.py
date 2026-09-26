import re

import httpx
import pytest
from fastapi import FastAPI

from pester.admin.auth import AdminAuth, hash_password, verify_password
from pester.runtime import Runtime
from tests.conftest import TOKEN_A, auth, job_payload

PASSWORD = "correct horse battery"


def admin_auth(app: FastAPI) -> AdminAuth:
    return app.state.admin


def csrf_of(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "page has no CSRF token"
    return match.group(1)


async def set_up(client: httpx.AsyncClient, app: FastAPI) -> str:
    """Complete first-run setup; the client is now signed in. Returns a CSRF token."""
    resp = await client.post(
        "/admin/setup",
        data={"code": admin_auth(app).setup_code, "password": PASSWORD, "confirm": PASSWORD},
    )
    assert resp.status_code == 303, resp.text
    return csrf_of((await client.get("/admin")).text)


@pytest.fixture
async def signed_in(client: httpx.AsyncClient, app: FastAPI) -> str:
    return await set_up(client, app)


def test_password_hashing_round_trips() -> None:
    stored = hash_password("s3cret-pass")
    assert stored.startswith("scrypt$")
    assert verify_password("s3cret-pass", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("s3cret-pass", "garbage")
    assert hash_password("same") != hash_password("same")  # salted


# ---- First run and sign-in ------------------------------------------------------------------------------


async def test_everything_redirects_to_setup_until_a_password_exists(client: httpx.AsyncClient) -> None:
    for path in ("/admin", "/admin/jobs", "/admin/login"):
        resp = await client.get(path)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/setup"
    assert "Setup code" in (await client.get("/admin/setup")).text


@pytest.mark.parametrize(
    ("code", "password", "confirm", "message"),
    [
        ("WRONG-CODE", PASSWORD, PASSWORD, "setup code is wrong"),
        (None, PASSWORD, "something else", "don&#39;t match"),
        (None, "short", "short", "at least 8 characters"),
    ],
)
async def test_setup_rejects_bad_input(
    client: httpx.AsyncClient, app: FastAPI, code: str | None, password: str, confirm: str, message: str
) -> None:
    resp = await client.post(
        "/admin/setup",
        data={"code": code or admin_auth(app).setup_code, "password": password, "confirm": confirm},
    )
    assert resp.status_code == 400
    assert message in resp.text
    assert not await admin_auth(app).is_set_up()


async def test_setup_code_ignores_case_and_dashes(client: httpx.AsyncClient, app: FastAPI) -> None:
    code = admin_auth(app).setup_code.replace("-", "").lower()
    resp = await client.post("/admin/setup", data={"code": code, "password": PASSWORD, "confirm": PASSWORD})
    assert resp.status_code == 303


async def test_setup_signs_in_and_then_closes(client: httpx.AsyncClient, app: FastAPI) -> None:
    await set_up(client, app)
    resp = await client.get("/admin")
    assert resp.status_code == 200
    assert "Dashboard" in resp.text
    # Setup can't be run again, even with the right code.
    assert (await client.get("/admin/setup")).headers["location"] == "/admin/login"
    again = await client.post(
        "/admin/setup", data={"code": admin_auth(app).setup_code, "password": "x" * 10, "confirm": "x" * 10}
    )
    assert again.headers["location"] == "/admin/login"
    assert await admin_auth(app).check_password(PASSWORD)


async def test_session_cookie_is_http_only_and_scoped(client: httpx.AsyncClient, app: FastAPI) -> None:
    resp = await client.post(
        "/admin/setup", data={"code": admin_auth(app).setup_code, "password": PASSWORD, "confirm": PASSWORD}
    )
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/admin" in cookie
    assert "secure" not in cookie  # plain HTTP on a LAN


async def test_login(client: httpx.AsyncClient, app: FastAPI) -> None:
    await admin_auth(app).set_password(PASSWORD)
    assert (await client.get("/admin")).headers["location"] == "/admin/login"

    wrong = await client.post("/admin/login", data={"password": "nope"})
    assert wrong.status_code == 401
    assert "Wrong password" in wrong.text

    right = await client.post("/admin/login", data={"password": PASSWORD})
    assert right.status_code == 303
    assert (await client.get("/admin")).status_code == 200


async def test_htmx_requests_get_hx_redirect(client: httpx.AsyncClient, app: FastAPI) -> None:
    await admin_auth(app).set_password(PASSWORD)
    resp = await client.get("/admin/partials/overview", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"] == "/admin/login"


async def test_state_changes_require_the_csrf_token(client: httpx.AsyncClient, signed_in: str) -> None:
    assert (await client.post("/admin/logout")).status_code == 403
    assert (await client.post("/admin/logout", data={"csrf": "forged"})).status_code == 403
    assert (await client.get("/admin")).status_code == 200  # still signed in

    resp = await client.post("/admin/logout", headers={"X-CSRF-Token": signed_in})
    assert resp.status_code == 303
    assert (await client.get("/admin")).headers["location"] == "/admin/login"


async def test_a_signed_out_cookie_stays_dead(client: httpx.AsyncClient, signed_in: str) -> None:
    cookie = client.cookies.get("pester_admin")
    await client.post("/admin/logout", data={"csrf": signed_in})
    client.cookies.set("pester_admin", cookie or "", path="/admin")
    assert (await client.get("/admin")).headers["location"] == "/admin/login"


async def test_changing_the_password_signs_out_other_sessions(
    client: httpx.AsyncClient, app: FastAPI, signed_in: str
) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as other:
        await other.post("/admin/login", data={"password": PASSWORD})
        assert (await other.get("/admin")).status_code == 200

        new = "an even better password"
        resp = await client.post(
            "/admin/settings/password",
            data={"csrf": signed_in, "current": PASSWORD, "password": new, "confirm": new},
        )
        assert resp.status_code == 303
        assert (await client.get("/admin")).status_code == 200  # this browser got a fresh session
        assert (await other.get("/admin")).headers["location"] == "/admin/login"
    assert await admin_auth(app).check_password(new)


async def test_password_change_needs_the_current_password(client: httpx.AsyncClient, signed_in: str) -> None:
    resp = await client.post(
        "/admin/settings/password",
        data={"csrf": signed_in, "current": "wrong", "password": "x" * 10, "confirm": "x" * 10},
    )
    assert resp.status_code == 400
    assert "current password is wrong" in resp.text


async def test_reset_returns_to_first_run(client: httpx.AsyncClient, app: FastAPI, signed_in: str) -> None:
    await admin_auth(app).reset()
    assert (await client.get("/admin")).headers["location"] == "/admin/setup"


# ---- Pages ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/admin", "Getting started"),
        ("/admin/jobs", "Jobs"),
        ("/admin/chat", "Messenger"),
        ("/admin/recipients", "kate"),
        ("/admin/clients", "producer-a"),
        ("/admin/channels", "mock"),
        ("/admin/personalities", "weather-goblin"),
        ("/admin/settings", "Quiet hours"),
        ("/admin/partials/overview", "Recent activity"),
    ],
)
async def test_pages_render(client: httpx.AsyncClient, signed_in: str, path: str, expected: str) -> None:
    resp = await client.get(path)
    assert resp.status_code == 200, resp.text
    assert expected in resp.text


async def test_static_assets_are_served(client: httpx.AsyncClient) -> None:
    for name in ("htmx.min.js", "admin.css", "admin.js"):
        assert (await client.get(f"/admin/static/{name}")).status_code == 200


async def test_producer_tokens_are_never_shown(client: httpx.AsyncClient, signed_in: str) -> None:
    page = (await client.get("/admin/clients")).text
    assert "sha256:" not in page


# ---- Jobs, messenger, recipients ------------------------------------------------------------------------


async def submit(client: httpx.AsyncClient, **job: object) -> str:
    resp = await client.post("/api/v1/jobs", json=job_payload(**job), headers=auth(TOKEN_A))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_full_loop_through_the_browser_messenger(
    client: httpx.AsyncClient, app: FastAPI, signed_in: str
) -> None:
    runtime: Runtime = app.state.runtime
    job_id = await submit(client, prompt="Did you water the plants?", response_options=["Yes", "No"])
    await runtime.run_until_idle()

    page = (await client.get("/admin/chat", params={"address": "kate"})).text
    assert "Did you water the plants?" in page
    assert "hx-vals" in page  # option buttons

    log = await client.post(
        "/admin/chat/kate",
        data={"selected_option": "Yes", "reply_to": "1"},
        headers={"X-CSRF-Token": signed_in},
    )
    assert log.status_code == 200
    assert 'id="chat-log"' in log.text
    await runtime.run_until_idle()

    fresh = await client.get("/admin/chat/kate/log", params={"after": 2})
    assert fresh.status_code == 200
    assert "Got it: Yes" in fresh.text  # the echo evaluator's feedback
    assert (await client.get("/admin/chat/kate/log", params={"after": 3})).status_code == 204

    detail = (await client.get(f"/admin/jobs/producer-a/{job_id}")).text
    for label in ("QUEUED", "DELIVERED", "ANSWERED", "EVALUATED", "COMPLETED"):
        assert label in detail
    assert "echo" in detail
    dashboard = (await client.get("/admin/partials/overview")).text
    assert "Did you water the plants?" in dashboard


async def test_typed_replies_and_commands(client: httpx.AsyncClient, app: FastAPI, signed_in: str) -> None:
    await submit(client)
    await app.state.runtime.run_until_idle()
    resp = await client.post(
        "/admin/chat/kate", data={"text": "/status"}, headers={"X-CSRF-Token": signed_in}
    )
    assert "/status" in resp.text
    assert "Did you water the plants?" in resp.text


async def test_messenger_needs_csrf(client: httpx.AsyncClient, signed_in: str) -> None:
    assert (await client.post("/admin/chat/kate", data={"text": "hi"})).status_code == 403


async def test_unknown_address_is_flagged(client: httpx.AsyncClient, signed_in: str) -> None:
    page = (await client.get("/admin/chat", params={"address": "stranger"})).text
    assert "Pester ignores its messages" in page


async def test_messenger_without_the_mock_channel(
    client: httpx.AsyncClient, app: FastAPI, signed_in: str
) -> None:
    app.state.runtime.channels = {}  # a deployment without the mock channel
    page = (await client.get("/admin/chat")).text
    assert "mock channel isn't running" in page
    assert (await client.get("/admin/chat/kate/log")).status_code == 404


async def test_jobs_list_filters_and_pages(client: httpx.AsyncClient, signed_in: str) -> None:
    for i in range(3):
        await submit(client, prompt=f"question {i}")
    page = (await client.get("/admin/jobs", params={"status": "QUEUED"})).text
    assert all(f"question {i}" in page for i in range(3))
    assert "question" not in (await client.get("/admin/jobs", params={"status": "COMPLETED"})).text


async def test_admin_can_cancel_a_job(client: httpx.AsyncClient, signed_in: str) -> None:
    job_id = await submit(client)
    resp = await client.post(f"/admin/jobs/producer-a/{job_id}/cancel", data={"csrf": signed_in})
    assert resp.status_code == 303
    status = await client.get(f"/api/v1/jobs/{job_id}", headers=auth(TOKEN_A))
    assert status.json()["status"] == "CANCELLED"
    assert "Cancel</button>" not in (await client.get(f"/admin/jobs/producer-a/{job_id}")).text


async def test_unknown_job_is_404(client: httpx.AsyncClient, signed_in: str) -> None:
    assert (await client.get("/admin/jobs/producer-a/nope")).status_code == 404


async def test_pause_and_resume_a_recipient(client: httpx.AsyncClient, app: FastAPI, signed_in: str) -> None:
    repo = app.state.repo
    await client.post("/admin/recipients/kate/pause", data={"csrf": signed_in})
    assert (await repo.recipient_status("kate")).paused
    assert "PAUSED" in (await client.get("/admin/recipients")).text
    await client.post("/admin/recipients/kate/resume", data={"csrf": signed_in})
    assert not (await repo.recipient_status("kate")).paused
    assert (await client.post("/admin/recipients/nobody/pause", data={"csrf": signed_in})).status_code == 404


async def test_preview(client: httpx.AsyncClient, signed_in: str) -> None:
    resp = await client.post(
        "/admin/personalities/preview",
        headers={"X-CSRF-Token": signed_in},
        data={
            "personality_id": "houseplant",
            "prompt": "Water?",
            "evaluator": "echo",
            "evaluation_prompt": "YES/NO",
            "reply": "Yes",
            "options": "Yes, No",
            "personality_prompt": "true",
        },
    )
    assert resp.status_code == 200
    assert "🌱 Water?" in resp.text  # voiced prompt
    assert "🌿" in resp.text  # voiced feedback


async def test_preview_reports_bad_input(client: httpx.AsyncClient, signed_in: str) -> None:
    resp = await client.post(
        "/admin/personalities/preview",
        headers={"X-CSRF-Token": signed_in},
        data={
            "personality_id": "default",
            "prompt": "Water?",
            "evaluator": "rule",
            "evaluation_prompt": "x",
            "reply": "Yes",
        },
    )
    assert "needs response options" in resp.text
