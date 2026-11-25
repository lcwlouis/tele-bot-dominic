-- Telegram menu cards the owner opened (/status, /chats, /preset, ...): each is deleted once it expires, even
-- across a restart.
CREATE TABLE menu_cards (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX idx_menu_cards_expires ON menu_cards(expires_at);

PRAGMA user_version = 30;
