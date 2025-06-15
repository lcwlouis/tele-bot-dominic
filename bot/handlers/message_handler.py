import random
import asyncio
import logging
from datetime import datetime
import uuid
from bot.config.settings import MIN_RESPONSE_DELAY, MAX_RESPONSE_DELAY
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from google.adk.events import Event
from bot.utils.bot_state import BotState

logger = logging.getLogger(__name__)

class MessageHandler:
    def __init__(self, bot_state: BotState, command_handler, runner: Runner, session_service: DatabaseSessionService):
        self.bot_state = bot_state
        self.command_handler = command_handler
        self.runner = runner
        self.session_service = session_service
        self.client = None  # Will be set by main.py
    
    async def handle_message(self, event):
        """Handle incoming messages."""
        logger.info(f"\n=== New Message Received from {event.chat_id} at {datetime.now().strftime('%d-%m-%Y %I:%M %p')} ===")
        chat_id = str(event.chat_id)
        if not await self.command_handler.is_allowed_chat(int(chat_id)):
            await event.respond("Sorry, I'm not allowed to participate in this chat.")
            logger.info(f"Chat {chat_id} is not allowed, skipping message processing")
            return

        # Handle different types of messages
        message_text = ""
        if hasattr(event.message, 'text') and event.message.text:
            message_text = event.message.text
            # Skip if this is a response to /history command
            if message_text.startswith("📜 Recent messages in this chat:"):
                logger.info("Skipping history command response")
                return
            # Skip if message starts with /ignore
            if message_text.startswith("/ignore"):
                logger.info("Skipping message with /ignore prefix")
                return
            if message_text.startswith("/"):
                logger.info("Skipping command")
                return
        elif hasattr(event.message, 'media') and event.message.media:
            message_text = f"[{event.message.media.__class__.__name__}]"
        else:
            # This is not currently stored. 
            message_text = "[Other Media]"
        
        sender = await event.get_sender()
        logger.info(f"Sender: {sender.first_name if sender else 'Unknown User'} (@{sender.username if sender else 'Unknown Username'}), Message: {message_text}")
        
        # Filtering out all messages that are not text
        if not hasattr(event.message, 'text') or not event.message.text:
            logger.info("Skipping non-text message for agent processing")
            return
        
        # Filtering out all messages that are for a sleeping chat
        if self.bot_state.is_sleeping(chat_id):
            # We just return as sleeping messages will not be stored
            logger.info(f"Bot for chat {chat_id} is sleeping, ignoring message")
            return
        
        # Get session for this chat
        session_id = f"chat_{chat_id}"
        try:
            try:
                session = await self.session_service.list_sessions(
                    app_name="dom",
                    user_id=chat_id,
                )
                session = session.sessions[0]
            except Exception as e:
                logger.error(f"Could not find Session, creating new one: {e}")
                session = await self.session_service.create_session(
                    app_name="dom",
                    user_id=chat_id,
                    session_id=session_id,
                )

            # As long as chat is not sleeping, we add the message to the queued messages
            message_id = event.message.id
            reply_to_id = event.message.reply_to_msg_id if hasattr(event.message, 'reply_to_msg_id') else None
            new_message = f"[{datetime.now().strftime('%d-%m-%Y %I:%M %p')}] {sender.first_name if sender else 'Unknown User'} (@{sender.username if sender else 'Unknown Username'}) [msg_id:{message_id}{f' reply_to:{reply_to_id}' if reply_to_id else ''}]: {message_text}"
            await self.bot_state.add_to_queued_messages(chat_id, new_message)
            
            # Check if chat is offline
            if self.bot_state.is_offline(chat_id):
                logger.info(f"Chat {chat_id} is offline, skipping message processing")
                return
            
            # Check if we're in a processing delay
            if self.bot_state.is_in_processing_delay(chat_id):
                logger.info(f"Currently in processing delay for chat {chat_id}, message will be processed later")
                return
        
            # Set processing delay
            # Use triangular distribution to skew towards MIN_RESPONSE_DELAY
            delay = int(random.triangular(MIN_RESPONSE_DELAY, MAX_RESPONSE_DELAY, MIN_RESPONSE_DELAY + (MAX_RESPONSE_DELAY - MIN_RESPONSE_DELAY) * 0.20))
            logger.info(f"Setting processing delay of {delay} seconds for chat {chat_id}")
            self.bot_state.set_processing_delay(chat_id, delay)
        
            # Show typing status during the delay
            async with event.client.action(event.chat_id, 'typing'):
                await asyncio.sleep(delay)
                
                # Get queued messages
                queued_messages = await self.bot_state.get_queued_messages(chat_id)
                print(f"queued_messages: {queued_messages}")
                
                # Create message object with context from queued messages
                logger.info("Creating message object for agent...")
                first_message_id = queued_messages.split("msg_id:")[1].split(" ")[0].replace("]:", "")
                message = types.Content(role="user", parts=[types.Part(text=f"Your last message id was {first_message_id}.\nUnread messages:\n{queued_messages}")])
                logger.debug(f"Message object: {message}")
                
                # Get response from agent using runner
                logger.info("Getting response from agent...")
                response_content = None
                
                # Clear queued messages
                await self.bot_state.clear_queued_messages(chat_id)
                
                # async with event.client.action(event.chat_id, 'typing'):
                async for event_response in self.runner.run_async(
                    user_id=chat_id,
                    session_id=session_id,
                    new_message=message,
                ):
                    logger.info(f"Event response received: {event_response}")
                    if event_response.is_final_response():
                        logger.debug(f"Event response received: {event_response.content}")
                        response_content = event_response.content
                        break
                
                if response_content:
                    messages = []
                    for part in response_content.parts:
                        texts = part.text.split("Dom (@domthebuilderbot):")[-1]
                        logger.info(f"Response part: {texts}")
                        # Split response into multiple messages if it contains "%next_message%"
                        for text in texts.split("%next_message%"):
                            if "%no_response%" in text:
                                logger.info("Received '%no_response%' part, skipping this message")
                                continue
                            messages.append(text.strip())

                    # Send each message separately
                    for msg in messages:
                        logger.info(f"msg: {msg}")
                        if msg.strip():  # Only send non-empty messages
                            # Send the response
                            await event.respond(msg.strip())
                            logger.info(f"Response sent successfully: {msg.strip()}")
                            # Add a small delay between messages to make it feel more natural
                            await asyncio.sleep(random.triangular(1, 3, 2))
                    # Clear the processing delay
                    self.bot_state.clear_processing_delay(chat_id)
                else:
                    raise Exception("No response received from agent")
                    
        except Exception as e:
            logger.error(f"Error getting response from agent: {e}")
            await event.respond("Sorry, I encountered an error while processing your message.")
        
        logger.info("=== Message Processing Complete ===\n")

    async def handle_after_idling_messages(self, chat_id_or_event):
        """Handle messages that were queued while the bot was offline.
        This will call the agent with all the new messages labeled under "New Messages:"
        """
        logger.info("\n=== Handling After Idling Messages ===")
        
        # Handle both event and chat_id inputs
        if isinstance(chat_id_or_event, str):
            chat_id = chat_id_or_event
            # Create a mock event with the chat_id
            class MockEvent:
                def __init__(self, chat_id, client):
                    self.chat_id = int(chat_id)
                    self.client = client
                
                async def respond(self, message):
                    # Use the bot's client to send the message
                    if self.client:
                        await self.client.send_message(self.chat_id, message)
                    else:
                        logger.error("No client available to send message")
            
            event = MockEvent(chat_id, self.client)
        else:
            event = chat_id_or_event
            chat_id = str(event.chat_id)
            
        if not await self.command_handler.is_allowed_chat(int(chat_id)):
            await event.respond("Sorry, I'm not allowed to participate in this chat.")
            logger.info(f"Chat {chat_id} is not allowed, skipping message processing")
            return
        
        # Check if chat is sleeping
        if self.bot_state.is_sleeping(chat_id):
            logger.info(f"Bot for chat {chat_id} is sleeping, skipping message processing")
            return
        
        # Check if chat is offline
        if self.bot_state.is_offline(chat_id):
            logger.info(f"Chat {chat_id} is offline, skipping message processing")
            return

        # Check if there are any messages in the queue
        queued_messages = await self.bot_state.get_queued_messages(chat_id)
        if not queued_messages:
            logger.info("No messages in queue, nothing to process")
            return
        
        # Get or create session for this chat
        session_id = f"chat_{chat_id}"
        try:
            session = await self.session_service.list_sessions(
                app_name="dom",
                user_id=chat_id,
            )
            session = session.sessions[0]
        except Exception as e:
            logger.info(f"Could not find Session, creating new one: {e}")
            session = await self.session_service.create_session(
                app_name="dom",
                user_id=chat_id,
                session_id=session_id,
            )

        # Create message object with context from queued messages
        logger.info("Creating message object for agent...")
        first_message_id = queued_messages.split("msg_id:")[1].split(" ")[0].replace("]:", "")
        message = types.Content(role="user", parts=[types.Part(text=f"Your last message id was {first_message_id}.\nUnread messages:\n{queued_messages}")])
        logger.debug(f"Message object: {message}")
        
        # Get response from agent using runner
        logger.info("Getting response from agent...")
        response_content = None
        
        # Clear queued messages
        await self.bot_state.clear_queued_messages(chat_id)
        
        async with event.client.action(event.chat_id, 'typing'):
            async for event_response in self.runner.run_async(
                user_id=chat_id,
                session_id=session_id,
                new_message=message,
            ):
                logger.info(f"Event response received: {event_response}")
                if event_response.is_final_response():
                    logger.debug(f"Event response received: {event_response.content}")
                    response_content = event_response.content
                    break
            
            if response_content:
                messages = []
                for part in response_content.parts:
                    texts = part.text.split("Dom (@domthebuilderbot):")[-1]
                    logger.info(f"Response part: {texts}")
                    # Split response into multiple messages if it contains "%next_message%"
                    for text in texts.split("%next_message%"):
                        if "%no_response%" in text:
                            logger.info("Received '%no_response%' part, skipping this message")
                            continue
                        messages.append(text.strip())

                # Send each message separately
                for msg in messages:
                    logger.info(f"msg: {msg}")
                    if msg.strip():  # Only send non-empty messages
                        # Send the response
                        await event.respond(msg.strip())
                        logger.info(f"Response sent successfully: {msg.strip()}")
                        # Add a small delay between messages to make it feel more natural
                        await asyncio.sleep(random.triangular(1, 3, 2))
            else:
                raise Exception("No response received from agent")
        logger.info("=== After Idling / Urgent Messages Processing Complete ===\n")
