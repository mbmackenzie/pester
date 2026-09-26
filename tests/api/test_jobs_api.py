import json
from pathlib import Path

import httpx
import pytest

from tests.conftest import TOKEN_A, TOKEN_B, TOKEN_READONLY, auth, job_payload

FIXTURE = Path(__file__).parents[2] / "fixtures" / "example_batch.json"


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic dXNlcjpwYXNz"}],
)
async def test_auth_required(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    resp = await client.post("/api/v1/jobs", json=job_payload(), headers=headers)
    assert resp.status_code == 401
    assert (await client.get("/api/v1/events", headers=headers)).status_code == 401


async def test_permission_enforced(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/jobs", json=job_payload(), headers=auth(TOKEN_READONLY))
    assert resp.status_code == 403
    assert (await client.get("/api/v1/events", headers=auth(TOKEN_READONLY))).status_code == 200


async def test_submit_then_replay(client: httpx.AsyncClient) -> None:
    body = job_payload(id="j1")
    first = await client.post("/api/v1/jobs", json=body, headers=auth(TOKEN_A))
    assert first.status_code == 201
    assert first.json() == {"id": "j1", "batch_id": None, "status": "QUEUED"}
    replay = await client.post("/api/v1/jobs", json=body, headers=auth(TOKEN_A))
    assert replay.status_code == 200
    assert replay.json() == first.json()


async def test_conflicting_replay_is_409(client: httpx.AsyncClient) -> None:
    await client.post("/api/v1/jobs", json=job_payload(id="j1"), headers=auth(TOKEN_A))
    resp = await client.post("/api/v1/jobs", json=job_payload(id="j1", prompt="other"), headers=auth(TOKEN_A))
    assert resp.status_code == 409


async def test_recipient_not_allowed_is_403(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/jobs", json=job_payload(recipient_id="sam"), headers=auth(TOKEN_B))
    assert resp.status_code == 403
    unknown = await client.post(
        "/api/v1/jobs", json=job_payload(recipient_id="nobody"), headers=auth(TOKEN_A)
    )
    assert unknown.status_code == 403


async def test_unknown_personality_or_channel_is_422(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/jobs", json=job_payload(personality_id="ghost"), headers=auth(TOKEN_A))
    assert resp.status_code == 422
    resp = await client.post(
        "/api/v1/jobs", json=job_payload(delivery={"channel": "telegram"}), headers=auth(TOKEN_A)
    )
    assert resp.status_code == 422
    assert "telegram" in resp.json()["detail"]


async def test_invalid_body_is_422(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/jobs", json={"prompt": "no recipient"}, headers=auth(TOKEN_A))
    assert resp.status_code == 422


async def test_get_job_and_isolation(client: httpx.AsyncClient) -> None:
    await client.post("/api/v1/jobs", json=job_payload(id="j1", metadata={"k": 1}), headers=auth(TOKEN_A))
    resp = await client.get("/api/v1/jobs/j1", headers=auth(TOKEN_A))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "QUEUED"
    assert body["job"]["metadata"] == {"k": 1}
    assert (await client.get("/api/v1/jobs/j1", headers=auth(TOKEN_B))).status_code == 404


async def test_cancel(client: httpx.AsyncClient) -> None:
    await client.post("/api/v1/jobs", json=job_payload(id="j1"), headers=auth(TOKEN_A))
    for _ in range(2):
        resp = await client.post("/api/v1/jobs/j1/cancel", headers=auth(TOKEN_A))
        assert resp.status_code == 200
        assert resp.json()["status"] == "CANCELLED"
    assert (await client.post("/api/v1/jobs/j1/cancel", headers=auth(TOKEN_B))).status_code == 404
    assert (await client.post("/api/v1/jobs/nope/cancel", headers=auth(TOKEN_A))).status_code == 404


async def test_example_batch_submit_and_replay(client: httpx.AsyncClient) -> None:
    body = json.loads(FIXTURE.read_text())
    first = await client.post("/api/v1/batches", json=body, headers=auth(TOKEN_A))
    assert first.status_code == 201, first.text
    assert [j["id"] for j in first.json()["jobs"]] == ["study-001", "plants-001"]
    replay = await client.post("/api/v1/batches", json=body, headers=auth(TOKEN_A))
    assert replay.status_code == 200
    assert replay.json() == first.json()

    body["jobs"].pop()
    assert (await client.post("/api/v1/batches", json=body, headers=auth(TOKEN_A))).status_code == 409


async def test_batch_validation_reports_index_and_writes_nothing(client: httpx.AsyncClient) -> None:
    body = {"batch_id": "b1", "jobs": [job_payload(id="ok"), job_payload(id="bad", recipient_id="sam")]}
    resp = await client.post("/api/v1/batches", json=body, headers=auth(TOKEN_B))
    assert resp.status_code == 403
    assert resp.json()["detail"].startswith("jobs[1]:")
    assert (await client.get("/api/v1/jobs/ok", headers=auth(TOKEN_B))).status_code == 404
