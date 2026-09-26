import httpx

from tests.conftest import TOKEN_A, TOKEN_B, auth, job_payload


async def test_events_scoped_and_paged(client: httpx.AsyncClient) -> None:
    for i in range(5):
        await client.post(
            "/api/v1/jobs", json=job_payload(id=f"a{i}", metadata={"i": i}), headers=auth(TOKEN_A)
        )
        await client.post("/api/v1/jobs", json=job_payload(id=f"b{i}"), headers=auth(TOKEN_B))
    await client.post("/api/v1/jobs/a0/cancel", headers=auth(TOKEN_A))

    seen: list[tuple[str, str]] = []
    cursor = 0
    while True:
        resp = await client.get("/api/v1/events", params={"after": cursor, "limit": 2}, headers=auth(TOKEN_A))
        assert resp.status_code == 200
        page = resp.json()
        if not page["events"]:
            assert page["next_cursor"] == cursor
            break
        seen += [(e["interaction_id"], e["type"]) for e in page["events"]]
        cursor = page["next_cursor"]

    assert seen == [(f"a{i}", "INTERACTION_QUEUED") for i in range(5)] + [("a0", "INTERACTION_CANCELLED")]


async def test_event_shape(client: httpx.AsyncClient) -> None:
    await client.post(
        "/api/v1/jobs", json=job_payload(id="j1", metadata={"concept": "x"}), headers=auth(TOKEN_A)
    )
    (event,) = (await client.get("/api/v1/events", headers=auth(TOKEN_A))).json()["events"]
    assert event["type"] == "INTERACTION_QUEUED"
    assert event["interaction_id"] == "j1"
    assert event["batch_id"] is None
    assert event["metadata"] == {"concept": "x"}
    assert event["payload"] == {}
    assert event["occurred_at"].endswith("Z")
    assert set(event) == {
        "cursor",
        "event_id",
        "type",
        "interaction_id",
        "batch_id",
        "occurred_at",
        "payload",
        "metadata",
    }


async def test_limit_bounds(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/events?limit=0", headers=auth(TOKEN_A))).status_code == 422
    assert (await client.get("/api/v1/events?limit=1001", headers=auth(TOKEN_A))).status_code == 422
    assert (await client.get("/api/v1/events?after=-1", headers=auth(TOKEN_A))).status_code == 422
