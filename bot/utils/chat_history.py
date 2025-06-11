import json
import logging
import os
from datetime import datetime
from bot.config.settings import HISTORY_FILE, MAX_HISTORY_LENGTH

logger = logging.getLogger(__name__)

class ChatHistoryManager:
    def __init__(self):
        self.chat_histories = {}
        self.load_history()

    def load_history(self):
        """Load chat history from file."""
        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                    loaded_history = json.load(f)
                    # Convert chat IDs to strings to ensure consistent key types
                    self.chat_histories = {str(chat_id): messages for chat_id, messages in loaded_history.items()}
        except Exception as e:
            logger.error(f"Error loading chat history: {e}")

    def save_history(self):
        """Save chat history to file."""
        try:
            # Convert chat IDs to strings to ensure consistent key types
            history_to_save = {str(chat_id): messages for chat_id, messages in self.chat_histories.items()}
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(history_to_save, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Error saving chat history: {e}")

    def add_message(self, chat_id: str, user: str, username: str, message: str, timestamp: str = None):
        """Add a message to chat history."""
        if chat_id not in self.chat_histories:
            self.chat_histories[chat_id] = []

        self.chat_histories[chat_id].append({
            "user": user,
            "username": username,
            "message": message,
            "timestamp": datetime.now().strftime("%d-%m-%Y %H:%M:%S")
        })

        # Trim history if it exceeds MAX_HISTORY_LENGTH
        if len(self.chat_histories[chat_id]) > MAX_HISTORY_LENGTH:
            self.chat_histories[chat_id] = self.chat_histories[chat_id][-MAX_HISTORY_LENGTH:]

        self.save_history()

    def get_recent_history(self, chat_id: str, limit: int = 20):
        """Get recent chat history for a specific chat."""
        if chat_id not in self.chat_histories:
            return []
        return self.chat_histories[chat_id][-limit:]

    def clear_history(self, chat_id: str):
        """Clear chat history for a specific chat."""
        if chat_id in self.chat_histories:
            self.chat_histories[chat_id] = []
            self.save_history()
            return True
        return False 