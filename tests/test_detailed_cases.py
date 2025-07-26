# tests/test_detailed_cases.py

import asyncio
import importlib
import os

import pytest
import httpx
from fastapi import HTTPException

from app.services.device_controller import DeviceController
from app.services.llm import call_llm_async, USE_OLLAMA
from app.services.supply_chain_service import extract_json_from_response
from app.services.llm import parse_json_response, build_dosing_prompt
from app.models import Device

# -----------------------------
# 1) Discover fallback ’ip’
# -----------------------------
@pytest.mark.asyncio
async def test_discover_fallback_ip_strips_http_prefix(monkeypatch):
    class DummyRes:
        def __init__(self, code, data):
            self.status_code = code
            self._json = data
        def json(self):
            return self._json

    class FakeCli:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, path, *a, **k):
            if path == "/discovery":
                return DummyRes(500, {})
            if path == "/state":
                return DummyRes(200, {"device_id": "X", "valves": []})
            return DummyRes(404, {})

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeCli())

    dc = DeviceController("10.0.0.5")
    result = await dc.discover()
    assert result["ip"] == "10.0.0.5"
    assert "http://" not in result["ip"]

# -----------------------------
# 2) extract_json_from_response
# -----------------------------
def test_extract_json_happy_path():
    s = 'ignore { "a": 1, "b": 2 } trailing'
    out = extract_json_from_response(s)
    assert out == {"a": 1, "b": 2}

def test_extract_json_throws_on_no_json():
    with pytest.raises(HTTPException):
        extract_json_from_response("no JSON here")

# -----------------------------
# 3) dynamic USE_OLLAMA logic
# -----------------------------
def test_use_ollama_flag_respects_testing(monkeypatch):
    monkeypatch.setenv("USE_OLLAMA", "false")
    monkeypatch.setenv("TESTING", "1")
    import app.services.llm as llm
    importlib.reload(llm)
    assert llm.USE_OLLAMA is True

def test_use_ollama_flag_respects_env_when_not_testing(monkeypatch):
    monkeypatch.setenv("USE_OLLAMA", "true")
    monkeypatch.setenv("TESTING", "0")
    import app.services.llm as llm
    importlib.reload(llm)
    assert llm.USE_OLLAMA is True

# -----------------------------
# 4) call_llm_async openai path
# -----------------------------
@pytest.mark.asyncio
async def test_call_llm_async_openai():
    import app.services.llm as llm

    if llm.USE_OLLAMA:
        pytest.skip("USE_OLLAMA=true – skipping OpenAI branch")
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY missing – skipping live OpenAI test")

    model = os.getenv("GPT_MODEL") or "gpt-3.5-turbo"
    parsed, raw = await call_llm_async("hi", model)
    assert isinstance(parsed, dict)
    assert raw.strip().startswith("{")

# -----------------------------
# 5) build_dosing_prompt errors
# -----------------------------
@pytest.mark.asyncio
async def test_build_dosing_prompt_raises_on_no_pumps():
    dummy = Device(
        id="d1",
        mac_id="m",
        name="n",
        type="dosing_unit",
        http_endpoint="e",
        pump_configurations=None,
        sensor_parameters={},
        valve_configurations=[],
        switch_configurations=[]
    )
    with pytest.raises(ValueError):
        await build_dosing_prompt(dummy, {"ph": 7, "tds": 100}, {})

# -----------------------------
# 6) parse_json_response edge
# -----------------------------
def test_parse_json_response_strips_extra_text():
    s = "junk { 'x': 10 } more junk"
    out = parse_json_response(s)
    assert out == {"x": 10}

@pytest.mark.asyncio
async def test_discover_both_endpoints_fail(monkeypatch):
    class DummyRes:
        def __init__(self, code): self.status_code = code
        def json(self): return {}
    class FakeCli:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, path, *a, **k):
            return DummyRes(500)  # always fail
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeCli())

    dc = DeviceController("10.0.0.99")
    result = await dc.discover()
    assert result is None

def test_extract_json_multiple_json_blocks():
    s = 'prefix {"a":1} middle {"b":2} suffix'
    out = extract_json_from_response(s)
    assert out == {"a": 1}

# -----------------------------
# 7) call_llm_async ollama error propagation
# -----------------------------
@pytest.mark.asyncio
async def test_call_llm_async_ollama_http_error():
    import app.services.llm as llm
    if not llm.USE_OLLAMA:
        pytest.skip("USE_OLLAMA=false – skipping Ollama branch")

    # An empty prompt should provoke a 400 from Ollama
    with pytest.raises(HTTPException):
        await call_llm_async("", llm.MODEL_1_5B)

# -----------------------------
# 8) parse_json_response top-level list
# -----------------------------
def test_parse_json_response_top_level_list():
    s = "[ {'x':10}, {'y':20} ] extra"
    out = parse_json_response(s)
    assert isinstance(out, list)
    assert out == [{"x": 10}, {"y": 20}]

# -----------------------------
# 9) build_dosing_prompt many pumps
# -----------------------------
@pytest.mark.asyncio
async def test_build_dosing_prompt_many_pumps():
    class D:
        def __init__(self):
            self.pump_configurations = [
                {"pump_number": i, "chemical_name": f"C{i}", "chemical_description": "D"}
                for i in range(1, 5)
            ]
            self.id = "X"
    dev = D()
    sensor = {"ph": 7, "tds": 100}
    profile = {
        "plant_name": "P", "plant_type": "T", "growth_stage": "G",
        "seeding_date": "2020", "region": "R", "location": "L",
        "target_ph_min": 5, "target_ph_max": 8,
        "target_tds_min": 50, "target_tds_max": 150,
        "dosing_schedule": {}
    }
    prompt = await build_dosing_prompt(dev, sensor, profile)
    for i in range(1, 5):
        assert f"Pump {i}:" in prompt
