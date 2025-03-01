import os
import httpx
import asyncio

# Retrieve the API key from the environment and print it
SERPER_API_KEY = os.getenv("SERPER_API_KEY")
if not SERPER_API_KEY:
    raise ValueError("The environment variable SERPER_API_KEY is not set.")


async def fetch_search_results(query: str) -> dict:
    # Base URL for the search API
    base_url = "https://google.serper.dev/search"
    # Prepare query parameters
    params = {
        "q": query,
        "gl": "in",
        "apiKey": SERPER_API_KEY
    }
    try:
        async with httpx.AsyncClient() as client:
            # Make a GET request with the provided parameters
            response = await client.get(base_url, params=params)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        print(f"HTTP error: {e.response.status_code} - {e.response.text}")
        raise
    except Exception as e:
        print("An error occurred:", e)
        raise


