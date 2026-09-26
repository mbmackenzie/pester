-- Pairing (M6): a message from an unknown address on a channel that accepts pairing becomes a request for
-- the admin to approve; a one-time invite code (/start <code>) approves it directly.
CREATE TABLE pairings (
    pk               INTEGER PRIMARY KEY,
    channel          TEXT NOT NULL,
    address          TEXT NOT NULL,
    recipient_config TEXT NOT NULL,  -- JSON: how to reach this address, from the channel
    first_text       TEXT,
    status           TEXT NOT NULL,  -- PENDING | APPROVED | REJECTED
    recipient_id     TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    welcomed_at      TEXT,
    UNIQUE (channel, address)
);

CREATE TABLE invites (
    code_hash    TEXT PRIMARY KEY,
    recipient_id TEXT NOT NULL,
    timezone     TEXT,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    used_at      TEXT,
    used_channel TEXT,
    used_address TEXT
);
