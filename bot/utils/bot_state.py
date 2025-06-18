import random
from datetime import datetime, timedelta
import logging
from bot.config.settings import MAX_ONLINE_TIME, MIN_ONLINE_TIME, DEV_MODE, DEV_CHAT_ID
from bot.services.database_service import DatabaseService

logger = logging.getLogger(__name__)

class BotState:
    def __init__(self):
        self.db = DatabaseService()
    
    def is_online(self, chat_id: str) -> bool:
        """Check if the bot is online for a specific chat."""
        # check if chat is allowed
        if DEV_MODE and int(chat_id) != DEV_CHAT_ID:
            return False
        return self.db.is_chat_online(str(chat_id))
    
    def is_sleeping(self, chat_id: str) -> bool:
        """Check if the bot is sleeping for a specific chat."""
        # check if chat is allowed
        if DEV_MODE and int(chat_id) != DEV_CHAT_ID:
            return False
        return self.db.is_chat_sleeping(str(chat_id))
    
    def is_offline(self, chat_id: str) -> bool:
        """Check if the bot is offline for a specific chat."""
        # check if chat is allowed
        if DEV_MODE and int(chat_id) != DEV_CHAT_ID:
            return False
        return self.db.is_chat_offline(str(chat_id))

    def set_processing_delay(self, chat_id: str, delay_seconds: int):
        """Set a processing delay for a specific chat."""
        delay_until = datetime.now() + timedelta(seconds=delay_seconds)
        self.db.set_processing_delay(str(chat_id), delay_until)
        logger.info(f"Set processing delay for chat {chat_id} until {delay_until}")
        return delay_until

    def is_in_processing_delay(self, chat_id: str) -> bool:
        """Check if a chat is currently in processing delay."""
        delay_until = self.db.get_processing_delay(str(chat_id))
        if not delay_until:
            return False
        return datetime.now() < delay_until

    def clear_processing_delay(self, chat_id: str):
        """Clear the processing delay for a specific chat."""
        self.db.clear_processing_delay(str(chat_id))
        logger.info(f"Cleared processing delay for chat {chat_id}")

    def add_to_queued_messages(self, chat_id: str, message: str) -> int:
        """Add a message to the queued messages for a specific chat."""
        number_of_queued_messages = self.db.add_to_message_queue(str(chat_id), str(message))
        logger.info(f"Message added to queued messages for chat {chat_id}")
        return number_of_queued_messages

    def get_queued_messages(self, chat_id: str) -> str:
        """Get the queued messages for a specific chat."""
        return self.db.get_message_queue(str(chat_id))

    def clear_queued_messages(self, chat_id: str):
        """Clear the queued messages for a specific chat."""
        self.db.clear_message_queue(str(chat_id))
        logger.info(f"Cleared queued messages for chat {chat_id}")

    def set_sleep(self, chat_id: str, duration_seconds: int):
        """Set the bot to sleep mode for the specified duration for a specific chat."""
        sleep_until = datetime.now() + timedelta(seconds=duration_seconds)
        self.db.set_chat_state(
            str(chat_id),
            is_sleeping=True,
            sleep_until=sleep_until,
            is_offline=True  # Also set offline to prevent message processing
        )
        logger.info(f"Bot is now sleeping for chat {chat_id} until: {sleep_until}")

    def force_online(self, chat_id: str):
        """Force the bot back online for a specific chat."""
        current_time = datetime.now()
        online_time = int(random.triangular(MIN_ONLINE_TIME, MAX_ONLINE_TIME, MAX_ONLINE_TIME - (MAX_ONLINE_TIME - MIN_ONLINE_TIME) * 0.2))
        self.db.set_chat_state(
            str(chat_id),
            is_sleeping=False,
            sleep_until=None,
            is_offline=False,
            offline_until=None,
            online_until=current_time + timedelta(seconds=online_time)
        )
        logger.info(f"Bot forced online for chat {chat_id}")
        return self.get_queued_messages(chat_id)

    def parse_sleep_duration(self, duration_str: str) -> int:
        """Parse sleep duration string into seconds.
        Format: <number><unit> where unit is s (seconds), m (minutes), h (hours), d (days)
        """
        try:
            # Get the last character as the unit
            unit = duration_str[-1].lower()
            # Get the number part
            number = int(duration_str[:-1])
            
            # Convert to seconds based on unit
            if unit == 's':
                return number
            elif unit == 'm':
                return number * 60
            elif unit == 'h':
                return number * 3600
            elif unit == 'd':
                return number * 86400
            else:
                raise ValueError(f"Invalid time unit: {unit}")
        except (ValueError, IndexError) as e:
            raise ValueError(f"Invalid duration format. Use format: <number><unit> (e.g., 30s, 5m, 2h, 1d)")

    def close(self):
        """Close the database connection."""
        self.db.close()

    def is_chat_online(self, chat_id: str) -> bool:
        """Check if a chat is online and handle state transitions."""
        logger.info(f"Checking if chat {chat_id} is online")
        return not self.is_offline(chat_id) and not self.is_sleeping(chat_id)

    def get_status(self, chat_id: str) -> str:
        """Get the status of a chat."""
        if self.is_chat_online(chat_id):
            return "I'm online and ready to chat!"
        elif self.is_sleeping(chat_id):
            return "I'm currently sleeping. I'll be back soon!"
        elif self.is_offline(chat_id):
            return "I'm currently offline. I'll be back soon!"
        else:
            return "I'm currently offline. I'll be back soon!"

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

    async def start_state_checker(self):
        # Implementation of start_state_checker method
        pass 

    def set_summarization_lock(self, chat_id: str) -> bool:
        """Set a summarization lock for a chat. Returns True if lock was acquired, False if already locked."""
        return self.db.set_summarization_lock(str(chat_id))

    def is_summarization_locked(self, chat_id: str) -> bool:
        """Check if a chat has a summarization lock."""
        return self.db.is_summarization_locked(str(chat_id))

    def clear_summarization_lock(self, chat_id: str):
        """Clear the summarization lock for a chat."""
        self.db.clear_summarization_lock(str(chat_id))
        logger.info(f"Cleared summarization lock for chat {chat_id}")

    def clear_stale_summarization_locks(self, timeout_minutes: int = 30) -> int:
        """Clear stale summarization locks that are older than the specified timeout.
        
        Args:
            timeout_minutes: Number of minutes after which a lock is considered stale. Default is 30 minutes.
            
        Returns:
            Number of stale locks cleared
        """
        count = self.db.clear_stale_summarization_locks(timeout_minutes)
        if count > 0:
            logger.info(f"Cleared {count} stale summarization locks")
        return count 