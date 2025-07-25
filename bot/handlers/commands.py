import logging
from bot.config.settings import ALLOWED_GROUP_IDS, DEV_MODE, DEV_CHAT_ID, SARCASTIC_LEVEL, PLAYFUL_LEVEL, HUMOR_LEVEL, FORMALITY_LEVEL, EMPATHY_LEVEL, ENTHUSIASM_LEVEL, SINGLISH_LEVEL, EMOJI_LEVEL
from .message_handler import MessageHandler
from bot.utils.bot_state import BotState
from google.adk.sessions import DatabaseSessionService


logger = logging.getLogger(__name__)

class CommandHandler:
    def __init__(self, bot_state: BotState, session_service: DatabaseSessionService):
        self.bot_state = bot_state
        self.session_service = session_service

    async def is_allowed_chat(self, chat_id: int) -> bool:
        """Check if the chat is in the whitelist or dev mode."""
        if DEV_MODE:
            logger.info(f"Dev mode enabled. Checking if chat ID {chat_id} matches dev chat ID {DEV_CHAT_ID}")
            return chat_id == DEV_CHAT_ID
        else:
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
        
        try:
            # Get session for this chat
            session_id = f"chat_{chat_id}"
            try:
                sessions = await self.session_service.list_sessions(
                    app_name="dom",
                    user_id=chat_id,
                )
                if not sessions.sessions:
                    await event.respond("No chat history found.")
                    return
                session = sessions.sessions[0]
            except Exception as e:
                logger.error(f"Error getting session: {e}")
                await event.respond("No chat history found.")
                return

            # Get session details
            session_object = await self.session_service.get_session(
                app_name="dom",
                user_id=chat_id,
                session_id=session.id,
            )

            if not session_object.events:
                await event.respond("No chat history found.")
                return

            # Format the history
            history_text = "📜 Recent messages in this chat:\n\n"
            for session_event in session_object.events[-20:]:  # Show last 20 messages
                if session_event.author == "user":
                    # Check if it is a function call
                    if session_event.content.parts[0].function_call:
                        history_text += f"Function called: {session_event.content.parts[0].function_call.name}\n"
                        history_text += f"Function response: {session_event.content.parts[0].function_call.response}\n"
                        continue
                    # Extract just the text portion from the content
                    content_text = session_event.content.parts[0].text if hasattr(session_event.content, 'parts') else session_event.content
                    history_text += f"User: {content_text}\n"
                elif session_event.author == "dom":
                    # Extract just the text portion from the content
                    # Handle multiple parts if they exist
                    if hasattr(session_event.content, 'parts'):
                        content_texts = []
                        for part in session_event.content.parts:
                            if part.function_call != None:
                                content_texts.append(f"Function called: {part.function_call.name}\n")
                                content_texts.append(f"Function arguments: {part.function_call.args}\n")
                                continue
                            if part.function_response != None:
                                content_texts.append(f"Function called: {part.function_response.name}\n")
                                content_texts.append(f"Function response: {part.function_response.response}\n")
                                continue
                            if hasattr(part, 'text'):
                                if part.text == None:
                                    print(part)
                                content_texts.append(part.text)
                        content_text = "\n".join(content_texts)
                    else:
                        content_text = session_event.content
                    
                    logger.debug(f"content_text: {content_text}")
                    history_text += f"Dom: {content_text.replace("%next_message%", "\n").replace("%no_response%", "")}\n"

            # Split message if it's too long for Telegram (4096 character limit)
            if len(history_text) >= 4096:
                # Split into chunks of 4000 characters to be safe
                chunk_size = 4000
                chunks = [history_text[i:i + chunk_size] for i in range(0, len(history_text), chunk_size)]
                
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        await event.respond(chunk)
                    else:
                        # Add continuation indicator for subsequent chunks
                        await event.respond(f"...(continued)\n{chunk}")
            else:
                await event.respond(history_text)
        except Exception as e:
            logger.error(f"Error getting chat history: {e}")
            await event.respond("Sorry, I encountered an error while retrieving the chat history.")

    async def handle_clear(self, event):
        """Handle the /clear command to clear chat history."""
        chat_id = str(event.chat_id)
        if not await self.is_allowed_chat(int(chat_id)):
            await event.respond("Sorry, I'm not allowed to participate in this chat.")
            return
        
        try:
            # Get session for this chat
            session_id = f"chat_{chat_id}"
            try:
                sessions = await self.session_service.list_sessions(
                    app_name="dom",
                    user_id=chat_id,
                )
                if not sessions.sessions:
                    await event.respond("No chat history to clear.")
                    return
                # session = sessions.sessions[0]
                if len(sessions.sessions) > 0:
                    session_oldest = sessions.sessions[-1]
                    # Delete the oldest session
                    await self.session_service.delete_session(
                        app_name="dom",
                        user_id=chat_id,
                        session_id=session_oldest.id,
                    )
                else:
                    await event.respond("No chat history to clear.")
            except Exception as e:
                logger.error(f"Error getting session: {e}")
                await event.respond("No chat history to clear.")
                return

            # Create a new session to clear history
            await self.session_service.create_session(
                app_name="dom",
                user_id=chat_id,
                session_id=session_id,
                state={
                    "chat_id": chat_id,
                    "individualisation_prompts": [],
                    "summary": "No summary available",
                    "sarcasm_level": SARCASTIC_LEVEL,
                    "playfulness_level": PLAYFUL_LEVEL,
                    "humor_level": HUMOR_LEVEL,
                    "formality_level": FORMALITY_LEVEL,
                    "empathy_level": EMPATHY_LEVEL,
                    "enthusiasm_level": ENTHUSIASM_LEVEL,
                    "singlish_level": SINGLISH_LEVEL,
                    "emoji_level": EMOJI_LEVEL,
                }
            )
            await event.respond("Chat history has been cleared.")
        except Exception as e:
            logger.error(f"Error clearing chat history: {e}")
            await event.respond("Sorry, I encountered an error while clearing the chat history.")

    async def handle_urgent(self, event, message_handler: MessageHandler):
        """Handle the /urgent command to force the bot back online."""
        chat_id = event.chat_id
        if not await self.is_allowed_chat(chat_id):
            await event.respond("Sorry, I'm not allowed to participate in this chat.")
            return
        
        if self.bot_state.is_offline(chat_id):
            # Force bot back online
            queued_messages = self.bot_state.force_online(chat_id)
            
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
            self.bot_state.set_sleep(chat_id, duration)
            
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

    async def handle_status(self, event):
        """Handle the /status command to check if the bot is offline and show last seen time."""
        chat_id = event.chat_id
        if not await self.is_allowed_chat(chat_id):
            await event.respond("Sorry, I'm not allowed to participate in this chat.")
            return
        
        status_message = self.bot_state.get_status(str(chat_id))
        await event.respond(status_message)

    def _format_time_delta(self, time_diff):
        """Format a timedelta into a human-readable string."""
        total_seconds = int(time_diff.total_seconds())
        if total_seconds < 60:
            return f"{total_seconds} seconds"
        elif total_seconds < 3600:
            minutes = total_seconds // 60
            return f"{minutes} minute{'s' if minutes != 1 else ''}"
        elif total_seconds < 86400:
            hours = total_seconds // 3600
            return f"{hours} hour{'s' if hours != 1 else ''}"
        else:
            days = total_seconds // 86400
            return f"{days} day{'s' if days != 1 else ''}" 