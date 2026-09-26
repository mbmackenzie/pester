import httpx

from tests.conftest import TOKEN_A, TOKEN_B, auth, job_payload

SCORE = {"type": "object", "properties": {"score": {"type": "number"}}, "required": ["score"]}


async def test_list_personalities(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/personalities", headers=auth(TOKEN_A))
    assert resp.status_code == 200
    assert resp.json() == {
        "default": "default",
        "personalities": [
            {"id": "default", "description": ""},
            {"id": "houseplant", "description": "Judgmental houseplant"},
            {"id": "weather-goblin", "description": "Grumpy"},
        ],
    }
    assert (await client.get("/api/v1/personalities")).status_code == 401


async def test_submit_rejects_unconfigured_evaluator(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/jobs",
        json=job_payload(evaluation={"evaluator": "magic", "prompt": "x"}),
        headers=auth(TOKEN_A),
    )
    assert resp.status_code == 422
    assert "available: echo, llm, rule" in resp.json()["detail"]


async def test_submit_rejects_rule_without_options(client: httpx.AsyncClient) -> None:
    body = job_payload(evaluation={"evaluator": "rule", "prompt": "x"})
    resp = await client.post("/api/v1/jobs", json=body, headers=auth(TOKEN_A))
    assert resp.status_code == 422
    body["response_options"] = ["Yes", "No"]
    assert (await client.post("/api/v1/jobs", json=body, headers=auth(TOKEN_A))).status_code == 201


async def test_submit_rejects_invalid_output_schema(client: httpx.AsyncClient) -> None:
    body = job_payload(evaluation={"prompt": "x", "output_schema": {"type": "nonsense"}})
    resp = await client.post("/api/v1/jobs", json=body, headers=auth(TOKEN_A))
    assert resp.status_code == 422
    assert "not a valid JSON Schema" in resp.json()["detail"]


async def test_preview_success_stores_nothing(client: httpx.AsyncClient) -> None:
    body = {"job": job_payload(personality_id="houseplant"), "response": {"text": "yes, this morning"}}
    resp = await client.post("/api/v1/jobs:preview", json=body, headers=auth(TOKEN_A))
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert preview["status"] == "SUCCESS"
    assert preview["prompt"] == "Did you water the plants?"  # verbatim unless the job opts in
    assert preview["result"] == {"echo": "yes, this morning"}  # dev mode without a key: llm -> echo
    assert preview["feedback"] == "🌿 Got it: yes, this morning"
    assert preview["personality"] == "houseplant" and preview["personality_fallback"] is False
    assert (await client.get("/api/v1/events", headers=auth(TOKEN_A))).json()["events"] == []


async def test_preview_renders_prompt_when_requested(client: httpx.AsyncClient) -> None:
    job = job_payload(personality_id="houseplant", delivery={"prompt_rendering": "personality"})
    resp = await client.post(
        "/api/v1/jobs:preview", json={"job": job, "response": {"text": "x"}}, headers=auth(TOKEN_A)
    )
    assert resp.json()["prompt"] == "🌱 Did you water the plants?"


async def test_preview_reports_evaluation_failure(client: httpx.AsyncClient) -> None:
    job = job_payload(evaluation={"prompt": "x", "output_schema": SCORE})
    resp = await client.post(
        "/api/v1/jobs:preview", json={"job": job, "response": {"text": "x"}}, headers=auth(TOKEN_A)
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "FAILED"
    assert "does not match output_schema" in resp.json()["error"]


async def test_preview_llm_personality_without_key_falls_back(client: httpx.AsyncClient) -> None:
    job = job_payload(personality_id="weather-goblin")
    resp = await client.post(
        "/api/v1/jobs:preview", json={"job": job, "response": {"text": "x"}}, headers=auth(TOKEN_A)
    )
    assert resp.json()["feedback"] == "Got it: x"
    assert resp.json()["personality_fallback"] is True


async def test_preview_requires_permission_and_valid_job(client: httpx.AsyncClient) -> None:
    body = {"job": job_payload(), "response": {"text": "x"}}
    assert (await client.post("/api/v1/jobs:preview", json=body, headers=auth(TOKEN_B))).status_code == 403
    bad = {"job": job_payload(recipient_id="sam"), "response": {"text": "x"}}
    assert (await client.post("/api/v1/jobs:preview", json=bad, headers=auth(TOKEN_A))).status_code == 200
    unknown = {"job": job_payload(personality_id="ghost"), "response": {"text": "x"}}
    assert (await client.post("/api/v1/jobs:preview", json=unknown, headers=auth(TOKEN_A))).status_code == 422
    empty = {"job": job_payload(), "response": {}}
    assert (await client.post("/api/v1/jobs:preview", json=empty, headers=auth(TOKEN_A))).status_code == 422
