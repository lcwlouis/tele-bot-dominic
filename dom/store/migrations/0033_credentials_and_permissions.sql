-- Encrypted secret store (reused by saved logins, E101 owner secrets, and W4 OAuth tokens),
-- desktop browser permissions (R4), saved login metadata (R5), approvals history, and E101 secret requests.

CREATE TABLE IF NOT EXISTS encrypted_secrets (
    namespace   TEXT NOT NULL,
    key         TEXT NOT NULL,
    key_id      TEXT NOT NULL,
    ciphertext  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);

CREATE TABLE IF NOT EXISTS saved_logins (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    site             TEXT NOT NULL,
    label            TEXT NOT NULL DEFAULT '',
    username_masked  TEXT NOT NULL DEFAULT '',
    has_username     INTEGER NOT NULL DEFAULT 0,
    has_password     INTEGER NOT NULL DEFAULT 1,
    match_subdomains INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_saved_logins_site ON saved_logins (site);

CREATE TABLE IF NOT EXISTS owner_secrets (
    key         TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS desktop_action_policies (
    action      TEXT PRIMARY KEY,
    policy      TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS desktop_site_permissions (
    site        TEXT NOT NULL,
    action      TEXT NOT NULL,
    policy      TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (site, action)
);

CREATE TABLE IF NOT EXISTS desktop_permission_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER,
    self_task_id    INTEGER,
    action          TEXT NOT NULL,
    site            TEXT NOT NULL DEFAULT '',
    detail          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,
    decided_via     TEXT NOT NULL DEFAULT '',
    token_hash      TEXT NOT NULL DEFAULT '',
    card_message_id INTEGER,
    created_at      TEXT NOT NULL,
    decided_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_dperm_history_created ON desktop_permission_history (created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_dperm_history_status ON desktop_permission_history (status);

CREATE TABLE IF NOT EXISTS secret_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT NOT NULL,
    reason          TEXT NOT NULL,
    target          TEXT NOT NULL,
    target_key      TEXT NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL,
    card_message_id INTEGER,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    resolved_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_secret_requests_status ON secret_requests (status, expires_at);

PRAGMA user_version = 34;
