import os
import httpx
import asyncio

# Retrieve the API key from the environment and print it
SERPER_API_KEY = os.getenv("SERPER_API_KEY")
if not SERPER_API_KEY:
    raise ValueError("The environment variable SERPER_API_KEY is not set.")

async def fetch_search_results(query: str, num_results: int = 5) -> dict:
    """
    Fetch search results from Google Serper API with additional parameters to return detailed content.
    
    Parameters:
      - query: The search query string.
      - num_results: Number of top results to return (default is 5).
    
    Returns:
      A dictionary with detailed search results.
      
    The added parameters:
      - "num": specifies the number of results to return.
      - "full": "true" signals the API to return the complete content.
      - "output": "detailed" requests that the output include as much content as possible (aiming for 80-90% of the page).
    """
    # Base URL for the search API
    base_url = "https://google.serper.dev/search"
    # Prepare query parameters with additional options for detailed content
    params = {
        "q": query,
        "gl": "in",
        "hl": "en",
        "apiKey": SERPER_API_KEY,
        "num": num_results,         # Return top num_results results
        "full": "true",             # Request full page content extraction if supported
        "output": "detailed"        # Request detailed output for richer content
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(base_url, params=params)
            response.raise_for_status()
            data = response.json()
            return data
    except httpx.HTTPStatusError as e:
        print(f"HTTP error: {e.response.status_code} - {e.response.text}")
        raise
    except Exception as e:
        print("An error occurred:", e)
        raise
