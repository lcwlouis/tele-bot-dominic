import asyncio
import logging
from telethon import TelegramClient, events
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from agent.agent import root_agent

from bot.config.settings import API_ID, API_HASH, BOT_TOKEN
from bot.utils.chat_history import ChatHistoryManager
from bot.utils.bot_state import BotState
from bot.handlers.commands import CommandHandler
from bot.handlers.message_handler import MessageHandler

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger(__name__)

chat_history_manager = ChatHistoryManager()

async def main():
    if not all([API_ID, API_HASH, BOT_TOKEN]):
        raise ValueError("TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_BOT_TOKEN environment variables must be set")
    
    # Initialize components
    bot_state = BotState()
    session_service = InMemorySessionService()
    runner = Runner(
        agent=root_agent,
        app_name="dom",
        session_service=session_service,
    )
    command_handler = CommandHandler(chat_history_manager, bot_state)
    message_handler = MessageHandler(chat_history_manager, bot_state, command_handler, runner, session_service)
    
    # Create the client
    client = TelegramClient('bot_session', API_ID, API_HASH)
    
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
    
    @client.on(events.NewMessage())
    async def incoming_message_handler(event):
        await message_handler.handle_message(event)
    
    # Start the bot
    logger.info("Starting the bot...")
    await client.start(bot_token=BOT_TOKEN)
    logger.info("Bot is running...")
    
    # Keep the bot running
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        logger.info("Saving chat history before exit...")
        chat_history_manager.save_history()
        logger.info("Chat history saved successfully.")
