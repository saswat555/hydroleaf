import os
import asyncio
import httpx
from httpx import HTTPStatusError
from bs4 import BeautifulSoup
from typing import Any, Dict
import logging
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

# --- configuration ---
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")
BASE_URL = "https://google.serper.dev/search"
HEADERS = {"User-Agent": "Hydroleaf/1.0 (+https://yourdomain.com)"}
MAX_SCRAPE_WORKERS = int(os.getenv("SERPER_MAX_WORKERS", "5"))
RETRY_ATTEMPTS = int(os.getenv("SERPER_RETRIES", "3"))
RETRY_BACKOFF_BASE = float(os.getenv("SERPER_BACKOFF", "1.0"))

RELIABLE_SOURCES: Dict[str, str] = {
    "Wikipedia": "https://en.wikipedia.org/wiki/",
    "Open Library": "https://openlibrary.org/search?q=",
    "Project Gutenberg": "https://www.gutenberg.org/ebooks/search/?query=",
    "PubMed": "https://pubmed.ncbi.nlm.nih.gov/?term=",
}

def _sync_scrape_text(url: str) -> str:
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
    except HTTPStatusError as http_err:
        logger.warning(f"HTTP error scraping {url}: {http_err}")
        return ""
    except Exception as exc:
        logger.warning(f"Scrape failed for {url}: {exc}")
        return ""

async def _scrape_page_text(url: str) -> str:
    return await asyncio.to_thread(_sync_scrape_text, url)

async def _get_json_with_retry(
    client: httpx.AsyncClient, url: str, params: Dict[str, Any]
) -> Dict[str, Any]:
    """HTTP GET with retry that works with both real and mocked responses."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(url, params=params, headers=HEADERS, timeout=10.0)

            # ✅ Works even if mocked object lacks raise_for_status
            if hasattr(resp, "raise_for_status") and callable(resp.raise_for_status):
                resp.raise_for_status()
            else:
                if getattr(resp, "status_code", 200) >= 400:
                    raise HTTPStatusError(
                        f"HTTP {getattr(resp, 'status_code', '?')}",
                        request=None,
                        response=resp
                    )

            return resp.json()

        except HTTPStatusError as http_err:
            # Real API errors
            logger.error(
                f"Serper API error [{getattr(http_err.response, 'status_code', '?')}]: "
                f"{getattr(http_err.response, 'text', http_err)}"
            )
            raise
        except Exception as exc:
            if attempt == RETRY_ATTEMPTS:
                logger.error(f"Serper API failed after {attempt} attempts: {exc}")
                raise
            backoff = RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
            logger.info(f"Retrying Serper API in {backoff:.1f}s (attempt {attempt}/{RETRY_ATTEMPTS})")
            await asyncio.sleep(backoff)

    raise RuntimeError("Unreachable retry logic in _get_json_with_retry")

async def fetch_search_results(
    query: str,
    num_results: int = 5,
    gl: str = "in",
    hl: str = "en",
) -> Dict[str, Any]:
    """Performs search via Serper or returns fallback links if no API key."""
    # --- fallback ---
    if not SERPER_API_KEY:
        logger.warning("SERPER_API_KEY missing: returning RELIABLE_SOURCES fallback")
        q = quote_plus(query)
        organic = [
            {
                "title": name,
                "link": prefix + q,
                "snippet": f"Search '{query}' on {name}",
                "page_content": "",
            }
            for name, prefix in RELIABLE_SOURCES.items()
        ]
        return {"organic": organic, "fallback": True}

    # --- API call ---
    params = {
        "q": query,
        "gl": gl,
        "hl": hl,
        "apiKey": SERPER_API_KEY,
        "num": num_results,
        "full": "true",
        "output": "detailed",
    }
    async with httpx.AsyncClient() as client:
        data = await _get_json_with_retry(client, BASE_URL, params)

    raw_organic = data.get("organic") or []
    results = raw_organic[:num_results]

    # --- scrape in parallel ---
    sem = asyncio.Semaphore(MAX_SCRAPE_WORKERS)

    async def _enrich(entry: Dict[str, Any]) -> None:
        link = entry.get("link") or ""
        content = ""
        if link:
            async with sem:
                content = await _scrape_page_text(link)
        entry["page_content"] = content or entry.get("snippet", "")
        if not entry["page_content"]:
            for name, prefix in RELIABLE_SOURCES.items():
                if name.lower() in link.lower():
                    entry["page_content"] = f"See {name}: {link}"
                    break

    await asyncio.gather(*(_enrich(item) for item in results))

    data["organic"] = results
    return data
