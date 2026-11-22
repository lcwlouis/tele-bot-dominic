ALTER TABLE turns ADD COLUMN model_calls TEXT NOT NULL DEFAULT '[]';
ALTER TABLE turns ADD COLUMN first_bubble_ms INTEGER;
ALTER TABLE turns ADD COLUMN last_bubble_ms INTEGER;

PRAGMA user_version = 17;
