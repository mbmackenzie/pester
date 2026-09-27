"""Forms generated from pydantic options models (channel adapters, personality types), and flash messages.

Supported field types are the ones adapters and personalities need: strings (``multiline`` for textareas),
integers, numbers, booleans, enums, and secrets (``SecretStr``). Anything else is edited as YAML.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast
from urllib.parse import quote, unquote

import yaml
from fastapi import Request, Response
from pydantic import BaseModel
from starlette.datastructures import FormData

Kind = Literal["text", "textarea", "integer", "number", "checkbox", "select", "secret", "yaml"]

FLASH_COOKIE = "pester_flash"


@dataclass
class FormField:
    name: str
    label: str
    kind: Kind
    required: bool = False
    nullable: bool = False
    default: Any = None
    choices: list[str] = field(default_factory=list[str])
    help: str = ""
    value: Any = None  # the current value, for rendering
    secret_status: str | None = None  # for secrets: "environment", "database", or None when not set
    env: str | None = None  # for secrets: the overriding environment variable

    @property
    def display(self) -> str:
        """The value as it goes into an input."""
        value = self.default if self.value is None and not self.nullable else self.value
        if value is None:
            return ""
        if self.kind == "yaml":
            return yaml.safe_dump(value, default_flow_style=True).strip()
        return str(value)


def _schema_kind(prop: Mapping[str, Any]) -> tuple[Kind, bool, list[str]]:
    """(kind, nullable, choices) for one JSON schema property."""
    nullable = False
    multiline = bool(prop.get("multiline"))
    options = cast(list[Mapping[str, Any]], prop.get("anyOf", []))
    if options:
        non_null = [o for o in options if o.get("type") != "null"]
        nullable = len(non_null) < len(options)
        prop = non_null[0] if len(non_null) == 1 else {}
    if prop.get("writeOnly") and prop.get("format") == "password":
        return "secret", nullable, []
    if "enum" in prop:
        return "select", nullable, [str(v) for v in cast(list[Any], prop["enum"])]
    match prop.get("type"):
        case "string":
            return ("textarea" if multiline else "text"), nullable, []
        case "integer":
            return "integer", nullable, []
        case "number":
            return "number", nullable, []
        case "boolean":
            return "checkbox", nullable, []
        case _:
            return "yaml", nullable, []


def fields_for(
    model: type[BaseModel],
    values: Mapping[str, Any] | None = None,
    secret_status: Mapping[str, str | None] | None = None,
) -> list[FormField]:
    schema = model.model_json_schema()
    required = set(cast(list[str], schema.get("required", [])))
    values = values or {}
    result: list[FormField] = []
    for name, raw in cast(dict[str, Mapping[str, Any]], schema.get("properties", {})).items():
        kind, nullable, choices = _schema_kind(raw)
        result.append(
            FormField(
                name=name,
                label=str(raw.get("title", name)),
                kind=kind,
                required=name in required,
                nullable=nullable,
                default=raw.get("default"),
                choices=choices,
                help=str(raw.get("description", "")),
                value=values.get(name),
                secret_status=(secret_status or {}).get(name) if kind == "secret" else None,
                env=str(raw["env"]) if kind == "secret" and "env" in raw else None,
            )
        )
    return result


@dataclass(frozen=True)
class ParsedForm:
    options: dict[str, Any]  # non-secret values, only those set
    secrets: dict[str, str | None]  # secret field -> new value (None clears); only fields being changed


class FormError(ValueError):
    pass


def parse_fields(fields: list[FormField], form: FormData, prefix: str = "opt_") -> ParsedForm:
    """Read submitted values for ``fields``.

    An emptied nullable input becomes None; other empty optional inputs are left out, so defaults apply.
    """
    options: dict[str, Any] = {}
    secrets: dict[str, str | None] = {}
    for f in fields:
        key = prefix + f.name
        if f.kind == "checkbox":
            options[f.name] = form.get(key) is not None
            continue
        raw = form.get(key)
        text = (
            raw.strip()
            if isinstance(raw, str) and f.kind != "textarea"
            else (raw if isinstance(raw, str) else "")
        )
        if f.kind == "secret":
            if form.get(key + "__clear") is not None:
                secrets[f.name] = None
            elif text:
                secrets[f.name] = text
            continue
        if text == "":
            if f.required:
                raise FormError(f"{f.label} is required")
            if f.nullable:
                options[f.name] = None  # cleared
            continue
        try:
            match f.kind:
                case "integer":
                    options[f.name] = int(text)
                case "number":
                    options[f.name] = float(text)
                case "yaml":
                    options[f.name] = yaml.safe_load(text)
                case _:
                    options[f.name] = text
        except (ValueError, yaml.YAMLError) as exc:
            raise FormError(f"{f.label}: {exc}") from exc
    return ParsedForm(options, secrets)


def parse_yaml_mapping(text: str, what: str) -> dict[str, Any]:
    """A YAML (or JSON) mapping typed into a textarea."""
    if not text.strip():
        return {}
    try:
        value: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FormError(f"{what} isn't valid YAML: {exc}") from exc
    if not isinstance(value, dict):
        raise FormError(f"{what} should be a mapping, like  address: kate")
    return cast(dict[str, Any], value)


def dump_mapping(value: Mapping[str, Any]) -> str:
    return yaml.safe_dump(json.loads(json.dumps(dict(value))), sort_keys=False).strip() if value else ""


# ---- Flash messages -------------------------------------------------------------------------------------


def flash(response: Response, message: str) -> Response:
    """Show ``message`` on the next page this browser loads."""
    response.set_cookie(
        FLASH_COOKIE, quote(message), max_age=60, path="/admin", httponly=True, samesite="lax"
    )
    return response


def take_flash(request: Request) -> str | None:
    value = request.cookies.get(FLASH_COOKIE)
    return unquote(value) if value else None
