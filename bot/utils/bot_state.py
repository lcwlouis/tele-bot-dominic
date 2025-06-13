import random
import asyncio
from datetime import datetime, timedelta
import logging
from bot.config.settings import MIN_OFFLINE_TIME, MAX_OFFLINE_TIME, MAX_ONLINE_TIME, MIN_ONLINE_TIME

logger = logging.getLogger(__name__)

class BotState:
    def __init__(self):
        self.is_offline = False
        self.offline_until = None
        self.message_queue = []
        self.message_buffer = {}  # chat_id -> list of messages
        self.last_response_time = None
        self.processing_delay_until = {}  # chat_id -> datetime
        self.is_sleeping = False  # New state for sleep mode
        self.sleep_until = None  # When to wake up from sleep
        # Initialize the first online period
        current_time = datetime.now()
        online_time = int(random.triangular(MIN_ONLINE_TIME, MAX_ONLINE_TIME, MAX_ONLINE_TIME - (MAX_ONLINE_TIME - MIN_ONLINE_TIME) * 0.2))
        self.online_until = current_time + timedelta(seconds=online_time)
        logger.info(f"Bot initialized and will be online until: {self.online_until}")

    def add_to_buffer(self, chat_id: str, message: dict):
        """Add a message to the buffer for a specific chat."""
        if chat_id not in self.message_buffer:
            self.message_buffer[chat_id] = []
        self.message_buffer[chat_id].append(message)
        logger.info(f"Message added to buffer for chat {chat_id}. Buffer size: {len(self.message_buffer[chat_id])}")

    def get_buffer(self, chat_id: str) -> list:
        """Get and clear the message buffer for a specific chat."""
        messages = self.message_buffer.get(chat_id, [])
        self.message_buffer[chat_id] = []
        return messages

    def has_buffered_messages(self, chat_id: str) -> bool:
        """Check if there are any buffered messages for a chat."""
        return len(self.message_buffer.get(chat_id, [])) > 0

    def set_processing_delay(self, chat_id: str, delay_seconds: int):
        """Set a processing delay for a specific chat."""
        self.processing_delay_until[chat_id] = datetime.now() + timedelta(seconds=delay_seconds)
        logger.info(f"Set processing delay for chat {chat_id} until {self.processing_delay_until[chat_id]}")

    def is_in_processing_delay(self, chat_id: str) -> bool:
        """Check if a chat is currently in processing delay."""
        if chat_id not in self.processing_delay_until:
            return False
        return datetime.now() < self.processing_delay_until[chat_id]

    def clear_processing_delay(self, chat_id: str):
        """Clear the processing delay for a specific chat."""
        if chat_id in self.processing_delay_until:
            del self.processing_delay_until[chat_id]
            logger.info(f"Cleared processing delay for chat {chat_id}")

    async def should_process_message(self, chat_id: int) -> bool:
        """Determine if the bot should process messages in this chat."""
        current_time = datetime.now()
        
        # If bot is sleeping, only wake up if sleep time is over
        if self.is_sleeping:
            if current_time >= self.sleep_until:
                self.is_sleeping = False
                self.is_offline = False
                # Set the next online period
                online_time = int(random.triangular(MIN_ONLINE_TIME, MAX_ONLINE_TIME, MAX_ONLINE_TIME - (MAX_ONLINE_TIME - MIN_ONLINE_TIME) * 0.2))
                self.online_until = current_time + timedelta(seconds=online_time)
                logger.info(f"Bot woke up from sleep and is now online until: {self.online_until}")
                return True
            return False
        
        # If bot is offline, check if it's time to come back online
        if self.is_offline:
            logger.info(f"Current time: {current_time}, Offline until: {self.offline_until}")
            if current_time >= self.offline_until:
                self.is_offline = False
                # Set the next online period
                online_time = int(random.triangular(MIN_ONLINE_TIME, MAX_ONLINE_TIME, MAX_ONLINE_TIME - (MAX_ONLINE_TIME - MIN_ONLINE_TIME) * 0.2))
                self.online_until = current_time + timedelta(seconds=online_time)
                logger.info(f"Bot is now online until: {self.online_until}")
                return True
            return False
        
        # If bot is online, check if it's time to go offline
        if self.online_until and current_time >= self.online_until:
            offline_time = random.randint(MIN_OFFLINE_TIME, MAX_OFFLINE_TIME)
            logger.info(f"Going offline for {offline_time} seconds.")
            self.is_offline = True
            self.offline_until = current_time + timedelta(seconds=offline_time)
            logger.info(f"Bot will be offline until: {self.offline_until}")
            # Start the offline timer task
            asyncio.create_task(self.process_offline_timer(chat_id, offline_time))
            return False
        
        return True

    async def process_offline_timer(self, chat_id: int, offline_time: int):
        """Process the latest message after the offline timer expires."""
        await asyncio.sleep(offline_time)
        logger.info(f"Offline timer expired for chat ID {chat_id}. Processing queued messages...")
        # Only process if we're still offline and this is the most recent timer
        logger.info(f"Current offline state: {self.is_offline}, Time diff: {(self.offline_until - datetime.now()).total_seconds()}")
        if self.is_offline and (self.offline_until - datetime.now()).total_seconds() < 5:
            return True
        return False

    def force_online(self):
        """Force the bot back online."""
        self.is_offline = False
        self.is_sleeping = False  # Also wake up from sleep
        self.offline_until = None
        self.sleep_until = None
        # Set a new online period when forcing online
        current_time = datetime.now()
        online_time = int(random.triangular(MIN_ONLINE_TIME, MAX_ONLINE_TIME, MAX_ONLINE_TIME - (MAX_ONLINE_TIME - MIN_ONLINE_TIME) * 0.2))
        self.online_until = current_time + timedelta(seconds=online_time)
        logger.info(f"Bot forced online until: {self.online_until}")
        return self.message_queue.copy()

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

    def set_sleep(self, duration_seconds: int):
        """Set the bot to sleep mode for the specified duration."""
        self.is_sleeping = True
        self.is_offline = True  # Also set offline to prevent message processing
        self.sleep_until = datetime.now() + timedelta(seconds=duration_seconds)
        logger.info(f"Bot is now sleeping until: {self.sleep_until}") 