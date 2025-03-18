# app/services/serper.py
import os
import httpx
import asyncio
import requests
import concurrent.futures
from bs4 import BeautifulSoup

SERPER_API_KEY = os.getenv("SERPER_API_KEY")
if not SERPER_API_KEY:
    raise ValueError("The environment variable SERPER_API_KEY is not set.")

def _scrape_page_text(url: str) -> str:
    """
    Synchronously fetches the given URL, parses the HTML with BeautifulSoup,
    and returns the visible text as a string.
    """
    if not url:
        return ""
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        # Remove script, style, and other non-text tags
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        # Combine all remaining text
        return soup.get_text(separator=" ", strip=True)
    except Exception as e:
        # If any error occurs, just return an empty string (or log it if you prefer)
        return ""

async def fetch_search_results(query: str, num_results: int = 5) -> dict:
    """
    Fetch search results from Google Serper API with additional parameters to return detailed content.
    Then, for each 'organic' result that has a valid link, we concurrently scrape the page text
    in a thread pool and attach it to the result under "page_content".
    """
    base_url = "https://google.serper.dev/search"
    params = {
        "q": query,
        "gl": "in",
        "hl": "en",
        "apiKey": SERPER_API_KEY,
        "num": num_results,
        "full": "true",
        "output": "detailed"
    }

    try:
        # 1) Fetch the main Serper search results asynchronously
        async with httpx.AsyncClient() as client:
            response = await client.get(base_url, params=params)
            response.raise_for_status()
            data = response.json()

        # 2) If 'organic' results exist, gather each link and scrape in parallel
        if "organic" in data:
            # Collect the top few results
            results_list = data["organic"][:num_results]

            # 3) Use a ThreadPoolExecutor to scrape each link's content
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                # Map each link to a future that calls _scrape_page_text
                futures_map = {
                    executor.submit(_scrape_page_text, entry.get("link")): entry
                    for entry in results_list if entry.get("link")
                }

                # 4) As each future completes, attach the page_content to that entry
                for future in concurrent.futures.as_completed(futures_map):
                    entry = futures_map[future]
                    try:
                        page_content = future.result()
                        entry["page_content"] = page_content
                    except Exception as e:
                        entry["page_content"] = ""

        return data

    except httpx.HTTPStatusError as e:
        print(f"HTTP error: {e.response.status_code} - {e.response.text}")
        raise
    except Exception as e:
        print("An error occurred:", e)
        raise
