"""A fake OpenAI-compatible endpoint. The SDK speaks httpx2, so this plugs in via httpx2.MockTransport."""

import json
from typing import Any

import httpx2
from openai import AsyncOpenAI

Reply = httpx2.Response | Exception


class FakeLLM:
    """Answers chat completion requests from a queue (then ``default``) and records every request body."""

    def __init__(self, default: Reply | None = None) -> None:
        self.default = default
        self.requests: list[dict[str, Any]] = []
        self._queue: list[Reply] = []

    def queue(self, *replies: Reply) -> None:
        self._queue.extend(replies)

    def client(self) -> AsyncOpenAI:
        transport = httpx2.MockTransport(self._handle)
        return AsyncOpenAI(
            api_key="sk-test",
            base_url="https://llm.test/v1",
            http_client=httpx2.AsyncClient(transport=transport),
            max_retries=0,
        )

    @property
    def last(self) -> dict[str, Any]:
        return self.requests[-1]

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        reply = self._queue.pop(0) if self._queue else self.default
        if reply is None:
            raise AssertionError(f"unexpected LLM request: {self.requests[-1]}")
        if isinstance(reply, Exception):
            raise reply
        return reply


def completion(
    content: str | None, model: str = "test-model", prompt_tokens: int = 10, completion_tokens: int = 5
) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        },
    )


def evaluation(result: dict[str, Any], facts: str = "Correct.", **kw: Any) -> httpx2.Response:
    return completion(json.dumps({"result": result, "feedback_facts": facts}), **kw)


def error(status: int, message: str = "boom") -> httpx2.Response:
    return httpx2.Response(status, json={"error": {"message": message, "type": "test", "code": None}})


def connection_error() -> Exception:
    return httpx2.ConnectError("connection refused")
