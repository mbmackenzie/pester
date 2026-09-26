# pester

A small, generic service for asynchronously asking people things.

Producers submit self-contained interaction jobs: a prompt, evaluation instructions, and delivery constraints. Pester decides when to deliver each one, sends it through a messaging channel, captures the reply, evaluates it with an LLM (or rules), sends feedback in a configurable personality, and exposes everything as a cursor-polled event stream.

```text
Jobs in. Humans bothered. Responses evaluated. Events out.
```

Pester is domain-agnostic. It doesn't know about quizzes, habits, or reminders; that meaning belongs to producers.

- **Spec:** [docs/spec.md](docs/spec.md)
- **Roadmap:** milestone issues M0–M6 on GitHub

Status: pre-alpha. The full loop runs locally against the fake channel, with LLM grading and pluggable personalities (M3).

## Try it locally

Dev mode uses a fake chat channel. Without an API key, `llm` evaluations are echoed back; add
`OPENAI_API_KEY=...` to a `.env` file (gitignored) for real grading and LLM personalities.

```sh
uv sync
uv run pester hash-token            # prints a token and its hash
cp config.example.yaml config.yaml  # paste the hash into clients.example-producer.token_hash

PESTER_DEV_MODE=true uv run pester serve --config config.yaml

# in another terminal: submit a job
curl -H "Authorization: Bearer <token>" -H 'content-type: application/json' \
  -d '{"recipient_id":"kate","prompt":"Did you water the plants?","response_options":["Yes","No"],"evaluation":{"prompt":"YES/NO"}}' \
  localhost:8000/api/v1/jobs

# in a third: be kate
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

Personalities are registered in the config file (see `config.example.yaml`): built-in `neutral`,
`template`, and `llm` types, or your own class via an import path like `my_package.voices:Pirate`.

## Development

```sh
uv run pytest && uv run ruff check && uv run pyright
uv run pytest -m live   # optional: real LLM calls using OPENAI_API_KEY from .env (costs a few cents)
```

Tests never read `.env` or call a real LLM unless you opt in with `-m live`.
