"""Thin typed wrapper around OpenAI-compatible chat completions, shared by evaluators and personalities."""

import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, cast

from openai import AsyncOpenAI, omit
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam, completion_create_params

from pester.config import LLMConfig

Message = ChatCompletionMessageParam


@dataclass
class LLMCall:
    """One chat completion, as recorded by ``capture_calls`` (the admin UI's preview shows these)."""

    purpose: str  # e.g. "evaluation", "personality: feedback"
    request: dict[str, Any]
    reply: str | None = None
    error: str | None = None
    latency_ms: int | None = None
    usage: dict[str, int] | None = None


_captured: ContextVar[list[LLMCall] | None] = ContextVar("pester_llm_calls", default=None)


@contextmanager
def capture_calls() -> Generator[list[LLMCall]]:
    """Record every chat completion made inside the block (in this task), with its exact request."""
    calls: list[LLMCall] = []
    token = _captured.set(calls)
    try:
        yield calls
    finally:
        _captured.reset(token)


def make_client(api_key: str, config: LLMConfig) -> AsyncOpenAI:
    """A client for the configured OpenAI-compatible provider."""
    return AsyncOpenAI(
        api_key=api_key,
        base_url=config.base_url,
        timeout=config.timeout_seconds,
    )


def system(content: str) -> Message:
    return {"role": "system", "content": content}


def user(content: str) -> Message:
    return {"role": "user", "content": content}


def assistant(content: str) -> Message:
    return {"role": "assistant", "content": content}


async def chat(
    client: AsyncOpenAI,
    *,
    model: str,
    messages: list[Message],
    response_format: dict[str, Any] | None = None,
    temperature: float | None = None,
    purpose: str = "llm",
) -> ChatCompletion:
    calls = _captured.get()
    call = (
        LLMCall(
            purpose,
            request_record(
                model=model, messages=messages, response_format=response_format, temperature=temperature
            ),
        )
        if calls is not None
        else None
    )
    started = time.perf_counter()
    try:
        completion = await client.chat.completions.create(
            model=model,
            messages=messages,
            response_format=(
                cast(completion_create_params.ResponseFormat, response_format) if response_format else omit
            ),
            temperature=temperature if temperature is not None else omit,
        )
    except Exception as exc:
        if call is not None and calls is not None:
            call.error = f"{type(exc).__name__}: {exc}"
            call.latency_ms = int((time.perf_counter() - started) * 1000)
            calls.append(call)
        raise
    if call is not None and calls is not None:
        call.reply = first_text(completion)
        call.latency_ms = int((time.perf_counter() - started) * 1000)
        if completion.usage is not None:
            call.usage = {
                "input_tokens": completion.usage.prompt_tokens,
                "output_tokens": completion.usage.completion_tokens,
            }
        calls.append(call)
    return completion


def request_record(
    *,
    model: str,
    messages: list[Message],
    response_format: dict[str, Any] | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    """The request as plain JSON, for the audit trail."""
    record: dict[str, Any] = {"model": model, "messages": [dict(m) for m in messages]}
    if response_format is not None:
        record["response_format"] = response_format
    if temperature is not None:
        record["temperature"] = temperature
    return record


def first_text(completion: ChatCompletion) -> str | None:
    return completion.choices[0].message.content if completion.choices else None
