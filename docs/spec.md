# Pester — Specification

**Version:** 0.2
**Status:** Accepted for MVP
**Type:** Generic asynchronous human-interaction runtime

```text
Jobs in.
Humans bothered.
Responses evaluated.
Events out.
```

---

## 1. What Pester is

Pester is a small, generic service that:

1. accepts self-contained **interaction jobs** from producers,
2. decides **when** to deliver them within the producer's constraints,
3. delivers them to a person through a **channel** (Telegram eventually; a fake channel first),
4. captures the person's **response**,
5. **evaluates** the response using producer-supplied instructions,
6. sends the person **feedback** in a configurable personality,
7. records everything as append-only **events** that producers poll with a cursor.

A producer answers: *what should this person be asked, and how should the answer be judged?*
Pester answers: *when and how should it be delivered, and what happened?*
An evaluator answers: *what does the response mean, according to the supplied instructions?*

### 1.1 What Pester is not

Not a learning-management system, spaced-repetition engine, RAG engine, knowledge graph, agent framework, workflow orchestrator, general task scheduler, or conversational memory system. Pester has no concept of quizzes, flashcards, mastery, habits, reminders, or study sessions. Those may appear in job `metadata`, but they never influence Pester's behavior.

### 1.2 Examples

A study system submits a question, an evidence packet, a grading rubric, and a concept ID in metadata. Pester delivers the question, grades the answer against the rubric, and emits the result.

A plant-care script submits *"Did you water the plants?"* with the instruction *"Classify as YES / NO / UNCLEAR"* and the personality `judgmental-houseplant`. Pester runs it without any code changes.

---

## 2. Architectural invariants

These are hard rules.

1. **Pester does not decide what a person is asked.**
2. **Pester does not own producer domain state.**
3. **Jobs are immutable after submission.** To change one, cancel it and submit a new one.
4. **Human responses are persisted before evaluation.** A failed model call never loses a response.
5. **Evaluations are auditable and re-runnable.** The exact rendered model request, model, parameters, and raw response are stored. (LLMs are not deterministic, so "reproducible" means *re-executable from stored inputs*, not *bit-identical*.)
6. **Personality may alter presentation, never evaluation.**
7. **Producer, consumer, and channel retries are safe.** All public operations are idempotent.
8. **Delivery channels are replaceable.** Core logic never imports channel-specific concepts.
9. **Evaluators are replaceable.**
10. **The integration contract is jobs in, events out.**

---

## 3. Architecture

A single async Python process:

```text
                    Producer(s)
                        │  HTTPS + bearer token
                        ▼
┌───────────────────────────────────────────────────────┐
│ Pester process                                        │
│                                                       │
│  FastAPI ──► Repository ◄── SQLite (WAL, single file) │
│                 ▲   ▲   ▲                             │
│   Scheduler ────┘   │   └──── Evaluation worker       │
│   worker            │              │                  │
│                Delivery worker     ├─► Evaluator      │
│                     │              └─► Personality    │
│                     ▼                                 │
│              DeliveryChannel  ◄── inbound ──┐         │
│         (Fake | InMemory | Telegram)        │         │
│                                     Response router   │
└─────────────────────┬───────────────────────▲─────────┘
                      ▼                       │
                    Human ────────────────────┘
```

- **SQLite is the queue.** No broker. Every state change and its event are written in one transaction.
- **Single writer.** One writer connection makes event cursors gap-free and commit-ordered.
- **Workers** each expose `run_once()`. Production loops them, woken by timers and by `asyncio.Event` signals. Tests call `run_once()` directly.
- **Injected `Clock`.** Nothing calls `datetime.now()` directly. This is what makes scheduling testable.

### 3.1 Components

| Component | Responsibility |
|---|---|
| API | Submit, cancel, preview, and inspect jobs; poll events; health endpoints |
| Repository | All persistence; state transitions and event emission in one transaction |
| Scheduler | Pure policy function plus a worker that applies its decisions |
| Delivery worker | Outbox-style sending with retry and backoff |
| Response router | Maps inbound messages and commands to jobs |
| Evaluation worker | Runs the evaluator, validates output, runs the personality, queues feedback |
| DeliveryChannel | Adapter to a messaging service |
| Evaluator | Turns (job, response) into a structured result |
| PersonalityRenderer | Turns an evaluation result into a message for the person |

---

## 4. Data model

### 4.1 InteractionJob (submitted by the producer)

```python
class InteractionJob(BaseModel):
    id: str | None = None             # producer-supplied for idempotent retries; else server ULID
    recipient_id: str
    prompt: str
    response_options: list[str] | None = None   # rendered as buttons where supported
    evaluation: EvaluationSpec
    personality_id: str | None = None
    delivery: DeliverySpec = DeliverySpec()
    metadata: dict[str, Any] = {}
    provenance: Provenance | None = None
```

Identity is `(client_id, id)`. Two producers cannot collide. `id` must match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`.

A job's `batch_id` is assigned only by `POST /batches`. It is not a field producers set on individual jobs.

### 4.2 EvaluationSpec

```python
class EvaluationSpec(BaseModel):
    evaluator: str = "llm"            # "llm" | "rule" | future types
    prompt: str                       # instructions for the evaluator
    context: dict[str, Any] | list[Any] | str | None = None   # opaque to Pester
    output_schema: dict[str, Any] | None = None               # JSON Schema for `result`
    model: str | None = None
    prompt_version: str | None = None
```

The `rule` evaluator does deterministic matching (e.g. exact match against `response_options`) with no LLM cost.

### 4.3 DeliverySpec

```python
class DeliverySpec(BaseModel):
    channel: str | None = None        # default: recipient's primary channel
    not_before: datetime | None = None
    expires_at: datetime | None = None          # applies to undelivered jobs only
    answer_within_seconds: int | None = None    # default from server config (24h)
    priority: float = 0.5                        # 0..1, higher first
    prompt_rendering: Literal["verbatim", "personality"] = "verbatim"
    allow_reminder: bool = False                 # post-MVP
    reminder_after_seconds: int | None = None    # post-MVP
```

### 4.4 Provenance

Optional producer / generator / source / evaluation version strings. Stored, never interpreted.

### 4.5 Internal records

- **DeliveryRecord:** job, channel, direction (prompt / feedback), status (`PENDING`/`SENDING`/`SENT`/`FAILED`), external message ID, attempts, timestamps. Kept separate from job state so core state never depends on channel IDs.
- **HumanResponse:** job, text (or selected option), channel, external message ID (unique), received and debounce-closed timestamps, optional raw payload.
- **EvaluationRecord:** job, attempt, status, `result`, `feedback_facts`, model, rendered request, raw response, token usage, latency.

---

## 5. Job lifecycle

```text
QUEUED ──► SENDING ──► AWAITING ──► ANSWERED ──► EVALUATED ──► COMPLETED
  │           │           │            │             │
  │           │           │            │             └──► FAILED  (feedback undeliverable)
  │           │           │            └──► FAILED  (evaluation exhausted retries)
  │           │           ├──► UNANSWERED   (answer_within elapsed)
  │           │           ├──► SKIPPED      (/skip)
  │           │           └──► QUEUED       (/snooze → new not_before)
  │           └──► FAILED   (send retries exhausted, or ambiguous send after crash)
  ├──► EXPIRED   (expires_at passed before delivery)
  └──► CANCELLED (producer cancel; allowed from any non-terminal state)
```

`COMPLETED` means the feedback message was delivered. `EVALUATED` means a result exists but feedback is still pending. Transitions are defined in one table. Illegal transitions raise errors.

---

## 6. Events

Every lifecycle change appends an event in the same transaction as the state change.

| Type | When |
|---|---|
| `INTERACTION_QUEUED` | Accepted |
| `INTERACTION_DELIVERED` | Prompt sent |
| `INTERACTION_ANSWERED` | Response captured (debounce closed) |
| `INTERACTION_EVALUATED` | Evaluation succeeded |
| `INTERACTION_COMPLETED` | Feedback delivered |
| `INTERACTION_SNOOZED` | Person snoozed |
| `INTERACTION_SKIPPED` | Person declined |
| `INTERACTION_UNANSWERED` | Answer window elapsed |
| `INTERACTION_EXPIRED` | Expired before delivery |
| `INTERACTION_CANCELLED` | Producer cancelled |
| `INTERACTION_FAILED` | Delivery or evaluation failed terminally |
| `INTERACTION_LATE_RESPONSE` | Message arrived after the job was evaluated or closed (not re-evaluated) |

```json
{
  "cursor": 194,
  "event_id": "01J...",
  "type": "INTERACTION_EVALUATED",
  "interaction_id": "job_01",
  "batch_id": "batch_2026_09_25",
  "occurred_at": "2026-09-25T19:30:00-04:00",
  "payload": {
    "response": {"text": "..."},
    "evaluation": {"result": {}, "model": "...", "usage": {"input_tokens": 0, "output_tokens": 0}, "latency_ms": 0},
    "delivery": {"sent_at": "...", "answered_at": "...", "response_latency_s": 0}
  },
  "metadata": {}
}
```

The job's `metadata` is echoed on every event so consumers can route events without a lookup.

---

## 7. API

All `/api/v1` routes require `Authorization: Bearer <token>`. Tokens are stored hashed in config.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/jobs` | Submit one job |
| `POST` | `/api/v1/batches` | Submit `{batch_id, jobs[]}` atomically |
| `POST` | `/api/v1/jobs/{id}/cancel` | Cancel a non-terminal job |
| `GET` | `/api/v1/jobs/{id}` | Inspect a job (debug/admin) |
| `POST` | `/api/v1/jobs:preview` | Evaluate `{job, response}` and render feedback without storing or delivering anything |
| `GET` | `/api/v1/personalities` | List the personalities this deployment offers, and the default |
| `GET` | `/api/v1/events?after=<cursor>&limit=<n>` | Poll this client's events |
| `GET` | `/health`, `/ready` | Liveness, readiness (DB writable, workers alive, channel connected, evaluator configured) |

### 7.1 Idempotency

- Each job is stored with a hash of its canonical JSON.
- Re-submitting the same `(client, id)` with the **same** hash returns `200` and the existing state.
- The same id with a **different** hash returns `409 Conflict`.
- Batches work the same way: same `batch_id` with the same job set returns `200`, a different set returns `409`. A batch is inserted all-or-nothing.

### 7.2 Events

- Events are scoped to the calling client.
- Cursors are monotonically increasing integers. `next_cursor` is returned even when the page is empty.
- There is no ack endpoint. Consumers persist their last processed cursor.

### 7.3 Authorization

```yaml
clients:
  study-system:
    token_hash: "sha256:..."
    permissions: [submit_jobs, read_events, preview]
    recipients: [kate]          # recipients this client may target
```

---

## 8. Scheduling

The policy is a **pure function** (`scheduler/policy.py`): no I/O and no clock.

```python
decide(now, candidates, recipients: Mapping[str, RecipientState], policy: Policy) -> Plan
# Plan: send (job keys), expire (job keys), wake_at (earliest future time the plan could change)
```

For each queued job, `earliest_send` computes the first moment it may go out. Every constraint applies, and the most restrictive wins:

- **Job:** `max(created_at, not_before, snoozed_until)` plus job jitter; `expires_at` (a job past it expires even while blocked).
- **Spacing:** at least `min_interval_minutes` after the recipient's last prompt, plus jitter.
- **Quiet hours** (the recipient's own window overrides the global one, evaluated in the recipient's `timezone`; windows may span midnight): pushed to the window's end plus that night's jitter.
- **Daily cap:** at most `max_messages_per_day` prompts in the recipient's **local** day; otherwise pushed to local midnight (and then usually to the end of quiet hours).
- **Paused** recipients get nothing. Their jobs can still expire.

Each recipient's eligible jobs are taken in priority order (highest priority, then oldest, then submission order) while outstanding slots remain. After each pick the recipient's state is updated as if it had been sent, so spacing and caps hold even when several jobs are due in the same pass.

**Jitter** is a deterministic function of `jitter_seed` and a fixed anchor: the job's own start, the last prompt (for spacing), or the date (for quiet hours). A job's send time is therefore stable across passes and restarts instead of receding. The quiet-hours release time is `end + jitter(that night)`, whether it's evaluated from inside the window or just after its nominal end.

Only prompts count towards spacing and caps. Feedback and notices are replies to something the person did, and are sent immediately.

A job occupies an outstanding slot from `SENDING` until its feedback is delivered (`SENDING`, `AWAITING`, `ANSWERED`, `EVALUATED`), so a person never gets a new question before feedback on the last one. Jobs whose recipient has no enabled channel stay `QUEUED`.

**Answer timeouts:** an `AWAITING` job becomes `UNANSWERED` once `answer_within_seconds` (per job, else `default_answer_within_seconds`) has passed since its prompt was sent, unless an answer is being collected (debounce). This frees the slot, and a later reply is recorded as `INTERACTION_LATE_RESPONSE`.

```yaml
scheduler:
  quiet_hours: {start: "21:00", end: "08:30"}   # null to disable
  min_interval_minutes: 120
  max_messages_per_day: 4
  max_outstanding: 1
  jitter_minutes: 30
  jitter_seed: pester
  default_answer_within_seconds: 86400
  debounce_seconds: 20
  max_snooze_hours: 168
```

Verified by table tests (quiet hours spanning midnight, both DST transitions, local-day caps), Hypothesis properties over random schedules and timezones, and a seeded week-long simulation that checks every constraint against the messages the recipient actually received.

---

## 9. Delivery channels

```python
class DeliveryChannel(Protocol):
    name: str
    async def send(self, address: ChannelAddress, msg: OutboundMessage) -> SentReceipt: ...
    async def start(self, on_inbound: Callable[[InboundMessage], Awaitable[None]]) -> None: ...
    async def stop(self) -> None: ...

class InboundMessage(BaseModel):
    channel: str
    external_id: str
    sender_address: str
    text: str | None
    command: Command | None            # parsed /skip, /snooze 2h, /pause, /resume, /status
    selected_option: str | None        # button press
    reply_to_external_id: str | None
    received_at: datetime
    raw: dict[str, Any] | None
```

Implementations:

| Channel | Use |
|---|---|
| `InMemoryChannel` | Tests. One conversation per address with sequential message ids, `inject(...)` for inbound messages, and send-failure injection. |
| `fake` | Local development: an `InMemoryChannel` named `fake`, enabled by `PESTER_DEV_MODE`, exposed through the unauthenticated dev routes `GET`/`POST /dev/chat/{address}` and the `pester chat <address>` CLI. Models reply threading like Telegram. |
| `TelegramChannel` | Production. Long polling via `python-telegram-bot`, no public webhook. Inline keyboards for `response_options`. |

### 9.1 Sending (outbox pattern)

1. Mark the delivery `SENDING` and commit.
2. Call `channel.send`.
3. Record the external ID, mark it `SENT`, and move the job to `AWAITING`.

Transient errors are retried with bounded exponential backoff. **After a crash, a delivery left in `SENDING` is not resent.** It is marked `FAILED` (`ambiguous_send`), because sending a person a duplicate is worse than losing one message.

### 9.2 Response routing

1. **Reply-to:** the message's `reply_to_external_id` matches a delivery → that delivery's job.
2. **Button press:** the selected option is attached to its message → that message's job.
3. **Fallback:** the recipient's single outstanding `AWAITING` job.
4. **Nothing matches:** reply "nothing pending", log it, and emit no event.

**Debounce:** people often answer in several messages. The first answer opens a window of `debounce_seconds`. Each further message routed to the same job is joined on (newline-separated) and slides the window. The job stays `AWAITING` while the answer is collected, and moves to `ANSWERED` (emitting `INTERACTION_ANSWERED` with the full text) when the window closes. A button press closes the window immediately. Feedback threads onto the latest message. `debounce_seconds: 0` records each answer at once.

A message that arrives after the job is answered, evaluated, or closed emits `INTERACTION_LATE_RESPONSE` and is not evaluated. Messages from unknown senders are dropped (allowlist). Duplicate inbound external IDs are ignored, for commands too.

### 9.3 Recipient commands

| Command | Effect |
|---|---|
| `/skip` | Open question → `SKIPPED` (discarding any partial answer) |
| `/snooze [duration]` | Open question → `QUEUED` until now + duration (`30m`, `2h`, `1h30m`, `1d`; default 1h, at most `max_snooze_hours`), emitting `INTERACTION_SNOOZED`. Stored as the scheduler-owned `snoozed_until`; the job spec is unchanged. It's asked again later, subject to all pacing rules |
| `/pause`, `/resume` | Stop or resume all prompts to this recipient (open questions stay open) |
| `/status` | Open questions (with when they were asked, in local time), queued count, paused state |
| anything else | Lists the commands |

`/skip` and `/snooze` act on the replied-to question, or the single open question. With several open and no reply-to, the person is asked to reply to the one they mean. Channels parse commands into the channel-agnostic `Command` type.

### 9.4 Recipients

```yaml
recipients:
  kate:
    timezone: America/New_York
    quiet_hours: {start: "22:00", end: "09:00"}   # optional override
    channels:
      telegram: {user_id: 123456789, chat_id: 123456789}
      fake: {address: kate}
```

---

## 10. Evaluation

```python
class Evaluator(Protocol):
    name: str
    async def evaluate(self, job: InteractionJob, response: HumanResponse) -> EvaluationOutcome: ...
```

`evaluation.evaluator` names a registered evaluator. Unregistered names are rejected at submit (422), as are invalid `output_schema`s and `rule` jobs without `response_options`.

| Name | Implementation | Registered when |
|---|---|---|
| `llm` | `LLMEvaluator` | `OPENAI_API_KEY` is set (in dev mode without a key, `llm` falls back to echo) |
| `rule` | `RuleEvaluator`: case-insensitive match against `response_options`; unmatched replies are recorded, not failed | always |
| `echo` | `EchoEvaluator` | dev mode |

**Output contract:**

- The evaluator returns `result`, which must validate against `output_schema` when one is given. This is checked for every evaluator, not just the LLM.
- It also returns `feedback_facts`: a neutral statement of what the person should be told.

**LLM evaluator:**

- Uses the `openai` SDK (chat completions) with a configurable `base_url` (OpenAI, OpenRouter, Ollama, or any compatible endpoint). Settings live under `llm:` in config; the key comes only from `OPENAI_API_KEY`.
- The producer's instructions go in the system message. The person's reply is passed as untrusted data in a JSON document, and the model is told never to follow instructions inside it.
- Uses `json_schema` response format (non-strict, since producer schemas rarely meet strict mode's rules) or `json_object`, and always validates locally.
- Invalid output (bad JSON, wrong shape, schema mismatch) is retried immediately with a correction turn. Transient provider errors (connection, 429, 5xx) are retried with exponential backoff. Permanent errors (auth, bad request) fail at once. After `llm.max_attempts` the job is `FAILED`.

**Stored for audit** (`evaluations` table, on success and failure): the exact request last sent (messages, model, parameters, including correction turns), the raw response, token usage, latency, attempts, and the personality used for feedback.

**Preview:** `POST /api/v1/jobs:preview` runs the same pipeline as the evaluation worker (evaluate → schema check → personality) on a sample reply, and returns the delivered prompt and feedback. Nothing is stored or sent. Requires the `preview` permission.

---

## 11. Personality

Personalities are **registered by the deployer** in config and **chosen by producers** per job with `personality_id`. Jobs without one use `default_personality`. A neutral `default` always exists. Producers discover what's available with `GET /api/v1/personalities`.

```python
class Personality(Protocol):
    async def render_prompt(self, ctx: PromptContext) -> str: ...        # only if delivery.prompt_rendering == "personality"
    async def render_feedback(self, ctx: FeedbackContext) -> str: ...

# FeedbackContext: job, response, feedback_facts, result (a read-only private copy)
# A factory builds a personality from its config options: (options, services) -> Personality
```

`type` is a built-in or an import path to a deployer's own factory (a function or class). Every other key is passed to the factory as options, and validated at startup, so misconfiguration fails before anything is served.

| Type | Options |
|---|---|
| `neutral` | none: prompts verbatim, feedback facts verbatim |
| `template` | `feedback`, `prompt`: Jinja templates rendered in a sandbox with strict undefined variables. Variables: `prompt`, `response_options`, `metadata`, `recipient_id`, and for feedback `feedback_facts`, `result`, `response` |
| `llm` | `prompt` or `prompt_file` (relative to the config file), optional `model`, `temperature`. Fixed rules appended to the persona forbid adding, removing, softening or contradicting facts |
| `package.module:factory` | whatever that factory accepts |

```yaml
default_personality: default
personalities:
  weather-goblin:
    type: llm
    description: Mildly antagonistic, secretly supportive
    prompt: You are a mildly antagonistic but supportive study goblin.
  pirate:
    type: my_package.voices:Pirate    # a deployer's own implementation
    description: Arr
    swagger: 11                       # passed to the factory
```

**Personality may alter presentation, never evaluation**, and this is structural: renderers receive a read-only copy of the result and can only return text. The result is stored exactly as the evaluator produced it.

**Failure never blocks delivery.** If a personality raises, returns nothing, or needs an LLM with no key configured, the neutral text is sent instead, and the event records `personality_fallback: true`.

---

## 12. Persistence

A SQLite file in WAL mode, accessed through `aiosqlite` with numbered `.sql` migrations and a `schema_version` table.

Tables: `batches`, `jobs`, `deliveries`, `responses`, `evaluations`, `events`, `recipient_state`, `schema_version`. Each table is added by a migration in the milestone that first uses it (M1: `batches`, `jobs`, `events`). Clients, recipients, and personalities live in config, not the database.

Unique constraints:

- `jobs(client_id, id)`
- `batches(client_id, batch_id)`
- `events(event_id)`
- `inbound_messages(channel, address, external_id)`, and `deliveries(channel, address, external_id)`. External message ids are only unique per conversation (Telegram message ids are per chat), so they are always keyed with the address.

### 12.1 Recovery on startup

| State at startup | Action |
|---|---|
| `QUEUED` | Scheduled normally |
| `SENDING` | `FAILED` (`ambiguous_send`) |
| `AWAITING` | Timeouts re-evaluated |
| `ANSWERED` | Re-evaluated |
| `EVALUATED` | Feedback re-queued |

---

## 13. Operations

- **Config:** YAML file (`PESTER_CONFIG`) plus environment variables via `pydantic-settings`.
- **Secrets:** `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, and producer token hashes. Secrets never go in job payloads or logs.
- **Logging:** structured JSON logs carrying `interaction_id`, `batch_id`, `recipient_id`, `client_id`, and `event`.
- **Deployment:** Docker Compose with one `pester` service and a volume at `/data/pester.sqlite`.
- **CLI:** `pester serve`, `pester chat <address>` (fake channel, dev mode), `pester hash-token`.

---

## 14. Testing strategy

1. **Pure unit tests:** scheduler `decide()` as table-driven cases plus `hypothesis` properties (never in quiet hours, never over the daily cap, never more than `max_outstanding`); state-machine legality; ID and hash canonicalization.
2. **Storage:** real temporary SQLite files. Idempotency and conflicts, gap-free cursors, migrations from an empty database.
3. **API:** `httpx.AsyncClient` + `ASGITransport`. Auth, 409s, paging, and client isolation.
4. **End-to-end scenarios:** the app with `InMemoryChannel`, `ScriptedEvaluator`, and `FakeClock`, driven with `run_once()`. Scenarios: happy path, ignored → unanswered, skip, snooze, late reply, debounce, duplicate batch, cancel mid-flight.
5. **Crash and recovery:** a second app instance on the same database file partway through a flow.
6. **Channel contract suite:** one set of tests run against every channel. Telegram runs it against a fake Bot API server through `python-telegram-bot`'s `base_url`, plus fixture tests for `Update → InboundMessage`.
7. **Evaluator:** `respx`-mocked OpenAI HTTP (invalid schema, retries, timeouts). Opt-in `@pytest.mark.live` golden tests for prompt quality.

Tooling: `uv`, `pytest`, `pytest-asyncio`, `hypothesis`, `respx`, `ruff`, `pyright`.

---

## 15. Roadmap

Each milestone is tracked as a GitHub issue.

| # | Milestone | Done when |
|---|---|---|
| M0 | Skeleton and tooling | Server boots; CI runs lint, types, and tests |
| M1 | Domain, storage, API | A batch submitted with curl produces `QUEUED` events; idempotency works |
| M2 | Fake channel and the full loop | Full loop by hand through `pester chat` with no external services |
| M3 | LLM evaluation and personality | Real grading and personality feedback in the fake chat; preview endpoint |
| M4 | Scheduling rules and recipient commands | Quiet hours, caps, jitter, timeouts, skip/snooze/pause |
| M5 | Hardening and deployment | Survives kill -9 mid-flow; Docker Compose |
| M6 | Telegram adapter | Real Telegram passes the channel contract suite |

**Post-MVP:** reminders, push webhooks to producers, admin/debug page, one-turn clarification (`NEEDS_CLARIFICATION`), voice-note transcription, per-recipient delivery windows.

---

## 16. Package layout

```text
src/pester/
├── api/            jobs.py, batches.py, events.py, preview.py, dev.py, auth.py
├── core/           models.py, states.py, events.py, clock.py, ids.py
├── storage/        db.py, repository.py, migrations/*.sql
├── scheduler/      policy.py (pure), worker.py
├── delivery/       base.py, memory.py, fake.py, telegram.py, worker.py, router.py
├── evaluation/     base.py, llm.py, rule.py, scripted.py, worker.py
├── personality/    base.py, neutral.py, template.py, llm.py, registry.py
├── cli.py
├── config.py
└── main.py
tests/
├── unit/  storage/  api/  e2e/  contract/  live/
fixtures/example_batch.json
```
