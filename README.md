# Tele Friend Bot

A Telegram bot that simulates human-like behavior with online/offline cycles and natural response delays.

## Features

- **Human-like Behavior**: Bot goes online/offline with realistic timing
- **Natural Response Delays**: Random delays before responding to messages
- **Sleep Mode**: Bot can be put to sleep for specified durations
- **Message Queuing**: Messages received while offline are queued and processed when back online
- **Chat History**: Commands to view and clear chat history
- **Dev Mode**: Development mode for testing with a single chat

## Dev Mode

Dev mode allows you to test the bot with only one specific chat, bypassing all online status checks.

### Environment Variables for Dev Mode

```bash
# Enable dev mode
DEV_MODE=true

# Specify the chat ID that should be allowed in dev mode
DEV_CHAT_ID=123456789
```

### Dev Mode Behavior

When `DEV_MODE=true`:
- Only the chat specified in `DEV_CHAT_ID` will be allowed
- All other chats will be rejected
- The bot will always appear online for the dev chat
- No online/offline status checks will be performed for the dev chat
- Sleep and offline states are ignored for the dev chat

### Usage

1. Set the environment variables:
   ```bash
   export DEV_MODE=true
   export DEV_CHAT_ID=your_chat_id_here
   ```

2. Start the bot normally - it will automatically detect dev mode and log when it's active

3. Only messages from the specified chat ID will be processed

## Commands

- `/start` - Initialize the bot in a chat
- `/history` - Show recent chat history
- `/clear` - Clear chat history
- `/urgent` - Force the bot back online
- `/sleep <duration>` - Put the bot to sleep (e.g., `/sleep 30s`, `/sleep 5m`, `/sleep 2h`)
- `/status` - Check bot's current status

## Configuration

Set the following environment variables:

```bash
# Telegram API
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_BOT_TOKEN=your_bot_token

# Allowed group IDs (comma-separated JSON array)
ALLOWED_GROUP_IDS=[123456789,987654321]

# Database
POSTGRES_USER=your_db_user
POSTGRES_PASSWORD=your_db_password
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DATABASE=your_db_name

# Dev Mode (optional)
DEV_MODE=false
DEV_CHAT_ID=123456789
```

## Installation

1. Clone the repository
2. Install dependencies: `pip install -r requirements.txt`
3. Set up your environment variables
4. Run the bot: `python main.py`

## Docker

```bash
# Build and run with docker-compose
docker-compose up --build
```