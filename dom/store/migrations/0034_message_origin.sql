-- Where a message came from when it was not Telegram itself: 'dashboard' for what the owner typed in the dashboard's
-- Talk page (sent on to the owner DM as a mirror bubble, whose Telegram id the row keeps). Older rows are NULL.
ALTER TABLE messages ADD COLUMN origin TEXT;
PRAGMA user_version = 35;
