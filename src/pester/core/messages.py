"""Channel-agnostic message types exchanged with delivery channels (spec §9)."""

from typing import Any

from pydantic import BaseModel, ConfigDict

from pester.core.models import UtcDatetime


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Command(_Model):
    """A recipient command such as ``/skip`` or ``/snooze 2h``."""

    name: str
    args: str = ""


class OutboundMessage(_Model):
    text: str
    options: list[str] | None = None
    reply_to_external_id: str | None = None


class SentReceipt(_Model):
    external_id: str
    sent_at: UtcDatetime


class InboundMessage(_Model):
    channel: str
    external_id: str  # unique per (channel, sender_address), like Telegram message ids per chat
    sender_address: str
    sender_name: str | None = None  # how the channel names the sender, if it knows (shown when pairing)
    text: str | None = None
    command: Command | None = None
    selected_option: str | None = None
    reply_to_external_id: str | None = None
    received_at: UtcDatetime
    raw: dict[str, Any] | None = None


def parse_command(text: str) -> Command | None:
    if not text.startswith("/"):
        return None
    name, _, args = text[1:].partition(" ")
    name = name.strip().lower()
    if not name.isidentifier():
        return None
    return Command(name=name, args=args.strip())
