"""The ``telegram`` channel adapter: a Telegram bot, over the Bot API with long polling.

Long polling means Pester only makes outbound requests; nothing has to be reachable from the internet.
Pester talks to the Bot API directly (rather than through a bot framework) because delivery safety depends
on knowing exactly what a failure means: a message that definitely wasn't sent may be retried, but one whose
fate is unknown must never be sent again (spec §9.1).

A recipient is reached through their private chat with the bot: ``{chat_id: 123456789}``. Group chats are
ignored. Pairing works as for any channel: a person presses Start (``/start``) and the admin approves them,
or they open an invite link (``t.me/<bot>?start=<code>``).
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from pester.core.messages import InboundMessage, OutboundMessage, SentReceipt, parse_command
from pester.delivery.adapters import ChannelServices
from pester.delivery.base import ChannelError, InboundHandler, PermanentChannelError

log = logging.getLogger(__name__)

MAX_TEXT = 4096  # Telegram's limit for one message
NON_TEXT_REPLY = "I can only read text messages for now."
_MAX_HANDLE_ATTEMPTS = 3


class TelegramOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot_token: SecretStr = Field(
        description="From @BotFather: /newbot, then copy the token it gives you.",
        json_schema_extra={"env": "TELEGRAM_BOT_TOKEN"},
    )
    api_base: str = Field(
        default="https://api.telegram.org", description="Change only if you run your own Bot API server."
    )
    poll_seconds: int = Field(default=30, ge=1, le=50, description="How long each long-poll request waits.")


class TelegramAdapter:
    description = "A Telegram bot. Create one with @BotFather and paste its token."
    options_model = TelegramOptions

    def create(self, name: str, options: Any, services: ChannelServices) -> "TelegramChannel":
        assert isinstance(options, TelegramOptions)
        return TelegramChannel(
            name,
            options.bot_token.get_secret_value(),
            api_base=options.api_base,
            poll_seconds=options.poll_seconds,
        )


class SendOutcomeUnknownError(Exception):
    """The request may have reached Telegram. Not a ChannelError, so Pester never resends it."""


class TelegramChannel:
    def __init__(
        self,
        name: str,
        token: str,
        *,
        api_base: str = "https://api.telegram.org",
        poll_seconds: int = 30,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_seconds: float = 1.0,
    ) -> None:
        self._name = name
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._poll_seconds = poll_seconds
        self._transport = transport  # tests use a fake Bot API
        self._retry_seconds = retry_seconds
        self._client: httpx.AsyncClient | None = None
        self._handler: InboundHandler | None = None
        self._task: asyncio.Task[None] | None = None
        self.username: str | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def status_note(self) -> str | None:
        """Shown in the admin UI next to the channel's status."""
        return f"connected as @{self.username}" if self.username else None

    def invite_link(self, code: str) -> str | None:
        """A link that opens the bot and sends ``/start <code>``."""
        return f"https://t.me/{self.username}?start={code}" if self.username else None

    def address_of(self, recipient_config: Any) -> str:
        chat_id = recipient_config.get("chat_id")
        if (
            isinstance(chat_id, bool)
            or not isinstance(chat_id, int | str)
            or not str(chat_id).lstrip("-").isdigit()
        ):
            raise ValueError(f"{self._name} channel config requires a numeric 'chat_id'")
        return str(int(chat_id))

    def recipient_config_for(self, address: str) -> dict[str, Any]:
        return {"chat_id": int(address)}

    # ---- Lifecycle ------------------------------------------------------------------------------------

    async def start(self, on_inbound: InboundHandler) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"{self._api_base}/bot{self._token}/",
            timeout=httpx.Timeout(10.0, read=self._poll_seconds + 10.0),
            transport=self._transport,
        )
        try:
            me = await self._call("getMe")
            self.username = str(me.get("username") or "")
            await self._call("deleteWebhook", drop_pending_updates=False)  # a webhook blocks long polling
        except BaseException:
            await self._client.aclose()
            self._client = None
            raise
        self._handler = on_inbound
        self._task = asyncio.create_task(self._poll(), name=f"telegram-{self._name}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._handler = None

    # ---- Sending --------------------------------------------------------------------------------------

    async def send(self, address: str, message: OutboundMessage) -> SentReceipt:
        payload: dict[str, Any] = {"chat_id": int(address), "text": _fit(message.text)}
        reply_to = message.reply_to_external_id
        if reply_to and reply_to.isdigit():  # button presses have no message id of their own
            payload["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        if message.options:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": o, "callback_data": f"o{i}"}] for i, o in enumerate(message.options)
                ]
            }
        result = await self._call("sendMessage", **payload)
        return SentReceipt(
            external_id=str(result["message_id"]), sent_at=datetime.fromtimestamp(result["date"], UTC)
        )

    async def _call(self, method: str, **payload: Any) -> Any:
        """Call a Bot API method and classify failures by what they mean for delivery."""
        if self._client is None:
            raise ChannelError(f"{self._name} channel is not started")
        try:
            response = await self._client.post(method, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise ChannelError(f"can't reach Telegram: {self._redact(repr(exc))}") from None
        except httpx.HTTPError as exc:
            raise SendOutcomeUnknownError(f"Telegram didn't answer: {self._redact(repr(exc))}") from None
        try:
            data = cast(dict[str, Any], response.json())
        except ValueError:
            data = {}
        if data.get("ok"):
            return data["result"]
        code = int(data.get("error_code") or response.status_code)
        description = str(data.get("description") or response.reason_phrase)
        if code == 429:
            retry_after = cast(dict[str, Any], data.get("parameters") or {}).get("retry_after")
            raise ChannelError(f"rate limited by Telegram (retry after {retry_after}s)")
        if code in (401, 404):
            raise ChannelError("Telegram rejected the bot token (check it in the channel settings)")
        if code in (400, 403):
            raise PermanentChannelError(f"Telegram refused: {description}")  # e.g. the person blocked the bot
        if code in (502, 503, 504):
            raise ChannelError(f"Telegram is unavailable ({code})")  # a gateway error: never processed
        raise SendOutcomeUnknownError(f"Telegram error {code}: {description}")

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<token>")

    # ---- Receiving ------------------------------------------------------------------------------------

    async def _poll(self) -> None:
        offset: int | None = None
        delay = self._retry_seconds
        failures: dict[int, int] = {}
        while True:
            try:
                updates = await self._call(
                    "getUpdates",
                    offset=offset,
                    timeout=self._poll_seconds,
                    allowed_updates=["message", "callback_query"],
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s: polling failed: %s", self._name, exc, extra={"channel": self._name})
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            delay = self._retry_seconds
            for update in cast(list[dict[str, Any]], updates):
                update_id = int(update["update_id"])
                try:
                    await self._handle(update)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Handling is idempotent (inbound messages are deduplicated), so retry this update on the
                    # next poll instead of losing it, unless it keeps failing.
                    failures[update_id] = failures.get(update_id, 0) + 1
                    if failures[update_id] < _MAX_HANDLE_ATTEMPTS:
                        log.exception("%s: failed to handle update %d; will retry", self._name, update_id)
                        await asyncio.sleep(delay)
                        break
                    log.exception("%s: giving up on update %d", self._name, update_id)
                failures.pop(update_id, None)
                offset = update_id + 1

    async def _handle(self, update: dict[str, Any]) -> None:
        if (message := update.get("message")) is not None:
            await self._handle_message(cast(dict[str, Any], message))
        elif (query := update.get("callback_query")) is not None:
            await self._handle_button(cast(dict[str, Any], query))

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat = cast(dict[str, Any], message["chat"])
        if chat.get("type") != "private" or self._handler is None:
            return
        text = message.get("text")
        if not isinstance(text, str):
            await self._reply_best_effort(chat["id"], message["message_id"], NON_TEXT_REPLY)
            return
        reply_to = cast(dict[str, Any] | None, message.get("reply_to_message"))
        await self._handler(
            InboundMessage(
                channel=self._name,
                external_id=str(message["message_id"]),
                sender_address=str(chat["id"]),
                sender_name=_display_name(message.get("from")),
                text=text,
                command=parse_command(_strip_bot_mention(text, self.username)),
                reply_to_external_id=str(reply_to["message_id"]) if reply_to else None,
                received_at=datetime.fromtimestamp(message["date"], UTC),
                raw=message,
            )
        )

    async def _handle_button(self, query: dict[str, Any]) -> None:
        message = cast(dict[str, Any] | None, query.get("message"))
        with contextlib.suppress(Exception):
            await self._call("answerCallbackQuery", callback_query_id=query["id"])  # stops the button spinner
        if message is None or cast(dict[str, Any], message["chat"]).get("type") != "private":
            return
        option = _option_for(query.get("data"), message)
        if option is None or self._handler is None:
            return
        chat_id = message["chat"]["id"]
        # Show the choice and remove the buttons, so it's clear what was picked and it can't be pressed twice.
        with contextlib.suppress(Exception):
            await self._call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message["message_id"],
                text=_fit(f"{message.get('text', '')}\n\n→ {option}"),
                reply_markup={"inline_keyboard": []},
            )
        await self._handler(
            InboundMessage(
                channel=self._name,
                external_id=f"cb:{query['id']}",
                sender_address=str(chat_id),
                sender_name=_display_name(query.get("from")),
                selected_option=option,
                reply_to_external_id=str(message["message_id"]),
                received_at=datetime.now(UTC),
                raw=query,
            )
        )

    async def _reply_best_effort(self, chat_id: int, message_id: int, text: str) -> None:
        with contextlib.suppress(Exception):
            await self._call(
                "sendMessage",
                chat_id=chat_id,
                text=text,
                reply_parameters={"message_id": message_id, "allow_sending_without_reply": True},
            )


def _fit(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def _display_name(user: object) -> str | None:
    if not isinstance(user, dict):
        return None
    fields = cast(dict[str, Any], user)
    name = " ".join(str(fields[k]) for k in ("first_name", "last_name") if fields.get(k))
    if username := fields.get("username"):
        name = f"{name} (@{username})" if name else f"@{username}"
    return name or None


def _strip_bot_mention(text: str, username: str | None) -> str:
    """``/start@PesterBot CODE`` → ``/start CODE`` (Telegram adds the bot's name in some clients)."""
    if not text.startswith("/"):
        return text
    head, sep, rest = text.partition(" ")
    command, at, bot = head.partition("@")
    if at and (username is None or bot.lower() == username.lower()):
        return command + sep + rest
    return text


def _option_for(data: object, message: dict[str, Any]) -> str | None:
    """The pressed button's text. ``callback_data`` holds its index, which fits Telegram's 64-byte limit."""
    if not isinstance(data, str) or not data.startswith("o") or not data[1:].isdigit():
        return None
    index = int(data[1:])
    markup = cast(dict[str, Any], message.get("reply_markup") or {})
    buttons = [
        b for row in cast(list[list[dict[str, Any]]], markup.get("inline_keyboard") or []) for b in row
    ]
    for button in buttons:
        if button.get("callback_data") == data:
            return str(button.get("text"))
    return None if index >= len(buttons) else str(buttons[index].get("text"))
