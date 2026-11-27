-- The turn a paid call belonged to: set once the turn's row is saved, for the calls the turn itself made (its own
-- trace, not the work after it or a background pass). The dashboard reads a reply's cost from it. Older rows and
-- calls outside a turn stay NULL.
ALTER TABLE usage ADD COLUMN turn_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_usage_turn ON usage(turn_id);

PRAGMA user_version = 38;
