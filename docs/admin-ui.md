# Admin UI: design

How a self-hoster sets up and runs Pester from a browser. Tracks
[M7 (#21)](https://github.com/mbmackenzie/pester/issues/21), built on
[M6 (#20)](https://github.com/mbmackenzie/pester/issues/20).

The experience we're designing for:

```text
Deploy in Dockge → open http://server:8000/admin → set a password → add a channel
→ pair a recipient → create a client key → producers start sending jobs.
```

## 1. Principles

- **LAN-only, one admin.** A single password, no user accounts, and no hardening aimed at the internet
  (rate limiting, 2FA). The UI is not safe to expose publicly, and the docs say so.
- **Same process, same port.** The UI lives at `/admin` in the FastAPI app, with no separate service.
- **No JS build step.** Server-rendered Jinja templates plus [htmx](https://htmx.org), both vendored into
  the package so the UI works on a network without internet access. There's one small hand-written
  stylesheet with a dark palette, neutral surfaces, and a restrained sage accent.
- **Nothing channel-specific in the UI.** Channel pages are generated from each adapter's declared config
  schema. The mock channel and Telegram use the same screens.
- **The UI calls the admin service layer, not the database.** M6's service layer (create, update, delete,
  validate) is shared by the CLI and the UI. Templates never see a repository.
- **The producer API is unchanged.** Producers still use bearer tokens on `/api/v1`, and the UI only
  manages who holds them.

## 2. Phasing

Built in three steps: the read-only **shell** (M7, first half) against YAML config; M6, which moved config
into the database behind the admin service, with a CLI for every change; and the **forms** (M7, second
half), which call that same service. Everything in §5 is in place except per-recipient overrides beyond
quiet hours (other pacing settings are global).

## 3. Authentication

**First run.** While no admin password exists, every `/admin` URL redirects to `/admin/setup`. At startup,
Pester generates a random 8-character setup code and logs it:

```text
WARNING pester.admin: Admin UI is not set up. Open /admin/setup and enter code K7QF-2MXD
```

The setup form asks for the code and a new password (twice). Requiring the code means that whoever can
read the container logs (the deployer) claims the instance, not whoever reaches the page first. The code
changes on every restart until setup is done, and it's never stored.

**Password storage.** `hashlib.scrypt` from the standard library, with a random salt, stored in a new
`admin_settings` table. No new dependency.

**Sessions.** A random session id kept in an `admin_sessions` table (id hash, CSRF token, created,
expires). It's sent in an `HttpOnly`, `SameSite=Lax` cookie scoped to `/admin`, with `Secure` only when the
request came in over HTTPS. (`Lax` rather than `Strict`, so following a link to Pester from Dockge doesn't
show a login page. The CSRF token covers the cross-site POSTs that `Strict` would have blocked.) Sessions expire after 30 days of inactivity. Logging out deletes the row. Resetting the
password (`pester admin reset-password` on the CLI) deletes every session.

**CSRF.** Every session has a CSRF token: it's rendered in a `<meta>` tag, htmx sends it as a header (`hx-headers` on
`<body>`), plain forms send it as a hidden field, and every unsafe method checks it.

**Lost password.** `docker compose exec pester pester admin reset-password` deletes the password, so the
next visit goes back through first-run setup.

## 4. Information architecture

```text
/admin                  Dashboard
/admin/jobs             Jobs (filter by status, recipient, client) → /admin/jobs/{client}/{job_id}
/admin/chat             Mock messenger
/admin/recipients       Recipients (+ pending pairings after M6)
/admin/clients          Producer clients and tokens
/admin/channels         Channel instances and health
/admin/personalities    Personalities + preview
/admin/settings         Pacing, LLM, admin password
/admin/setup, /login, /logout
```

A left sidebar holds these sections, with a health dot next to **Channels** and a count badge next to
**Recipients** when pairings are pending.

## 5. Screens

### Dashboard

```text
┌ Pester ─────────┬────────────────────────────────────────────────────────────┐
│ ● Dashboard     │ Getting started                                  (dismiss) │
│   Jobs          │  ✓ Admin password   ✓ Channel   ○ Recipient   ○ Client key │
│   Messenger     ├───────────────┬───────────────┬──────────────┬─────────────┤
│   Recipients ②  │ Queued     3  │ Awaiting   1  │ Today  12 ✓  │ Failed   0  │
│   Clients       ├───────────────┴───────────────┴──────────────┴─────────────┤
│   Channels ●    │ Health   db ●  scheduler ●  delivery ●  evaluation ●       │
│   Personalities │          mock ●   llm ● gpt-5-mini                         │
│   Settings      ├────────────────────────────────────────────────────────────┤
│                 │ Recent activity                                            │
│                 │ 12:04  kate  COMPLETED   "Why does WAL help?"   0.8        │
│                 │ 11:58  kate  ANSWERED    "Why does WAL help?"              │
│                 │ 09:30  kate  DELIVERED   "Why does WAL help?"              │
└─────────────────┴────────────────────────────────────────────────────────────┘
```

- The **Getting started** checklist appears until every step is done. Each item links to the page where
  you do it. In the shell, the steps are worked out from config ("a client exists", "a recipient
  exists").
- Health reuses the `/ready` checks. The page polls the health and activity sections with htmx every 10 s.
- "Failed" counts jobs in `FAILED` (for example `ambiguous_send`, or evaluation failure) and links to the
  filtered jobs list.

### Jobs

A table (status, recipient, client, prompt preview, updated), newest first, with filters and cursor
paging. The detail page is a timeline of everything Pester knows about the job:

```text
Why does WAL help?                                      COMPLETED   [Cancel]
client study-app · recipient kate · personality weather-goblin · job 01M3F…

09:00  QUEUED
09:30  DELIVERED       via mock → kate  (attempt 1)
11:58  ANSWERED        "it appends instead of overwriting"
11:58  EVALUATED       llm gpt-5-mini · 1.2 s · 812 tokens    [request ▸] [raw ▸]
                       score 0.8 — "Right idea; mention readers not blocking writers."
12:04  COMPLETED       feedback: "Close enough, goblin approves. Mostly."
```

This is the admin/debug page from #10, so that issue folds into M7. The admin can cancel a job (the same
transition as the producer cancel endpoint) but can't edit one: jobs are immutable.

### Mock messenger

```text
┌ Messenger ─────────── channel [mock ▾]  as [kate ▾]  [+ New address] ─┐
│                                                                       │
│   ┌──────────────────────────────┐                                    │
│   │ Did you water the plants?    │                                    │
│   │ [ Yes ] [ No ]               │                                    │
│   └──────────────────────────────┘                                    │
│                                             ┌──────┐                  │
│                                             │ Yes  │                  │
│                                             └──────┘                  │
│   ┌──────────────────────────────┐                                    │
│   │ The fern rustles. Good.      │                                    │
│   └──────────────────────────────┘                                    │
│                                                                       │
│ /status  /skip  /snooze 2h  /pause  /resume                           │
│ [ Type a reply…                                          ] [ Send ]   │
└───────────────────────────────────────────────────────────────────────┘
```

- It behaves like a real chat. Option buttons send `selected_option`. Clicking a message and then
  "Reply" sets `reply_to`. Command chips fill in the input.
- New messages arrive by htmx polling (`every 2s`, with an `after=` cursor). That's plenty on a LAN and
  avoids the complexity of SSE or websockets.
- **+ New address** starts a conversation as someone Pester doesn't know yet. After M6, that's how you test
  pairing: send `/start` (or `/start <invite-code>`) and a pending recipient appears under Recipients.
- In the shell, this page drives the dev-mode `fake` channel through the same `InMemoryChannel` methods
  the `/dev` routes use, but behind admin login. After M6, the mock channel is a normal channel instance
  (adapter type `mock`) that doesn't need dev mode, and it keeps its transcript in SQLite so a restart
  doesn't wipe the conversation.
- It replaces `pester chat` for most uses. The CLI chat stays for terminal workflows.

### Recipients (after M6)

```text
Pending pairings
  mock · address "sam" · said "/start" · 2 min ago     [Approve…] [Reject]

Recipients
  kate   America/New_York   quiet 22:00–09:00   mock: kate   ● active    [Pause] [Edit]
```

**Approve…** opens a form for the recipient id, display name, timezone (defaulting to the browser's) and
optional quiet hours. On approval, the address is bound to the recipient and they get a welcome message.
**Invite codes** are one-time codes, optionally tied to a preset recipient id, for when you want `/start
<code>` to skip approval.

### Clients (after M6)

```text
[+ New client]
  study-app    submit_jobs, read_events, preview    recipients: kate    created 2026-09-20   [Edit] [Revoke]
```

Creating a client shows the token **once**, with a copy-paste snippet that uses the host the admin is
browsing from:

```sh
curl -H "Authorization: Bearer pst_…" -H 'content-type: application/json' \
  -d '{"recipient_id":"kate","prompt":"Did you water the plants?","evaluation":{"prompt":"YES/NO"}}' \
  http://192.168.1.20:8000/api/v1/jobs
```

"Rotate token" issues a new token and invalidates the old one.

### Channels (after M6)

Each channel adapter declares a Pydantic model for its config, with secret fields marked. The page
generates the form from that model's JSON schema. Supported field types are string, int, bool, enum and
secret, which is enough for known adapters, and anything more complex is a sign the adapter should be
simplified.

```text
mock      ● running                              [Configure] [Restart] [Disable]
telegram  ● running · connected as @pester_bot   [Configure] [Restart] [Disable]
[+ Add channel ▾ mock | telegram | my_pkg.channels:Signal]
```

Secret fields are write-only. Once saved, they render as `•••• set` with a "Replace" button, and they are
never sent back to the browser. If an environment variable provides the value, the field shows "set by
environment" and is read-only (environment wins). Saving a channel's config restarts only that channel.
Adapters can provide `validate()` (Telegram: `getMe`) and the UI shows its result.

### Personalities

List (id, type, description), plus a **preview** panel: pick a personality, type a prompt, an evaluation
prompt and a sample reply, and see the evaluation and voiced feedback. This uses the existing preview code
path (`/api/v1/jobs:preview`), so it works in the shell. Create/edit after M6 covers the built-in
`neutral`, `template` and `llm` types. Import-path personalities can be registered but only configured
through YAML.

### Settings

- **Pacing:** global scheduler settings, with an explanation next to each one ("at most 4 messages a
  day"). Per-recipient overrides live on the recipient.
- **LLM:** model, base URL, response format, API key (write-only, env overrides), **Test connection**
  (one tiny structured-output call).
- **Admin:** change password, sign out everywhere.

## 6. Config ownership after M6

- **The database is the source of truth.** Edits through the UI or CLI take effect immediately.
  Workers, the router and the registries read versioned config snapshots, so a change never lands halfway
  through a step.
- **YAML is for seeding and backup.** If `PESTER_CONFIG` is set and the database has no config yet, it's
  imported on first start. After that, the file is ignored and Pester logs a warning if it has changed.
  `pester export` writes the current config as YAML (secrets omitted). `pester import` replaces it.
- **Environment variables override secrets** (`OPENAI_API_KEY`, and each channel's declared env var), so
  people who prefer config-as-code can keep secrets out of the database entirely.

## 7. Security notes

- The UI is LAN-only, and the docs and the first-run page both say so.
- Stored secrets are in plaintext in SQLite, protected by filesystem permissions on `./data` (uid 1000).
  Encryption with a key stored next to the database would add nothing on a single host. Anyone who can
  read `./data` can read the secrets, the same as with a `.env` file.
- Secrets are never rendered, logged or exported.
- The unauthenticated `/dev` routes stay behind `PESTER_DEV_MODE`. Once the mock channel is a real
  adapter (M6), deployments no longer need dev mode, and `compose.yaml` stops setting it.

## 8. Implementation outline (shell)

```text
src/pester/admin/
  __init__.py      router assembly, mounted at /admin
  auth.py          setup code, scrypt, sessions, CSRF, require_admin dependency
  views.py         page routes (dashboard, jobs, chat, read-only config pages)
  queries.py       read models for the UI (job list/detail, counts), on top of the repository
  templates/       base.html, partials/*.html (htmx fragments), one template per page
  static/          htmx.min.js (vendored), admin.css
migrations/006_admin.sql   admin_settings, admin_sessions
```

Tests use `httpx.AsyncClient`: setup-code flow, login/logout, CSRF rejection, redirects when logged out,
each page renders, and chat round-trips through the fake channel. There's no browser automation. Pages are
checked by status and key content.

## 9. Admin design pass

The desktop layout groups navigation into Overview, Connections, and Preferences. Channels, recipient
invitations, and app connections have direct dashboard shortcuts and primary page actions. The small-screen
layout keeps every navigation section visible and lets wide tables scroll within their cards.

Page headers explain the task; settings have section links; custom adapter and personality entry points
sit under expandable advanced options. Status text accompanies health colors, keyboard focus is visible,
and a skip link leads directly to the page content. Dashboard polling reports interrupted updates.

The in-progress dashboard count links to all four outstanding job states (sending, awaiting, answered,
and evaluated). Job filters support an empty status from the browser form and an explicit in-progress
filter, including when paging.

Validation includes the admin integration suite and temporary Chromium checks of the main pages at
1440, 390, and 320 pixels, plus the channel, recipient, invite, and client-token flows. Browser tooling is
used for local verification only; no frontend build step or browser dependency is added to Pester.
