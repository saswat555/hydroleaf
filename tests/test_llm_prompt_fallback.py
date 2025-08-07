# tests/test_llm_prompt_fallback.py
import pytest
import asyncio
from app.services.llm import build_plan_prompt

@pytest.mark.asyncio
async def test_build_plan_prompt_no_search_results(monkeypatch):
    # force internal serper search to return empty
    dummy = asyncio.Future()
    dummy.set_result([])
    monkeypatch.setattr(
        "app.services.llm._serper_search",
        lambda q: dummy,
    )

    prompt = await build_plan_prompt(
        sensor_data={},
        profile={"plant_name": "X", "plant_type": "Y"},
        query="anything"
    )
    assert "No external insights found" in prompt
