-- Audit detail for evaluations (spec §10): the exact request, usage, latency, attempts, and how feedback
-- was voiced.
ALTER TABLE evaluations ADD COLUMN request TEXT;          -- JSON
ALTER TABLE evaluations ADD COLUMN usage TEXT;            -- JSON
ALTER TABLE evaluations ADD COLUMN latency_ms INTEGER;
ALTER TABLE evaluations ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1;
ALTER TABLE evaluations ADD COLUMN personality_id TEXT;
ALTER TABLE evaluations ADD COLUMN personality_fallback INTEGER;
ALTER TABLE evaluations ADD COLUMN feedback_text TEXT;
