from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from pester.personality.base import BasePersonality, PersonalityServices, parse_options


class _NoOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NeutralPersonality(BasePersonality):
    """Delivers prompts verbatim and feedback facts exactly as the evaluator wrote them."""


def neutral(options: Mapping[str, Any], services: PersonalityServices) -> NeutralPersonality:
    parse_options(_NoOptions, options)
    return NeutralPersonality()
