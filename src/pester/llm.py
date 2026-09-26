"""Thin typed wrapper around OpenAI-compatible chat completions, shared by evaluators and personalities."""

from typing import Any, cast

from openai import AsyncOpenAI, omit
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam, completion_create_params

from pester.config import LLMConfig, Settings

Message = ChatCompletionMessageParam


def make_client(settings: Settings, config: LLMConfig) -> AsyncOpenAI | None:
    """A client for the configured provider, or None when no API key is set."""
    if settings.openai_api_key is None:
        return None
    return AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
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
) -> ChatCompletion:
    return await client.chat.completions.create(
        model=model,
        messages=messages,
        response_format=(
            cast(completion_create_params.ResponseFormat, response_format) if response_format else omit
        ),
        temperature=temperature if temperature is not None else omit,
    )


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
