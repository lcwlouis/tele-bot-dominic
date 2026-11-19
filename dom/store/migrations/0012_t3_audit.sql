CREATE TABLE t3_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    machine TEXT NOT NULL,
    target TEXT NOT NULL,
    summary TEXT NOT NULL,
    outcome TEXT NOT NULL,
    error TEXT NOT NULL
);

CREATE INDEX t3_audit_at ON t3_audit(at);
CREATE INDEX t3_audit_chat_id ON t3_audit(chat_id, id DESC);

PRAGMA user_version = 13;
