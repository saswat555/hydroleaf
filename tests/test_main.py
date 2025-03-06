import json
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.schemas import DeviceType

# Fixed JSON to simulate a successful device discovery response.
FIXED_DISCOVERY_RESPONSE = {
    "device_id": "dummy_device",
    "status": "online",
    "version": "2.0.0",
    "type": "DOSING_MONITOR_UNIT",
    "ip": "192.168.54.198"
}

# Test data for devices.
TEST_DOSING_DEVICE = {
    "name": "Test Dosing Unit",
    "type": DeviceType.DOSING_UNIT,
    "http_endpoint": "krishiverse/devices/test_dosing",  # a unique suffix will be appended
    "location_description": "Test Location",
    "pump_configurations": [
        {
            "pump_number": 1,
            "chemical_name": "Nutrient A",
            "chemical_description": "Primary nutrient mix"
        },
        {
            "pump_number": 2,
            "chemical_name": "Nutrient B",
            "chemical_description": "Secondary nutrient mix"
        }
    ]
}

TEST_SENSOR_DEVICE = {
    "name": "Test pH/TDS Sensor",
    "type": DeviceType.PH_TDS_SENSOR,
    "http_endpoint": "krishiverse/devices/test_sensor",  # a unique suffix will be appended
    "location_description": "Test Location",
    "sensor_parameters": {
        "ph_calibration": "7.0",
        "tds_calibration": "500"
    }
}


@pytest.fixture
def test_dosing_device_fixture() -> dict:
    unique_endpoint = f"krishiverse/devices/test_dosing_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
    device = TEST_DOSING_DEVICE.copy()
    device["http_endpoint"] = unique_endpoint
    return device


@pytest.fixture
def test_sensor_device_fixture() -> dict:
    unique_endpoint = f"krishiverse/devices/test_sensor_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
    device = TEST_SENSOR_DEVICE.copy()
    device["http_endpoint"] = unique_endpoint
    return device


# Dummy test_session fixture (used in plant endpoints)
@pytest.fixture
def test_session():
    # Return a dummy session; note that we override get_db in plant tests.
    return None


# -------------------------
# Test Devices endpoints
# -------------------------
class TestDevices:
    @pytest.mark.asyncio
    async def test_create_dosing_device(self, test_dosing_device_fixture: dict, monkeypatch):
        monkeypatch.setattr(
            "app.services.device_controller.DeviceController.discover",
            lambda self: FIXED_DISCOVERY_RESPONSE
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
        assert resp.status_code == 200, f"Response: {resp.text}"
        data = resp.json()
        assert data["name"] == test_dosing_device_fixture["name"]

    @pytest.mark.asyncio
    async def test_create_sensor_device(self, test_sensor_device_fixture: dict):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/api/v1/devices/sensor", json=test_sensor_device_fixture)
        assert resp.status_code == 200, f"Response: {resp.text}"
        data = resp.json()
        assert data["name"] == test_sensor_device_fixture["name"]
        assert data["type"] == DeviceType.PH_TDS_SENSOR

    @pytest.mark.asyncio
    async def test_get_device_list(self, test_dosing_device_fixture: dict, test_sensor_device_fixture: dict):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            await ac.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            await ac.post("/api/v1/devices/sensor", json=test_sensor_device_fixture)
            resp = await ac.get("/api/v1/devices")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 2

    @pytest.mark.asyncio
    async def test_check_device_not_found(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/v1/devices/discover", params={"ip": "192.0.2.1"})
        assert resp.status_code == 404
        assert "No device found" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_check_device_found(self, monkeypatch):
        async def dummy_discover(self):
            return FIXED_DISCOVERY_RESPONSE
        monkeypatch.setattr("app.services.device_controller.DeviceController.discover", dummy_discover)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/v1/devices/discover", params={"ip": "192.168.54.198"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ip"] == "192.168.54.198"
        assert data["status"] == "online"


# -------------------------
# Test Dosing endpoints (LLM flow without calling get_device)
# -------------------------
class TestDosing:
    @pytest.mark.asyncio
    async def test_create_dosing_profile(self, test_dosing_device_fixture: dict, test_session):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_resp = await ac.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            assert device_resp.status_code == 200, f"Device creation failed: {device_resp.text}"
            device_id = device_resp.json()["id"]
            test_profile = {
                "device_id": device_id,
                "plant_name": "Test Tomato",
                "plant_type": "Vegetable",
                "growth_stage": "Seedling",
                "seeding_date": datetime.now(timezone.utc).isoformat(),
                "target_ph_min": 5.5,
                "target_ph_max": 6.5,
                "target_tds_min": 600,
                "target_tds_max": 800,
                "dosing_schedule": {"morning": 50.0, "evening": 40.0}
            }
            profile_resp = await ac.post("/api/v1/config/dosing-profile", json=test_profile)
            assert profile_resp.status_code == 200, f"Profile creation failed: {profile_resp.text}"
            profile_data = profile_resp.json()
            assert profile_data["device_id"] == device_id
            assert profile_data["plant_name"] == test_profile["plant_name"]
            profiles_resp = await ac.get(f"/api/v1/config/dosing-profiles/{device_id}")
            assert profiles_resp.status_code == 200
            profiles = profiles_resp.json()
            assert isinstance(profiles, list)
            assert len(profiles) >= 1

    @pytest.mark.asyncio
    async def test_execute_dosing(self, test_dosing_device_fixture: dict, monkeypatch):
        async def dummy_execute_dosing_operation(device_id, http_endpoint, dosing_actions, combined=False):
            return {
                "device_id": device_id,
                "operation_id": "dummy_operation",
                "actions": [{
                    "pump_number": 1,
                    "chemical_name": "Dummy Chemical",
                    "dose_ml": 30,
                    "reasoning": "Test dosing operation"
                }],
                "status": "command_sent",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        monkeypatch.setattr("app.services.dose_manager.dose_manager.execute_dosing", dummy_execute_dosing_operation)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_resp = await ac.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            device_data = device_resp.json()
            device_id = device_data.get("id")
            assert device_id is not None
            exec_resp = await ac.post(
                f"/api/v1/dosing/execute/{device_id}",
                json=[{"pump": 1, "amount": 30}],
                follow_redirects=True
            )
            assert exec_resp.status_code == 200, f"Execution failed: {exec_resp.text}"
            data = exec_resp.json()
            assert data.get("device_id") == device_id
            assert "operation_id" in data
            action = data.get("actions", [])[0]
            for key in ("pump_number", "chemical_name", "dose_ml", "reasoning"):
                assert key in action


# -------------------------
# Test Supply Chain Analysis endpoint
# -------------------------
class TestSupplyChain:
    @pytest.mark.asyncio
    async def test_supply_chain_analysis(self, monkeypatch):
        request_data = {
            "origin": "Mumbai",
            "destination": "Delhi",
            "produce_type": "Tomato",
            "weight_kg": 100,
            "transport_mode": "railway"
        }
        async def dummy_fetch_and_average_value(query: str) -> float:
            if "distance" in query.lower():
                return 1400.0
            elif "cost" in query.lower():
                return 0.5
            elif "travel" in query.lower():
                return 24.0
            elif "perish" in query.lower():
                return 48.0
            elif "market price" in query.lower():
                return 1.0
            return 0.0
        monkeypatch.setattr("app.services.supply_chain_service.fetch_and_average_value", dummy_fetch_and_average_value)
        # Patch ChatOllama clients to simulate a valid LLM call.
        from app.services.supply_chain_service import ollama_client_1_5b, ollama_client_7b
        class DummyOllama:
            def chat(self, messages):
                return {"message": {"content": '{"final_recommendation": "Use refrigerated containers", "reasoning": "Optimized plan"}'}}
        dummy_ollama = DummyOllama()
        # Since ChatOllama's attributes are immutable, patch on the class:
        type(ollama_client_1_5b).chat = lambda cls, messages: dummy_ollama.chat(messages)
        type(ollama_client_7b).chat = lambda cls, messages: dummy_ollama.chat(messages)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/api/v1/supply_chain/", json=request_data)
        assert resp.status_code == 200, f"Supply chain analysis failed: {resp.text}"
        data = resp.json()
        assert data["origin"] == "Mumbai"
        assert "final_recommendation" in data
