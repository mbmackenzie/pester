-- Scheduler-owned state (M4). Jobs stay immutable: a snooze is recorded beside the spec, not in it.
ALTER TABLE jobs ADD COLUMN snoozed_until TEXT;

-- Set while a response's debounce window is open, so follow-up messages can be joined; NULL once closed.
ALTER TABLE responses ADD COLUMN closes_at TEXT;

CREATE TABLE recipient_state (
    recipient_id TEXT PRIMARY KEY,
    paused       INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);
