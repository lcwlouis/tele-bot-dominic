-- Preserve all existing proposals and admit the new cross-chat approval kind.
CREATE TABLE proposals_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('soul_edit', 'memory_fact', 'skill', 'plugin', 'lan_address', 'memory_edit', 'cross_chat')),
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

CREATE TABLE cross_chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('alert', 'owner_note', 'message')),
    source_chat_id INTEGER NOT NULL,
    target_chat_id INTEGER NOT NULL,
    sender_user_id INTEGER,
    source_message TEXT UNIQUE,       -- original group and message id for duplicate updates
    text TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('awaiting_approval', 'queued', 'sending', 'sent', 'dropped')),
    drop_reason TEXT,
    proposal_id INTEGER REFERENCES proposals(id) ON DELETE SET NULL,
    sent_msg_id INTEGER,
    created_at TEXT NOT NULL,
    queued_at TEXT,
    sending_at TEXT,
    card_claimed_at TEXT,
    updated_at TEXT NOT NULL,
    status_history TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX cross_chat_status ON cross_chat(status, created_at);
CREATE INDEX cross_chat_created ON cross_chat(created_at);
CREATE INDEX cross_chat_proposal ON cross_chat(proposal_id);
CREATE INDEX cross_chat_target ON cross_chat(target_chat_id);
CREATE INDEX cross_chat_source ON cross_chat(source_chat_id);

PRAGMA user_version = 23;
