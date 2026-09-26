"""``pester chat``: a terminal stand-in for a phone, talking to the fake channel's /dev routes.

Input syntax:
    some text        send a plain message (routed to your single open question)
    >3 some text     reply to message #3
    !3 2             press button 2 on message #3
    /skip            commands are sent as-is
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import httpx

HELP = __doc__.split("Input syntax:", 1)[1].rstrip() if __doc__ else ""

_REPLY = re.compile(r"^>(\d+)\s+(.+)$")
_PRESS = re.compile(r"^!(\d+)\s+(\d+)$")


@dataclass(frozen=True)
class ChatLine:
    text: str | None = None
    reply_to: int | None = None
    press: tuple[int, int] | None = None  # (message id, 1-based option number)


def parse_line(line: str) -> ChatLine | None:
    line = line.strip()
    if not line:
        return None
    if match := _PRESS.match(line):
        return ChatLine(press=(int(match.group(1)), int(match.group(2))))
    if match := _REPLY.match(line):
        return ChatLine(text=match.group(2), reply_to=int(match.group(1)))
    return ChatLine(text=line)


def format_message(message: dict[str, Any]) -> str:
    reply = f" (re #{message['reply_to']})" if message.get("reply_to") else ""
    lines = [f"[#{message['id']}] pester{reply}: {message['text']}"]
    if options := message.get("options"):
        lines.append("      " + "   ".join(f"{i}) {opt}" for i, opt in enumerate(options, 1)))
    return "\n".join(lines)


class ChatSession:
    def __init__(self, http: httpx.AsyncClient, address: str) -> None:
        self._http = http
        self._address = address
        self._last_seen = 0
        self._options: dict[int, list[str]] = {}

    async def poll(self) -> list[str]:
        resp = await self._http.get(f"/dev/chat/{self._address}", params={"after": self._last_seen})
        resp.raise_for_status()
        printed: list[str] = []
        for message in resp.json():
            self._last_seen = max(self._last_seen, message["id"])
            if message["direction"] == "out":
                if message.get("options"):
                    self._options[message["id"]] = message["options"]
                printed.append(format_message(message))
        return printed

    async def send(self, line: ChatLine) -> str | None:
        """Send a parsed line. Returns an error message for the user, or None on success."""
        body: dict[str, Any]
        if line.press is not None:
            message_id, number = line.press
            options = self._options.get(message_id)
            if not options or not 1 <= number <= len(options):
                return f"message #{message_id} has no option {number}"
            body = {"selected_option": options[number - 1], "reply_to": message_id}
        else:
            body = {"text": line.text, "reply_to": line.reply_to}
        resp = await self._http.post(f"/dev/chat/{self._address}", json=body)
        if resp.status_code == 404:
            return "the server has no fake channel; start it with PESTER_DEV_MODE=true"
        resp.raise_for_status()
        return None


async def run_chat(base_url: str, address: str, poll_seconds: float = 1.0) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
        session = ChatSession(http, address)
        print(f"Chatting as {address!r} with {base_url}. Ctrl-D to quit.{HELP}\n")

        async def poll_forever() -> None:
            while True:
                try:
                    for text in await session.poll():
                        print(text)
                except httpx.HTTPError as exc:
                    print(f"(poll failed: {exc})")
                await asyncio.sleep(poll_seconds)

        poller = asyncio.create_task(poll_forever())
        try:
            while True:
                try:
                    raw = await asyncio.to_thread(input)
                except EOFError:
                    return
                if (line := parse_line(raw)) is not None and (error := await session.send(line)):
                    print(f"({error})")
        finally:
            poller.cancel()
