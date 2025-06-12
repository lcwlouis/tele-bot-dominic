from google.adk.agents import Agent
# from google.adk.tools import FunctionTool
from google.adk.tools import google_search
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmResponse, LlmRequest
from google.genai import types
import logging

# from .tools.search import search

from .prompt import personality_prompt

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('agent.log')
    ]
)
logger = logging.getLogger(__name__)

def update_date_time_callback(callback_context: CallbackContext, llm_request: LlmRequest):
    """Callback function to update date and time in the agent's context."""
    from datetime import datetime
    current_time = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    
    # Set the current time in the prompt
    llm_request.config.system_instruction = llm_request.config.system_instruction.replace(
        "$current_time$", current_time
    )
    
    return None

root_agent = Agent(
    model="gemini-2.0-flash",
    name="dom",
    instruction=personality_prompt,
    before_model_callback=update_date_time_callback,
    tools=[
        # FunctionTool(
        #     func=search,
        # ),
        google_search
    ],
)
import uuid
async def test():
    session_service = DatabaseSessionService(db_url="sqlite:///./dom_sessions.db")
    # session_id = str(uuid.uuid4())
    try:
        sessions = await session_service.list_sessions(
            app_name="dom",
            user_id="test_user",
        )
        print(f"Found {sessions} sessions for user 'test_user'.")
        if sessions:
            session = sessions.sessions[0]
            session_id = session.id
            logger.info(f"Using existing session: {session_id}")
        else:
            raise Exception("No existing session found.")
        
    except Exception:
        session = await session_service.create_session(
            app_name="dom",
            user_id="test_user",
            session_id=session_id,
            state={"current_time": "01-01-2023 00:00:00"}
        )
    
    
    runner = Runner(
        agent=root_agent,
        app_name="dom",
        session_service=session_service,
    )
    
    
    prompt = (
        f"Hi Dom! How's it going? Can you tell me a bit about yourself?"
    )
    
    
    message = types.Content(role="user",parts=[types.Part(text=prompt)])
    
    
    """Test the root agent with a simple message."""
    async for event in runner.run_async(
        user_id="test_user",
        session_id=session.id,
        new_message=message,
    ):
        
        if event.is_final_response():
            response_text = event.content.parts[0].text
            logger.info(f"Final response from Dom: {response_text}")

# import asyncio
# asyncio.run(test())