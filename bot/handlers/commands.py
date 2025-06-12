import logging
from datetime import datetime
from bot.config.settings import ALLOWED_GROUP_IDS, MAX_HISTORY_LENGTH
from .message_handler import MessageHandler



logger = logging.getLogger(__name__)

class CommandHandler:
    def __init__(self, chat_history_manager, bot_state):
        self.chat_history_manager = chat_history_manager
        self.bot_state = bot_state

    async def is_allowed_chat(self, chat_id: int) -> bool:
        """Check if the chat is in the whitelist."""
        logger.info(f"Checking if chat ID {chat_id} is allowed... against {ALLOWED_GROUP_IDS}")
        return chat_id in ALLOWED_GROUP_IDS

    async def handle_start(self, event):
        """Handle the /start command."""
        chat_id = event.chat_id
        if await self.is_allowed_chat(chat_id):
            await event.respond("Hello! I'm now active in this group chat.")
        else:
            await event.respond("Sorry, I'm not allowed to participate in this chat.")

    async def handle_history(self, event):
        """Handle the /history command to show recent chat history."""
        chat_id = str(event.chat_id)
        if not await self.is_allowed_chat(int(chat_id)):
            await event.respond("Sorry, I'm not allowed to participate in this chat.")
            return
        
        history = self.chat_history_manager.get_recent_history(chat_id, MAX_HISTORY_LENGTH)
        
        if not history:
            await event.respond("No recent messages in the chat history.")
            return
        
        # Format the history with timestamps
        history_text = "📜 Recent messages in this chat:\n\n"
        for msg in history:
            formatted_time = msg['timestamp']
            user_mention = f"@{msg['username']}" if msg.get('username') else msg['user']
            history_text += f"[{formatted_time}] {user_mention}: {msg['message']}\n"
        
        # Add to chat history
        self.chat_history_manager.add_message(
            chat_id=chat_id,
            user="Dom",
            username="domthebuilderbot",
            message=history_text
        )
        
        await event.respond(history_text)

    async def handle_clear(self, event):
        """Handle the /clear command to clear chat history."""
        chat_id = str(event.chat_id)
        if not await self.is_allowed_chat(int(chat_id)):
            await event.respond("Sorry, I'm not allowed to participate in this chat.")
            return
        
        if self.chat_history_manager.clear_history(chat_id):
            await event.respond("Chat history has been cleared.")
            logger.info(f"Chat history cleared for chat ID: {chat_id}")
        else:
            await event.respond("No chat history to clear.")

    async def handle_urgent(self, event, message_handler: MessageHandler):
        """Handle the /urgent command to force the bot back online."""
        chat_id = event.chat_id
        if not await self.is_allowed_chat(chat_id):
            await event.respond("Sorry, I'm not allowed to participate in this chat.")
            return
        
        if self.bot_state.is_offline:
            # Force bot back online
            queued_messages = self.bot_state.force_online()
            
            if queued_messages:
                await event.respond("I'm back online! What's up? Let's catch up on the messages I missed")
                # Now process the messages in the context
                await message_handler.handle_after_idling_messages(event)
            else:
                await event.respond("I'm back online! What's up?")
        else:
            await event.respond("I'm already online! What do you need?")

    async def handle_sleep(self, event):
        """Handle the /sleep command to put the bot to sleep for a specified duration."""
        chat_id = event.chat_id
        if not await self.is_allowed_chat(chat_id):
            await event.respond("Sorry, I'm not allowed to participate in this chat.")
            return

        # Get the duration from the message
        message_text = event.message.text.strip()
        parts = message_text.split()
        
        if len(parts) != 2:
            await event.respond("Please specify a duration in the format: /sleep <duration>\nExample: /sleep 30s, /sleep 5m, /sleep 2h, /sleep 1d")
            return

        try:
            duration = self.bot_state.parse_sleep_duration(parts[1])
            self.bot_state.set_sleep(duration)
            
            # Format the duration for display
            if duration < 60:
                display_duration = f"{duration} seconds"
            elif duration < 3600:
                display_duration = f"{duration // 60} minutes"
            elif duration < 86400:
                display_duration = f"{duration // 3600} hours"
            else:
                display_duration = f"{duration // 86400} days"
            
            await event.respond(f"I'm going to sleep for {display_duration}. Use /urgent to wake me up if needed.")
        except ValueError as e:
            await event.respond(str(e)) 