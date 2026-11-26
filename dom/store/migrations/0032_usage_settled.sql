-- A late-reported cost is set once the provider answers, even when the answer is $0 (a failed video job bills
-- nothing). `settled` tells that apart from a row still waiting for its cost. Older rows read as not settled.
ALTER TABLE usage ADD COLUMN settled INTEGER NOT NULL DEFAULT 0;
PRAGMA user_version = 33;
