-- The mock channel's conversations (M6), so the browser messenger survives restarts and message ids are
-- never reused.
CREATE TABLE mock_messages (
    channel TEXT NOT NULL,
    address TEXT NOT NULL,
    id      INTEGER NOT NULL,
    message TEXT NOT NULL,   -- JSON ChatMessage
    PRIMARY KEY (channel, address, id)
);
