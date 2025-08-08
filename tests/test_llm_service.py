# tests/test_llm_service.py
"""
Integration-style tests for app.services.llm
-------------------------------------------

These tests exercise:
- Utility functions (no I/O)
- Prompt builders
- A live call against Ollama's HTTP API at /api/models and /api/generate
"""

import os
import re
import json
import pytest
import httpx
from dotenv import load_dotenv

# 1) Load .env and THEN force USE_OLLAMA (and disable TESTING)
ROOT = os.path.dirname(os.path.dirname(__file__))
load_dotenv(os.path.join(ROOT, ".env"))
os.environ.pop("TESTING", None)
os.environ["USE_OLLAMA"] = "true"

# 2) Reload the module so it picks up the new env settings
import importlib
import app.services.llm as llm
importlib.reload(llm)

from app.services.llm import (
    enhance_query,
    parse_json_response,
    parse_ollama_response,
    parse_openai_response,
    validate_llm_response,
    build_dosing_prompt,
    build_plan_prompt,
    call_llm_async,
)

# ───────────────────────────────────────────────────────────────────────────── #
# Helpers for integration checks                                              #
# ───────────────────────────────────────────────────────────────────────────── #

async def is_ollama_up() -> bool:
    """Check that Ollama's HTTP API is reachable at /api/models."""
    base = llm.OLLAMA_URL.rsplit("/api/", 1)[0]
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(f"{base}/api/models")
            return r.status_code == 200
    except Exception:
        return False

# ───────────────────────────────────────────────────────────────────────────── #
# 1. Pure utility functions (no I/O)                                           #
# ───────────────────────────────────────────────────────────────────────────── #

def test_enhance_query_adds_context():
    q = "Adjust dose"
    profile = {
        "plant_name": "Rose",
        "plant_type": "Flower",
        "growth_stage": "Veg",
        "seeding_date": "2020",
        "location": "Farm",
    }
    out = enhance_query(q, profile)
    assert "Please consider that the plant 'Rose'" in out

def test_enhance_query_no_duplicate_location():
    q = "dose in Farm"
    profile = {"location": "Farm"}
    out = enhance_query(q, profile)
    assert "Please consider that the plant" not in out

def test_parse_json_response_valid_simple():
    # single‐quoted JSON with trailing text
    s = "blah { 'a': 1 , 'b': [2,3] } extra"
    res = parse_json_response(s)
    assert res == {"a": 1, "b": [2, 3]}

def test_parse_json_response_double_quotes_and_nested():
    s = '{"x": {"y":2},"z":3} tail'
    res = parse_json_response(s)
    assert res == {"x": {"y": 2}, "z": 3}

def test_parse_json_response_malformed():
    with pytest.raises(ValueError):
        parse_json_response("no json here")

def test_parse_ollama_response_strips_all_think_blocks():
    raw = "<think>foo</think>   <think>bar</think>   {\"x\":1} tail"
    cleaned = parse_ollama_response(raw)
    assert cleaned.strip() == '{"x":1}'

def test_parse_openai_response_extracts_first_json_block():
    raw = (
        "prefix```analysis\n"
        "{'k':4,'l':[5,6]}\n"
        "```\n"
        "suffix {\"m\":7}"
    )
    out = parse_openai_response(raw)
    # Must be a JSON string with double quotes and valid structure
    assert re.match(r'^\{.*"k"\s*:\s*4.*"l"\s*:\s*\[5,6\].*\}$', out)

def test_parse_openai_response_nested_and_escaped():
    raw = "foo {\"a\":{\"b\":[1,2,3]}} bar"
    out = parse_openai_response(raw)
    parsed = json.loads(out)
    assert parsed == {"a": {"b": [1, 2, 3]}}

def test_parse_openai_response_bad_format():
    with pytest.raises(ValueError):
        parse_openai_response("definitely no braces")

def test_validate_llm_response_good():
    payload = {
        "actions": [
            {"pump_number": 1, "chemical_name": "A", "dose_ml": 10, "reasoning": "ok"}
        ]
    }
    # should not raise
    validate_llm_response(payload)

def test_validate_llm_response_missing_actions():
    with pytest.raises(ValueError):
        validate_llm_response({})

def test_validate_llm_response_bad_dose():
    bad = {
        "actions": [
            {"pump_number": 1, "chemical_name": "A", "dose_ml": -5, "reasoning": "oops"}
        ]
    }
    with pytest.raises(ValueError):
        validate_llm_response(bad)

# ───────────────────────────────────────────────────────────────────────────── #
# 2. Prompt builders                                                           #
# ───────────────────────────────────────────────────────────────────────────── #

class _DummyDevice:
    def __init__(self):
        self.id = "d1"
        self.pump_configurations = [
            {"pump_number": 1, "chemical_name": "Chem-A", "chemical_description": "Desc"}
        ]

@pytest.mark.asyncio
async def test_build_dosing_prompt_contains_expected_sections():
    dev = _DummyDevice()
    sensor = {"ph": 6.5, "tds": 300}
    profile = {
        "plant_name": "P",
        "plant_type": "T",
        "growth_stage": "G",
        "seeding_date": "2020",
        "region": "R",
        "location": "L",
        "target_ph_min": 5,
        "target_ph_max": 7,
        "target_tds_min": 100,
        "target_tds_max": 500,
        "dosing_schedule": {},
    }
    prompt = await build_dosing_prompt(dev, sensor, profile)
    assert re.search(r"(?:- )?pH:\s*6\.5\b", prompt) or re.search(r"Current Sensor Readings:.*pH[^0-9]*6\.5\b", prompt)

@pytest.mark.asyncio
async def test_build_dosing_prompt_raises_when_no_pumps():
    class NoPump:
        id = "x"
        pump_configurations = None
    with pytest.raises(ValueError):
        await build_dosing_prompt(NoPump(), {"ph": 7, "tds": 100}, {})

@pytest.mark.asyncio
async def test_build_plan_prompt_includes_search_insights_or_placeholder():
    sensor_data = {"P": 1, "TDS": 2}
    profile = {
        "plant_name": "TestPlant",
        "plant_type": "Veggie",
        "growth_stage": "Seedling",
        "seeding_date": "2023-01-01",
        "region": "Europe",
        "location": "Berlin",
    }
    prompt = await build_plan_prompt(sensor_data, profile, "optimal growth")
    assert "Detailed Search Insights" in prompt

# ───────────────────────────────────────────────────────────────────────────── #
# 3. Integration: live against local Ollama                                   #
# ───────────────────────────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_call_llm_async_integration():
    # 1) Skip if Ollama isn't running
    if not await is_ollama_up():
        pytest.skip("Local Ollama server not reachable at %s" % llm.OLLAMA_URL)

    # 2) Ask it to return a simple JSON
    prompt = 'Return exactly this JSON: {"foo":42}'
    parsed, raw = await call_llm_async(prompt, llm.OLLAMA_MODEL)

    # 3) Verify the parsed structure
    assert isinstance(parsed, dict)
    assert parsed.get("foo") == 42

    # 4) Verify raw is compact JSON matching parsed
    compact = json.dumps(parsed, separators=(",", ":"))
    assert raw.strip() == compact

def test_parse_openai_response_multiple_fences():
    raw = ("Intro\n```analysis\n"
           "{'a':1}\n```\n"
           "some text\n```analysis\n{'b':2}\n```\n"
           "end")
    out = parse_openai_response(raw)
    # must pick the FIRST valid block
    assert out == '{"a":1}'

@pytest.mark.asyncio
async def test_build_plan_prompt_empty_insights_and_search(monkeypatch):
    # no serper results AND no profile entries
    async def empty_search(q): return []
    monkeypatch.setattr("app.services.llm._serper_search", empty_search)

    prompt = await build_plan_prompt(sensor_data={}, profile={}, query="grow")
    assert "No external insights found" in prompt
    assert "sensor data" in prompt or "plant profile" in prompt  # still include placeholders