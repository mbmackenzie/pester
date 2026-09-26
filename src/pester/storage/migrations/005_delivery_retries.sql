-- Retry scheduling for deliveries (M5). A PENDING delivery is not attempted before next_attempt_at.
ALTER TABLE deliveries ADD COLUMN next_attempt_at TEXT;
