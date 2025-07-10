# tests/conftest.py

import os
import sys
import pytest

# ── ensure the project root (where app/ lives) is on sys.path ────────────────
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── force the TESTING flag before any FastAPI startup logic ─────────────────
os.environ["TESTING"] = "1"

from app.main import app
from httpx import AsyncClient

# ── A mock that we only want to use for endpoint tests ───────────────────────
class MockController:
    """
    Returns a canned discovery payload depending on the trailing path fragment.
    """
    def __init__(self, device_ip: str, request_timeout: float = 10.0):
        self.device_ip = device_ip

    async def discover(self):
        if self.device_ip.endswith("dosing"):
            return {
                "device_id": "dev-dosing",
                "name":      "Mock Dosing",
                "type":      "dosing_unit",
                "ip":        self.device_ip,
            }
        if self.device_ip.endswith("sensor"):
            return {
                "device_id": "dev-sensor",
                "name":      "Mock Sensor",
                "type":      "ph_tds_sensor",
                "ip":        self.device_ip,
            }
        if self.device_ip.endswith("valve"):
            return {
                "device_id": "dev-valve",
                "name":      "Mock Valve",
                "type":      "valve_controller",
                "ip":        self.device_ip,
            }
        if self.device_ip.endswith("switch"):
            return {
                "device_id": "dev-switch",
                "name":      "Mock Switch",
                "type":      "smart_switch",
                "ip":        self.device_ip,
            }
        return None

@pytest.fixture(autouse=True)
def _patch_device_controller_for_endpoints(monkeypatch, request):
    """
    Only patch DeviceController → MockController when the test
    actually needs the FastAPI client (i.e. endpoint tests).
    """
    if "async_client" in request.fixturenames:
        import app.services.device_controller as dc_mod
        monkeypatch.setattr(dc_mod, "DeviceController", MockController)

# ── async_client fixture for making requests against FastAPI ────────────────
@pytest.fixture
async def async_client():
    async with AsyncClient(app=app, base_url="http://testserver") as client:
        yield client
