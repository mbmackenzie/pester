from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pester.config import LLMConfig, PesterConfig
from pester.core.models import HumanResponse, InteractionJob
from pester.personality.base import (
    BasePersonality,
    FeedbackContext,
    PersonalityConfigError,
    PersonalityServices,
    PromptContext,
)
from pester.personality.registry import PersonalityRegistry, build_registry
from tests.llm_fakes import FakeLLM, completion, error

JOB = InteractionJob.model_validate(
    {
        "id": "j1",
        "recipient_id": "kate",
        "prompt": "Did you water the plants?",
        "evaluation": {"prompt": "x"},
        "metadata": {"plant": "fern"},
    }
)
REPLY = HumanResponse(
    interaction_id="j1",
    text="yes",
    channel="fake",
    address="kate",
    external_id="2",
    received_at=datetime(2026, 1, 1, tzinfo=UTC),
)


def feedback_ctx(facts: str = "Logged: watered.", result: dict[str, Any] | None = None) -> FeedbackContext:
    return FeedbackContext(JOB, REPLY, facts, result or {"answer": "YES", "nested": {"n": 1}})


def registry(
    personalities: dict[str, Any], llm: FakeLLM | None = None, base_dir: Path = Path(), **config: Any
) -> PersonalityRegistry:
    cfg = PesterConfig.model_validate({"personalities": personalities, **config})
    services = PersonalityServices(LLMConfig(model="base-model"), llm.client() if llm else None, base_dir)
    return build_registry(cfg, services)


# ---- A deployer's own personality, registered by import path ----------------------------------------


class Shouty(BasePersonality):
    def __init__(self, options: Mapping[str, Any], services: PersonalityServices) -> None:
        self.suffix = str(options.get("suffix", "!"))

    async def render_feedback(self, ctx: FeedbackContext) -> str:
        return ctx.feedback_facts.upper() + self.suffix


def make_meddler(options: Mapping[str, Any], services: PersonalityServices) -> BasePersonality:
    class Meddler(BasePersonality):
        async def render_feedback(self, ctx: FeedbackContext) -> str:
            ctx.result["answer"]["x"] = 1  # type: ignore[index]  # tries to tamper with the evaluation
            return "meddled"

    return Meddler()


NOT_CALLABLE = 42


# ---- Tests ------------------------------------------------------------------------------------------


async def test_default_is_neutral() -> None:
    reg = registry({})
    entry = reg.resolve(None)
    assert (entry.id, entry.type) == ("default", "neutral")
    assert (await entry.feedback(feedback_ctx())).text == "Logged: watered."
    assert (await entry.prompt(PromptContext(JOB))).text == "Did you water the plants?"


async def test_custom_default_personality() -> None:
    reg = registry(
        {"calm": {"type": "template", "feedback": "~ {{ feedback_facts }} ~"}}, default_personality="calm"
    )
    assert reg.resolve(None).id == "calm"
    assert "calm" in reg and "default" in reg and "ghost" not in reg


async def test_template_personality() -> None:
    reg = registry(
        {
            "houseplant": {
                "type": "template",
                "description": "Judgmental",
                "feedback": (
                    "The {{ metadata.plant }} heard you say '{{ response }}'. "
                    "{{ feedback_facts }} ({{ result.answer }})"
                ),
                "prompt": "Human. {{ prompt }}",
            }
        }
    )
    entry = reg.resolve("houseplant")
    assert entry.description == "Judgmental"
    feedback = await entry.feedback(feedback_ctx())
    assert feedback == type(feedback)("The fern heard you say 'yes'. Logged: watered. (YES)", fallback=False)
    assert (await entry.prompt(PromptContext(JOB))).text == "Human. Did you water the plants?"


async def test_template_undefined_variable_falls_back_to_neutral() -> None:
    reg = registry({"typo": {"type": "template", "feedback": "{{ feedbak_facts }}"}})
    rendered = await reg.resolve("typo").feedback(feedback_ctx())
    assert rendered.text == "Logged: watered."
    assert rendered.fallback


async def test_template_sandbox_blocks_unsafe_access() -> None:
    reg = registry({"evil": {"type": "template", "feedback": "{{ result.__class__.__mro__ }}"}})
    assert (await reg.resolve("evil").feedback(feedback_ctx())).fallback


@pytest.mark.parametrize(
    ("personalities", "message"),
    [
        ({"p": {"type": "template", "feedback": "{% if %}"}}, "personality 'p': invalid template"),
        (
            {"p": {"type": "template", "feedbak": "x"}},
            "personality 'p': feedbak: Extra inputs are not permitted",
        ),
        ({"p": {"type": "neutral", "tone": "x"}}, "personality 'p': tone"),
        ({"p": {"type": "llm"}}, "set exactly one of 'prompt' or 'prompt_file'"),
        ({"p": {"type": "llm", "prompt": "a", "prompt_file": "b"}}, "set exactly one"),
        ({"p": {"type": "llm", "prompt_file": "missing.md"}}, "cannot read prompt_file"),
        ({"p": {"type": "sparkly"}}, "unknown type 'sparkly'"),
        ({"p": {"type": "no_such_module_xyz:Thing"}}, "cannot import 'no_such_module_xyz'"),
        ({"p": {"type": "tests.unit.test_personalities:NOT_CALLABLE"}}, "is not a callable"),
        ({"p": {"type": "tests.unit.test_personalities:missing"}}, "is not a callable"),
    ],
)
def test_misconfiguration_fails_at_startup(personalities: dict[str, Any], message: str) -> None:
    with pytest.raises(PersonalityConfigError, match=message.replace("(", r"\(").replace(")", r"\)")):
        registry(personalities)


async def test_custom_personality_by_import_path() -> None:
    reg = registry({"shouty": {"type": "tests.unit.test_personalities:Shouty", "suffix": "!!!"}})
    assert (await reg.resolve("shouty").feedback(feedback_ctx())).text == "LOGGED: WATERED.!!!"
    assert (await reg.resolve("shouty").prompt(PromptContext(JOB))).text == JOB.prompt  # inherited


async def test_personality_cannot_modify_the_evaluation() -> None:
    reg = registry({"meddler": {"type": "tests.unit.test_personalities:make_meddler"}})
    result = {"answer": {"value": "YES"}}
    rendered = await reg.resolve("meddler").feedback(feedback_ctx(result=result))
    assert rendered.text == "meddled"  # it only ever touched its own deep copy
    assert result == {"answer": {"value": "YES"}}


def test_feedback_context_result_is_a_private_copy() -> None:
    result = {"nested": {"n": 1}}
    ctx = feedback_ctx(result=result)
    with pytest.raises(TypeError):
        ctx.result["new"] = 1  # type: ignore[index]
    ctx.result["nested"]["n"] = 2
    assert result == {"nested": {"n": 1}}


async def test_llm_personality_feedback() -> None:
    llm = FakeLLM(completion("Hmph. You watered it. Fine."))
    reg = registry({"goblin": {"type": "llm", "prompt": "You are a goblin.", "temperature": 0.9}}, llm)
    rendered = await reg.resolve("goblin").feedback(feedback_ctx())
    assert rendered.text == "Hmph. You watered it. Fine."
    assert not rendered.fallback
    system, user = llm.last["messages"]
    assert system["content"].startswith("You are a goblin.")
    assert "Do not add, remove, soften, or contradict" in system["content"]
    assert "Facts to convey: Logged: watered." in user["content"]
    assert llm.last["model"] == "base-model"
    assert llm.last["temperature"] == 0.9


async def test_llm_personality_prompt_and_model_override(tmp_path: Path) -> None:
    (tmp_path / "goblin.md").write_text("You are a goblin from a file.")
    llm = FakeLLM(completion("Oi. Plants. Watered?"))
    reg = registry(
        {"goblin": {"type": "llm", "prompt_file": "goblin.md", "model": "cheap"}}, llm, base_dir=tmp_path
    )
    rendered = await reg.resolve("goblin").prompt(PromptContext(JOB))
    assert rendered.text == "Oi. Plants. Watered?"
    assert llm.last["model"] == "cheap"
    assert llm.last["messages"][0]["content"].startswith("You are a goblin from a file.")
    assert "Ask for exactly the same thing" in llm.last["messages"][0]["content"]


@pytest.mark.parametrize("reply", [error(500), completion(""), completion(None)])
async def test_llm_personality_failure_falls_back(reply: Any) -> None:
    llm = FakeLLM(reply)
    reg = registry({"goblin": {"type": "llm", "prompt": "goblin"}}, llm)
    rendered = await reg.resolve("goblin").feedback(feedback_ctx())
    assert (rendered.text, rendered.fallback) == ("Logged: watered.", True)


async def test_llm_personality_without_api_key_falls_back() -> None:
    reg = registry({"goblin": {"type": "llm", "prompt": "goblin"}})  # no client: builds fine
    rendered = await reg.resolve("goblin").feedback(feedback_ctx())
    assert (rendered.text, rendered.fallback) == ("Logged: watered.", True)


def test_listing_is_sorted_with_descriptions() -> None:
    reg = registry({"zeta": {"type": "neutral", "description": "Z"}, "alpha": {"type": "neutral"}})
    assert [(p.id, p.description) for p in reg.all()] == [("alpha", ""), ("default", ""), ("zeta", "Z")]
