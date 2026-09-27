"""Shared helpers for admin UI tests: first-run setup and CSRF tokens."""

import re

import httpx
import pytest
from fastapi import FastAPI

from pester.admin.auth import AdminAuth

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
