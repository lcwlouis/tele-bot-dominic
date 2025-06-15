from datetime import datetime, timedelta
from typing import Optional
import logging
import random
import asyncio
from sqlalchemy import create_engine, Column, String, DateTime, Text, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from bot.config.settings import DB_URL, MIN_ONLINE_TIME, MAX_ONLINE_TIME

logger = logging.getLogger(__name__)
Base = declarative_base()

class ChatState(Base):
    __tablename__ = 'chat_states'
    
    chat_id = Column(String, primary_key=True)
    is_sleeping = Column(Boolean, default=False)
    sleep_until = Column(DateTime, nullable=True)
    is_offline = Column(Boolean, default=False)
    offline_until = Column(DateTime, nullable=True)
    online_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class ProcessingDelay(Base):
    __tablename__ = 'processing_delays'
    
    chat_id = Column(String, primary_key=True)
    delay_until = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.now)

class MessageQueue(Base):
    __tablename__ = 'message_queues'
    
    chat_id = Column(String, primary_key=True)
    messages = Column(Text)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

class DatabaseService:
    def __init__(self):
        self.engine = create_engine(DB_URL)
        self.Session = sessionmaker(bind=self.engine)
        Base.metadata.create_all(self.engine)

    def _initialize_chat_state(self, chat_id: str) -> ChatState:
        """Initialize a new chat state with default values."""
        current_time = datetime.now()
        online_time = int(random.triangular(MIN_ONLINE_TIME, MAX_ONLINE_TIME, MAX_ONLINE_TIME - (MAX_ONLINE_TIME - MIN_ONLINE_TIME) * 0.2))
        return ChatState(
            chat_id=str(chat_id),
            is_sleeping=False,
            is_offline=False,
            online_until=current_time + timedelta(seconds=online_time)
        )

    def get_chat_state(self, chat_id: str) -> Optional[ChatState]:
        """Get the state for a chat."""
        session = self.Session()
        try:
            chat_state = session.query(ChatState).filter_by(chat_id=str(chat_id)).first()
            if not chat_state:
                # Initialize new chat state if it doesn't exist
                chat_state = self._initialize_chat_state(str(chat_id))
                session.add(chat_state)
                session.commit()
                logger.info(f"Initialized new chat state for {chat_id}")
            return chat_state
        finally:
            session.close()

    def set_chat_state(self, chat_id: str, is_sleeping: bool = None, sleep_until: datetime = None,
                      is_offline: bool = None, offline_until: datetime = None,
                      online_until: datetime = None) -> None:
        """Set the state for a chat."""
        session = self.Session()
        try:
            chat_state = session.query(ChatState).filter_by(chat_id=str(chat_id)).first()
            if not chat_state:
                chat_state = self._initialize_chat_state(str(chat_id))

            if is_sleeping is not None:
                chat_state.is_sleeping = is_sleeping
            if sleep_until is not None:
                chat_state.sleep_until = sleep_until
            if is_offline is not None:
                chat_state.is_offline = is_offline
            if offline_until is not None:
                chat_state.offline_until = offline_until
            if online_until is not None:
                chat_state.online_until = online_until

            session.commit()
            logger.info(f"Updated chat state for {chat_id}")
        except Exception as e:
            session.rollback()
            logger.error(f"Error updating chat state: {e}")
            raise
        finally:
            session.close()

    def is_chat_sleeping(self, chat_id: str) -> bool:
        """Check if a chat is in sleeping state."""
        with self.Session() as session:
            chat_state = session.query(ChatState).filter(ChatState.chat_id == chat_id).first()
            if not chat_state:
                return False
            # Refresh the instance to ensure it's not detached
            session.refresh(chat_state)
            if not chat_state.is_sleeping:
                return False
            if chat_state.sleep_until and datetime.now() >= chat_state.sleep_until:
                # Auto-wake up if sleep time is over
                self.set_chat_state(str(chat_id), is_sleeping=False, sleep_until=None)
                return False
            return True

    def is_chat_offline(self, chat_id: str) -> bool:
        """Check if a chat is offline and handle state transitions."""
        # logger.info(f"Checking if chat {chat_id} is offline")
        with self.Session() as session:
            chat_state = session.query(ChatState).filter(ChatState.chat_id == chat_id).first()
            if not chat_state:
                logger.info(f"Chat {chat_id} not found, initializing new state")
                chat_state = self._initialize_chat_state(str(chat_id))
                session.add(chat_state)
                session.commit()
                return False

            current_time = datetime.now()
            session.refresh(chat_state)

            # Handle offline state transition
            if chat_state.is_offline and chat_state.offline_until and current_time >= chat_state.offline_until:
                logger.info(f"Chat {chat_id} offline period ended, transitioning to online")
                chat_state.is_offline = False
                chat_state.offline_until = None
                # Set new online period
                online_time = int(random.triangular(MIN_ONLINE_TIME, MAX_ONLINE_TIME, MAX_ONLINE_TIME - (MAX_ONLINE_TIME - MIN_ONLINE_TIME) * 0.2))
                chat_state.online_until = current_time + timedelta(seconds=online_time)
                session.commit()
                # Emit event for offline to online transition
                self.on_offline_to_online(chat_id)
                return False

            # Handle online state transition
            if not chat_state.is_offline and chat_state.online_until and current_time >= chat_state.online_until:
                logger.info(f"Chat {chat_id} online period ended, transitioning to offline")
                chat_state.is_offline = True
                offline_time = int(random.triangular(MIN_ONLINE_TIME, MAX_ONLINE_TIME, MAX_ONLINE_TIME - (MAX_ONLINE_TIME - MIN_ONLINE_TIME) * 0.2))
                chat_state.offline_until = current_time + timedelta(seconds=offline_time)
                session.commit()
                return True

            return chat_state.is_offline

    def on_offline_to_online(self, chat_id: str):
        """Callback for when a chat transitions from offline to online."""
        if hasattr(self, 'message_handler'):
            asyncio.create_task(self.message_handler.handle_after_idling_messages(chat_id))

    def is_chat_online(self, chat_id: str) -> bool:
        """Check if a chat is online and handle state transitions."""
        logger.info(f"Checking if chat {chat_id} is online")
        return not self.is_chat_offline(chat_id) and not self.is_chat_sleeping(chat_id)

    async def start_state_checker(self):
        """Start a background task to periodically check and update chat states."""
        while True:
            try:
                with self.Session() as session:
                    chat_states = session.query(ChatState).all()
                    for chat_state in chat_states:
                        # This will trigger state transitions if needed
                        self.is_chat_offline(chat_state.chat_id)
                await asyncio.sleep(60)  # Check every 60 seconds
            except Exception as e:
                logger.error(f"Error in state checker: {e}")
                await asyncio.sleep(60)  # Still wait before retrying

    def set_processing_delay(self, chat_id: str, delay_until: datetime) -> None:
        """Set processing delay for a chat."""
        session = self.Session()
        try:
            delay = ProcessingDelay(
                chat_id=str(chat_id),
                delay_until=delay_until
            )
            session.merge(delay)
            session.commit()
            logger.info(f"Set processing delay for chat {chat_id} until {delay_until}")
        except Exception as e:
            session.rollback()
            logger.error(f"Error setting processing delay: {e}")
            raise
        finally:
            session.close()

    def get_processing_delay(self, chat_id: str) -> Optional[datetime]:
        """Get processing delay for a chat."""
        session = self.Session()
        try:
            delay = session.query(ProcessingDelay).filter_by(chat_id=str(chat_id)).first()
            return delay.delay_until if delay else None
        finally:
            session.close()

    def clear_processing_delay(self, chat_id: str) -> None:
        """Clear processing delay for a chat."""
        session = self.Session()
        try:
            session.query(ProcessingDelay).filter_by(chat_id=str(chat_id)).delete()
            session.commit()
            logger.info(f"Cleared processing delay for chat {chat_id}")
        except Exception as e:
            session.rollback()
            logger.error(f"Error clearing processing delay: {e}")
            raise
        finally:
            session.close()

    def add_to_message_queue(self, chat_id: str, message: str) -> None:
        """Add a message to the queue for a chat."""
        session = self.Session()
        try:
            queue = session.query(MessageQueue).filter_by(chat_id=str(chat_id)).first()
            if queue:
                queue.messages = (queue.messages or "") + f"{message}\n"
            else:
                queue = MessageQueue(chat_id=str(chat_id), messages=f"{message}\n")
                session.add(queue)
            session.commit()
            logger.info(f"Message added to queue for chat {chat_id}")
        except Exception as e:
            session.rollback()
            logger.error(f"Error adding message to queue: {e}")
            raise
        finally:
            session.close()

    def get_message_queue(self, chat_id: str) -> str:
        """Get all queued messages for a chat."""
        session = self.Session()
        try:
            queue = session.query(MessageQueue).filter_by(chat_id=str(chat_id)).first()
            return queue.messages if queue else ""
        finally:
            session.close()

    def clear_message_queue(self, chat_id: str) -> None:
        """Clear all queued messages for a chat."""
        session = self.Session()
        try:
            session.query(MessageQueue).filter_by(chat_id=str(chat_id)).delete()
            session.commit()
            logger.info(f"Cleared message queue for chat {chat_id}")
        except Exception as e:
            session.rollback()
            logger.error(f"Error clearing message queue: {e}")
            raise
        finally:
            session.close()

    def close(self):
        """Close the database connection."""
        self.engine.dispose()
        logger.info("Database connection closed") 