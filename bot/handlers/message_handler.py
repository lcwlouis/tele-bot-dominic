import random
import asyncio
import logging
from datetime import datetime
import uuid
from bot.config.settings import MIN_RESPONSE_DELAY, MAX_RESPONSE_DELAY
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types


logger = logging.getLogger(__name__)

class MessageHandler:
    def __init__(self, chat_history_manager, bot_state, command_handler, runner: Runner, session_service: DatabaseSessionService):
        self.chat_history_manager = chat_history_manager
        self.bot_state = bot_state
        self.command_handler = command_handler
        self.runner = runner
        self.session_service = session_service

    async def handle_message(self, event):
        """Handle incoming messages."""
        logger.info("\n=== New Message Received ===")
        logger.info(f"Time: {datetime.now()}")
        
        chat_id = str(event.chat_id)
        
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
        elif hasattr(event.message, 'media') and event.message.media:
            message_text = f"[{event.message.media.__class__.__name__}]"
        else:
            message_text = "[Other Media]"
        
        logger.info(f"Chat ID: {chat_id}")
        logger.info(f"Message Type: {message_text}")
        
        is_allowed = await self.command_handler.is_allowed_chat(int(chat_id))
        logger.info(f"Is allowed chat: {is_allowed}")
        
        # Check if this is a command
        if message_text.startswith('/'):
            return
        
        sender = await event.get_sender()
        
        should_process = await self.bot_state.should_process_message(int(chat_id))
        logger.info(f"Should process message: {should_process}")
        
        if not should_process:
            if self.bot_state.is_offline and not self.bot_state.is_sleeping:
                logger.info(f"Bot is offline until: {self.bot_state.offline_until}")
                # We append to the bot state the message that is queued in the same format as the chat history
                self.bot_state.message_queue.append({
                    'chat_id': chat_id,
                    'user': sender.first_name if sender else "Unknown User",
                    'username': sender.username if sender else None,
                    'message': event.message.message,
                    'timestamp': datetime.now().strftime("%d-%m-%Y %H:%M:%S")
                })
                logger.info(f"Message added to queue. Queue size: {len(self.bot_state.message_queue)}")
            elif self.bot_state.is_sleeping:
                logger.info(f"Bot is sleeping until: {self.bot_state.sleep_until}, ignoring message")
            return
        
        # Add message to buffer
        message_data = {
            'chat_id': chat_id,
            'user': sender.first_name if sender else "Unknown User",
            'username': sender.username if sender else None,
            'message': message_text,
            'timestamp': datetime.now().strftime("%d-%m-%Y %H:%M:%S")
        }
        self.bot_state.add_to_buffer(chat_id, message_data)
        
        # Add message to chat history
        self.chat_history_manager.add_message(
            chat_id=chat_id,
            user=sender.first_name if sender else "Unknown User",
            username=sender.username if sender else None,
            message=message_text,
            timestamp=datetime.now().strftime("%d-%m-%Y %H:%M:%S")
        )
        
        # Only process text messages for the agent
        if not hasattr(event.message, 'text') or not event.message.text:
            logger.info("Skipping non-text message for agent processing")
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
        
        try:
            # Create or get session for this chat
            session_id = f"chat_{chat_id}"
            try:
                logger.info(f"Getting session: {session_id}")
                session = await self.session_service.list_sessions(
                    app_name="dom",
                    user_id=chat_id,
                )
                session = session.sessions[0]
                logger.info(f"Session found: {session}")
            except Exception as e:
                logger.error(f"Error getting session: {e}")
                logger.info(f"Creating/getting session: {session_id}")
                session = await self.session_service.create_session(
                    app_name="dom",
                    user_id=chat_id,
                    session_id=session_id,
                )
            
            # Get buffered messages
            buffered_messages = self.bot_state.get_buffer(chat_id)
            
            # Create message object with context from chat history and buffered messages
            logger.info("Creating message object for agent...")
            
            # Include recent chat history in the context
            recent_history = self.chat_history_manager.get_recent_history(chat_id, limit=20)
            context = "\n".join([f"[{msg['timestamp']}] {msg['user']} (@{msg['username']}): {msg['message']}" for msg in recent_history])
            
            # Add buffered messages to context
            if buffered_messages:
                buffered_context = "\n".join([f"[{msg['timestamp']}] {msg['user']} (@{msg['username']}): {msg['message']}" for msg in buffered_messages])
                context = f"{context}\n\nNew Messages:\n{buffered_context}"
            
            logger.info(f"Context for agent:\n{context}")
            message = types.Content(role="user", parts=[types.Part(text=f"Recent chat context:\n{context}")])
            
            # Get response from agent using runner
            logger.info("Getting response from agent...")
            response_content = None
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
                    logger.info(f"Response part: {part.text}")
                    # Split response into multiple messages if it contains "%next_message%"
                    for text in part.text.split("%next_message%"):
                        if "%no_response%" in text:
                            logger.info("Received '%no_response%' part, skipping this message")
                            continue
                        messages.append(text.strip())

                # Send each message separately
                for msg in messages:
                    logger.info(f"msg: {msg}")
                    if msg.strip():  # Only send non-empty messages
                        # Add agent's response to chat history
                        self.chat_history_manager.add_message(
                            chat_id=chat_id,
                            user="Dom",
                            username="domthebuilderbot",
                            message=msg.strip()
                        )
                        
                        # Add a small delay between messages to make it feel more natural
                        await asyncio.sleep(1)
                        
                        # Send the response
                        await event.respond(msg.strip())
                        logger.info(f"Response sent successfully: {msg.strip()}")
            else:
                raise Exception("No response received from agent")
                
        except Exception as e:
            logger.error(f"Error getting response from agent: {e}")
            await event.respond("Sorry, I encountered an error while processing your message.")
        finally:
            # Clear the processing delay
            self.bot_state.clear_processing_delay(chat_id)
        
        logger.info("=== Message Processing Complete ===\n")

    async def handle_after_idling_messages(self, event):
        """Handle messages that were queued while the bot was offline.
        This will call the agent with all the new messages labeled under "New Messages:"
        """
        logger.info("\n=== Handling After Idling Messages ===")
        chat_id = str(event.chat_id)
        logger.info(f"Chat ID: {chat_id}")
        if not await self.command_handler.is_allowed_chat(int(chat_id)):
            logger.info("Chat is not allowed, skipping processing")
            return
        if not self.bot_state.message_queue:
            logger.info("No messages in queue, nothing to process")
            return
        if self.bot_state.is_sleeping:
            logger.info("Bot is sleeping, skipping processing of queued messages")
            return
        # Create or get session for this chat
        session_id = f"chat_{chat_id}"
        logger.info(f"Creating/getting session: {session_id}")
        await self.session_service.create_session(
            app_name="dom",
            user_id=chat_id,
            session_id=session_id,
        )
        # Create message object from the history since all new messages were appended there
        recent_history = self.chat_history_manager.get_recent_history(chat_id, limit=20)
        # Get messages that are not in the queue
        regular_messages = [msg for msg in recent_history if msg not in self.bot_state.message_queue]
        # Format regular messages
        regular_context = "\n".join([f"[{msg['timestamp']}] {msg['user']} (@{msg['username']}): {msg['message']}" for msg in regular_messages])
        # Format queued messages separately and add to chat history
        # Add queued messages to chat history and format them for context
        queued_context = ""
        for msg in self.bot_state.message_queue:
            self.chat_history_manager.add_message(
                chat_id=chat_id,
                user=msg['user'],
                username=msg['username'],
                message=msg['message'],
                timestamp=msg['timestamp']
            )
            queued_context += f"[{msg['timestamp']}] {msg['user']} (@{msg['username']}): {msg['message']}\n"
        
        # Clear the message queue after processing
        self.bot_state.message_queue.clear()
        # Combine contexts with a separator
        context = f"Recent chat context: {regular_context}\n\nNew Messages:\n{queued_context}" if queued_context else regular_context
        message = types.Content(
            role="user",
            parts=[types.Part(text=context)]
        )
        logger.info(f"Context for agent:\n{context}")
        logger.info("Getting response from agent...")
        response_content = None
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
                logger.info(f"Response part: {part.text}")
                # Split response into multiple messages if it contains "%next_message%"
                for text in part.text.split("%next_message%"):
                    if "%no_response%" in text:
                        logger.info("Received '%no_response%' part, skipping this message")
                        continue
                    messages.append(text.strip())

            # Send each message separately
            for msg in messages:
                logger.info(f"msg: {msg}")
                if msg.strip():  # Only send non-empty messages
                    # Add agent's response to chat history
                    self.chat_history_manager.add_message(
                        chat_id=chat_id,
                        user="Dom",
                        username="domthebuilderbot",
                        message=msg.strip()
                    )
                    
                    # Add a small delay between messages to make it feel more natural
                    await asyncio.sleep(1)
                    
                    # Send the response
                    await event.respond(msg.strip())
                    logger.info(f"Response sent successfully: {msg.strip()}")
        else:
            raise Exception("No response received from agent")
        logger.info("=== Urgent / Came back online Message Processing Complete ===\n") 