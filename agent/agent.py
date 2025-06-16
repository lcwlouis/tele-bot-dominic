from google.adk.agents import Agent
# from google.adk.tools import FunctionTool
from google.adk.tools import google_search
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, llm_response
from google.genai import types
import logging
from datetime import datetime

# from .tools.search import search

from .prompt import personality_prompt

# Configure logging
# logging.basicConfig(
#     level=logging.INFO,
#     format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
#     handlers=[
#         logging.StreamHandler(),
#         logging.FileHandler('agent.log')
#     ]
# )
logger = logging.getLogger(__name__)

def update_date_time_callback(callback_context: CallbackContext, llm_request: LlmRequest):
    """Callback function to update date and time in the agent's context."""
    current_time = datetime.now().strftime("%d-%m-%Y %I:%M %p")
    
    # Set the current time in the prompt
    llm_request.config.system_instruction = llm_request.config.system_instruction.replace(
        "$current_time$", current_time
    )
    return None

def after_model_addTeleInfo_callback(callback_context: CallbackContext, llm_response: llm_response):
    """Callback function to add tele info to the agent's context."""
    # Print the llm_response
    print(f"llm_response: {llm_response}")
    llm_response.content.parts[0].text = f"[{datetime.now().strftime('%d-%m-%Y %I:%M %p')}] Dom (@domthebuilderbot): {llm_response.content.parts[0].text}"
    return None

root_agent = Agent(
    model="gemini-2.0-flash",
    # model="gemini-2.0-flash-lite",
    # model="gemini-2.5-flash-preview-04-17",
    # model="gemini-2.5-flash-preview-05-20"
    name="dom",
    instruction=personality_prompt,
    before_model_callback=update_date_time_callback,
    after_model_callback=after_model_addTeleInfo_callback,
    tools=[
        # FunctionTool(
        #     func=search,
        # ),
        google_search
    ],
)
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
            state={"current_time": "01-01-2023 00:00:00"}
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
        agent=root_agent,
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