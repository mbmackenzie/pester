-- Deployment config moves into the database (M6). Each change stores the whole validated config as a new
-- version, so history, rollback, and export are trivial. Secrets live apart from it, so neither exports nor
-- stored versions ever contain them.
CREATE TABLE config_versions (
    version    INTEGER PRIMARY KEY AUTOINCREMENT,
    config     TEXT NOT NULL,   -- JSON of PesterConfig
    comment    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE secrets (
    name       TEXT PRIMARY KEY,  -- e.g. llm.api_key, channel.telegram.bot_token
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
