"""Just enough of the Telegram Bot API to test the channel against, plugged in through httpx.MockTransport."""

import asyncio
import json
from typing import Any

import httpx

Reply = httpx.Response | Exception
ACK_SECONDS = 5.0  # how long a test waits for the channel to handle an update


class FakeBotAPI:
    def __init__(self, token: str = "123456:TEST-TOKEN", username: str = "PesterTestBot") -> None:
        self.token = token
        self.username = username
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sent: dict[int, list[dict[str, Any]]] = {}  # chat id -> messages the bot sent (latest state)
        self._updates: list[dict[str, Any]] = []
        self._next_update = 1
        self._next_message: dict[int, int] = {}
        self._confirmed = 0  # updates below this id were acknowledged by a getUpdates offset
        self._arrived = asyncio.Event()
        self._acknowledged = asyncio.Event()
        self._failures: dict[str, list[Reply]] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def fail_next(self, method: str, reply: Reply) -> None:
        self._failures.setdefault(method, []).append(reply)

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == method]

    def texts(self, chat_id: int) -> list[str]:
        return [m["text"] for m in self.sent.get(chat_id, [])]

    # ---- The person's side ----------------------------------------------------------------------------

    def person_sends(
        self,
        chat_id: int,
        text: str | None = "hello",
        *,
        reply_to: int | None = None,
        first_name: str = "Kate",
        username: str | None = "kate",
        chat_type: str = "private",
        extra: dict[str, Any] | None = None,
    ) -> int:
        """Queue an incoming message; returns its update id."""
        message: dict[str, Any] = {
            "message_id": self._message_id(chat_id),
            "date": 1790000000,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {
                "id": chat_id,
                "is_bot": False,
                "first_name": first_name,
                **({"username": username} if username else {}),
            },
            **({"text": text} if text is not None else {}),
            **(extra or {}),
        }
        if reply_to is not None:
            message["reply_to_message"] = self._find(chat_id, reply_to) or {"message_id": reply_to}
        return self._queue({"message": message})

    def person_presses(self, chat_id: int, message_id: int, index: int) -> int:
        """Press a button on a message the bot sent."""
        message = self._find(chat_id, message_id)
        assert message is not None, "no such message"
        button = message["reply_markup"]["inline_keyboard"][index][0]
        query = {
            "id": f"q{self._next_update}",
            "from": {"id": chat_id, "is_bot": False, "first_name": "Kate", "username": "kate"},
            "message": dict(message),
            "data": button["callback_data"],
        }
        return self._queue({"callback_query": query})

    async def acknowledged(self, update_id: int) -> None:
        """Wait until the channel has handled ``update_id`` (it polls again with a later offset)."""
        async with asyncio.timeout(ACK_SECONDS):
            while self._confirmed <= update_id:
                self._acknowledged.clear()
                await self._acknowledged.wait()

    # ---- The Bot API ----------------------------------------------------------------------------------

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        prefix = f"/bot{self.token}/"
        if not request.url.path.startswith(prefix):
            return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
        method = request.url.path.removeprefix(prefix)
        payload: dict[str, Any] = json.loads(request.content) if request.content else {}
        self.calls.append((method, payload))
        if failures := self._failures.get(method):
            reply = failures.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        handler = getattr(self, f"_api_{method}", None)
        if handler is None:
            return _error(404, "Not Found: method not found")
        return await handler(payload)

    async def _api_getMe(self, payload: dict[str, Any]) -> httpx.Response:
        return _ok({"id": 1, "is_bot": True, "first_name": "Pester", "username": self.username})

    async def _api_deleteWebhook(self, payload: dict[str, Any]) -> httpx.Response:
        return _ok(True)

    async def _api_getUpdates(self, payload: dict[str, Any]) -> httpx.Response:
        offset = payload.get("offset")
        if offset is not None and offset > self._confirmed:
            self._confirmed = offset
            self._updates = [u for u in self._updates if u["update_id"] >= offset]
            self._acknowledged.set()
        if not self._updates:
            self._arrived.clear()
            try:
                async with asyncio.timeout(min(float(payload.get("timeout") or 0), 0.2)):
                    await self._arrived.wait()
            except TimeoutError:
                pass
        return _ok(list(self._updates))

    async def _api_sendMessage(self, payload: dict[str, Any]) -> httpx.Response:
        chat_id = int(payload["chat_id"])
        message = {
            "message_id": self._message_id(chat_id),
            "date": 1790000100,
            "chat": {"id": chat_id, "type": "private"},
            "text": payload["text"],
            **({"reply_markup": payload["reply_markup"]} if "reply_markup" in payload else {}),
            **(
                {"reply_to": payload["reply_parameters"]["message_id"]}
                if "reply_parameters" in payload
                else {}
            ),
        }
        self.sent.setdefault(chat_id, []).append(message)
        return _ok(message)

    async def _api_answerCallbackQuery(self, payload: dict[str, Any]) -> httpx.Response:
        return _ok(True)

    async def _api_editMessageText(self, payload: dict[str, Any]) -> httpx.Response:
        message = self._find(int(payload["chat_id"]), int(payload["message_id"]))
        if message is None:
            return _error(400, "Bad Request: message to edit not found")
        message["text"] = payload["text"]
        message["reply_markup"] = payload.get("reply_markup", {"inline_keyboard": []})
        return _ok(message)

    # ---- Helpers --------------------------------------------------------------------------------------

    def _queue(self, update: dict[str, Any]) -> int:
        update_id = self._next_update
        self._next_update += 1
        self._updates.append({"update_id": update_id, **update})
        self._arrived.set()
        return update_id

    def _message_id(self, chat_id: int) -> int:
        self._next_message[chat_id] = self._next_message.get(chat_id, 0) + 1
        return self._next_message[chat_id]

    def _find(self, chat_id: int, message_id: int) -> dict[str, Any] | None:
        return next((m for m in self.sent.get(chat_id, []) if m["message_id"] == message_id), None)


def _ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def _error(code: int, description: str, **parameters: Any) -> httpx.Response:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return httpx.Response(code, json=body)


def error(code: int, description: str = "error", **parameters: Any) -> httpx.Response:
    return _error(code, description, **parameters)
