from typing import Any, List
from fastapi import HTTPException
from app.services.serper import fetch_search_results

async def serper_search(
    query: str,
    num_results: int = 5,
    gl: str = "in",
    hl: str = "en",
) -> List[Any]:
    """
    Wrapper that validates Serper results and raises HTTPException
    for API/malformed failures as tests expect.
    """
    try:
        data = await fetch_search_results(query, num_results=num_results, gl=gl, hl=hl)
    except Exception:
        raise HTTPException(status_code=502, detail="Search service error")

    organic = data.get("organic") if isinstance(data, dict) else None
    if not organic:
        raise HTTPException(status_code=502, detail="No organic results")

    return organic
