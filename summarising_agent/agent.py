from google.adk.agents import Agent
from google.genai import types
import logging
from typing import List

from pydantic import BaseModel, Field

class UserInformation(BaseModel):
    telegram_handle: str = Field(description="The telegram handle of the user.")
    telegram_name: str = Field(description="The telegram name of the user.")
    preferred_name: str = Field(description="The preferred name of the user.")
    habits_and_style: str = Field(description="The habits and style of the user.")
    communication_preferences: str = Field(description="The communication preferences of the user.")
    special_notes: str = Field(description="The special notes of the user.")

class SummaryOutput(BaseModel):
    summary: str = Field(description="The summary of the conversation.")
    user_information: List[UserInformation] = Field(description="The list of user information for all users in the conversation.")

from .prompt import summarisation_prompt

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

summarising_agent = Agent(
    model="gemini-2.5-flash-preview-04-17",
    # model="gemini-2.5-flash-preview-05-20"
    name="summarising_agent",
    instruction=summarisation_prompt,
    output_schema=SummaryOutput,
)