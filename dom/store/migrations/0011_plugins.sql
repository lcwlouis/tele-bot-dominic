CREATE TABLE plugins (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'disabled')),
    hash TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    approved_hash TEXT,
    approved_at TEXT,
    updated_at TEXT NOT NULL
);

PRAGMA user_version = 12;
