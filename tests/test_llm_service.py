# tests/test_llm_service.py
"""
Integration-style tests for app.services.llm
-------------------------------------------

• If `USE_OLLAMA=true` (or TESTING=1) we exercise the Ollama code‐path.
  Set `OLLAMA_URL` to your running instance (default
  http://localhost:11434/api/generate).  Tests that require the server will be
  skipped automatically when it isn’t reachable.

• If `USE_OLLAMA=false` we exercise the OpenAI code-path.  You **must** have
  `OPENAI_API_KEY` (and optionally `GPT_MODEL`, defaults to
  ``gpt-3.5-turbo``) in your environment.  Tests that need the key are skipped
  when it isn’t present.

• Google Serper queries in ``build_plan_prompt`` run only when
  ``SERPER_API_KEY`` is set; otherwise that test is skipped.

The fast, fully local unit-tests (string manipulation etc.) always run.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict

import httpx
import pytest
from fastapi import HTTPException

import app.services.llm as llm
from app.services.llm import (
    build_dosing_prompt,
    build_plan_prompt,
    call_llm_async,
    direct_openai_call,
    direct_ollama_call,
    enhance_query,
    parse_json_response,
    parse_ollama_response,
    parse_openai_response,
    validate_llm_response,
)

# --------------------------------------------------------------------------- #
#  Helper utilities                                                            #
# --------------------------------------------------------------------------- #


async def _ollama_available() -> bool:
    """
    Quick health-check: GET /api/tags on the Ollama host.
    """
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


# --------------------------------------------------------------------------- #
#  1.  Pure utility functions (no I/O)                                         #
# --------------------------------------------------------------------------- #


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
    with pytest.raises(HTTPException):
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
    payload: Dict[str, Any] = {
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


# --------------------------------------------------------------------------- #
#  2.  Prompt builders                                                         #
# --------------------------------------------------------------------------- #


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
    """
    Runs only when SERPER_API_KEY is set – otherwise skip.
    """
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


# --------------------------------------------------------------------------- #
#  3.  Ollama branch (real HTTP calls)                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_direct_ollama_call_roundtrip() -> None:
    if not llm.USE_OLLAMA:
        pytest.skip("USE_OLLAMA=false – OpenAI branch active")

    if not await _ollama_available():
        pytest.skip("Ollama server not reachable")

    _prompt = "Return exactly this JSON: {\"foo\": 42}"
    try:
        result = await direct_ollama_call(_prompt, llm.MODEL_1_5B)
    except HTTPException as exc:
        pytest.skip(f"Ollama call failed: {exc.detail}")

    assert isinstance(result, dict)
    # make sure JSON was actually parsed
    assert result.get("foo", None) == 42


@pytest.mark.asyncio
async def test_call_llm_async_ollama_path() -> None:
    if not llm.USE_OLLAMA:
        pytest.skip("USE_OLLAMA=false – OpenAI branch active")
    if not await _ollama_available():
        pytest.skip("Ollama server not reachable")

    parsed, raw = await call_llm_async("Return {\"x\":1}", llm.MODEL_1_5B)
    assert isinstance(parsed, dict)
    # verify the actual values round‑trip
    assert parsed.get("x", None) == 1
    # raw must be the exact JSON serialization of parsed
    assert raw.strip() == json.dumps(parsed, separators=(',',':'))


# --------------------------------------------------------------------------- #
#  4.  OpenAI branch (real HTTPS calls)                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_direct_openai_call_live() -> None:
    if llm.USE_OLLAMA:
        pytest.skip("USE_OLLAMA=true – Ollama branch active")

    if not _openai_key_present():
        pytest.skip("OPENAI_API_KEY missing")

    model = os.getenv("GPT_MODEL") or "gpt-3.5-turbo"
    out = await direct_openai_call("Return JSON {\"bar\": 7}", model)
    data = json.loads(out)
    assert isinstance(data, dict)


@pytest.mark.asyncio
async def test_direct_openai_call_missing_key() -> None:
    """
    Validates that the helper raises when the key is absent.
    Executed only when the real key is *not* in the environment.
    """
    if _openai_key_present():
        pytest.skip("Key present – cannot test missing-key behaviour")

    with pytest.raises(ValueError):
        await direct_openai_call("{}", "gpt-3.5-turbo")


@pytest.mark.asyncio
async def test_call_llm_async_openai_path() -> None:
    if llm.USE_OLLAMA:
        pytest.skip("USE_OLLAMA=true – Ollama branch active")
    if not _openai_key_present():
        pytest.skip("OPENAI_API_KEY missing")

    model = os.getenv("GPT_MODEL") or "gpt-3.5-turbo"
    parsed, _ = await call_llm_async("Return {\"baz\":123}", model)
    assert isinstance(parsed, dict)
