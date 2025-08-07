# tests/test_search_service.py
import os
import pytest
from fastapi import HTTPException
from app.services.search_service import serper_search

@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("SERPER_API_KEY"),
    reason="SERPER_API_KEY not set in environment"
)
async def test_serper_search_real():
    """
    Integration test using the real Serper API and query 'hydroponics india'.
    Requires SERPER_API_KEY in .env or environment variables.
    """
    query = "hydroponics india"
    results = await serper_search(query, num_results=5)

    # Basic shape and content checks
    assert isinstance(results, list), "Expected a list of results"
    assert len(results) > 0, "No results returned from Serper API"

    first = results[0]
    assert "title" in first and first["title"], "First result missing title"
    assert "link" in first and first["link"], "First result missing link"

    print(f"✅ First result: {first['title']} -> {first['link']}")
