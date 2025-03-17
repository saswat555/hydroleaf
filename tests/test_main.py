import os
import json
import pytest
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

# --- Ensure fresh schema ---
@pytest.fixture(scope="session", autouse=True)
def recreate_database():
    async def _recreate():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_recreate())


# --- Override authentication ---
dummy_user = type("DummyUser", (), {
    "id": 1,
    "email": "dummy@example.com",
    "hashed_password": "dummy",
    "role": "user",
    "created_at": datetime.now(timezone.utc)
})
app.dependency_overrides[get_current_user] = lambda: dummy_user

# --- Updated Fixtures for devices with unique HTTP endpoints ---
@pytest.fixture
def test_dosing_device_fixture():
    unique_endpoint = f"http://localhost/simulated_esp_dosing/{uuid4()}"
    return {
        "name": "HighPrecision Dosing Unit",
        "type": DeviceType.DOSING_UNIT,
        "mac_id": "MAC_TEST_DOSING_" + str(uuid4()),
        "http_endpoint": unique_endpoint,
        "location_description": "Greenhouse #12, East Wing",
        "pump_configurations": [
            {"pump_number": 1, "chemical_name": "Nutrient A", "chemical_description": "Core nutrient blend"},
            {"pump_number": 2, "chemical_name": "Nutrient B", "chemical_description": "Supplemental nutrient blend"},
            {"pump_number": 3, "chemical_name": "Nutrient C", "chemical_description": "pH balancer"},
            {"pump_number": 4, "chemical_name": "Nutrient D", "chemical_description": "Tertiary trace elements"}
        ]
    }

@pytest.fixture
def test_sensor_device_fixture():
    unique_endpoint = f"http://localhost/simulated_esp_sensor/{uuid4()}"
    return {
        "name": "HighAccuracy pH/TDS Sensor",
        "type": DeviceType.PH_TDS_SENSOR,
        "mac_id": "MAC_TEST_SENSOR_" + str(uuid4()),
        "http_endpoint": unique_endpoint,
        "location_description": "Row 5, Reservoir Edge",
        "sensor_parameters": {"ph_calibration": "7.01", "tds_calibration": "600"}
    }

# --- Helper fixture to create an AsyncClient with ASGITransport ---
@pytest.fixture
def async_client():
    transport = ASGITransport(app)
    client = AsyncClient(transport=transport, base_url="http://test", follow_redirects=True)
    yield client
    asyncio.run(client.aclose())

# --- Helper to patch successful discovery for dosing devices ---
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

# --- Tests Begin ---

class TestHealth:
    @pytest.mark.asyncio
    async def test_health_endpoints(self, async_client):
        async with LifespanManager(app):
            resp = await async_client.get("/api/v1/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "healthy"

            resp_db = await async_client.get("/api/v1/health/database")
            assert resp_db.status_code == 200
            data_db = resp_db.json()
            assert "status" in data_db

            resp_all = await async_client.get("/api/v1/health/all")
            assert resp_all.status_code == 200
            data_all = resp_all.json()
            assert "system" in data_all and "database" in data_all

class TestDevices:
    @pytest.mark.asyncio
    async def test_create_dosing_device(self, test_dosing_device_fixture, async_client, monkeypatch):
        patch_successful_discovery(monkeypatch, test_dosing_device_fixture)
        async with LifespanManager(app):
            resp = await async_client.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            assert resp.status_code == 200, f"Device creation failed: {resp.text}"
            data = resp.json()
            assert data["name"] == test_dosing_device_fixture["name"]
            assert data["mac_id"] == test_dosing_device_fixture["mac_id"]

    @pytest.mark.asyncio
    async def test_create_sensor_device(self, test_sensor_device_fixture, async_client):
        async with LifespanManager(app):
            resp = await async_client.post("/api/v1/devices/sensor", json=test_sensor_device_fixture)
            assert resp.status_code == 200, f"Sensor device creation failed: {resp.text}"
            data = resp.json()
            assert data["name"] == test_sensor_device_fixture["name"]
            assert data["type"] == DeviceType.PH_TDS_SENSOR

    @pytest.mark.asyncio
    async def test_get_device_list(self, test_dosing_device_fixture, test_sensor_device_fixture, async_client, monkeypatch):
        patch_successful_discovery(monkeypatch, test_dosing_device_fixture)
        async with LifespanManager(app):
            await async_client.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            await async_client.post("/api/v1/devices/sensor", json=test_sensor_device_fixture)
            resp = await async_client.get("/api/v1/devices")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)
            assert len(data) >= 2

    @pytest.mark.asyncio
    async def test_discover_device_not_found(self, monkeypatch, async_client):
        async def dummy_discover(self):
            return None
        monkeypatch.setattr(DeviceController, "discover", dummy_discover)
        async with LifespanManager(app):
            resp = await async_client.get("/api/v1/devices/discover", params={"ip": "192.0.2.1"})
            assert resp.status_code == 404

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
            resp = await async_client.get("/api/v1/devices/discover", params={"ip": "192.168.54.198"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["id"] == "dummy_device"
            assert data["ip"] == "192.168.54.198"

    @pytest.mark.asyncio
    async def test_get_device_details_not_found(self, async_client):
        async with LifespanManager(app):
            resp = await async_client.get("/api/v1/devices/9999")
            assert resp.status_code == 404

class TestDosing:
    @pytest.mark.asyncio
    async def test_create_dosing_profile(self, test_dosing_device_fixture, async_client, monkeypatch):
        patch_successful_discovery(monkeypatch, test_dosing_device_fixture)
        async with LifespanManager(app):
            device_resp = await async_client.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            assert device_resp.status_code == 200, f"Device creation failed: {device_resp.text}"
            device_id = device_resp.json()["id"]
            now_iso = datetime.now(timezone.utc).isoformat()
            profile = {
                "device_id": device_id,
                "plant_name": "Tomato",
                "plant_type": "Vegetable",
                "growth_stage": "Seedling",
                "seeding_date": now_iso,
                "target_ph_min": 5.5,
                "target_ph_max": 6.5,
                "target_tds_min": 600,
                "target_tds_max": 800,
                "dosing_schedule": {"morning": 50.0, "evening": 40.0}
            }
            resp_profile = await async_client.post("/api/v1/config/dosing-profile", json=profile)
            assert resp_profile.status_code == 200, f"Profile creation failed: {resp_profile.text}"
            data = resp_profile.json()
            assert data["device_id"] == device_id

    @pytest.mark.asyncio
    async def test_execute_dosing_operation(self, test_dosing_device_fixture, monkeypatch, async_client):
        patch_successful_discovery(monkeypatch, test_dosing_device_fixture)
        async def dummy_execute_dosing(device_id, http_endpoint, dosing_actions, combined=False):
            return {
                "device_id": device_id,
                "operation_id": "dummy_op",
                "actions": [{"pump_number": 1, "chemical_name": "Dummy", "dose_ml": 10, "reasoning": "Test"}],
                "status": "command_sent",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        monkeypatch.setattr("app.services.dose_manager.dose_manager.execute_dosing", dummy_execute_dosing)
        async with LifespanManager(app):
            device_resp = await async_client.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            assert device_resp.status_code == 200, f"Device creation failed: {device_resp.text}"
            device_id = device_resp.json()["id"]
            payload = [{"pump": 1, "amount": 10}]
            resp = await async_client.post(f"/api/v1/dosing/execute/{device_id}?combined=true", json=payload)
            assert resp.status_code == 200
            data = resp.json()
            assert data["device_id"] == device_id
            assert "operation_id" in data

    @pytest.mark.asyncio
    async def test_cancel_dosing_operation(self, test_dosing_device_fixture, monkeypatch, async_client):
        patch_successful_discovery(monkeypatch, test_dosing_device_fixture)
        async def dummy_cancel_dosing(device_id, http_endpoint):
            return {"status": "dosing_cancelled", "device_id": device_id, "response": {"msg": "All pumps off"}}
        monkeypatch.setattr("app.services.dose_manager.dose_manager.cancel_dosing", dummy_cancel_dosing)
        async with LifespanManager(app):
            device_resp = await async_client.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            assert device_resp.status_code == 200, f"Device creation failed: {device_resp.text}"
            device_id = device_resp.json()["id"]
            resp = await async_client.post(f"/api/v1/dosing/cancel/{device_id}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "dosing_cancelled"

    @pytest.mark.asyncio
    async def test_llm_dosing_request(self, test_dosing_device_fixture, monkeypatch, async_client):
        # Patch discovery so device creation succeeds.
        patch_successful_discovery(monkeypatch, test_dosing_device_fixture)
        
        # Patch process_dosing_request so that it only generates a prompt and returns dummy LLM responses
        async def dummy_process_dosing_request(device_id, sensor_data, plant_profile, db):
            # Do NOT call execute_dosing_plan here. Simply return a dummy dosing plan.
            return ({"recommended_dose": [{"dummy": "dose"}]}, "raw llm response")
        
        monkeypatch.setattr("app.services.llm.process_dosing_request", dummy_process_dosing_request)
        
        async with LifespanManager(app):
            device_resp = await async_client.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            assert device_resp.status_code == 200, f"Device creation failed: {device_resp.text}"
            device_id = device_resp.json()["id"]
            req_payload = {
                "sensor_data": {"ph": 6.0, "tds": 700},
                "plant_profile": {"plant_name": "Tomato", "plant_type": "Vegetable"}
            }
            resp = await async_client.post(f"/api/v1/dosing/llm-request?device_id={device_id}", json=req_payload)
            assert resp.status_code == 200, f"LLM dosing request failed: {resp.text}"
            data = resp.json()
            # Since a tuple is serialized as a list, we expect a list of two elements.
            assert isinstance(data, list)
            assert isinstance(data[0], dict)
            assert "recommended_dose" in data[0]


    @pytest.mark.asyncio
    async def test_llm_plan(self, test_dosing_device_fixture, monkeypatch, async_client):
        # Patch discovery so device creation succeeds.
        patch_successful_discovery(monkeypatch, test_dosing_device_fixture)
        async with LifespanManager(app):
            device_resp = await async_client.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            assert device_resp.status_code == 200, f"Device creation failed: {device_resp.text}"
            device_id = device_resp.json()["id"]
            req_payload = {
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
            resp = await async_client.post(f"/api/v1/dosing/llm-plan?device_id={device_id}", json=req_payload)
            assert resp.status_code == 200, f"LLM plan request failed: {resp.text}"
            data = resp.json()
            assert "plan" in data

class TestConfig:
    @pytest.mark.asyncio
    async def test_system_info(self, async_client):
        async with LifespanManager(app):
            resp = await async_client.get("/api/v1/config/system-info")
            assert resp.status_code == 200, f"System info failed: {resp.text}"
            data = resp.json()
            assert "version" in data
            assert "device_count" in data

class TestSupplyChain:
    @pytest.mark.asyncio
    async def test_supply_chain_analysis(self, monkeypatch, async_client):
        request_data = {
            "origin": "Rewa, Madhya Pradesh",
            "destination": "Bhopal, Madhya Pradesh",
            "produce_type": "Lettuce",
            "weight_kg": 50,
            "transport_mode": "railway"
        }
        async def dummy_fetch_and_average_value(query: str) -> float:
            q = query.lower()
            if "distance" in q:
                return 350.0
            if "cost" in q:
                return 1.0
            if "travel" in q:
                return 6.0
            if "perish" in q:
                return 24.0
            if "market price" in q:
                return 2.5
            return 0.0
        monkeypatch.setattr("app.services.supply_chain_service.fetch_and_average_value", dummy_fetch_and_average_value)
        async def dummy_call_llm(prompt: str, model_name: str = None) -> dict:
            return {
                "final_recommendation": "Use refrigerated rail transport",
                "reasoning": "Cost-effective and quick."
            }
        monkeypatch.setattr("app.services.supply_chain_service.call_llm", dummy_call_llm)
        async with LifespanManager(app):
            resp = await async_client.post("/api/v1/supply_chain", json=request_data)
            assert resp.status_code == 200, f"Supply chain analysis failed: {resp.text}"
            data = resp.json()
            assert data["origin"] == "Rewa, Madhya Pradesh"
            assert "final_recommendation" in data

class TestCloud:
    @pytest.mark.asyncio
    async def test_authenticate_cloud(self, async_client):
        auth_request = {"device_id": "dummy_device", "cloud_key": "my_cloud_secret"}
        async with LifespanManager(app):
            resp = await async_client.post("/api/v1/authenticate", json=auth_request)
            assert resp.status_code == 200
            data = resp.json()
            assert "token" in data and "message" in data

    @pytest.mark.asyncio
    async def test_dosing_cancel_cloud(self, async_client):
        cancel_request = {"device_id": "dummy_device", "event": "dosing_cancelled"}
        async with LifespanManager(app):
            resp = await async_client.post("/api/v1/dosing_cancel", json=cancel_request)
            assert resp.status_code == 200
            data = resp.json()
            assert data["message"] == "Dosing cancellation received"

    

class TestAdminAndHeartbeat:
    @pytest.mark.asyncio
    async def test_admin_devices(self, async_client):
        async with LifespanManager(app):
            resp = await async_client.get("/admin/devices")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_heartbeat(self, async_client):
        payload = {"device_id": "dummy_device"}
        async with LifespanManager(app):
            resp = await async_client.post("/heartbeat", json=payload)
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
