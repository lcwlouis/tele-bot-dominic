from .agent import conversation_agent, conversation_agent_lite, google_search_agent, search_agent
from bot.config.models import LITELLM_MODE

# Proxy agents that automatically select based on LITELLM_MODE
def get_conversation_agent():
    """Returns the appropriate conversation agent based on LITELLM_MODE setting."""
    return conversation_agent_lite if LITELLM_MODE else conversation_agent

def get_search_agent():
    """Returns the appropriate search agent based on LITELLM_MODE setting."""
    return search_agent if LITELLM_MODE else google_search_agent

# Export the proxy functions and individual agents
__all__ = [
    'get_conversation_agent',
    'get_search_agent', 
    'conversation_agent',
    'conversation_agent_lite',
    'google_search_agent',
    'search_agent'
]