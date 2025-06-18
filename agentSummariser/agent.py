from google.adk.agents import Agent
from google.genai import types
import logging
from typing import List
from google.adk.models.lite_llm import LiteLlm

from pydantic import BaseModel, Field
from bot.config.models import GEMINI_SUMMARISATION_MODEL, LITELLM_SUMMARISATION_MODEL

class UserInformation(BaseModel):
    telegram_handle: str = Field(description="The telegram handle of the user.")
    telegram_name: str = Field(description="The telegram name of the user.")
    preferred_name: str = Field(description="The preferred name of the user.")
    habits_and_style: str = Field(description="The habits and style of the user.")
    communication_preferences: str = Field(description="The communication preferences of the user.")
    special_notes: str = Field(description="The special notes of the user.")

class ChatParameters(BaseModel):
    sarcasm_level: float = Field(description="The sarcasm level of the user.")
    playfulness_level: float = Field(description="The playfulness level of the user.")
    humor_level: float = Field(description="The humor level of the user.")
    formality_level: float = Field(description="The formality level of the user.")
    empathy_level: float = Field(description="The empathy level of the user.")
    enthusiasm_level: float = Field(description="The enthusiasm level of the user.")
    singlish_level: float = Field(description="The singlish level of the user.")
    emoji_level: float = Field(description="The emoji level of the user.")

class SummaryOutput(BaseModel):
    summary: str = Field(description="The summary of the conversation.")
    user_information: List[UserInformation] = Field(description="The list of user information for all users in the conversation.")
    chat_parameters: ChatParameters = Field(description="The chat parameters of the conversation.")

from .prompt import summarisation_prompt

logger = logging.getLogger(__name__)

# Summarising Agent (Gemini)
summarising_agent = Agent(
    model=GEMINI_SUMMARISATION_MODEL,
    name="summarising_agent",
    instruction=summarisation_prompt,
    output_schema=SummaryOutput,
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    generate_content_config=types.GenerateContentConfig(
        max_output_tokens=2000,
        temperature=0.5,
        top_p=0.9,
    )
)

# Summarising Agent (LiteLLM)
summarising_agent_lite = Agent(
    model=LiteLlm(
        model=LITELLM_SUMMARISATION_MODEL["model"],
        api_base=LITELLM_SUMMARISATION_MODEL["api_base"],
        # Add timeout and retry settings to prevent hanging
        timeout=30,
        max_retries=2,
        # Add temperature and other parameters for better stability
        temperature=0.5,
        max_tokens=2000
    ),
    name="summarising_agent",
    instruction=summarisation_prompt,
    output_schema=SummaryOutput,
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)