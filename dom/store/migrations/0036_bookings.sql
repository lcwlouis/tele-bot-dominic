-- Booking within the owner's windows (v0.8.6 W2): what Dom offered a friend and what became of it. One row per offer;
-- the pending offer of a chat is what lets a friend's pick map to a slot Dom really offered, across restarts.
CREATE TABLE booking_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('friend_dm', 'group')),
    sender_id INTEGER,
    friend TEXT NOT NULL DEFAULT '',
    minutes INTEGER NOT NULL,
    slots TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL CHECK(state IN ('offered', 'awaiting', 'booked', 'declined', 'expired')),
    picked TEXT,
    email TEXT NOT NULL DEFAULT '',
    event_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    settled_at TEXT
);
CREATE INDEX idx_booking_offers_chat ON booking_offers(chat_id, state);

PRAGMA user_version = 37;
