-- A special day in Dom's own life (a trip, a concert, his birthday) on which he may raise his media limits.
-- `day` is a calendar date in the owner's time zone; a cancelled day keeps its row, so the dashboard can list it.
CREATE TABLE event_days (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    title TEXT NOT NULL,
    multiplier REAL NOT NULL CHECK(multiplier >= 1),
    declared_at TEXT NOT NULL,
    cancelled_at TEXT
);
CREATE INDEX idx_event_days_day ON event_days(day);

PRAGMA user_version = 32;
