-- Timestamps are UTC ISO-8601 strings with fixed width (see storage.db.to_db), so they sort lexically.

CREATE TABLE batches (
    pk           INTEGER PRIMARY KEY,
    client_id    TEXT NOT NULL,
    batch_id     TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (client_id, batch_id)
);

CREATE TABLE jobs (
    pk             INTEGER PRIMARY KEY,
    client_id      TEXT NOT NULL,
    job_id         TEXT NOT NULL,
    batch_id       TEXT,
    batch_position INTEGER,
    recipient_id   TEXT NOT NULL,
    status         TEXT NOT NULL,
    priority       REAL NOT NULL,
    not_before     TEXT,
    expires_at     TEXT,
    content_hash   TEXT NOT NULL,
    spec           TEXT NOT NULL,  -- canonical JSON of the submitted InteractionJob
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE (client_id, job_id),
    FOREIGN KEY (client_id, batch_id) REFERENCES batches (client_id, batch_id)
);

CREATE INDEX jobs_by_recipient_status ON jobs (recipient_id, status, priority DESC, created_at);
CREATE INDEX jobs_by_batch ON jobs (client_id, batch_id, batch_position);

-- AUTOINCREMENT guarantees cursors are never reused. With a single writer they are gap-free and
-- commit-ordered.
CREATE TABLE events (
    cursor      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL UNIQUE,
    client_id   TEXT NOT NULL,
    job_pk      INTEGER NOT NULL REFERENCES jobs (pk),
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE INDEX events_by_client ON events (client_id, cursor);
