import os
import json
from dotenv import load_dotenv
from sqlalchemy.engine.url import URL

# Load environment variables
load_dotenv()

# Telegram API Configuration
API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_GROUP_IDS = json.loads(os.getenv("ALLOWED_GROUP_IDS", "[]"))

# Database Configuration
DB_URL = URL.create(
    drivername="postgresql",
    username=os.getenv("POSTGRES_USER"),
    password=os.getenv("POSTGRES_PASSWORD"),
    host=os.getenv("POSTGRES_HOST"),
    port=os.getenv("POSTGRES_PORT"),
    database=os.getenv("POSTGRES_DATABASE")
)

# Bot Behavior Configuration
MIN_OFFLINE_TIME = 300  # 5 minutes in seconds
MAX_OFFLINE_TIME = 3600  # 1 hour in seconds
MIN_ONLINE_TIME = 300  # 5 minutes minimum online time
MAX_ONLINE_TIME = 900  # 15 minutes maximum online time
MIN_RESPONSE_DELAY = 3  # 3 seconds
MAX_RESPONSE_DELAY = 12  # 12 seconds
SUMMARISING_AGENT_TOKEN_THRESHOLD = 4000

# Personality Parameters (0.0 to 1.0 scale)
SARCASTIC_LEVEL = float(os.getenv("SARCASTIC_LEVEL", "0.7"))
PLAYFUL_LEVEL = float(os.getenv("PLAYFUL_LEVEL", "0.7"))
HUMOR_LEVEL = float(os.getenv("HUMOR_LEVEL", "0.4"))
FORMALITY_LEVEL = float(os.getenv("FORMALITY_LEVEL", "0.5"))
EMPATHY_LEVEL = float(os.getenv("EMPATHY_LEVEL", "0.7"))
ENTHUSIASM_LEVEL = float(os.getenv("ENTHUSIASM_LEVEL", "0.5"))
SINGLISH_LEVEL = float(os.getenv("SINGLISH_LEVEL", "0.05"))
EMOJI_LEVEL = float(os.getenv("EMOJI_LEVEL", "0.1"))