# tests/test_llm_service.py
"""
Integration-style tests for app.services.llm
-------------------------------------------

• If USE_OLLAMA=true (or TESTING=1) we hit your local Ollama server.
  Tests are skipped if it isn’t reachable.
• Otherwise we hit OpenAI.  Skipped if OPENAI_API_KEY is missing.
• SERPER_API_KEY tests for build_plan_prompt are also skipped if missing.
"""
from __future__ import annotations
import os
import json
import pytest
import httpx
from dotenv import load_dotenv

# 1) Load .env and THEN import llm so it sees the right env vars
ROOT = os.path.dirname(os.path.dirname(__file__))
load_dotenv(os.path.join(ROOT, ".env"))

import importlib
import app.services.llm as llm
importlib.reload(llm)  # ensure module picks up freshly loaded env vars

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
# Helper utilities                                                             #
# ───────────────────────────────────────────────────────────────────────────── #

async def _ollama_available() -> bool:
    base = llm.OLLAMA_URL.split("/api/")[0]
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(f"{base}/api/tags")
            return r.status_code == 200
    except Exception:
        return False

def _openai_key_present() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))

def _serper_key_present() -> bool:
    return bool(os.getenv("SERPER_API_KEY"))

# ───────────────────────────────────────────────────────────────────────────── #
# 1. Pure utility functions (no I/O)                                           #
# ───────────────────────────────────────────────────────────────────────────── #

def test_enhance_query_adds_context() -> None:
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

def test_enhance_query_no_duplicate_location() -> None:
    q = "dose in Farm"
    profile = {"location": "Farm"}
    out = enhance_query(q, profile)
    assert "Please consider that the plant" not in out

def test_parse_json_response_valid() -> None:
    s = "{ 'a': 1, 'b': 2 } trailing"
    res = parse_json_response(s)
    assert res == {"a": 1, "b": 2}

def test_parse_json_response_malformed() -> None:
    with pytest.raises(ValueError):
        parse_json_response("no json here")

def test_parse_ollama_response_removes_think() -> None:
    raw = "<think>debug</think>{\"x\":1}"
    cleaned = parse_ollama_response(raw)
    assert cleaned == "{\"x\":1}"

def test_parse_openai_response_good() -> None:
    raw = "prefix {\"y\":2} suffix"
    out = parse_openai_response(raw)
    assert out == json.dumps({"y": 2})

def test_parse_openai_response_nested() -> None:
    raw = "foo {\"a\":{\"b\":2}} bar"
    out = parse_openai_response(raw)
    assert out == json.dumps({"a": {"b": 2}})

def test_parse_openai_response_bad() -> None:
    with pytest.raises(ValueError):
        parse_openai_response("no braces at all")

def test_validate_llm_response_good() -> None:
    payload = {
        "actions": [
            {"pump_number": 1, "chemical_name": "A", "dose_ml": 10, "reasoning": "ok"}
        ]
    }
    validate_llm_response(payload)  # should not raise

def test_validate_llm_response_missing_actions() -> None:
    with pytest.raises(ValueError):
        validate_llm_response({})

def test_validate_llm_response_bad_dose() -> None:
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
    def __init__(self) -> None:
        self.id = "d1"
        self.pump_configurations = [
            {
                "pump_number": 1,
                "chemical_name": "Chem-A",
                "chemical_description": "Desc",
            }
        ]

@pytest.mark.asyncio
async def test_build_dosing_prompt_contains_expected_sections() -> None:
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
    assert "Pump 1: Chem-A" in prompt
    assert "- pH: 6.5" in prompt

@pytest.mark.asyncio
async def test_build_dosing_prompt_raises_when_no_pumps() -> None:
    class NoPump:
        id = "x"
        pump_configurations = None

    with pytest.raises(ValueError):
        await build_dosing_prompt(NoPump(), {"ph": 7, "tds": 100}, {})

@pytest.mark.asyncio
async def test_build_plan_prompt_live_search() -> None:
    if not _serper_key_present():
        pytest.skip("SERPER_API_KEY missing – skipping live Serper test")

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
# 3. Unified integration test                                                #
# ───────────────────────────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_call_llm_async_integration() -> None:
    prompt = 'Return exactly this JSON: {"foo":42}'
    if llm.USE_OLLAMA:
        # Ollama must be reachable
        available = await _ollama_available()
        assert available, "Local Ollama server must be running for this integration test"
        parsed, raw = await call_llm_async(prompt, llm.MODEL_1_5B)
        assert isinstance(parsed, dict)
        assert parsed["foo"] == 42
        # raw should be the compact JSON
        assert raw.strip() == json.dumps(parsed, separators=(",", ":"))
    else:
        # OPENAI_API_KEY must be present
        assert _openai_key_present(), "OPENAI_API_KEY must be set for OpenAI integration test"
        model = os.getenv("GPT_MODEL") or "gpt-3.5-turbo"
        parsed, raw = await call_llm_async(prompt, model)
        assert isinstance(parsed, dict)
        assert parsed["foo"] == 42
        # raw must be parseable to the same dict
        assert json.loads(raw) == parsed
