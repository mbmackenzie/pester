-- Admin UI (M7): the single admin's password hash and their browser sessions.
CREATE TABLE admin_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE admin_sessions (
    id_hash    TEXT PRIMARY KEY,  -- sha256 of the cookie value; the cookie itself is never stored
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
