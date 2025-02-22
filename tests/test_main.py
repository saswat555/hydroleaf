# tests/test_main.py

import pytest
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone
from sqlalchemy import select
from app.main import app
from app import models
from app.services.llm import dosing_manager
from app.schemas import DeviceType

# Test data – note that we will update the mqtt_topic to be unique in the fixtures
TEST_DOSING_DEVICE = {
    "name": "Test Dosing Unit",
    "type": DeviceType.DOSING_UNIT,
    "mqtt_topic": "krishiverse/devices/test_dosing",  # unique suffix will be appended
    "location_description": "Test Location",
    "pump_configurations": [
        {
            "pump_number": 1,
            "chemical_name": "Nutrient A",
            "chemical_description": "Primary nutrients"
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
    "mqtt_topic": "krishiverse/devices/test_sensor",  # unique suffix will be appended
    "location_description": "Test Location",
    "sensor_parameters": {
        "ph_calibration": "7.0",
        "tds_calibration": "500"
    }
}


@pytest.fixture
def test_dosing_device_fixture() -> dict:
    # Append a unique suffix to avoid UNIQUE constraint errors.
    unique_topic = f"krishiverse/devices/test_dosing_{int(datetime.now(timezone.utc).timestamp()*1000)}"
    device = TEST_DOSING_DEVICE.copy()
    device["mqtt_topic"] = unique_topic
    return device


@pytest.fixture
def test_sensor_device_fixture() -> dict:
    unique_topic = f"krishiverse/devices/test_sensor_{int(datetime.now(timezone.utc).timestamp()*1000)}"
    device = TEST_SENSOR_DEVICE.copy()
    device["mqtt_topic"] = unique_topic
    return device


class TestDevices:
    @pytest.mark.asyncio
    async def test_create_dosing_device(self, test_dosing_device_fixture: dict):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
        assert response.status_code == 200, f"Response: {response.text}"
        data = response.json()
        assert data["name"] == test_dosing_device_fixture["name"]

    @pytest.mark.asyncio
    async def test_create_sensor_device(self, test_sensor_device_fixture: dict):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.post("/api/v1/devices/sensor", json=test_sensor_device_fixture)
        assert response.status_code == 200, f"Response: {response.text}"
        data = response.json()
        assert data["name"] == test_sensor_device_fixture["name"]
        assert data["type"] == DeviceType.PH_TDS_SENSOR

    @pytest.mark.asyncio
    async def test_get_device_list(self, test_dosing_device_fixture: dict, test_sensor_device_fixture: dict):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Create both device types.
            await ac.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            await ac.post("/api/v1/devices/sensor", json=test_sensor_device_fixture)
            response = await ac.get("/api/v1/devices")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 2


class TestDosing:
    @pytest.mark.asyncio
    async def test_create_dosing_profile(self, test_dosing_device_fixture: dict, test_session):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Create dosing device
            device_resp = await ac.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            assert device_resp.status_code == 200, f"Device creation failed: {device_resp.text}"
            device_id = device_resp.json()["id"]

            # Use a flat dosing_schedule as per our working curl script.
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
            assert saved_profile.device_id == device_id

            # Test creating profile for non-existent device.
            invalid_profile = test_profile.copy()
            invalid_profile["device_id"] = 9999
            invalid_resp = await ac.post("/api/v1/config/dosing-profile", json=invalid_profile)
            assert invalid_resp.status_code == 404
            assert "Device not found" in invalid_resp.json()["detail"]

            # Test creating profile with invalid data.
            invalid_profile = test_profile.copy()
            invalid_profile["target_ph_min"] = 15  # invalid pH
            invalid_resp = await ac.post("/api/v1/config/dosing-profile", json=invalid_profile)
            assert invalid_resp.status_code == 422

            # Get profiles using the correct endpoint.
            profiles_resp = await ac.get(f"/api/v1/config/dosing-profiles/{device_id}")
            # Depending on your logic, if no profile exists, a 404 may be returned.
            # Here we expect at least one profile (the one we just created).
            assert profiles_resp.status_code == 200, f"Profiles retrieval failed: {profiles_resp.text}"
            profiles = profiles_resp.json()
            assert isinstance(profiles, list)
            assert len(profiles) >= 1
            assert profiles[0]["id"] == profile_data["id"]

            # Delete the profile.
            delete_resp = await ac.delete(f"/api/v1/config/dosing-profiles/{profile_data['id']}")
            assert delete_resp.status_code == 200
            assert delete_resp.json()["message"] == "Profile deleted successfully"

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
            assert history_resp.status_code == 200, f"History retrieval failed: {history_resp.text}"
            history = history_resp.json()
            assert isinstance(history, list)
            assert len(history) >= 1
            assert history[0]["device_id"] == device_id

    @pytest.mark.asyncio
    async def test_get_dosing_history_empty(self, test_dosing_device_fixture: dict):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_resp = await ac.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            device_data = device_resp.json()
            device_id = device_data.get("id")
            assert device_id is not None, f"Device creation failed: {device_resp.text}"
            history_resp = await ac.get(f"/api/v1/dosing/history/{device_id}")
            assert history_resp.status_code == 200, f"Empty history retrieval failed: {history_resp.text}"
            assert history_resp.json() == []

    @pytest.mark.asyncio
    async def test_get_dosing_history_invalid_device(self):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get("/api/v1/dosing/history/999")
            assert response.status_code == 404
            assert "Device not found" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_execute_dosing(self, test_dosing_device_fixture: dict):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_resp = await ac.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            device_data = device_resp.json()
            device_id = device_data.get("id")
            assert device_id is not None, f"Device creation failed: {device_resp.text}"
            # Executing dosing on a device without dose_ml (if not provided) should fail.
            exec_resp = await ac.post(f"/api/v1/dosing/execute/{device_id}")
            assert exec_resp.status_code == 500
            assert "Dosing action must include pump number and dose amount" in exec_resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_cancel_dosing(self, test_dosing_device_fixture: dict):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_resp = await ac.post("/api/v1/devices/dosing", json=test_dosing_device_fixture)
            device_data = device_resp.json()
            device_id = device_data.get("id")
            assert device_id is not None, f"Device creation failed: {device_resp.text}"
            # Start dosing operation (even if execution fails, cancellation should work).
            await ac.post(f"/api/v1/dosing/execute/{device_id}")
            cancel_resp = await ac.post(f"/api/v1/dosing/cancel/{device_id}")
            assert cancel_resp.status_code == 200
            assert cancel_resp.json()["message"] == "Dosing operation cancelled"

    @pytest.mark.asyncio
    async def test_llm_dosing_flow_actual(self, test_dosing_device_fixture: dict):
        """
        Test the full LLM-based dosing flow with an actual LLM call.
        Note: This test requires your LLM service (Ollama) to be available.
        """
        # Clear any previous device registrations.
        dosing_manager.devices.clear()

        # Prepare a dosing device with dose_ml included.
        dosing_device = test_dosing_device_fixture.copy()
        unique_topic = f"krishiverse/devices/test_llm_{int(datetime.now(timezone.utc).timestamp()*1000)}"
        dosing_device["mqtt_topic"] = unique_topic
        dosing_device["pump_configurations"][0]["dose_ml"] = 50.0

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_resp = await ac.post("/api/v1/devices/dosing", json=dosing_device)
            assert device_resp.status_code == 200, f"Device creation failed: {device_resp.text}"
            device_data = device_resp.json()
            device_id = device_data["id"]

            # Register the device in the dosing manager.
            dosing_manager.register_device(
                device_id,
                {f"pump{idx+1}": config for idx, config in enumerate(device_data["pump_configurations"])}
            )

            # Prepare sensor data and plant profile.
            sensor_data = {"ph": 6.8, "tds": 450}
            plant_profile = {
                "plant_name": "Cucumber",
                "plant_type": "Vegetable",
                "current_age": 30,
                "seeding_age": 10,
                "weather_locale": "Local"
            }

            llm_resp = await ac.post(
                f"/api/v1/dosing/llm-request?device_id={device_id}",
                json={"sensor_data": sensor_data, "plant_profile": plant_profile}
            )
            assert llm_resp.status_code == 200, f"LLM dosing flow failed: {llm_resp.text}"
            result = llm_resp.json()
            assert "actions" in result, "Dosing plan missing 'actions' key"
            assert isinstance(result["actions"], list), "'actions' should be a list"
            assert "next_check_hours" in result, "Dosing plan missing 'next_check_hours'"
            # Print the LLM dosing plan for manual verification.
            print("LLM dosing plan:", result)

