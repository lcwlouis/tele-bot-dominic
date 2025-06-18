from google.adk.agents import Agent
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools import google_search
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, llm_response
from google.adk.models.lite_llm import LiteLlm
from google.genai import types
import logging
from datetime import datetime
import asyncio

from .tools.search import searxng_search
from bot.services.database_service import increase_online_time, get_online_for_seconds
from .prompt import personality_prompt, search_prompt
from bot.config.models import GEMINI_SEARCH_MODEL, GEMINI_CONVERSATION_MODEL, LITELLM_CONVERSATION_MODEL, LITELLM_SEARCH_MODEL

logger = logging.getLogger(__name__)

def update_prompt_variables_callback(callback_context: CallbackContext, llm_request: LlmRequest):
    """Callback function to update date and time in the agent's context."""
    current_time = datetime.now().strftime("%d-%m-%Y %I:%M %p")
    
    logger.debug(f"Callback context: {callback_context}")
    logger.debug(f"Llm request: {llm_request}")
    
    # Set the current time in the prompt
    llm_request.config.system_instruction = llm_request.config.system_instruction.replace(
        "$current_time$", current_time
    )
    llm_request.config.system_instruction = llm_request.config.system_instruction.replace(
        "$online_for_seconds$", str(get_online_for_seconds(chat_id=callback_context.state["chat_id"]))
    )
    return None

def after_model_addTeleInfo_callback(callback_context: CallbackContext, llm_response: llm_response):
    """Callback function to add tele info to the agent's context."""
    # Print the llm_response
    # print(f"user_content: {callback_context.user_content.parts[0].text}")
    # print(f"llm_response: {llm_response}")
    # llm_response.content.parts[0].text = f"[{datetime.now().strftime('%d-%m-%Y %I:%M %p')}] Dom (@domthebuilderbot): {llm_response.content.parts[0].text}"
    return None

# Search Agent (Gemini)
google_search_agent = Agent(
    model=GEMINI_SEARCH_MODEL,
    description="A general search agent with access to google search",
    name="google_search",
    instruction=search_prompt,
    tools=[
        google_search
    ],
    generate_content_config=types.GenerateContentConfig(
        max_output_tokens=1000,
        temperature=0.6,
        top_p=0.9,
    )
)

# Root Conversation Agent (Gemini)
conversation_agent = Agent(
    description="Dom is a university student in Singapore. He is a friendly and relatable person who is always willing to help. He is also a bit of a nerd and loves to learn new things.",
    model=GEMINI_CONVERSATION_MODEL,
    name="dom",
    instruction=personality_prompt,
    before_model_callback=update_prompt_variables_callback,
    after_model_callback=after_model_addTeleInfo_callback,
    tools=[
        AgentTool(agent=google_search_agent),
        FunctionTool(func=increase_online_time)
    ],
    generate_content_config=types.GenerateContentConfig(
        max_output_tokens=2000,
        temperature=1.25,
        top_p=0.95,
    )
)

# Search Agent (LiteLLM)
search_agent = Agent(
    description="A general search agent with access to searxng search",
    model=LiteLlm(
        model=LITELLM_SEARCH_MODEL["model"],
        api_base=LITELLM_SEARCH_MODEL["api_base"],
        # Add timeout and retry settings to prevent hanging
        timeout=30,
        max_retries=2,
        # Add temperature and other parameters for better stability
        temperature=0.7,
        max_tokens=1000
    ),
    name="searxng_search",
    instruction=search_prompt,
    tools=[
        FunctionTool(func=searxng_search),
    ],
)

# Root Conversation Agent (LiteLLM)
conversation_agent_lite = Agent(
    description="Dom is a university student in Singapore. He is a friendly and relatable person who is always willing to help. He is also a bit of a nerd and loves to learn new things.",
    model=LiteLlm(
        model=LITELLM_CONVERSATION_MODEL["model"],
        api_base=LITELLM_CONVERSATION_MODEL["api_base"],
        # Add timeout and retry settings to prevent hanging
        timeout=60,
        max_retries=3,
        # Add temperature and other parameters for better stability
        temperature=0.8,
        max_tokens=2000
    ),
    name="dom",
    instruction=personality_prompt,
    before_model_callback=update_prompt_variables_callback,
    after_model_callback=after_model_addTeleInfo_callback,
    tools=[
        AgentTool(agent=search_agent),
        FunctionTool(func=increase_online_time)
    ],
)

# For testing the agent
import uuid
async def test():
    session_service = DatabaseSessionService(db_url="postgresql://postgres:password123@192.168.68.40:5432/dominic_storage")
    session_id = str(uuid.uuid4())
    try:
        sessions = await session_service.list_sessions(
            app_name="dom",
            user_id="test_user",
        )
        print(f"Found {sessions} sessions for user 'test_user'.")
        if sessions:
            session = sessions.sessions[0]
            session_id = session.id
            # logger.info(f"Using existing session: {session_id}")
        else:
            raise Exception("No existing session found.")
        
    except Exception:
        session = await session_service.create_session(
            app_name="dom",
            user_id="test_user",
            session_id=session_id,
            state={"chat_id": "test_user", "summary": "No summary available", "individualisation_prompts": [], "sarcasm_level": 0.5, "playfulness_level": 0.5, "humor_level": 0.5, "formality_level": 0.5, "empathy_level": 0.5, "enthusiasm_level": 0.5, "singlish_level": 0.5, "emoji_level": 0.5}
        )
    session_object = await session_service.get_session(
        app_name="dom",
        user_id="test_user",
        session_id=session.id,
    )
    # session_object.events = Event(
    #     author="system",
    # )
    print(session_object.events)
    
    runner = Runner(
        agent=conversation_agent,
        app_name="dom",
        session_service=session_service,
    )
    
    prompt = (
        # f"Hi Dom! How's it going? Can you tell me a bit about yourself?"
        f"Is this my first message?"
    )
    
    message = types.Content(role="user",parts=[types.Part(text=prompt)])
    
    """Test the root agent with a simple message."""
    for event in runner.run(
        user_id="test_user",
        session_id=session.id,
        new_message=message,
    ):
        
        if event.is_final_response():
            response_text = event.content.parts[0].text
            print(f"Final response from Dom: {response_text}")
            # logger.info(f"Final response from Dom: {response_text}")

# import asyncio
# asyncio.run(test())