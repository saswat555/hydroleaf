# tests/test_device_controller.py
import pytest
import httpx
from app.services.device_controller import DeviceController, get_device_controller

# ----------------------------------------
# Helpers for mocking HTTPX AsyncClient
# ----------------------------------------
class DummyResponse:
    def __init__(self, status_code: int, json_data: dict):
        self.status_code = status_code
        self._json = json_data
    def json(self) -> dict:
        return self._json
    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"Status code {self.status_code}", request=None, response=self)

class FakeAsyncClient:
    def __init__(self, responses: dict[str, tuple[int, dict]]):
        # responses: path suffix -> (status_code, json_data)
        self._responses = responses
    async def __aenter__(self):
        return self
    async def __aexit__(self, exc_type, exc, tb):
        pass
    async def get(self, url: str, *args, **kwargs):
        for path, (code, data) in self._responses.items():
            if url.endswith(path):
                return DummyResponse(code, data)
        return DummyResponse(404, {})
    async def post(self, url: str, *args, json=None, **kwargs):
        for path, (code, data) in self._responses.items():
            if url.endswith(path):
                return DummyResponse(code, data)
        return DummyResponse(404, {})

# ----------------------------------------
# Global fixture: patch httpx.AsyncClient
# ----------------------------------------
@pytest.fixture(autouse=True)
def patch_async_client(monkeypatch):
    # Default mock behavior for all endpoints
    default_responses = {
        '/discovery': (200, {'device_id': 'dev123', 'type': 'dosing_unit', 'version': '1.2.3'}),
        '/version': (200, {'version': '2.0.0'}),
        '/monitor': (200, {'ph': 7.2, 'tds': 450.0}),
        '/pump': (200, {'message': 'pump executed'}),
        '/dose_monitor': (200, {'message': 'combined pump executed'}),
        '/pump_calibration': (200, {'message': 'dosing cancelled'}),
        '/state': (200, {'device_id': 'valve456', 'valves': [{'id': 1, 'state': 'on'}]}),
        '/toggle': (200, {'new_state': 'off'}),
    }
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *args, **kwargs: FakeAsyncClient(default_responses))

# ----------------------------------------
# Test cases for discovery
# ----------------------------------------
@pytest.mark.asyncio
async def test_discover_via_discovery_endpoint():
    """
    Ensure discover() returns primary discovery JSON when /discovery succeeds.
    """
    dc = get_device_controller('http://mock-device')
    data = await dc.discover()
    assert data['device_id'] == 'dev123'
    assert data['type'] == 'dosing_unit'
    assert data['version'] == '1.2.3'
    assert data['ip'] == 'http://mock-device'

@pytest.mark.asyncio
async def test_discover_fallback_to_state_on_discovery_failure(monkeypatch):
    """
    Simulate /discovery failure and verify fallback to /state for valve_controller.
    """
    # Patch AsyncClient with only /state
    responses = {'/discovery': (500, {}), '/state': (200, {'device_id': 'valve789', 'valves': [{'id': 2, 'state': 'off'}]})}
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *args, **kwargs: FakeAsyncClient(responses))

    dc = DeviceController('192.168.0.10')
    result = await dc.discover()
    assert result['device_id'] == 'valve789'
    assert result['type'] == 'valve_controller'
    assert isinstance(result['valves'], list)
    assert result['valves'][0]['id'] == 2
    assert result['ip'] == '192.168.0.10'

# ----------------------------------------
# Test cases for version endpoint
# ----------------------------------------
@pytest.mark.asyncio
async def test_get_version_prefers_version_endpoint():
    """
    get_version() should return /version when available.
    """
    dc = get_device_controller('device-ip')
    version = await dc.get_version()
    assert version == '2.0.0'

@pytest.mark.asyncio
async def test_get_version_fallbacks_to_discover_on_error(monkeypatch):
    """
    If /version returns error, get_version() should fallback to discover().
    """
    responses = {'/version': (404, {}), '/discovery': (200, {'device_id': 'd1', 'version': '3.3.3'})}
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *args, **kwargs: FakeAsyncClient(responses))

    dc = DeviceController('devip')
    version = await dc.get_version()
    assert version == '3.3.3'

# ----------------------------------------
# Test cases for sensor readings
# ----------------------------------------
@pytest.mark.asyncio
async def test_get_sensor_readings_success():
    """
    get_sensor_readings() should parse ph and tds correctly.
    """
    dc = get_device_controller('devip')
    readings = await dc.get_sensor_readings()
    assert isinstance(readings, dict)
    assert readings['ph'] == pytest.approx(7.2)
    assert readings['tds'] == pytest.approx(450.0)

@pytest.mark.asyncio
async def test_get_sensor_readings_http_error(monkeypatch):
    """
    get_sensor_readings() should raise HTTPException on non-200.
    """
    responses = {'/monitor': (500, {'error': 'fail'})}
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *args, **kwargs: FakeAsyncClient(responses))

    dc = DeviceController('devip')
    with pytest.raises(httpx.HTTPStatusError):
        await dc.get_sensor_readings()

# ----------------------------------------
# Test cases for dosing operations
# ----------------------------------------
@pytest.mark.asyncio
async def test_execute_dosing_single_and_combined():
    """
    execute_dosing() should call /pump and /dose_monitor based on flag.
    """
    dc = get_device_controller('devip')
    single = await dc.execute_dosing(1, 100)
    assert single['message'] == 'pump executed'
    combined = await dc.execute_dosing(2, 50, combined=True)
    assert combined['message'] == 'combined pump executed'

@pytest.mark.asyncio
async def test_cancel_dosing_command():
    """
    cancel_dosing() should post to /pump_calibration with stop command.
    """
    dc = get_device_controller('devip')
    res = await dc.cancel_dosing()
    assert res['message'] == 'dosing cancelled'

# ----------------------------------------
# Test cases for valve state and toggle
# ----------------------------------------
@pytest.mark.asyncio
async def test_get_state_success():
    """
    get_state() should fetch full valve state JSON.
    """
    dc = get_device_controller('devip')
    state = await dc.get_state()
    assert state['device_id'] == 'valve456'
    assert isinstance(state['valves'], list)
    assert state['valves'][0]['state'] == 'on'

@pytest.mark.asyncio
async def test_toggle_valve_success():
    """
    toggle_valve() should post to /toggle and return new_state.
    """
    dc = get_device_controller('devip')
    result = await dc.toggle_valve(1)
    assert 'new_state' in result
    assert result['new_state'] == 'off'

# ----------------------------------------
# Test invalid inputs
# ----------------------------------------
@pytest.mark.asyncio
async def test_toggle_valve_invalid_channel(monkeypatch):
    """
    toggle_valve() should raise ValueError if channel not in 1-4.
    """
    dc = get_device_controller('devip')
    with pytest.raises(ValueError):
        # channel 0 is invalid
        await dc.toggle_valve(0)

@pytest.mark.asyncio
async def test_get_version_both_fail(monkeypatch):
    """If both /version and /discovery fail, get_version returns None."""
    class DR:
        def __init__(self, code): self.status_code=code
        def json(self): return {}
    class FC:
        async def __aenter__(self): return self
        async def __aexit__(self,*a): pass
        async def get(self, path, *a,**k):
            return DR(500)  # always error
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a,**k: FC())
    dc = DeviceController("10.1.1.1")
    v = await dc.get_version()
    assert v is None

@pytest.mark.asyncio
async def test_execute_dosing_http_error(monkeypatch):
    """If the /pump endpoint returns 500, execute_dosing raises HTTPStatusError."""
    # arrange FakeAsyncClient whose /pump returns 500
    from app.services.device_controller import DeviceController
    class FR:
        def __init__(self,*a,**k): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*a): pass
        async def post(self, url, *a, json=None, **k):
            return DummyResponse(500, {})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a,**k: FR())
    dc = DeviceController("ip")
    with pytest.raises(httpx.HTTPStatusError):
        await dc.execute_dosing(1,100)

@pytest.mark.asyncio
async def test_toggle_valve_http_error(monkeypatch):
    """If /toggle returns 500, toggle_valve raises HTTPStatusError."""
    class FR:
        def __init__(self,*a,**k): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*a): pass
        async def post(self, url, *a, json=None, **k):
            return DummyResponse(500,{})
    from app.services.device_controller import DeviceController
    monkeypatch.setattr(httpx,'AsyncClient',lambda *a,**k: FR())
    dc = DeviceController("ip")
    with pytest.raises(httpx.HTTPStatusError):
        await dc.toggle_valve(2)

