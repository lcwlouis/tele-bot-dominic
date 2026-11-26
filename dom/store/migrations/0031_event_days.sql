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

-- Every fresh still made for a video note, delivered or not: each is studio work that counts toward the daily total.
CREATE TABLE studio_stills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_studio_stills_created ON studio_stills(created_at);

PRAGMA user_version = 32;
