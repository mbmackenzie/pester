"""Logging setup. Job events carry interaction/batch/client/recipient ids as structured fields.

Text is the default (readable in Dockge's log view); JSON is one setting away for log shippers. Secrets are
never passed to loggers: tokens are hashed before use and API keys live in SecretStr.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Literal

FIELDS = ("event", "interaction_id", "batch_id", "client_id", "recipient_id", "channel")


def _fields(record: logging.LogRecord) -> dict[str, object]:
    return {name: value for name in FIELDS if (value := getattr(record, name, None)) is not None}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_fields(record),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        if fields := _fields(record):
            line += " [" + " ".join(f"{k}={v}" for k, v in fields.items()) + "]"
        return line


def configure_logging(level: str = "INFO", fmt: Literal["text", "json"] = "text") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
