-- Allow LAN address changes found by find_devices as proposals. Older releases read these rows
-- fine (they show the title and refuse to apply an unknown kind), so a rollback stays safe.
CREATE TABLE proposals_new (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('soul_edit', 'memory_fact', 'skill', 'plugin', 'lan_address')),
    title TEXT NOT NULL,
    body_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected', 'expired')),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    source TEXT NOT NULL
);
INSERT INTO proposals_new SELECT id, kind, title, body_json, status, created_at, decided_at, source FROM proposals;
DROP TABLE proposals;
ALTER TABLE proposals_new RENAME TO proposals;

PRAGMA user_version = 14;
