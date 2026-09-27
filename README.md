# pester

A small, generic service for asynchronously asking people things.

Producers submit self-contained interaction jobs: a prompt, evaluation instructions, and delivery constraints. Pester decides when to deliver each one, sends it through a messaging channel, captures the reply, evaluates it with an LLM (or rules), sends feedback in a configurable personality, and exposes everything as a cursor-polled event stream.

```text
Jobs in. Humans bothered. Responses evaluated. Events out.
```

Pester is domain-agnostic. It doesn't know about quizzes, habits, or reminders; that meaning belongs to producers.

- **Spec:** [docs/spec.md](docs/spec.md)
- **Deploying:** [docs/deploy.md](docs/deploy.md) (Docker Compose / Dockge, LAN-only)
- **Telegram:** [docs/telegram.md](docs/telegram.md) (create a bot and connect it)
- **Roadmap:** milestone issues M0–M8 on GitHub

Status: pre-alpha. The full loop runs locally on a built-in mock channel, with LLM grading, pluggable personalities and channels, realistic pacing, crash recovery, config in the database with live changes, pairing, and a LAN-only admin UI at `/admin` (design: [docs/admin-ui.md](docs/admin-ui.md)).

## Try it locally

Config lives in a SQLite database and is managed in the admin UI (`/admin`) or with the `pester` CLI; no
config file is needed. The steps below use the CLI; [docs/deploy.md](docs/deploy.md) does the same in the browser. Dev mode adds the echo evaluator, so `llm` evaluations work without an API key (add
`OPENAI_API_KEY=...` to a `.env` file, gitignored, for real grading and LLM personalities).

```sh
uv sync
uv run pester channel add mock --type mock                     # a messenger built into Pester
uv run pester recipient add kate --timezone America/New_York
uv run pester recipient link kate mock address=kate
uv run pester client create my-app --recipient kate            # prints a token, once
# Pacing is production-like by default (quiet hours, 2h spacing, jitter). To see questions right away:
uv run pester settings set scheduler.quiet_hours=null scheduler.min_interval_minutes=0 scheduler.jitter_minutes=0

PESTER_DEV_MODE=true uv run pester serve

# in another terminal: submit a job
curl -H "Authorization: Bearer <token>" -H 'content-type: application/json' \
  -d '{"recipient_id":"kate","prompt":"Did you water the plants?","response_options":["Yes","No"],"evaluation":{"prompt":"YES/NO"}}' \
  localhost:8000/api/v1/jobs

# be kate (try /send, /status, /skip, /snooze 2h, /pause, /resume): open http://localhost:8000/admin (the setup
# code is in the server log) and use the Messenger page, or chat from a terminal:
uv run pester chat kate

# see what happened
curl -H "Authorization: Bearer <token>" localhost:8000/api/v1/events

# which personalities can a job use? (set "personality_id" on the job)
curl -H "Authorization: Bearer <token>" localhost:8000/api/v1/personalities

# try an evaluation prompt and personality on a sample reply, without sending anything
curl -H "Authorization: Bearer <token>" -H 'content-type: application/json' \
  -d '{"job":{"recipient_id":"kate","prompt":"Why WAL?","personality_id":"weather-goblin","evaluation":{"prompt":"Score 0-1"}},"response":{"text":"it appends"}}' \
  localhost:8000/api/v1/jobs:preview
```

Instead of the CLI steps, `pester import config.example.yaml` loads a whole example deployment (paste a
token hash from `pester hash-token` into it first), and `pester export` prints the current config.
Personalities come as built-in `neutral`, `template`, and `llm` types, or your own class via an import path
like `my_package.voices:Pirate`; channels likewise (`mock`, or `my_package.chat:Adapter`). Someone new can
pair themselves: from an unknown address, send `/start` and approve the request with `pester pairing`.

## Development

```sh
uv run pytest && uv run ruff check && uv run pyright
uv run pytest -m live   # optional: real LLM calls using OPENAI_API_KEY from .env (costs a few cents)
```

Tests never read `.env` or call a real LLM unless you opt in with `-m live`.
