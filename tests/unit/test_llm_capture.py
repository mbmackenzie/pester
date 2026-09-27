import pytest

from pester.llm import capture_calls, chat, user
from tests.llm_fakes import FakeLLM, completion, error


async def test_capture_records_requests_replies_and_errors() -> None:
    llm = FakeLLM()
    client = llm.client()
    llm.queue(completion("hi there", prompt_tokens=7, completion_tokens=2), error(400, "bad model"))
    with capture_calls() as calls:
        await chat(client, model="m1", messages=[user("hello")], temperature=0.2, purpose="evaluation")
        with pytest.raises(Exception, match="bad model"):
            await chat(client, model="m2", messages=[user("again")], purpose="personality: feedback")
    first, second = calls
    assert first.purpose == "evaluation"
    assert first.request == {
        "model": "m1",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.2,
    }
    assert (first.reply, first.usage) == ("hi there", {"input_tokens": 7, "output_tokens": 2})
    assert first.latency_ms is not None
    assert second.error is not None and "bad model" in second.error
    assert second.reply is None


async def test_nothing_is_recorded_outside_capture() -> None:
    llm = FakeLLM(default=completion("ok"))
    with capture_calls() as calls:
        pass
    await chat(llm.client(), model="m", messages=[user("x")])
    assert calls == []
