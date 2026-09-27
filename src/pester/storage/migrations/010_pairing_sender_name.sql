-- The name a channel reports for someone asking to pair (e.g. a Telegram user's name), so the admin can tell
-- who's asking when the address itself is an opaque id.
ALTER TABLE pairings ADD COLUMN sender_name TEXT;
