# Deploying Pester

Pester runs as a single container with a SQLite database in `/data`. The intended home is a small server
on your LAN, managed with [Dockge](https://github.com/louislam/dockge) (any Docker Compose setup works the
same way).

> Status: images are not published yet. The compose file builds the image straight from GitHub, so no
> registry is involved. When images are published, swap `build:` for `image: ghcr.io/mbmackenzie/pester`.

## Network model

Pester is **LAN-only** by design. Nothing needs to reach it from the internet: producers on your network
call its API, and it makes only outbound connections (your LLM provider and your messaging channels).
Don't port-forward 8000 or put it behind a public reverse proxy. The admin UI uses a single admin password,
which is fine on a LAN and not fine on the internet.

## Dockge walkthrough

1. In Dockge, click **+ Compose**, name the stack `pester`, paste [`compose.yaml`](../compose.yaml), and
   **Deploy**. The first deploy builds the image, which takes a minute or two.
2. Open `http://<server>:8000/admin` and set the admin password. The setup code it asks for is in the
   stack's logs (see [Admin UI](#admin-ui)).
3. Configure Pester. Config lives in the database, so there's no file to edit. Until the admin UI's edit
   forms land, use the CLI inside the container. In Dockge, open the stack's terminal (or run
   `docker compose exec pester sh` on the server):

   ```sh
   pester channel add mock --type mock          # a messenger built into Pester, for trying it out
   pester llm key                               # optional: paste an OpenAI key for LLM grading
   ```

4. **Pair yourself.** In the admin UI, open **Messenger**, choose **New address**, type a name (say `me`),
   and send `/start`. Pester replies that it has asked the admin, and the request appears under
   **Recipients**. Approve it:

   ```sh
   pester pairing list
   pester pairing approve 1 --as me --timezone America/New_York
   ```

   A welcome message arrives in the Messenger within a few seconds. (To skip approval, give someone an
   invite instead: `pester invite create me`, then they send `/start <code>`.)

5. **Create a client key** for the producer that will send jobs:

   ```sh
   pester client create study-app --recipient me    # prints the token once
   ```

6. **Go.** Submit a job with that token, and answer it in the Messenger:

   ```sh
   curl -H "Authorization: Bearer <token>" -H 'content-type: application/json' \
     -d '{"recipient_id":"me","prompt":"Did you water the plants?","response_options":["Yes","No"],"evaluation":{"evaluator":"rule","prompt":"match"}}' \
     http://<server>:8000/api/v1/jobs
   ```

   With an LLM key set, use `"evaluation": {"prompt": "..."}` for LLM grading instead of `rule`. Pacing is
   production-like by default (quiet hours, spacing, a daily cap); to see questions right away while
   trying it out: `pester settings set scheduler.quiet_hours=null scheduler.min_interval_minutes=0
   scheduler.jitter_minutes=0`.

## Managing config

Every change is saved as a new config version in the database and takes effect within a few seconds, with
no restart. `pester --help` lists everything; the main commands:

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

Then choose the admin password. The UI has a dashboard (queue counts, health, recent activity), a job
browser with each job's full timeline and evaluation audit, a **Messenger** page where you can be a
recipient on a mock channel, personality preview, pending pairing requests, and pause/resume per recipient.
Clients, recipients, channels, and settings are read-only there for now, with the CLI command for each.

Forgot the password? `docker compose exec pester pester admin reset-password`, restart, and set a new one.

## Updating

To update, use **Update** in Dockge (or `docker compose build --pull && docker compose up -d`). Pending
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
| `PESTER_DEV_MODE` | `false` | Development only: the echo evaluator, a mock channel if none is configured, and unauthenticated `/dev` chat routes |

## Health checks

- `GET /health`: liveness; the image's `HEALTHCHECK` uses it.
- `GET /ready`: 200 when the database is writable, workers are running, and every enabled channel has
  started; 503 with the failing checks (including each channel's error) otherwise.

## Testing the image locally

```sh
scripts/smoke-container.sh   # builds the image, configures it with the CLI, and runs one job through it
```
