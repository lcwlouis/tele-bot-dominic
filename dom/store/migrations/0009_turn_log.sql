CREATE TABLE turns (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    calls INTEGER NOT NULL,
    trace TEXT NOT NULL,
    model TEXT NOT NULL,
    fallback INTEGER NOT NULL CHECK(fallback IN (0, 1)),
    duration_ms INTEGER NOT NULL,
    outcome TEXT NOT NULL
);

CREATE INDEX idx_turns_at ON turns(at);
CREATE INDEX idx_turns_chat_at ON turns(chat_id, at);

CREATE TABLE online_sessions (
    id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    FOREIGN KEY (chat_id) REFERENCES chats(chat_id)
);

CREATE INDEX idx_online_sessions_chat ON online_sessions(chat_id, id);
CREATE INDEX idx_online_sessions_end ON online_sessions(ends_at);

PRAGMA user_version = 10;
