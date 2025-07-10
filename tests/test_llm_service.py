# tests/test_llm_service.py

import pytest
import json
import asyncio
from fastapi import HTTPException
import respx
from httpx import Response

import app.services.llm as llm
from app.services.llm import (
    enhance_query,
    parse_json_response,
    parse_ollama_response,
    parse_openai_response,
    validate_llm_response,
    build_dosing_prompt,
    build_plan_prompt,
    call_llm_async,
    direct_ollama_call,
    OLLAMA_URL,
)


# ─── enhance_query ───────────────────────────────────────────────────────────

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


# ─── parse_json_response ──────────────────────────────────────────────────────

def test_parse_json_response_valid():
    s = "{ 'a': 1, 'b': 2 } extra"
    res = parse_json_response(s)
    assert res == {"a": 1, "b": 2}

def test_parse_json_response_malformed():
    with pytest.raises(HTTPException):
        parse_json_response("no json here")


# ─── parse_ollama_response ────────────────────────────────────────────────────

def test_parse_ollama_response_removes_think():
    raw = "<think>debug</think>{\"x\":1}"
    cleaned = parse_ollama_response(raw)
    assert cleaned == "{\"x\":1}"


# ─── parse_openai_response ───────────────────────────────────────────────────

def test_parse_openai_response_good():
    raw = "Some text {\"y\":2}\n"
    out = parse_openai_response(raw)
    # Should return a JSON string
    assert out == json.dumps({"y": 2})

def test_parse_openai_response_bad():
    with pytest.raises(ValueError):
        parse_openai_response("no json here")


# ─── validate_llm_response ────────────────────────────────────────────────────

def test_validate_llm_response_good():
    data = {
        "actions": [
            {
                "pump_number": 1,
                "chemical_name": "A",
                "dose_ml": 10,
                "reasoning": "ok",
            }
        ]
    }
    # No exception
    validate_llm_response(data)

def test_validate_llm_response_missing_actions():
    with pytest.raises(ValueError):
        validate_llm_response({})


# ─── build_dosing_prompt ─────────────────────────────────────────────────────

class DummyDevice:
    def __init__(self):
        self.id = "d1"
        self.pump_configurations = [
            {"pump_number": 1, "chemical_name": "Chem", "chemical_description": "Desc"}
        ]

@pytest.mark.asyncio
async def test_build_dosing_prompt_contains_all():
    device = DummyDevice()
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
    prompt = await build_dosing_prompt(device, sensor, profile)
    assert "Pump 1: Chem" in prompt
    assert "- pH: 6.5" in prompt


# ─── build_plan_prompt ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_build_plan_prompt_with_search(monkeypatch):
    # stub out fetch_search_results to return one organic result
    fake = {"organic": [{"title": "T1", "snippet": "S1", "link": "http://l"}]}
    monkeypatch.setattr(
        "app.services.llm.fetch_search_results",
        lambda *args, **kwargs: asyncio.sleep(0, result=fake),
    )
    pd = {
        "plant_name": "X",
        "plant_type": "Y",
        "growth_stage": "Z",
        "seeding_date": "D",
        "region": "R",
        "location": "L",
    }
    p = await build_plan_prompt({"P": 1, "TDS": 2}, pd, "hello")
    assert "Title: T1" in p
    assert "Detailed Search Insights" in p


# ─── direct_ollama_call (HTTP) ───────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_direct_ollama_call_success(respx_mock):
    # stub the Ollama endpoint
    respx_mock.post(OLLAMA_URL).mock(
        return_value=Response(200, json={"response": "{\"z\":3}"})
    )

    out = await direct_ollama_call("any prompt", "any-model")
    assert out == {"z": 3}

@pytest.mark.asyncio
@respx.mock
async def test_direct_ollama_call_http_error(respx_mock):
    respx_mock.post(OLLAMA_URL).mock(return_value=Response(500, json={}))

    with pytest.raises(HTTPException):
        await direct_ollama_call("prompt", "model")


# ─── call_llm_async (HTTP + JSON parsing) ────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_call_llm_async_ollama_via_http(respx_mock):
    # make sure llm.USE_OLLAMA is True (set in your .env)
    body = {"model": llm.MODEL_1_5B, "prompt": "hello", "stream": False}
    stub = {
        "response": "{\"actions\":[{\"pump_number\":2,\"chemical_name\":\"Foo\",\"dose_ml\":20,\"reasoning\":\"Test\"}]}"
    }
    route = respx_mock.post(OLLAMA_URL, json=body).mock(
        return_value=Response(200, json=stub)
    )

    parsed, raw = await call_llm_async("hello", llm.MODEL_1_5B)
    assert isinstance(parsed, dict)
    assert parsed["actions"][0]["pump_number"] == 2
    assert raw == stub["response"]
    assert route.called

@pytest.mark.asyncio
@respx.mock
async def test_call_llm_async_ollama_http_error(respx_mock):
    body = {"model": llm.MODEL_1_5B, "prompt": "hello", "stream": False}
    respx_mock.post(OLLAMA_URL, json=body).mock(return_value=Response(500, json={}))

    with pytest.raises(HTTPException):
        await call_llm_async("hello", llm.MODEL_1_5B)

def test_validate_llm_response_bad_dose():
    bad = {"actions":[{"pump_number":1,"chemical_name":"A","dose_ml":-5,"reasoning":"ok"}]}
    with pytest.raises(ValueError):
        validate_llm_response(bad)
@pytest.mark.asyncio
async def test_build_plan_prompt_no_search(monkeypatch):
    """When fetch_search_results returns no 'organic', we still get a prompt with fallback."""
    monkeypatch.setattr("app.services.llm.fetch_search_results",
                        lambda *a,**k: asyncio.sleep(0, result={"organic":[]}))

    pd = {"plant_name":"X","plant_type":"Y","growth_stage":"Z",
          "seeding_date":"D","region":"R","location":"L"}
    p = await build_plan_prompt({"P":1,"TDS":2}, pd, "query")
    assert "Detailed Search Insights" in p
    assert "No additional information available." in p


@pytest.mark.asyncio
@respx.mock
async def test_direct_ollama_call_malformed_json(respx_mock):
    respx_mock.post(OLLAMA_URL).mock(return_value=Response(200, json={"response":"not json"}))
    with pytest.raises(HTTPException):
        await direct_ollama_call("p","m")

def test_parse_openai_response_nested_braces():
    raw = "foo {\"a\": {\"b\":2}} bar"
    out = parse_openai_response(raw)
    assert out == json.dumps({"a":{"b":2}})
