-- External message ids are only unique per conversation (Telegram message ids are per chat), so every
-- external id is keyed by (channel, address, external_id).

CREATE TABLE deliveries (
    pk          INTEGER PRIMARY KEY,
    job_pk      INTEGER NOT NULL REFERENCES jobs (pk),
    kind        TEXT NOT NULL,   -- PROMPT | FEEDBACK
    channel     TEXT NOT NULL,
    address     TEXT NOT NULL,
    message     TEXT NOT NULL,   -- JSON OutboundMessage
    status      TEXT NOT NULL,   -- PENDING | SENDING | SENT | FAILED | CANCELLED
    attempts    INTEGER NOT NULL DEFAULT 0,
    external_id TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    sent_at     TEXT
);

CREATE INDEX deliveries_by_status ON deliveries (status, pk);
CREATE INDEX deliveries_by_job ON deliveries (job_pk, kind);
CREATE UNIQUE INDEX deliveries_by_external_id ON deliveries (channel, address, external_id)
    WHERE external_id IS NOT NULL;

-- Every inbound message ever processed, for dedupe of channel redeliveries.
CREATE TABLE inbound_messages (
    channel     TEXT NOT NULL,
    address     TEXT NOT NULL,
    external_id TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (channel, address, external_id)
);

CREATE TABLE responses (
    pk              INTEGER PRIMARY KEY,
    job_pk          INTEGER NOT NULL UNIQUE REFERENCES jobs (pk),
    text            TEXT NOT NULL,
    selected_option TEXT,
    channel         TEXT NOT NULL,
    address         TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    raw             TEXT
);

CREATE TABLE evaluations (
    pk             INTEGER PRIMARY KEY,
    job_pk         INTEGER NOT NULL REFERENCES jobs (pk),
    status         TEXT NOT NULL,  -- SUCCESS | FAILED
    evaluator      TEXT NOT NULL,
    model          TEXT,
    result         TEXT,           -- JSON
    feedback_facts TEXT,
    raw            TEXT,           -- JSON
    error          TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX evaluations_by_job ON evaluations (job_pk);
