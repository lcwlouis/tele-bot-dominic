import os
import json
import datetime
from dotenv import load_dotenv
from sqlalchemy.engine.url import URL
import logging

# Load environment variables
load_dotenv()

# Logging Configuration
if os.getenv("LOG_LEVEL") == "DEBUG":
    LOG_LEVEL = logging.DEBUG
elif os.getenv("LOG_LEVEL") == "INFO":
    LOG_LEVEL = logging.INFO
elif os.getenv("LOG_LEVEL") == "WARNING":
    LOG_LEVEL = logging.WARNING
else:
    LOG_LEVEL = logging.INFO

# Telegram API Configuration
API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_GROUP_IDS = json.loads(os.getenv("ALLOWED_GROUP_IDS", "[]"))

# Dev Mode Configuration
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"
DEV_CHAT_ID = int(os.getenv("DEV_CHAT_ID", "0")) if os.getenv("DEV_CHAT_ID") else None

# Database Configuration
DB_URL = URL.create(
    drivername="postgresql",
    username=os.getenv("POSTGRES_USER"),
    password=os.getenv("POSTGRES_PASSWORD"),
    host=os.getenv("POSTGRES_HOST"),
    port=os.getenv("POSTGRES_PORT",""),
    database=os.getenv("POSTGRES_DATABASE")
)

# Bot Behavior Configuration
WAKE_UP_TIME = datetime.time.fromisoformat(os.getenv("WAKE_UP_TIME", "07:30:00"))  # Format: HH:MM:SS
SLEEP_TIME = datetime.time.fromisoformat(os.getenv("SLEEP_TIME", "23:59:59"))  # Format: HH:MM:SS
MIN_OFFLINE_TIME = int(os.getenv("MIN_OFFLINE_TIME", 300))  # in seconds
MAX_OFFLINE_TIME = int(os.getenv("MAX_OFFLINE_TIME", 3600))  # in seconds
MIN_ONLINE_TIME = int(os.getenv("MIN_ONLINE_TIME", 300))  # in seconds
MAX_ONLINE_TIME = int(os.getenv("MAX_ONLINE_TIME", 900))  # in seconds
MIN_RESPONSE_DELAY = int(os.getenv("MIN_RESPONSE_DELAY", 3))  # in seconds
MAX_RESPONSE_DELAY = int(os.getenv("MAX_RESPONSE_DELAY", 12))  # in seconds
SUMMARISING_AGENT_TOKEN_THRESHOLD = int(os.getenv("SUMMARISING_AGENT_TOKEN_THRESHOLD", 4000))
MAX_OFFLINE_MESSAGES = int(os.getenv("MAX_OFFLINE_MESSAGES", 50))

# Personality Parameters (0.0 to 1.0 scale)
SARCASTIC_LEVEL = float(os.getenv("SARCASTIC_LEVEL", "0.7"))
PLAYFUL_LEVEL = float(os.getenv("PLAYFUL_LEVEL", "0.7"))
HUMOR_LEVEL = float(os.getenv("HUMOR_LEVEL", "0.4"))
FORMALITY_LEVEL = float(os.getenv("FORMALITY_LEVEL", "0.5"))
EMPATHY_LEVEL = float(os.getenv("EMPATHY_LEVEL", "0.7"))
ENTHUSIASM_LEVEL = float(os.getenv("ENTHUSIASM_LEVEL", "0.5"))
SINGLISH_LEVEL = float(os.getenv("SINGLISH_LEVEL", "0.05"))
EMOJI_LEVEL = float(os.getenv("EMOJI_LEVEL", "0.1"))