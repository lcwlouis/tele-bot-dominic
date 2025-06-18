import asyncio
import logging
from telethon import TelegramClient, events
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService

from agentConversation import get_conversation_agent

from bot.config.settings import API_ID, API_HASH, BOT_TOKEN, DB_URL, DEV_MODE, DEV_CHAT_ID, LOG_LEVEL
from bot.utils.bot_state import BotState
from bot.handlers.commands import CommandHandler
from bot.handlers.message_handler import MessageHandler
from bot.services.database_service import DatabaseService
from bot.utils.postgres_logger import PostgreSQLHandler

# Configure logging
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Keep console output for development
        PostgreSQLHandler(level=LOG_LEVEL)  # Add PostgreSQL logging
    ]
)

# Set specific logging levels for different components
# Agent modules - only log ERROR level to reduce spam
logging.getLogger('agentConversation').setLevel(logging.INFO)
logging.getLogger('agentSummariser').setLevel(logging.ERROR)

# Google ADK modules - set to WARNING to reduce noise
logging.getLogger('google.adk').setLevel(logging.WARNING)
logging.getLogger('google.adk.sessions').setLevel(logging.WARNING)

# Telethon - set to WARNING to reduce connection noise
logging.getLogger('telethon').setLevel(logging.WARNING)

# Keep main application logging at INFO level
logger = logging.getLogger(__name__)

async def main():
    if not all([API_ID, API_HASH, BOT_TOKEN]):
        raise ValueError("TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_BOT_TOKEN environment variables must be set")
    
    # Initialize components
    bot_state = BotState()
    session_service = DatabaseSessionService(db_url=DB_URL)
    runner = Runner(
        agent=get_conversation_agent(),
        app_name="dom",
        session_service=session_service,
    )
    command_handler = CommandHandler(bot_state, session_service)
    message_handler = MessageHandler(bot_state, command_handler, runner, session_service)
    
    # Initialize database service and start state checker
    db_service = DatabaseService()
    db_service.message_handler = message_handler  # Add message handler to database service
    state_checker_task = asyncio.create_task(db_service.start_state_checker())
    
    # Start periodic cleanup of stale summarization locks
    async def cleanup_stale_locks():
        """Periodically clean up stale summarization locks."""
        while True:
            try:
                await asyncio.sleep(300)  # Run every 5 minutes
                stale_count = bot_state.clear_stale_summarization_locks(timeout_minutes=30)
                if stale_count > 0:
                    logger.info(f"Cleaned up {stale_count} stale summarization locks")
            except Exception as e:
                logger.error(f"Error in stale lock cleanup: {e}")
                await asyncio.sleep(60)  # Wait a bit before retrying
    
    cleanup_task = asyncio.create_task(cleanup_stale_locks())
    
    # Create the client
    client = TelegramClient('bot_session', API_ID, API_HASH)
    
    # Add client to message handler
    message_handler.client = client
    
    # Register event handlers
    @client.on(events.NewMessage(pattern='/start'))
    async def start_handler(event):
        await command_handler.handle_start(event)
    
    @client.on(events.NewMessage(pattern='/history'))
    async def history_handler(event):
        await command_handler.handle_history(event)
    
    @client.on(events.NewMessage(pattern='/clear'))
    async def clear_handler(event):
        await command_handler.handle_clear(event)
    
    @client.on(events.NewMessage(pattern='/urgent'))
    async def urgent_handler(event):
        await command_handler.handle_urgent(event, message_handler=message_handler)
    
    @client.on(events.NewMessage(pattern='/sleep'))
    async def sleep_handler(event):
        await command_handler.handle_sleep(event)
    
    @client.on(events.NewMessage(pattern='/status'))
    async def status_handler(event):
        await command_handler.handle_status(event)
    
    list_to_patterns_to_exclude = [
        '/start',
        '/history',
        '/clear',
        '/urgent',
        '/sleep',
        '/urgent',
        '/',
    ]
    
    async def filter(event):
        return event.message.text not in list_to_patterns_to_exclude
    
    @client.on(events.NewMessage(func=filter))
    async def incoming_message_handler(event):
        await message_handler.handle_message(event)
    
    # Start the bot
    logger.info("Starting the bot...")
    if DEV_MODE:
        logger.info(f"🚀 DEV MODE ACTIVE - Only chat {DEV_CHAT_ID} will be allowed")
        logger.info("📝 Online status checks are disabled for dev chat")
    await client.start(bot_token=BOT_TOKEN)
    logger.info("Bot is running...")
    
    try:
        # Keep the bot running
        await client.run_until_disconnected()
    finally:
        # Clean up
        state_checker_task.cancel()
        cleanup_task.cancel()
        try:
            await state_checker_task
        except asyncio.CancelledError:
            pass
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        db_service.close()
        logger.info("Bot and state checker stopped")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
