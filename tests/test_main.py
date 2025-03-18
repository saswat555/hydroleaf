# test_main.py

import os
import json
import pytest
import pytest_asyncio
import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from httpx import AsyncClient, ASGITransport
from asgi_lifespan import LifespanManager

from app.main import app
from app.schemas import DeviceType
from app.core.database import Base, engine
from app.dependencies import get_current_user
from app.services.device_controller import DeviceController

# ------------------------------------------------------------------------------
# Re-create the database schema once per session
# ------------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def recreate_database():
    """Create a fresh database schema for this test session."""
    async def _recreate():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_recreate())

# ------------------------------------------------------------------------------
# Launch the simulated device server on port 8080 for the duration of tests
# ------------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def start_simulated_device():
    """
    Launch the simulated ESP device server using the simulate_device.py script.
    This ensures that endpoints like /discovery, /pump, /monitor are available.
    """
    import subprocess, time
    proc = subprocess.Popen(["python", "simulate_device.py"])
    time.sleep(2)  # wait a bit for the server to start
    yield
    proc.terminate()
    proc.wait()

# ------------------------------------------------------------------------------
# Override authentication globally with a dummy user
# ------------------------------------------------------------------------------
dummy_user = type("DummyUser", (), {
    "id": 1,
    "email": "dummy@example.com",
    "hashed_password": "dummy",
    "role": "user",
    "created_at": datetime.now(timezone.utc)
})
app.dependency_overrides[get_current_user] = lambda: dummy_user

# ------------------------------------------------------------------------------
# Fixtures for dosing and sensor devices (ensuring unique http_endpoint)
# ------------------------------------------------------------------------------
@pytest.fixture
def test_dosing_device_fixture():
    return {
        "name": "HighPrecision Dosing Unit",
        "type": DeviceType.DOSING_UNIT,
        "mac_id": f"MAC_TEST_DOSING_{uuid4()}",
        # Append a unique path so that the http_endpoint is unique
        "http_endpoint": f"http://localhost:8080/{uuid4()}",
        "location_description": "Greenhouse #12, East Wing",
        "farm_id": None,
        "pump_configurations": [
            {"pump_number": 1, "chemical_name": "Nutrient A", "chemical_description": "Core nutrient blend"},
            {"pump_number": 2, "chemical_name": "Nutrient B", "chemical_description": "Supplemental nutrient blend"},
            {"pump_number": 3, "chemical_name": "Nutrient C", "chemical_description": "pH balancer"},
            {"pump_number": 4, "chemical_name": "Nutrient D", "chemical_description": "Trace elements"}
        ]
    }

@pytest.fixture
def test_sensor_device_fixture():
    return {
        "name": "HighAccuracy pH/TDS Sensor",
        "type": DeviceType.PH_TDS_SENSOR,
        "mac_id": f"MAC_TEST_SENSOR_{uuid4()}",
        # Append a unique path for uniqueness
        "http_endpoint": f"http://localhost:8080/{uuid4()}",
        "location_description": "Row 5, Reservoir Edge",
        "farm_id": None,
        "sensor_parameters": {"ph_calibration": "7.01", "tds_calibration": "600"}
    }

# ------------------------------------------------------------------------------
# Create an async test client
# ------------------------------------------------------------------------------
@pytest_asyncio.fixture
async def async_client():
    transport = ASGITransport(app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=True) as client:
        yield client

# ------------------------------------------------------------------------------
# Helper to patch device discovery so it always succeeds
# ------------------------------------------------------------------------------
def patch_successful_discovery(monkeypatch, device_fixture):
    async def dummy_discover(self):
        return {
            "device_id": "dummy_device",
            "name": device_fixture["name"],
            "type": "dosing_unit",
            "status": "online",
            "version": "1.0",
            "ip": self.device_ip
        }
    monkeypatch.setattr(DeviceController, "discover", dummy_discover)

# ------------------------------------------------------------------------------
# Patch LLM functions for dosing endpoints so that they return valid JSON
# ------------------------------------------------------------------------------
@pytest.fixture
def patch_llm_for_dosing(monkeypatch):
    async def dummy_call_llm_async(prompt: str, model_name: str = None):
        # Return a dummy dosing plan in a valid format
        return (
            {
                "actions": [
                    {
                        "pump_number": 2,
                        "chemical_name": "Nutrient B",
                        "dose_ml": 25,
                        "reasoning": "Dummy LLM dosing recommendation"
                    }
                ],
                "next_check_hours": 24
            },
            "raw LLM response"
        )
    monkeypatch.setattr("app.services.llm.call_llm_async", dummy_call_llm_async)

# ------------------------------------------------------------------------------
# Patch LLM functions for supply chain endpoints so that they return valid JSON
# ------------------------------------------------------------------------------
@pytest.fixture
def patch_llm_for_supply_chain(monkeypatch):
    async def dummy_call_llm(prompt: str, model_name: str = None):
        # Return a dummy JSON response for supply chain analysis
        return {
            "final_recommendation": "Dummy optimized transport plan",
            "reasoning": "Dummy detailed explanation"
        }
    monkeypatch.setattr("app.services.supply_chain_service.call_llm", dummy_call_llm)

# ------------------------------------------------------------------------------
# Patch fetch_and_average_value for supply chain endpoints
# ------------------------------------------------------------------------------
@pytest.fixture
def patch_fetch_and_average(monkeypatch):
    async def dummy_fetch_and_average_value(q: str) -> float:
        if "distance" in q.lower():
            return 350.0
        if "cost" in q.lower():
            return 1.0
        if "travel" in q.lower():
            return 6.0
        if "perish" in q.lower():
            return 24.0
        if "market price" in q.lower():
            return 2.5
        return 0.0
    monkeypatch.setattr("app.services.supply_chain_service.fetch_and_average_value", dummy_fetch_and_average_value)

# ------------------------------------------------------------------------------
# TEST CLASSES
# ------------------------------------------------------------------------------
class TestHealth:
    @pytest.mark.asyncio
    async def test_health_endpoints(self, async_client):
        async with LifespanManager(app):
            r = await async_client.get("/api/v1/health")
            assert r.status_code == 200
            d = r.json()
            assert d["status"] == "healthy"

            r_db = await async_client.get("/api/v1/health/database")
            assert r_db.status_code == 200
            dbd = r_db.json()
            assert "status" in dbd

            r_all = await async_client.get("/api/v1/health/all")
            assert r_all.status_code == 200
            all_d = r_all.json()
            assert "system" in all_d
            assert "database" in all_d

class TestDevices:
    @pytest.mark.asyncio
    async def test_create_dosing_device(self, test_dosing_device_fixture, async_client, monkeypatch):
        patch_successful_discovery(monkeypatch, test_dosing_device_fixture)
        async with LifespanManager(app):
            r = await async_client.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["name"] == test_dosing_device_fixture["name"]
            assert data["mac_id"] == test_dosing_device_fixture["mac_id"]

    @pytest.mark.asyncio
    async def test_create_sensor_device(self, test_sensor_device_fixture, async_client):
        async with LifespanManager(app):
            r = await async_client.post("/api/v1/devices/sensor", json=test_sensor_device_fixture)
            assert r.status_code == 200, r.text
            d = r.json()
            assert d["name"] == test_sensor_device_fixture["name"]
            assert d["type"] == DeviceType.PH_TDS_SENSOR

    @pytest.mark.asyncio
    async def test_get_device_list(self, test_dosing_device_fixture, test_sensor_device_fixture, async_client, monkeypatch):
        patch_successful_discovery(monkeypatch, test_dosing_device_fixture)
        async with LifespanManager(app):
            # Create one dosing device and one sensor device
            await async_client.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            await async_client.post("/api/v1/devices/sensor", json=test_sensor_device_fixture)
            r = await async_client.get("/api/v1/devices")
            assert r.status_code == 200, r.text
            data = r.json()
            # Expect at least two devices to be returned
            assert len(data) >= 2

    @pytest.mark.asyncio
    async def test_discover_device_not_found(self, monkeypatch, async_client):
        async def dummy_discover(self):
            return None
        monkeypatch.setattr(DeviceController, "discover", dummy_discover)
        async with LifespanManager(app):
            r = await async_client.get("/api/v1/devices/discover", params={"ip": "192.0.2.1"})
            assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_discover_device_found(self, monkeypatch, async_client):
        async def dummy_discover(self):
            if self.device_ip == "192.168.54.198":
                return {
                    "device_id": "dummy_device",
                    "name": "dummy_device",
                    "type": "dosing_unit",
                    "status": "online",
                    "version": "1.0",
                    "ip": self.device_ip
                }
            return None
        monkeypatch.setattr(DeviceController, "discover", dummy_discover)
        async with LifespanManager(app):
            r = await async_client.get("/api/v1/devices/discover", params={"ip": "192.168.54.198"})
            assert r.status_code == 200, r.text
            d = r.json()
            assert d["id"] == "dummy_device"
            assert d["ip"] == "192.168.54.198"

    @pytest.mark.asyncio
    async def test_get_device_details_not_found(self, async_client):
        async with LifespanManager(app):
            r = await async_client.get("/api/v1/devices/9999")
            assert r.status_code == 404

class TestDosing:
    @pytest.mark.asyncio
    async def test_create_dosing_profile(self, test_dosing_device_fixture, async_client, monkeypatch, patch_llm_for_dosing):
        patch_successful_discovery(monkeypatch, test_dosing_device_fixture)
        async with LifespanManager(app):
            # Create a dosing device
            r_dev = await async_client.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            assert r_dev.status_code == 200, r_dev.text
            dev_id = r_dev.json()["id"]

            profile = {
                "device_id": dev_id,
                "plant_name": "Tomato",
                "plant_type": "Vegetable",
                "growth_stage": "Seedling",
                "seeding_date": datetime.now(timezone.utc).isoformat(),
                "target_ph_min": 5.5,
                "target_ph_max": 6.5,
                "target_tds_min": 600,
                "target_tds_max": 800,
                "dosing_schedule": {"morning": 50.0, "evening": 40.0}
            }
            pr = await async_client.post("/api/v1/config/dosing-profile", json=profile)
            assert pr.status_code == 200, pr.text
            d = pr.json()
            assert d["device_id"] == dev_id

    @pytest.mark.asyncio
    async def test_execute_dosing_operation(self, test_dosing_device_fixture, monkeypatch, async_client, patch_llm_for_dosing):
        patch_successful_discovery(monkeypatch, test_dosing_device_fixture)
        async def dummy_execute_dosing(device_id, http_endpoint, dosing_actions, combined=False):
            return {
                "device_id": device_id,
                "operation_id": "dummy_op",
                "actions": [
                    {"pump_number": 1, "chemical_name": "Dummy Chem", "dose_ml": 10, "reasoning": "Test reason"}
                ],
                "status": "command_sent",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        monkeypatch.setattr("app.services.dose_manager.dose_manager.execute_dosing", dummy_execute_dosing)
        async with LifespanManager(app):
            r_dev = await async_client.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            assert r_dev.status_code == 200, r_dev.text
            dev_id = r_dev.json()["id"]

            payload = [{"pump": 1, "amount": 10}]
            r_dosing = await async_client.post(f"/api/v1/dosing/execute/{dev_id}?combined=true", json=payload)
            assert r_dosing.status_code == 200, r_dosing.text
            d = r_dosing.json()
            assert d["device_id"] == dev_id
            assert len(d["actions"]) == 1
            assert d["actions"][0]["chemical_name"] == "Dummy Chem"

    @pytest.mark.asyncio
    async def test_cancel_dosing_operation(self, test_dosing_device_fixture, monkeypatch, async_client, patch_llm_for_dosing):
        patch_successful_discovery(monkeypatch, test_dosing_device_fixture)
        async def dummy_cancel_dosing(device_id, http_endpoint):
            return {"status": "dosing_cancelled", "device_id": device_id, "response": {"msg": "All pumps off"}}
        monkeypatch.setattr("app.services.dose_manager.dose_manager.cancel_dosing", dummy_cancel_dosing)
        async with LifespanManager(app):
            r_dev = await async_client.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            assert r_dev.status_code == 200, r_dev.text
            dev_id = r_dev.json()["id"]

            r_cancel = await async_client.post(f"/api/v1/dosing/cancel/{dev_id}")
            assert r_cancel.status_code == 200, r_cancel.text
            d = r_cancel.json()
            assert d["status"] == "dosing_cancelled"

    @pytest.mark.asyncio
    async def test_llm_dosing_request(self, test_dosing_device_fixture, monkeypatch, async_client, patch_llm_for_dosing):
        # For this test, we do not patch the LLM call so that an actual LLM call is made.
        patch_successful_discovery(monkeypatch, test_dosing_device_fixture)
        async with LifespanManager(app):
            r_dev = await async_client.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            assert r_dev.status_code == 200, r_dev.text
            dev_id = r_dev.json()["id"]

            payload = {
                "sensor_data": {"ph": 6.0, "tds": 700},
                "plant_profile": {"plant_name": "Tomato", "plant_type": "Vegetable"}
            }
            r_llm = await async_client.post(f"/api/v1/dosing/llm-request?device_id={dev_id}", json=payload)
            assert r_llm.status_code == 200, f"LLM dosing request failed: {r_llm.text}"
            data = r_llm.json()
            # Expect a list with two elements: the dosing plan and the raw LLM response
            assert isinstance(data, list)
            assert len(data) == 2
            plan, raw_llm = data
            assert "actions" in plan

    @pytest.mark.asyncio
    async def test_llm_plan(self, test_dosing_device_fixture, monkeypatch, async_client, patch_llm_for_dosing):
        # For this test, we also use the actual LLM call.
        patch_successful_discovery(monkeypatch, test_dosing_device_fixture)
        async with LifespanManager(app):
            r_dev = await async_client.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            assert r_dev.status_code == 200, r_dev.text
            dev_id = r_dev.json()["id"]

            payload = {
                "sensor_data": {"ph": 6.5, "tds": 750},
                "plant_profile": {
                    "plant_name": "Tomato",
                    "plant_type": "Vegetable",
                    "location": "Greenhouse A",
                    "seeding_date": datetime.now(timezone.utc).isoformat(),
                    "growth_stage": "Seedling"
                },
                "query": "Optimize growth conditions"
            }
            r_plan = await async_client.post(f"/api/v1/dosing/llm-plan?device_id={dev_id}", json=payload)
            assert r_plan.status_code == 200, r_plan.text
            d = r_plan.json()
            assert "plan" in d

class TestConfig:
    @pytest.mark.asyncio
    async def test_system_info(self, async_client):
        async with LifespanManager(app):
            r = await async_client.get("/api/v1/config/system-info")
            assert r.status_code == 200, r.text
            d = r.json()
            assert "version" in d
            assert "device_count" in d

class TestSupplyChain:
    @pytest.mark.asyncio
    async def test_supply_chain_analysis(self, async_client, patch_llm_for_supply_chain, patch_fetch_and_average):
        req = {
            "origin": "Rewa, Madhya Pradesh",
            "destination": "Bhopal, Madhya Pradesh",
            "produce_type": "Lettuce",
            "weight_kg": 50,
            "transport_mode": "railway"
        }
        async with LifespanManager(app):
            r = await async_client.post("/api/v1/supply_chain", json=req)
            assert r.status_code == 200, f"Supply chain analysis failed: {r.text}"
            data = r.json()
            assert data["origin"] == "Rewa, Madhya Pradesh"
            assert "final_recommendation" in data

    @pytest.mark.asyncio
    async def test_supply_chain_analysis_rewa(self, monkeypatch, async_client, patch_llm_for_supply_chain):
        async def dummy_fetch_and_average_value(q: str) -> float:
            if "distance" in q.lower():
                return 345.0
            if "cost" in q.lower():
                return 1.5
            if "travel" in q.lower():
                return 5.5
            if "perish" in q.lower():
                return 24.0
            if "market price" in q.lower():
                return 3.0
            return 0.0
        monkeypatch.setattr("app.services.supply_chain_service.fetch_and_average_value", dummy_fetch_and_average_value)
    
        req = {
            "origin": "Rewa, Madhya Pradesh",
            "destination": "Bhopal, Madhya Pradesh",
            "produce_type": "Tomatoes",
            "weight_kg": 100,
            "transport_mode": "railway"
        }
        async with LifespanManager(app):
            r = await async_client.post("/api/v1/supply_chain", json=req)
            assert r.status_code == 200, f"Supply chain analysis failed: {r.text}"
            d = r.json()
            assert d["origin"] == "Rewa, Madhya Pradesh"
            # Expect the dummy fetch to return 345.0 km
            assert d["distance_km"] == 345.0
            assert "final_recommendation" in d

class TestCloud:
    @pytest.mark.asyncio
    async def test_authenticate_cloud(self, async_client):
        auth_req = {"device_id": "dummy_device", "cloud_key": "my_cloud_secret"}
        async with LifespanManager(app):
            r = await async_client.post("/api/v1/authenticate", json=auth_req)
            assert r.status_code == 200, r.text
            d = r.json()
            assert "token" in d
            assert "message" in d

    @pytest.mark.asyncio
    async def test_dosing_cancel_cloud(self, async_client):
        req = {"device_id": "dummy_device", "event": "dosing_cancelled"}
        async with LifespanManager(app):
            r = await async_client.post("/api/v1/dosing_cancel", json=req)
            assert r.status_code == 200, r.text
            d = r.json()
            assert d["message"] == "Dosing cancellation received"

class TestAdminAndHeartbeat:
    @pytest.mark.asyncio
    async def test_admin_devices(self, async_client):
        async with LifespanManager(app):
            r = await async_client.get("/admin/devices")
            assert r.status_code == 200, r.text
            data = r.json()
            assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_heartbeat(self, async_client):
        payload = {"device_id": "dummy_device"}
        async with LifespanManager(app):
            r = await async_client.post("/heartbeat", json=payload)
            assert r.status_code == 200, r.text
            d = r.json()
            assert d["status"] == "ok"
