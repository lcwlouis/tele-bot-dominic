CREATE TABLE goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '' CHECK(category IN (
        '', 'health', 'relationships', 'money', 'career', 'interests', 'productivity', 'other'
    )),
    target_date TEXT,
    check_in_rhythm TEXT NOT NULL DEFAULT '' CHECK(check_in_rhythm IN (
        '', 'daily', 'every_few_days', 'weekly', 'biweekly', 'monthly'
    )),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'paused', 'done', 'dropped')),
    progress_note TEXT NOT NULL DEFAULT '',
    last_checkin_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE INDEX idx_goals_status ON goals(status, updated_at DESC);

CREATE TABLE goal_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN (
        'created', 'status', 'progress', 'edited', 'checkin', 'reminder_linked', 'reminder_unlinked'
    )),
    from_status TEXT,
    to_status TEXT,
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'owner',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_goal_history_goal ON goal_history(goal_id, id DESC);

CREATE TABLE goal_reminders (
    goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    reminder_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (goal_id, reminder_id)
);

CREATE INDEX idx_goal_reminders_reminder ON goal_reminders(reminder_id);

CREATE TABLE noticings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('deadline', 'help', 'followup', 'idea')),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    action_kind TEXT NOT NULL DEFAULT 'chat' CHECK(action_kind IN ('chat', 'reminder', 'self_work', 'cross_chat', 'brief')),
    action_payload_json TEXT NOT NULL DEFAULT '{}',
    source_kind TEXT NOT NULL CHECK(source_kind IN ('owner_dm', 'group_paraphrase', 'memory', 'self_work', 'goal')),
    source_chat_id TEXT,
    source_msg_id INTEGER,
    source_label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'accepted', 'dismissed')),
    outcome TEXT NOT NULL DEFAULT '',
    sent_as_opener_at TEXT,
    opener_msg_id INTEGER,
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE INDEX idx_noticings_status ON noticings(status, id DESC);
CREATE INDEX idx_noticings_day ON noticings(day, id DESC);
CREATE INDEX idx_noticings_opener ON noticings(sent_as_opener_at);

PRAGMA user_version = 27;
