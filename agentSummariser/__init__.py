from .agent import summarising_agent, summarising_agent_lite
from bot.config.models import LITELLM_MODE

# Proxy agent that automatically selects based on LITELLM_MODE
def get_summarising_agent():
    """Returns the appropriate summarising agent based on LITELLM_MODE setting."""
    return summarising_agent_lite if LITELLM_MODE else summarising_agent

# Export the proxy function and individual agents
__all__ = [
    'get_summarising_agent',
    'summarising_agent',
    'summarising_agent_lite'
]