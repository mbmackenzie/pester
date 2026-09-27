"""The Telegram channel against a fake Bot API."""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from pester.core.messages import InboundMessage, OutboundMessage
from pester.delivery.base import ChannelError, PermanentChannelError
from pester.delivery.telegram import MAX_TEXT, NON_TEXT_REPLY, SendOutcomeUnknownError, TelegramChannel
from tests.fake_telegram import FakeBotAPI, error

CHAT = 424242


def make(api: FakeBotAPI, token: str | None = None) -> TelegramChannel:
    return TelegramChannel(
        "telegram", token or api.token, poll_seconds=1, transport=api.transport(), retry_seconds=0.01
    )


@pytest.fixture
def api() -> FakeBotAPI:
    return FakeBotAPI()


@pytest.fixture
async def running(api: FakeBotAPI) -> AsyncIterator[tuple[TelegramChannel, list[InboundMessage]]]:
    inbox: list[InboundMessage] = []

    async def handler(message: InboundMessage) -> None:
        inbox.append(message)

    channel = make(api)
    await channel.start(handler)
    yield channel, inbox
    await channel.stop()


async def ignore(message: InboundMessage) -> None:
    pass


# ---- Startup --------------------------------------------------------------------------------------------


async def test_start_checks_the_token_and_clears_any_webhook(
    running: tuple[TelegramChannel, list[InboundMessage]], api: FakeBotAPI
) -> None:
    channel, _ = running
    assert channel.username == "PesterTestBot"
    assert channel.status_note == "connected as @PesterTestBot"
    assert channel.invite_link("ABCD-EFGH") == "https://t.me/PesterTestBot?start=ABCD-EFGH"
    assert api.calls_to("deleteWebhook") == [{"drop_pending_updates": False}]


async def test_a_bad_token_fails_to_start_without_leaking_it(api: FakeBotAPI) -> None:
    channel = make(api, token="999:WRONG-SECRET")
    with pytest.raises(ChannelError, match="rejected the bot token") as exc:
        await channel.start(ignore)
    assert "WRONG-SECRET" not in str(exc.value)


async def test_unreachable_telegram_does_not_leak_the_token(api: FakeBotAPI) -> None:
    api.fail_next("getMe", httpx.ConnectError(f"cannot connect to /bot{api.token}/getMe"))
    with pytest.raises(ChannelError) as exc:
        await make(api).start(ignore)
    assert api.token not in str(exc.value)
    assert "<token>" in str(exc.value)


# ---- Receiving ------------------------------------------------------------------------------------------


async def test_text_messages(running: tuple[TelegramChannel, list[InboundMessage]], api: FakeBotAPI) -> None:
    _, inbox = running
    await api.acknowledged(api.person_sends(CHAT, "hello", first_name="Kate", username="kate"))
    (message,) = inbox
    assert (message.channel, message.sender_address, message.text) == ("telegram", str(CHAT), "hello")
    assert message.sender_name == "Kate (@kate)"
    assert message.external_id == "1"
    assert message.received_at.tzinfo is not None


async def test_commands_addressed_to_the_bot(
    running: tuple[TelegramChannel, list[InboundMessage]], api: FakeBotAPI
) -> None:
    _, inbox = running
    await api.acknowledged(api.person_sends(CHAT, "/start@PesterTestBot ABCD-EFGH"))
    command = inbox[-1].command
    assert command is not None and (command.name, command.args) == ("start", "ABCD-EFGH")


async def test_group_chats_are_ignored(
    running: tuple[TelegramChannel, list[InboundMessage]], api: FakeBotAPI
) -> None:
    _, inbox = running
    await api.acknowledged(api.person_sends(-1001, "hi all", chat_type="group"))
    assert inbox == []


async def test_non_text_messages_get_a_reply(
    running: tuple[TelegramChannel, list[InboundMessage]], api: FakeBotAPI
) -> None:
    _, inbox = running
    await api.acknowledged(api.person_sends(CHAT, None, extra={"sticker": {"file_id": "x"}}))
    assert inbox == []
    assert api.texts(CHAT) == [NON_TEXT_REPLY]


async def test_button_press(running: tuple[TelegramChannel, list[InboundMessage]], api: FakeBotAPI) -> None:
    channel, inbox = running
    receipt = await channel.send(str(CHAT), OutboundMessage(text="Water the plants?", options=["Yes", "No"]))
    await api.acknowledged(api.person_presses(CHAT, int(receipt.external_id), 0))
    message = inbox[-1]
    assert message.selected_option == "Yes"
    assert message.reply_to_external_id == receipt.external_id
    assert message.external_id.startswith("cb:")
    assert len(api.calls_to("answerCallbackQuery")) == 1
    prompt = api.sent[CHAT][0]
    assert prompt["text"] == "Water the plants?\n\n→ Yes"  # the choice is shown...
    assert prompt["reply_markup"] == {"inline_keyboard": []}  # ...and the buttons are gone


async def test_options_longer_than_callback_data_allows(
    running: tuple[TelegramChannel, list[InboundMessage]], api: FakeBotAPI
) -> None:
    channel, inbox = running
    long_option = "An answer much longer than Telegram's sixty-four byte callback data limit, clearly"
    receipt = await channel.send(str(CHAT), OutboundMessage(text="Pick", options=["short", long_option]))
    await api.acknowledged(api.person_presses(CHAT, int(receipt.external_id), 1))
    assert inbox[-1].selected_option == long_option


async def test_a_failing_update_is_retried_not_lost(api: FakeBotAPI) -> None:
    attempts: list[str] = []

    async def flaky(message: InboundMessage) -> None:
        attempts.append(message.external_id)
        if len(attempts) == 1:
            raise RuntimeError("database is locked")

    channel = make(api)
    await channel.start(flaky)
    try:
        await api.acknowledged(api.person_sends(CHAT, "hello"))
    finally:
        await channel.stop()
    assert attempts == ["1", "1"]  # handled again (inbound handling is idempotent)


async def test_polling_survives_errors(
    running: tuple[TelegramChannel, list[InboundMessage]], api: FakeBotAPI
) -> None:
    _, inbox = running
    api.fail_next("getUpdates", error(502, "Bad Gateway"))
    api.fail_next("getUpdates", httpx.ReadTimeout("slow"))
    await api.acknowledged(api.person_sends(CHAT, "still there?"))
    assert inbox[-1].text == "still there?"


async def test_stop_is_clean(api: FakeBotAPI) -> None:
    channel = make(api)
    await channel.start(ignore)
    await asyncio.sleep(0.05)
    await channel.stop()
    with pytest.raises(ChannelError, match="not started"):
        await channel.send(str(CHAT), OutboundMessage(text="hi"))


# ---- Sending --------------------------------------------------------------------------------------------


async def test_send(running: tuple[TelegramChannel, list[InboundMessage]], api: FakeBotAPI) -> None:
    channel, _ = running
    receipt = await channel.send(
        str(CHAT), OutboundMessage(text="Good job.", reply_to_external_id="7", options=["More", "Stop"])
    )
    (payload,) = api.calls_to("sendMessage")
    assert payload["chat_id"] == CHAT
    assert payload["reply_parameters"] == {"message_id": 7, "allow_sending_without_reply": True}
    assert payload["reply_markup"]["inline_keyboard"] == [
        [{"text": "More", "callback_data": "o0"}],
        [{"text": "Stop", "callback_data": "o1"}],
    ]
    assert receipt.external_id == "1"


async def test_replies_to_button_presses_are_not_threaded(
    running: tuple[TelegramChannel, list[InboundMessage]], api: FakeBotAPI
) -> None:
    channel, _ = running
    await channel.send(str(CHAT), OutboundMessage(text="Recorded.", reply_to_external_id="cb:q9"))
    assert "reply_parameters" not in api.calls_to("sendMessage")[0]


async def test_long_messages_are_cut_to_telegrams_limit(
    running: tuple[TelegramChannel, list[InboundMessage]], api: FakeBotAPI
) -> None:
    channel, _ = running
    await channel.send(str(CHAT), OutboundMessage(text="x" * (MAX_TEXT + 100)))
    text = api.calls_to("sendMessage")[0]["text"]
    assert len(text) == MAX_TEXT and text.endswith("…")


@pytest.mark.parametrize(
    ("failure", "raised"),
    [
        (httpx.ConnectError("no route"), ChannelError),  # never sent: retry
        (error(429, "Too Many Requests", retry_after=3), ChannelError),
        (error(502, "Bad Gateway"), ChannelError),
        (error(403, "Forbidden: bot was blocked by the user"), PermanentChannelError),
        (error(400, "Bad Request: chat not found"), PermanentChannelError),
        (error(500, "Internal Server Error"), SendOutcomeUnknownError),  # might have been sent: never resend
        (httpx.ReadTimeout("no answer"), SendOutcomeUnknownError),
    ],
)
async def test_send_failures_are_classified_by_what_they_mean(
    running: tuple[TelegramChannel, list[InboundMessage]],
    api: FakeBotAPI,
    failure: httpx.Response | Exception,
    raised: type[Exception],
) -> None:
    channel, _ = running
    api.fail_next("sendMessage", failure)
    with pytest.raises(raised) as exc:
        await channel.send(str(CHAT), OutboundMessage(text="hi"))
    if raised is SendOutcomeUnknownError:
        assert not isinstance(exc.value, ChannelError)  # Pester retries only ChannelErrors
    if raised is ChannelError:
        assert not isinstance(exc.value, PermanentChannelError)


def test_addresses() -> None:
    channel = TelegramChannel("telegram", "t")
    assert channel.address_of({"chat_id": 123}) == "123"
    assert channel.address_of({"chat_id": "123"}) == "123"
    assert channel.recipient_config_for("123") == {"chat_id": 123}
    for bad in ({}, {"chat_id": "abc"}, {"chat_id": True}, {"user_id": 5}):
        with pytest.raises(ValueError, match="chat_id"):
            channel.address_of(bad)
