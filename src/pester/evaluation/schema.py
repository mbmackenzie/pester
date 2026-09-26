"""JSON Schema checks for producer-supplied ``output_schema``."""

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


def schema_error(schema: dict[str, Any]) -> str | None:
    """Why ``schema`` is not a valid JSON Schema, or None if it is."""
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return exc.message
    return None


def result_error(schema: dict[str, Any] | None, result: dict[str, Any]) -> str | None:
    """Why ``result`` does not match ``schema``, or None if it does (or there is no schema)."""
    if schema is None:
        return None
    validator = Draft202012Validator(schema)
    errors: list[ValidationError] = sorted(
        validator.iter_errors(result),  # pyright: ignore[reportUnknownMemberType]
        key=lambda e: [str(p) for p in e.absolute_path],
    )
    if not errors:
        return None
    first = errors[0]
    where = "/".join(str(p) for p in first.absolute_path) or "<root>"
    more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
    return f"{where}: {first.message}{more}"
