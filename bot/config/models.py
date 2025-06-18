import os
from dotenv import load_dotenv

load_dotenv()

# model="gemini-2.0-flash-lite",
# model="gemini-2.5-flash-preview-04-17",
# model="gemini-2.5-flash-preview-05-20"
LITELLM_MODE = os.getenv("LITELLM_MODE", "false").lower() in ("true", "1", "yes", "on")
os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY", "abc")

# Set LiteLLM environment variables for better compatibility
if LITELLM_MODE:
    # Reduce logging noise from LiteLLM
    os.environ["LITELLM_LOG"] = "ERROR"
    # Set timeout for better stability
    os.environ["LITELLM_TIMEOUT"] = "60"
    # Disable some features that might cause serialization issues
    os.environ["LITELLM_DISABLE_LOGGING"] = "true"

GEMINI_SEARCH_MODEL = os.getenv("GEMINI_SEARCH_MODEL", "gemini-2.0-flash-lite")
GEMINI_CONVERSATION_MODEL = os.getenv("GEMINI_CONVERSATION_MODEL", "gemini-2.0-flash")
GEMINI_SUMMARISATION_MODEL = os.getenv("GEMINI_SUMMARISATION_MODEL", "gemini-2.5-flash-preview-04-17")

LITELLM_SEARCH_MODEL = {
    "model": os.getenv("LITELLM_SEARCH_MODEL", "ollama/gemma3:4b-it-qat"),
    "api_base": os.getenv("LITELLM_SEARCH_BASE_URL", "http://192.168.68.23:11434/v1")
}
LITELLM_CONVERSATION_MODEL = {
    "model": os.getenv("LITELLM_CONVERSATION_MODEL", "ollama/gemma3:27b-it-qat"),
    "api_base": os.getenv("LITELLM_CONVERSATION_BASE_URL", "http://192.168.68.23:11434/v1")
}
LITELLM_SUMMARISATION_MODEL = {
    "model": os.getenv("LITELLM_SUMMARISATION_MODEL", "ollama/gemma3:27b-it-qat"),
    "api_base": os.getenv("LITELLM_SUMMARISATION_BASE_URL", "http://192.168.68.23:11434/v1")
}