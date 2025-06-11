import os
import json
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Telegram API Configuration
API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_GROUP_IDS = json.loads(os.getenv("ALLOWED_GROUP_IDS", "[]"))

# Bot Behavior Configuration
OFFLINE_CHANCE = float(os.getenv("OFFLINE_CHANCE", "0.8"))  # 10% chance to go offline
MIN_OFFLINE_TIME = 180  # 3 minutes in seconds
MAX_OFFLINE_TIME = 3600  # 1 hour in seconds
MIN_ONLINE_TIME = 30  # 30 seconds minimum online time
MAX_ONLINE_TIME = 600  # 10 minutes maximum online time
MIN_RESPONSE_DELAY = 3  # 3 seconds
MAX_RESPONSE_DELAY = 20  # 5 seconds
MAX_HISTORY_LENGTH = 100  # Maximum number of messages to keep in history
HISTORY_FILE = "chat_history.json"  # File to store chat histories 

# omg there should be random values to determin how playful the bot is or sarcastic