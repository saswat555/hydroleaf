# tests/test_main.py

import httpx
import pytest
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone
from sqlalchemy import select
from app.main import app
from app import models
from app.services.llm import build_plan_prompt, call_llm_plan, dosing_manager, process_sensor_plan
from app.schemas import DeviceType
from app.services.device_discovery import DeviceDiscoveryService
from app.services.serper import fetch_search_results

PUMP_IP = "192.168.54.198" 
# Updated test data: now using "http_endpoint" 
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
    # Append a unique suffix to avoid UNIQUE constraint errors.
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

    @pytest.mark.asyncio
    async def test_check_device_not_found(self):
        """
        Test the new discovery endpoint when no device is found at the provided IP.
        """
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get("/api/v1/devices/discover", params={"ip": "192.0.2.1"})
        # Expect a 404 if no device is responding at that IP.
        assert response.status_code == 404
        assert "No device found" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_check_device_found(self, monkeypatch):
        """
        Test the new discovery endpoint when a device is found.
        We simulate a successful device response by monkeypatching _get_device_info.
        """
        async def dummy_get_device_info(self, client, ip):
            return {"ip": ip, "device_id": "dummy_device", "status": "online"}
        monkeypatch.setattr(DeviceDiscoveryService, "_get_device_info", dummy_get_device_info)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get("/api/v1/devices/discover", params={"ip": "192.168.54.198"})
        assert response.status_code == 200
        data = response.json()
        assert data["ip"] == "192.168.54.198"
        assert data["status"] == "online"


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
            # Expect at least one profile (the one we just created).
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
            exec_resp = await ac.post(f"/api/v1/dosing/execute/{device_id}", json={})
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
            await ac.post(f"/api/v1/dosing/execute/{device_id}", json={})
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
        unique_endpoint = f"krishiverse/devices/test_llm_{int(datetime.now(timezone.utc).timestamp()*1000)}"
        dosing_device["http_endpoint"] = unique_endpoint
        dosing_device["pump_configurations"][0]["dose_ml"] = 50.0

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            device_resp = await ac.post("/api/v1/devices/dosing", json=dosing_device)
            assert device_resp.status_code == 200, f"Device creation failed: {device_resp.text}"
            device_data = device_resp.json()
            device_id = device_data["id"]

            # Register the device in the dosing manager (now including the http_endpoint).
            dosing_manager.register_device(
                device_id,
                {f"pump{idx+1}": config for idx, config in enumerate(device_data["pump_configurations"])},
                device_data["http_endpoint"]
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
            print("LLM dosing plan:", result)





    @pytest.mark.asyncio
    async def test_llm_dosing_with_real_pumps(self, test_dosing_device_fixture: dict):
        """Test full dosing process with LLM feedback and real pump activation."""

        dosing_manager.devices.clear()

        # Prepare a dosing device with dose_ml included.
        dosing_device = test_dosing_device_fixture.copy()
        unique_endpoint = f"krishiverse/devices/test_llm_{int(datetime.now(timezone.utc).timestamp()*1000)}"
        dosing_device["http_endpoint"] = unique_endpoint
        dosing_device["pump_configurations"][0]["dose_ml"] = 10

        # Step 1: Define sensor data and plant profile
        sensor_data = {"ph": 6.8, "tds": 450}
        plant_profile = {
            "plant_name": "Cucumber",
            "plant_type": "Vegetable",
            "growth_stage": 30,
            "seeding_date": 10,
            "weather_locale": "Local"
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Step 2: Register the dosing device
            device_resp = await ac.post("/api/v1/devices/dosing", json=dosing_device)
            assert device_resp.status_code == 200, f"Device creation failed: {device_resp.text}"
            
            device_data = device_resp.json()
            device_id = device_data["id"]

            # Manually register device in dosing manager to ensure LLM request succeeds
            dosing_manager.register_device(
                device_id,
                {f"pump{idx+1}": config for idx, config in enumerate(device_data["pump_configurations"])},
                device_data["http_endpoint"]
            )

            # Step 3: Send request to LLM via correct endpoint
            llm_resp = await ac.post(
                f"/api/v1/dosing/llm-request?device_id={device_id}",
                json={"sensor_data": sensor_data, "plant_profile": plant_profile}
            )

            assert llm_resp.status_code == 200, f"LLM request failed: {llm_resp.text}"
            result = llm_resp.json()

            # Step 4: Ensure LLM response contains actions
            assert "actions" in result, "Dosing plan missing 'actions' key"
            assert isinstance(result["actions"], list), "'actions' should be a list"
            assert "next_check_hours" in result, "Dosing plan missing 'next_check_hours'"

            # Step 5: Activate pumps based on LLM response using a REAL HTTP client
            async with httpx.AsyncClient() as real_client:
                for action in result["actions"]:
                    pump_number = action["pump_number"]
                    amount = action["dose_ml"]

                    # Debugging log for pump request
                    print(f"Sending request to pump: Pump {pump_number}, Amount: {amount}")

                    pump_response = await real_client.post(
                        "http://192.168.3.198/pump",
                        json={"pump": pump_number, "amount": amount}
                    )

                    # Debugging log for pump response
                    print(f"Pump API Response: {pump_response.status_code} - {pump_response.text}")

                    assert pump_response.status_code == 200, f"Pump {pump_number} activation failed: {pump_response.text}"
                    pump_data = pump_response.json()
                    assert pump_data.get("message") == "Pump started", f"Unexpected response: {pump_data}"

        print("✅ LLM dosing flow completed successfully:", result)



@pytest.mark.asyncio
async def test_build_plan_prompt():

    
    
    plant_profile = {
        "plant_name": "Strawberry",
        "plant_type": "Fruit",
        "growth_stage": 30,
        "seeding_date": "2024-01-01",
        "weather_locale": "Rajasthan"
    }

    sensor_data = {
        "pH" : 6.7690,
        "TDS": 300.02
    }

    
    query = "Tell me optimal conditions for growing the given plant."

    # search_query = await fetch_search_results(query)
   
    result = await process_sensor_plan(plant_profile, sensor_data, query)

    planPrompt = await call_llm_plan(result)

    # print("\n Generate Search result: \n", search_query)
    print("\nGenerated Prompt:\n", planPrompt)
   
    assert "Plant: Strawberry" in result
    assert "Plant Type: Fruit" in result
    assert "Growth Stage: 30 days from seeding" in result
    assert "Location: Rajasthan" in result
    assert "Additional Info" in result
