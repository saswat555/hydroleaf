# tests/test_main.py

import httpx
import pytest
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone
from sqlalchemy import select
from app.main import app
from app import models
from app.schemas import DeviceType, SimpleDosingCommand
from app.services.device_controller import DeviceController
from unittest.mock import patch

# Fixed JSON to simulate a successful discovery response.
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
    "http_endpoint": "krishiverse/devices/test_dosing",  # unique suffix will be appended
    "location_description": "Test Location",
    "pump_configurations": [
        {
            "pump_number": 1,
            "chemical_name": "Nutrient A",
            "chemical_description": "8 macro"
        },
        {
            "pump_number": 2,
            "chemical_name": "Nutrient B",
            "chemical_description": "Secondary nutrients"
        }
    ]
}

TEST_SENSOR_DEVICE = {
    "name": "Test pH/TDS Sensor",
    "type": DeviceType.PH_TDS_SENSOR,
    "http_endpoint": "krishiverse/devices/test_sensor",  # unique suffix will be appended
    "location_description": "Test Location",
    "sensor_parameters": {
        "ph_calibration": "7.0",
        "tds_calibration": "500"
    }
}

@pytest.fixture
def test_dosing_device_fixture() -> dict:
    unique_endpoint = f"krishiverse/devices/test_dosing_{int(datetime.now(timezone.utc).timestamp()*1000)}"
    device = TEST_DOSING_DEVICE.copy()
    device["http_endpoint"] = unique_endpoint
    return device

@pytest.fixture
def test_sensor_device_fixture() -> dict:
    unique_endpoint = f"krishiverse/devices/test_sensor_{int(datetime.now(timezone.utc).timestamp()*1000)}"
    device = TEST_SENSOR_DEVICE.copy()
    device["http_endpoint"] = unique_endpoint
    return device

class TestDevices:
    @pytest.mark.asyncio
    async def test_create_dosing_device(self, test_dosing_device_fixture: dict):
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
            # Create both device types.
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
        # Monkey-patch the discover method to return our fixed JSON.
        async def dummy_discover(self):
            return FIXED_DISCOVERY_RESPONSE
        monkeypatch.setattr(DeviceController, "discover", dummy_discover)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/v1/devices/discover", params={"ip": "192.168.54.198"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ip"] == "192.168.54.198"
        assert data["status"] == "online"

class TestDosing:
    @pytest.mark.asyncio
    async def test_create_dosing_profile(self, test_dosing_device_fixture: dict, test_session):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Create a dosing device first.
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

            result = await test_session.execute(
                select(models.DosingProfile).where(models.DosingProfile.id == profile_data["id"])
            )
            saved_profile = result.scalar_one_or_none()
            assert saved_profile is not None

            invalid_profile = test_profile.copy()
            invalid_profile["device_id"] = 9999
            invalid_resp = await ac.post("/api/v1/config/dosing-profile", json=invalid_profile)
            assert invalid_resp.status_code == 404

            invalid_profile = test_profile.copy()
            invalid_profile["target_ph_min"] = 15  # invalid pH value
            invalid_resp = await ac.post("/api/v1/config/dosing-profile", json=invalid_profile)
            assert invalid_resp.status_code == 422

            profiles_resp = await ac.get(f"/api/v1/config/dosing-profiles/{device_id}")
            assert profiles_resp.status_code == 200
            profiles = profiles_resp.json()
            assert isinstance(profiles, list)
            assert len(profiles) >= 1

            delete_resp = await ac.delete(f"/api/v1/config/dosing-profiles/{profile_data['id']}")
            assert delete_resp.status_code == 200
            result = await test_session.execute(
                select(models.DosingProfile).where(models.DosingProfile.id == profile_data["id"])
            )
            deleted_profile = result.scalar_one_or_none()
            assert deleted_profile is None

    @pytest.mark.asyncio
    async def test_get_dosing_history(self, test_dosing_device_fixture: dict, test_session):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_resp = await ac.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            assert device_resp.status_code == 200, f"Device creation failed: {device_resp.text}"
            device_id = device_resp.json()["id"]

            # Manually insert a dosing operation.
            operation = models.DosingOperation(
                device_id=device_id,
                operation_id=f"test_op_{int(datetime.now(timezone.utc).timestamp())}",
                actions=[{
                    "pump_number": 1,
                    "dose_ml": 50.0,
                    "chemical_name": "Nutrient A",
                    "reasoning": "Test reason"
                }],
                status="completed",
                timestamp=datetime.now(timezone.utc)
            )
            test_session.add(operation)
            await test_session.commit()

            history_resp = await ac.get(f"/api/v1/dosing/history/{device_id}")
            assert history_resp.status_code == 200
            history = history_resp.json()
            assert isinstance(history, list)
            assert len(history) >= 1

    @pytest.mark.asyncio
    async def test_get_dosing_history_empty(self, test_dosing_device_fixture: dict):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_resp = await ac.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            device_data = device_resp.json()
            device_id = device_data.get("id")
            assert device_id is not None
            history_resp = await ac.get(f"/api/v1/dosing/history/{device_id}")
            assert history_resp.status_code == 200
            assert history_resp.json() == []

    @pytest.mark.asyncio
    async def test_get_dosing_history_invalid_device(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/v1/dosing/history/999")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_execute_dosing(self, test_dosing_device_fixture: dict, monkeypatch):
        # Monkey-patch execute_dosing_operation to return a fixed response.
        async def dummy_execute_dosing_operation(device_id, http_endpoint, dosing_action):
            if "pump" not in dosing_action or "amount" not in dosing_action:
                raise ValueError("Dosing action must include pump number and dose amount")
            return {"message": "Pump started", "pump": dosing_action["pump"], "amount": dosing_action["amount"]}
        monkeypatch.setattr("app.services.dose_manager.execute_dosing_operation", dummy_execute_dosing_operation)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_resp = await ac.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            device_data = device_resp.json()
            device_id = device_data.get("id")
            assert device_id is not None
            exec_resp = await ac.post(f"/api/v1/dosing/execute/{device_id}", json={"pump": 1, "amount": 30})
            assert exec_resp.status_code == 200, f"Execution failed: {exec_resp.text}"
            data = exec_resp.json()
            assert "message" in data and data["message"] == "Pump started"

    @pytest.mark.asyncio
    async def test_cancel_dosing(self, test_dosing_device_fixture: dict):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_resp = await ac.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            device_data = device_resp.json()
            device_id = device_data.get("id")
            assert device_id is not None
            # Start dosing operation (simulate execution)
            await ac.post(f"/api/v1/dosing/execute/{device_id}", json={"pump": 1, "amount": 30})
            cancel_resp = await ac.post(f"/api/v1/dosing/cancel/{device_id}")
            assert cancel_resp.status_code == 200
            assert cancel_resp.json()["message"] == "Dosing operation cancelled"

    @pytest.mark.asyncio
    async def test_llm_dosing_request(self, test_dosing_device_fixture: dict):
        # This test will call the real LLM if available.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_resp = await ac.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            assert device_resp.status_code == 200, f"Device creation failed: {device_resp.text}"
            device_id = device_resp.json()["id"]
            payload = {
                "sensor_data": {"ph": 6.5, "tds": 500},
                "plant_profile": {
                    "plant_name": "Test Plant",
                    "plant_type": "Vegetable",
                    "growth_stage": "Seedling",
                    "seeding_date": datetime.now(timezone.utc).isoformat()
                }
            }
            resp = await ac.post(f"/api/v1/dosing/llm-request?device_id={device_id}", json=payload)
            # Expect a 200 response; further assertions depend on your actual LLM output.
            assert resp.status_code == 200

class TestPlants:
    @pytest.mark.asyncio
    async def test_create_and_fetch_plant(self, test_session):
        plant_data = {
            "name": "Test Plant",
            "type": "Vegetable",
            "growth_stage": "Seedling",
            "seeding_date": datetime.now(timezone.utc).isoformat(),
            "region": "Test Region",
            "location": "Test Location"
        }
        # Use trailing slash so that router prefix is correctly appended.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            create_resp = await ac.post("/api/v1/plants/", json=plant_data)
            assert create_resp.status_code == 200, f"Plant creation failed: {create_resp.text}"
            plant = create_resp.json()
            assert plant["name"] == plant_data["name"]

            fetch_resp = await ac.get(f"/api/v1/plants/{plant['id']}")
            assert fetch_resp.status_code == 200
            fetched_plant = fetch_resp.json()
            assert fetched_plant["name"] == plant_data["name"]

    @pytest.mark.asyncio
    async def test_execute_dosing_for_plant(self, test_session):
        # Create a plant with target parameters.
        plant_data = {
            "name": "Test Plant",
            "type": "Vegetable",
            "growth_stage": "Seedling",
            "seeding_date": datetime.now(timezone.utc).isoformat(),
            "region": "Test Region",
            "location": "Test Location",
            "target_ph_min": 5.5,
            "target_ph_max": 6.5,
            "target_tds_min": 600,
            "target_tds_max": 800
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            create_resp = await ac.post("/api/v1/plants/", json=plant_data)
            assert create_resp.status_code == 200, f"Plant creation failed: {create_resp.text}"
            plant = create_resp.json()
            # Insert dummy sensor readings for the plant's location.
            from app.models import SensorReading
            reading_ph = SensorReading(
                device_id=plant["id"],  # Using plant id for simulation.
                reading_type="ph",
                value=6.0,
                timestamp=datetime.now(timezone.utc)
            )
            reading_tds = SensorReading(
                device_id=plant["id"],
                reading_type="tds",
                value=700,
                timestamp=datetime.now(timezone.utc)
            )
            test_session.add(reading_ph)
            test_session.add(reading_tds)
            await test_session.commit()
            exec_resp = await ac.post(f"/api/v1/plants/execute-dosing/{plant['id']}")
            assert exec_resp.status_code == 200
            result = exec_resp.json()
            assert "actions" in result
