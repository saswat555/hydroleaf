# tests/test_detailed_cases.py

import functools
import pytest
import httpx
import importlib
import asyncio
from fastapi import HTTPException
from app.services.device_controller import DeviceController, get_device_controller
from app.services.llm import call_llm_async, direct_ollama_call, direct_openai_call, parse_openai_response
from app.services.supply_chain_service import extract_json_from_response
from app.services.llm import USE_OLLAMA as LL_USE_OLLAMA
from app.services.llm import parse_json_response
from app.services.llm import build_dosing_prompt
from app.models import Device

# -----------------------------
# 1) Discover fallback ’ip’
# -----------------------------
@pytest.mark.asyncio
async def test_discover_fallback_ip_strips_http_prefix(monkeypatch):
    # only /discovery fails, /state succeeds
    class DummyRes:
        def __init__(self, code, data):
            self.status_code = code
            self._json = data
        def json(self): return self._json
    class FakeCli:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, path, *a, **k):
            if path == "/discovery":
                return DummyRes(500, {})
            if path == "/state":
                return DummyRes(200, {"device_id":"X","valves":[]})
            return DummyRes(404,{})
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a,**k: FakeCli())

    dc = DeviceController("10.0.0.5")
    result = await dc.discover()
    assert result["ip"] == "10.0.0.5"
    assert "http://" not in result["ip"]

# -----------------------------
# 2) extract_json_from_response
# -----------------------------
def test_extract_json_happy_path():
    s = "ignore { \"a\": 1, \"b\": 2 } trailing"
    out = extract_json_from_response(s)
    assert out == {"a":1,"b":2}

def test_extract_json_throws_on_no_json():
    with pytest.raises(HTTPException):
        extract_json_from_response("no JSON here")

# -----------------------------
# 3) dynamic USE_OLLAMA logic
# -----------------------------
def test_use_ollama_flag_respects_testing(monkeypatch):
    # force TESTING=1, USE_OLLAMA env=0
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
async def test_call_llm_async_openai(monkeypatch):
    # simulate openai branch
    monkeypatch.setenv("USE_OLLAMA", "false")
    monkeypatch.setenv("TESTING", "0")
    import app.services.llm as llm
    importlib.reload(llm)

    # stub out the OpenAI call+parser
    async def fake_openai(prompt, model):
        return '{"actions":[{"pump_number":1,"chemical_name":"X","dose_ml":5,"reasoning":"OK"}]}'
    monkeypatch.setattr(llm, "direct_openai_call", lambda p,m: asyncio.sleep(0, result='{"actions":[{}]}'))
    monkeypatch.setattr(llm, "parse_openai_response", lambda r: r)
    parsed, raw = await llm.call_llm_async("hi", "mymodel")
    assert isinstance(parsed, dict)
    assert raw.startswith("{")

# -----------------------------
# 5) build_dosing_prompt errors
# -----------------------------
def test_build_dosing_prompt_raises_on_no_pumps():
    dummy = Device(id="d1", mac_id="m", name="n", type="dosing_unit",
                   http_endpoint="e", pump_configurations=None,
                   sensor_parameters={}, valve_configurations=[],
                   switch_configurations=[])
    with pytest.raises(ValueError):
        # no pump_configurations → ValueError
        pytest.run(functools.partial(build_dosing_prompt, dummy, {"ph":7,"tds":100}, {}))

# -----------------------------
# 6) parse_json_response edge
# -----------------------------
def test_parse_json_response_strips_extra_text():
    s = "junk { 'x': 10 } more junk"
    out = parse_json_response(s)
    assert out == {"x":10}

@pytest.mark.asyncio
async def test_discover_both_endpoints_fail(monkeypatch):
    """
    If both /discovery and /state return non-200, discover() should return None.
    """
    class DummyRes:
        def __init__(self, code): self.status_code = code
        def json(self): return {}
    class FakeCli:
        async def __aenter__(self): return self
        async def __aexit__(self,*a): pass
        async def get(self, path, *a,**k):
            return DummyRes(500)  # always fail
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a,**k: FakeCli())

    dc = DeviceController("10.0.0.99")
    result = await dc.discover()
    assert result is None


def test_extract_json_multiple_json_blocks():
    s = "prefix {\"a\":1} middle {\"b\":2} suffix"
    out = extract_json_from_response(s)
    # should grab the *first* {...} block
    assert out == {"a":1}

@pytest.mark.asyncio
async def test_call_llm_async_ollama_http_error(monkeypatch):
    """If direct_ollama_call raises HTTPException, call_llm_async propagates it."""
    monkeypatch.setenv("USE_OLLAMA","true")
    monkeypatch.setenv("TESTING","1")
    import app.services.llm as llm; importlib.reload(llm)

    async def boom(prompt,model):
        raise llm.HTTPException(status_code=500, detail="boom")
    monkeypatch.setattr(llm, "direct_ollama_call", boom)

    with pytest.raises(llm.HTTPException):
        await llm.call_llm_async("foo","bar")

def test_parse_json_response_top_level_list():
    s = "[ {'x':10}, {'y':20} ] extra"
    out = parse_json_response(s)
    assert isinstance(out, list)
    assert out == [{"x":10},{"y":20}]

@pytest.mark.asyncio
async def test_build_dosing_prompt_many_pumps():
    class D:
        def __init__(self):
            self.pump_configurations = [
                {"pump_number": i, "chemical_name": f"C{i}", "chemical_description":"D"} for i in range(1,5)
            ]
            self.id = "X"
    dev = D()
    sensor = {"ph":7,"tds":100}
    profile = {
        "plant_name":"P","plant_type":"T","growth_stage":"G",
        "seeding_date":"2020","region":"R","location":"L",
        "target_ph_min":5,"target_ph_max":8,
        "target_tds_min":50,"target_tds_max":150,
        "dosing_schedule":{}
    }
    prompt = await build_dosing_prompt(dev,sensor,profile)
    for i in range(1,5):
        assert f"Pump {i}:" in prompt
