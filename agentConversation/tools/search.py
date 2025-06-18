from google.adk.tools import ToolContext
from google.genai import types
import aiohttp
import json
from typing import Dict, Any
from dotenv import load_dotenv
import logging

# Configure logging
# logging.basicConfig(
#     level=logging.INFO,
#     format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
#     handlers=[
#         logging.StreamHandler(),
#         logging.FileHandler('search.log')
#     ]
# )
logger = logging.getLogger(__name__)

load_dotenv()
import os

async def searxng_search(keywords: str, tool_context: ToolContext) -> str:
    """
    Searches the web using Searxng. 
    
    Args:
        keywords (str): The search query.
        tool_context (ToolContext): The context for the tool execution.
        
    Returns:
        str: The top search results with the data.
    """
    SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8888")  # Default to localhost if not set
    
    # Prepare the search parameters
    params = {
        "q": keywords,
        "format": "json",
        "engines": "google,bing,duckduckgo,brave",  # Using multiple search engines for better results
        "language": "en"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{SEARXNG_URL}/search", params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Format the results
                    results = []
                    for result in data.get("results", [])[:5]:  # Get top 5 results
                        logger.debug(f"Processing result: {result}")  # Debugging line
                        formatted_result = {
                            "title": result.get("title", ""),
                            "url": result.get("url", ""),
                            "content": result.get("content", ""),
                            "engine": result.get("engine", ""),
                            "score": result.get("score", 0),
                        }
                        results.append(formatted_result)
                    
                    # Return formatted results as a string
                    if results:
                        return json.dumps(results, indent=2)
                    else:
                        return "No results found."
                else:
                    return f"Error: Search request failed with status {response.status}"
                    
    except Exception as e:
        return f"Error occurred during search: {str(e)}"

async def test_search():
    """
    Test function for the search tool.
    """
    # Create a mock ToolContext
    class MockToolContext:
        pass
    
    tool_context = MockToolContext()
    
    # Test case 1: Basic search
    result = await searxng_search("What is LightRAG", tool_context)
    logger.info("Test 1 - Basic search:")
    logger.info(result)
    logger.info("\n")
    
    # # Test case 2: Search with special characters
    # result = await search("python & java", tool_context)
    # print("Test 2 - Search with special characters:")
    # print(result)
    # print("\n")
    
    # # Test case 3: Empty search
    # result = await search("", tool_context)
    # print("Test 3 - Empty search:")
    # print(result)
    # print("\n")

# Example usage:
# import asyncio
# asyncio.run(test_search())
