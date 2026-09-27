# Telegram

Pester can reach people through a Telegram bot that you create. It uses long polling: Pester asks Telegram
for new messages, so nothing on your network has to be reachable from the internet. It only needs outbound
HTTPS to `api.telegram.org`.

## 1. Create the bot (2 minutes, in Telegram)

1. In Telegram, open a chat with **[@BotFather](https://t.me/BotFather)** (Telegram's official bot for
   making bots; check for the blue verified tick).
2. Send `/newbot`. BotFather asks for:
   - a **name**, shown in chats (e.g. `Pester`), and
   - a **username**, which must end in `bot` (e.g. `matts_pester_bot`). This is how people find it:
     `t.me/matts_pester_bot`.
3. BotFather replies with a **token** like `8012345678:AAH...`. Copy it. Treat it like a password: anyone
   with it can act as your bot.

Optional, but nice: send BotFather `/setcommands`, pick your bot, and paste this, so Telegram shows a menu
of Pester's commands:

```text
send - Send me a question now
status - What's waiting for me
skip - Skip the open question
snooze - Ask me again later (e.g. /snooze 2h)
pause - Stop sending questions
resume - Start sending questions again
```

`/setdescription` and `/setuserpic` set what people see before they press Start.

## 2. Connect it to Pester

In the admin UI: **Channels → Add a channel → telegram**. Paste the token into **Bot Token** and click **Add
channel**. Within a second or two the channel shows **running · connected as @your_bot**. If it says
Telegram rejected the token, check you copied all of it.

The token is stored as a secret: it's never shown again, exported, or logged. If you'd rather keep it out of
the database, set `TELEGRAM_BOT_TOKEN` in the compose file instead and leave the field blank.

(From the command line: `pester channel add telegram --type telegram`, then
`pester channel secret telegram bot_token` and paste it.)

## 3. Pair people

Telegram bots can't message someone first: each person has to open the bot and press **Start** once.

- **With approval:** they open `t.me/your_bot` and press Start. Pester replies that it has asked you, and the
  request shows up under **Recipients** with their Telegram name. Approve it as a new recipient (or as an
  existing one, to add Telegram to someone who already uses another channel).
- **With an invite:** Recipients → **Invite someone**. Along with the `/start` code, the invite page shows a
  one-tap link like `https://t.me/your_bot?start=ABCD-EFGH`. Opening it and pressing Start pairs them
  directly, with no approval step. It works once, within its expiry.

After that, questions arrive as normal messages. Answer options appear as buttons; the chosen answer is
shown on the question and the buttons disappear, so nobody answers twice. People can also reply in text,
and use the commands above.

## Good to know

- **Only private chats.** Pester ignores group chats.
- **Only text.** Photos, stickers, and voice notes get a polite "I can only read text messages for now."
- **One Pester per bot.** Telegram delivers each bot's messages to a single poller. If two Pester instances
  use the same token, they fight over updates (you'll see polling warnings in the logs). Make a second bot for
  a second instance.
- **Privacy.** Bot chats go through Telegram's servers and aren't end-to-end encrypted. Pester stores
  messages in its own database as usual.
- **A leaked token:** send BotFather `/revoke`, pick the bot, and paste the new token into the channel's
  settings in Pester. People stay paired: pairing is tied to their chat, not to the token.

## How delivery failures are handled

Pester never risks sending someone the same question twice. When Telegram can't be reached or is rate
limiting, the message definitely wasn't sent, so Pester retries with backoff. When Telegram refuses (the
person blocked the bot, or the chat doesn't exist), it doesn't retry. When a request times out after it was
sent, Pester can't know whether it arrived, so it marks that delivery failed rather than risk a duplicate.
