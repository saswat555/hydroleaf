import os
import asyncio
import httpx
from bs4 import BeautifulSoup
from typing import Any, Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

# Configuration
SERPER_API_KEY: str = os.getenv("SERPER_API_KEY") or ""
if not SERPER_API_KEY:
    raise RuntimeError("Environment variable SERPER_API_KEY is not set.")

BASE_URL = "https://google.serper.dev/search"
HEADERS = {"User-Agent": "Hydroleaf/1.0 (+https://yourdomain.com)"}
MAX_SCRAPE_WORKERS: int = int(os.getenv("SERPER_MAX_WORKERS", "5"))
RETRY_ATTEMPTS: int = int(os.getenv("SERPER_RETRIES", "3"))
RETRY_BACKOFF_BASE: float = float(os.getenv("SERPER_BACKOFF", "1.0"))


def _sync_scrape_text(url: str) -> str:
    """
    Blocking function to fetch and parse page text.
    """
    if not url:
        return ""
    try:
        with httpx.Client(headers=HEADERS, timeout=5.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            return soup.get_text(separator=" ", strip=True)
    except Exception as exc:
        logger.warning(f"Scraping failed for {url}: {exc}")
        return ""


async def _scrape_page_text(url: str) -> str:
    """
    Async wrapper around the blocking scraper using a thread.
    """
    return await asyncio.to_thread(_sync_scrape_text, url)


async def _get_json_with_retry(
    client: httpx.AsyncClient, url: str, params: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Perform a GET request with retry and exponential backoff.
    """
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            response = await client.get(url, params=params, headers=HEADERS, timeout=10.0)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as http_err:
            logger.error(f"Serper API HTTP error ({http_err.response.status_code}): {http_err.response.text}")
            raise
        except Exception as exc:
            if attempt == RETRY_ATTEMPTS:
                logger.error(f"Serper request failed after {attempt} attempts: {exc}")
                raise
            backoff = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
            logger.info(f"Retrying Serper API in {backoff:.1f}s (attempt {attempt}/{RETRY_ATTEMPTS})")
            await asyncio.sleep(backoff)
    # Should never reach here
    raise RuntimeError("Exceeded retry attempts for Serper API")


async def fetch_search_results(
    query: str,
    num_results: int = 5,
    gl: str = "in",
    hl: str = "en"
) -> Dict[str, Any]:
    """
    Fetch search results from Google Serper API and enrich organic results with page content.

    :param query: Search query string.
    :param num_results: Number of organic results to return.
    :param gl: Geolocation parameter (e.g., 'in').
    :param hl: Language parameter (e.g., 'en').
    :return: Raw API response including 'organic' entries with added 'page_content'.
    """
    params = {
        "q": query,
        "gl": gl,
        "hl": hl,
        "apiKey": SERPER_API_KEY,
        "num": num_results,
        "full": "true",
        "output": "detailed"
    }

    async with httpx.AsyncClient() as client:
        data = await _get_json_with_retry(client, BASE_URL, params)

    organic: Optional[List[Dict[str, Any]]] = data.get("organic")
    if not organic:
        logger.debug("No organic results found in Serper response.")
        return data

    # Limit to desired number of entries
    results_list = organic[:num_results]

    # Concurrently scrape each page's text with bounded concurrency
    semaphore = asyncio.Semaphore(MAX_SCRAPE_WORKERS)

    async def sem_scrape(entry: Dict[str, Any]) -> None:
        link = entry.get("link")
        if not link:
            entry["page_content"] = ""
            return
        async with semaphore:
            entry["page_content"] = await _scrape_page_text(link)

    # Kick off all scraping tasks
    await asyncio.gather(*(sem_scrape(entry) for entry in results_list))

    # Overwrite the organic list with enriched entries
    data["organic"] = results_list
    return data
