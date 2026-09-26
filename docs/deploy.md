# Deploying Pester

Pester runs as a single container with a SQLite database in `/data`. The intended home is a small server
on your LAN, managed with [Dockge](https://github.com/louislam/dockge) (any Docker Compose setup works the
same way).

> Status: images are not published yet. The compose file builds the image straight from GitHub, so no
> registry is involved. When images are published, swap `build:` for `image: ghcr.io/mbmackenzie/pester`.

## Network model

Pester is **LAN-only** by design. Nothing needs to reach it from the internet: producers on your network
call its API, and it makes only outbound connections (your LLM provider, and messaging channels once they
exist). Don't port-forward 8000 or put it behind a public reverse proxy. The admin UI (coming in M7) uses a
single admin password, which is fine on a LAN and not fine on the internet.

## Dockge walkthrough

1. In Dockge, click **+ Compose**, name the stack `pester`, and paste [`compose.yaml`](../compose.yaml).
2. Until config moves into the database (M6), clients and recipients come from a YAML file. On the server,
   in the stack's directory (e.g. `/opt/stacks/pester`):

   ```sh
   mkdir -p data
   curl -o data/config.yaml https://raw.githubusercontent.com/mbmackenzie/pester/main/config.example.yaml
   ```

   Create a producer token (`pester hash-token` in a checkout, or once the stack is up:
   `docker compose exec pester pester hash-token`), put the hash in `data/config.yaml`, and uncomment
   `PESTER_CONFIG` in the compose file.
3. Optionally set `OPENAI_API_KEY` in the compose file (or in Dockge's `.env` editor) for LLM grading.
   Without it, `llm` evaluations are echoed back.
4. **Deploy.** The first deploy builds the image, which takes a minute or two.
5. Check it: `http://<server>:8000/ready` should say `"ready"`.
6. Submit a job and answer it through the mock channel:

   ```sh
   curl -H "Authorization: Bearer <token>" -H 'content-type: application/json' \
     -d '{"recipient_id":"kate","prompt":"Did you water the plants?","response_options":["Yes","No"],"evaluation":{"prompt":"YES/NO"}}' \
     http://<server>:8000/api/v1/jobs
   uv run pester chat kate --url http://<server>:8000   # from a checkout on your machine
   ```

To update, use **Update** in Dockge (or `docker compose build --pull && docker compose up -d`). Pending
work survives restarts; a message that was mid-send during a crash is never resent (see spec §12.1).

## Data and backups

Everything lives in `./data`: `pester.sqlite` (with `-wal`/`-shm` files while running) and your
`config.yaml`. To back up, either stop the stack and copy the directory, or take a consistent online copy:

```sh
docker compose exec pester python -c \
  "import sqlite3; sqlite3.connect('/data/pester.sqlite').backup(sqlite3.connect('/data/backup.sqlite'))"
```

The container runs as uid 1000. If `./data` was created by root, `chown 1000:1000 data`.

## Environment variables

| Variable | Default (container) | Meaning |
|---|---|---|
| `PESTER_CONFIG` | unset | Path to the deployment YAML (clients, recipients, personalities, pacing) |
| `PESTER_DATABASE_PATH` | `/data/pester.sqlite` | SQLite database |
| `PESTER_DEV_MODE` | `false` | Enables the mock (`fake`) channel and unauthenticated `/dev` chat routes |
| `PESTER_LOG_FORMAT` | `text` | `text` or `json` |
| `PESTER_LOG_LEVEL` | `INFO` | Python log level |
| `OPENAI_API_KEY` | unset | Enables LLM evaluation and LLM personalities |

## Health checks

- `GET /health`: liveness; the image's `HEALTHCHECK` uses it.
- `GET /ready`: 200 when the database is writable, workers are running, and at least one channel has
  started; 503 with the failing checks otherwise.

## Testing the image locally

```sh
scripts/smoke-container.sh   # builds the image and runs one job through it (docker or podman)
```
