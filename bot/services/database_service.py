from datetime import datetime, timedelta
from typing import Optional
import logging
import random
import asyncio
from sqlalchemy import create_engine, Column, String, DateTime, Text, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from bot.config.settings import DB_URL, MIN_ONLINE_TIME, MAX_ONLINE_TIME, DEV_MODE, DEV_CHAT_ID

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
        if DEV_MODE and int(chat_id) != DEV_CHAT_ID:
            logger.info(f"Skipping offline check for chat {chat_id} in dev mode")
            return False
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

            # Handle sleep state transition
            if chat_state.is_sleeping and chat_state.sleep_until and current_time >= chat_state.sleep_until:
                chat_state.is_sleeping = False
                chat_state.sleep_until = None
                logger.info(f"Chat {chat_id} sleep period ended")
                session.commit()
                return False

            # Handle offline state transition
            if chat_state.is_offline and chat_state.offline_until and current_time >= chat_state.offline_until:
                chat_state.is_offline = False
                chat_state.offline_until = None
                # Set new online period
                online_time = int(random.triangular(MIN_ONLINE_TIME, MAX_ONLINE_TIME, MAX_ONLINE_TIME - (MAX_ONLINE_TIME - MIN_ONLINE_TIME) * 0.2))
                chat_state.online_until = current_time + timedelta(seconds=online_time)
                logger.info(f"Chat {chat_id} offline period ended, transitioning to online till {chat_state.online_until}")
                session.commit()
                # Emit event for offline to online transition
                self.on_offline_to_online(chat_id)
                return False

            # Handle online state transition
            if not chat_state.is_offline and chat_state.online_until and current_time >= chat_state.online_until:
                chat_state.is_offline = True
                time_to_sleep = datetime.now().time().replace(hour=SLEEP_TIME.hour, minute=SLEEP_TIME.minute, second=SLEEP_TIME.second)
                time_to_wake_up = datetime.now().time().replace(hour=WAKE_UP_TIME.hour, minute=WAKE_UP_TIME.minute, second=WAKE_UP_TIME.second)
                if datetime.now().time() > time_to_sleep or datetime.now().time() < time_to_wake_up:
                    offline_time = int(random.triangular(MIN_OFFLINE_TIME, MAX_OFFLINE_TIME, MAX_OFFLINE_TIME - (MAX_OFFLINE_TIME - MIN_OFFLINE_TIME) * 0.2))
                    wake_up_time = datetime.now().replace(hour=WAKE_UP_TIME.hour, minute=WAKE_UP_TIME.minute, second=WAKE_UP_TIME.second)
                    chat_state.offline_until = wake_up_time + timedelta(seconds=offline_time)
                else:
                    offline_time = int(random.triangular(MIN_OFFLINE_TIME, MAX_OFFLINE_TIME, MAX_OFFLINE_TIME - (MAX_OFFLINE_TIME - MIN_OFFLINE_TIME) * 0.2))
                    chat_state.offline_until = current_time + timedelta(seconds=offline_time)
                logger.info(f"Chat {chat_id} online period ended, transitioning to offline till {chat_state.offline_until}")
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

def increase_online_time(chat_id: str, seconds: int = 60) -> dict:
    """Increase the online time for a chat. Default is 60 seconds.
    
    Args:
        chat_id: The chat ID
        seconds: The number of seconds to increase the online time by. Default is 60 seconds.
    
    Returns:
        Dictionary containing the online_until time and the time left in the online period in seconds
    """
    session = DatabaseService().Session()
    try:
        chat_state = session.query(ChatState).filter_by(chat_id=str(chat_id)).first()
        current_online_time = (chat_state.online_until - datetime.now()).total_seconds()
        chat_state.online_until = datetime.now() + timedelta(seconds=(int(current_online_time) + int(seconds)))
        session.commit()
        logger.info(f"Increased online time for chat {chat_id} by {seconds} seconds")
        return {
            "status": "success",
            "online_until": chat_state.online_until,
            "time_left_in_seconds": int(current_online_time) + int(seconds)
        }
    except Exception as e:
        session.rollback()
        logger.error(f"Error increasing online time: {e}")
        return {
            "status": "error",
            "error": str(e)
        }
    finally:
        session.close()