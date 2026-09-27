# Deploying Pester

Pester runs as a single container with a SQLite database in `/data`. The intended home is a small server
on your LAN, managed with [Dockge](https://github.com/louislam/dockge) (any Docker Compose setup works the
same way).

The image is `ghcr.io/mbmackenzie/pester`, for amd64 and arm64:

| Tag | What it is |
|---|---|
| `:latest` | the newest release (what `compose.yaml` uses) |
| `:0.1.0`, `:0.1` | a specific release, or the newest patch of a minor version |
| `:main` | every change merged to `main`, as soon as CI passes |

## Network model

Pester is **LAN-only** by design. Nothing needs to reach it from the internet: producers on your network
call its API, and it makes only outbound connections (your LLM provider and your messaging channels).
Don't port-forward 8000 or put it behind a public reverse proxy. The admin UI uses a single admin password,
which is fine on a LAN and not fine on the internet.

## Dockge walkthrough

Everything after deploying happens in the browser.

1. In Dockge, click **+ Compose**, name the stack `pester`, paste [`compose.yaml`](../compose.yaml), and
   **Deploy**. Dockge pulls the image and starts it.
2. Open `http://<server>:8000/admin` and set the admin password. The setup code it asks for is in the
   stack's logs (see [Admin UI](#admin-ui)). The dashboard's **Getting started** checklist walks you through
   the rest.
3. **Add a channel.** Channels → Add a channel → `mock` (a messenger built into Pester, for trying it out).
4. **Pair yourself.** Messenger → **New address**, type a name (say `me`), and send `/start`. Pester replies
   that it has asked the admin. Recipients now shows the request: pick an id and your timezone, and
   **Approve**. A welcome message arrives in the Messenger. (To skip approval, create an invite on the
   Recipients page instead; whoever sends `/start <code>` is paired directly.)
5. **Create a client key.** Clients → New client, allowed to message `me`. The token is shown once, with a
   ready-to-run `curl` command.
6. **Go.** Run that `curl` command (or point your producer at Pester with the token), and answer the question
   in the Messenger.

**Telegram:** to reach people on their phones, add a Telegram bot as a channel: see
[docs/telegram.md](telegram.md). It takes a few minutes, and nothing needs to be exposed to the internet.

Optional: add an LLM API key under Settings (and **Test connection**) for LLM grading and LLM personalities.
Pacing is production-like by default (quiet hours, spacing, a daily cap); while trying things out, clear the
quiet hours and set the minimum interval and jitter to 0 under Settings → Pacing.

## Managing config

Every change, from the admin UI or the CLI, is saved as a new config version in the database and takes
effect within a few seconds, with no restart. The CLI is handy for scripting and for the container's
terminal (`docker compose exec pester pester …`); `pester --help` lists everything. The main commands:

| Command | What it does |
|---|---|
| `pester client create/list/update/rotate/revoke` | Producer clients and their tokens (a token is shown once) |
| `pester recipient add/list/update/link/unlink/remove` | People, their timezone and quiet hours, and how to reach them |
| `pester pairing list/approve/reject`, `pester invite create/list` | Pairing requests and one-time invite codes |
| `pester channel add/list/update/remove/secret` | Delivery channels and their secrets |
| `pester personality list/set/remove/default` | Feedback personalities |
| `pester settings show/set`, `pester llm key` | Pacing, LLM, and delivery settings; the LLM API key |
| `pester history`, `pester export`, `pester import FILE` | Config versions, and YAML backup or config-as-code |

**Secrets** (the LLM key, channel tokens) are stored apart from config and never shown again, exported, or
logged. They're read from a prompt, or from stdin for scripts (`echo "$KEY" | pester llm key`). An
environment variable, where one exists (`OPENAI_API_KEY`), takes precedence over the stored value.

**Config as code.** `pester export -o config.yaml` writes the current config (without secrets);
`pester import config.yaml` replaces it. Setting `PESTER_CONFIG` to a file seeds an **empty** database on
first start; after that the file is ignored (Pester logs a warning if it differs), so edits in the UI or
CLI are never silently overwritten.

## Admin UI

Open `http://<server>:8000/admin`. On first visit it asks for a **setup code**, which Pester prints in its
logs at startup (Dockge's log view, or `docker compose logs pester`):

```text
WARNING pester.admin.auth: Admin UI is not set up. Open /admin/setup and enter code K7QF-2MXD
```

Then choose the admin password. From there you can manage everything: channels (with forms generated from
each channel type's settings, and write-only secrets), recipients and pairing requests, invites, clients and
their tokens, personalities (with a live preview), pacing, the LLM and its key, and config history. There's
also a dashboard (queue counts, health, recent activity), a job browser with each job's full timeline and
evaluation audit, and a **Messenger** page where you can be a recipient on a mock channel.

Forgot the password? `docker compose exec pester pester admin reset-password`, restart, and set a new one.

## Updating

To update, use **Update** in Dockge (or `docker compose pull && docker compose up -d`). The running version
is shown at the bottom of the admin UI's sidebar and in `GET /health`. Database migrations run
automatically at startup; back up `./data` before a big jump. Pending
work survives restarts; a message that was mid-send during a crash is never resent (see spec §12.1).

## Data and backups

Everything lives in `./data/pester.sqlite` (with `-wal`/`-shm` files while running): config and its
history, secrets, jobs, events, and the mock channel's conversations. Treat it as sensitive. To back up,
either stop the stack and copy the directory, or take a consistent online copy:

```sh
docker compose exec pester python -c \
  "import sqlite3; sqlite3.connect('/data/pester.sqlite').backup(sqlite3.connect('/data/backup.sqlite'))"
```

`pester export` is a readable backup of config alone (without secrets).

The container runs as uid 1000. If `./data` was created by root, `chown 1000:1000 data`.

## Environment variables

| Variable | Default (container) | Meaning |
|---|---|---|
| `PESTER_DATABASE_PATH` | `/data/pester.sqlite` | SQLite database |
| `PESTER_CONFIG` | unset | YAML file that seeds an empty database on first start |
| `PESTER_LOG_FORMAT` | `text` | `text` or `json` |
| `PESTER_LOG_LEVEL` | `INFO` | Python log level |
| `OPENAI_API_KEY` | unset | LLM API key; overrides one stored with `pester llm key` |
| `TELEGRAM_BOT_TOKEN` | unset | Telegram bot token; overrides one saved in a `telegram` channel's settings |
| `PESTER_DEV_MODE` | `false` | Development only: the echo evaluator, a mock channel if none is configured, and unauthenticated `/dev` chat routes |

## Health checks

- `GET /health`: liveness; the image's `HEALTHCHECK` uses it.
- `GET /ready`: 200 when the database is writable, workers are running, and every enabled channel has
  started; 503 with the failing checks (including each channel's error) otherwise.

## Testing the image locally

```sh
scripts/smoke-container.sh   # builds the image, configures it with the CLI, and runs one job through it
```

## Releasing (maintainers)

Every merge to `main` publishes `:main` once CI passes. To cut a release:

1. Bump `version` in `pyproject.toml` (and `uv lock`), merge it.
2. Tag that commit and push the tag: `git tag v0.2.0 && git push origin v0.2.0`.
3. The **Publish image** workflow checks the tag matches `pyproject.toml`, runs CI, and pushes `:0.2.0`, `:0.2`,
   and `:latest`. Optionally, write release notes: `gh release create v0.2.0 --generate-notes`.

The package must be public for Dockge to pull it without logging in. That's a one-time setting on GitHub:
the package's page → **Package settings** → **Change visibility** → Public.
