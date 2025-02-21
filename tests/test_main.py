# tests/test_main.py

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, UTC
from typing import Dict
from app.main import app
from app.services.device_discovery import DeviceDiscoveryService
from app.services.mqtt import MQTTPublisher
from httpx import AsyncClient

# Test data
TEST_DOSING_DEVICE = {
    "name": "Test Dosing Unit",
    "type": "dosing_unit",
    "mqtt_topic": "krishiverse/devices/test_dosing",
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
    "type": "ph_tds_sensor",
    "mqtt_topic": "krishiverse/devices/test_sensor",
    "location_description": "Test Location",
    "sensor_parameters": {
        "ph_calibration": "7.0",
        "tds_calibration": "500"
    }
}

@pytest.fixture
def test_dosing_device() -> Dict:
    return TEST_DOSING_DEVICE

@pytest.fixture
def test_sensor_device() -> Dict:
    return TEST_SENSOR_DEVICE

class TestDevices:
    @pytest.mark.asyncio
    async def test_create_dosing_device(
        self,
        client: TestClient,
        test_session: AsyncSession,
        test_dosing_device: Dict
    ):
        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.post("/api/v1/devices/dosing", json=test_dosing_device)
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == test_dosing_device["name"]

    async def test_create_sensor_device(
        self,
        client: TestClient,
        test_session: AsyncSession,
        test_sensor_device: Dict
    ):
        response = client.post("/api/v1/devices/sensor", json=test_sensor_device)
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == test_sensor_device["name"]
        assert data["type"] == "ph_tds_sensor"

    async def test_get_device_list(
        self,
        client: TestClient,
        test_session: AsyncSession,
        test_dosing_device: Dict,
        test_sensor_device: Dict
    ):
        # Create test devices
        client.post("/api/v1/devices/dosing", json=test_dosing_device)
        client.post("/api/v1/devices/sensor", json=test_sensor_device)
        
        response = client.get("/api/v1/devices")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 2

@pytest.mark.asyncio
class TestDosing:
    async def test_create_dosing_profile(
        self,
        client: TestClient,
        test_session: AsyncSession,
        test_dosing_device: Dict
    ):
        # First create a dosing device
        device_response = client.post("/api/v1/devices/dosing", json=test_dosing_device)
        assert device_response.status_code == 200
        device_id = device_response.json()["id"]

        # Create a dosing profile
        test_profile = {
            "device_id": device_id,
            "plant_name": "Test Tomato",
            "plant_type": "Vegetable",
            "growth_stage": "vegetative",
            "seeding_date": datetime.now(UTC).isoformat(),
            "target_ph_min": 5.5,
            "target_ph_max": 6.5,
            "target_tds_min": 800,
            "target_tds_max": 1200,
            "dosing_schedule": {
                "nutrient_a": {
                    "morning": 50.0,
                    "evening": 50.0
                },
                "nutrient_b": {
                    "morning": 25.0,
                    "evening": 25.0
                }
            }
        }

        response = client.post("/api/v1/dosing/profile", json=test_profile)
        assert response.status_code == 200
        
        profile_data = response.json()
        assert profile_data["device_id"] == device_id
        assert profile_data["plant_name"] == test_profile["plant_name"]
        assert profile_data["plant_type"] == test_profile["plant_type"]
        assert profile_data["growth_stage"] == test_profile["growth_stage"]
        assert profile_data["target_ph_min"] == test_profile["target_ph_min"]
        assert profile_data["target_ph_max"] == test_profile["target_ph_max"]
        assert profile_data["target_tds_min"] == test_profile["target_tds_min"]
        assert profile_data["target_tds_max"] == test_profile["target_tds_max"]
        assert "created_at" in profile_data
        assert "updated_at" in profile_data

        # Verify profile was saved in database
        result = await test_session.execute(
            select(models.DosingProfile).where(
                models.DosingProfile.id == profile_data["id"]
            )
        )
        saved_profile = result.scalar_one_or_none()
        assert saved_profile is not None
        assert saved_profile.device_id == device_id

        # Test creating profile for non-existent device
        invalid_profile = test_profile.copy()
        invalid_profile["device_id"] = 9999
        response = client.post("/api/v1/dosing/profile", json=invalid_profile)
        assert response.status_code == 404
        assert "Device not found" in response.json()["detail"]

        # Test creating profile with invalid data
        invalid_profile = test_profile.copy()
        invalid_profile["target_ph_min"] = 15  # pH can't be > 14
        response = client.post("/api/v1/dosing/profile", json=invalid_profile)
        assert response.status_code == 422  # Validation error

        # Test getting profiles for a device
        response = client.get(f"/api/v1/dosing/profiles/{device_id}")
        assert response.status_code == 200
        profiles = response.json()
        assert len(profiles) == 1
        assert profiles[0]["id"] == profile_data["id"]

        # Test deleting profile
        response = client.delete(f"/api/v1/dosing/profiles/{profile_data['id']}")
        assert response.status_code == 200
        assert response.json()["message"] == "Profile deleted successfully"

        # Verify profile was deleted
        result = await test_session.execute(
            select(models.DosingProfile).where(
                models.DosingProfile.id == profile_data["id"]
            )
        )
        deleted_profile = result.scalar_one_or_none()
        assert deleted_profile is None

    @pytest.mark.asyncio
    async def test_get_dosing_history(
        self,
        client: TestClient,
        test_session: AsyncSession,
        test_dosing_device: Dict
    ):
        async with AsyncClient(app=app, base_url="http://test") as ac:
            # Create device first
            device_response = await ac.post("/api/v1/devices/dosing", json=test_dosing_device)
            assert device_response.status_code == 200
            device_id = device_response.json()["id"]
    
            # Create test operation
            operation = models.DosingOperation(
                device_id=device_id,
                operation_id=f"test_op_{int(datetime.now(UTC).timestamp())}",
                actions=[{
                    "pump_number": 1,
                    "dose_ml": 50.0,
                    "chemical_name": "Nutrient A"
                }],
                status="completed",
                timestamp=datetime.now(UTC)
            )
            test_session.add(operation)
            await test_session.commit()
    
            # Get history
            response = await ac.get(f"/api/v1/dosing/history/{device_id}")
            assert response.status_code == 200
            history = response.json()
            assert len(history) == 1
            assert history[0]["device_id"] == device_id

    async def test_get_dosing_history_empty(self, client, test_session, test_dosing_device):
        # Test getting history for device with no operations
        device_response = client.post("/api/v1/devices/dosing", json=test_dosing_device)
        device_id = device_response.json()["id"]

        response = client.get(f"/api/v1/dosing/history/{device_id}")
        assert response.status_code == 200
        assert response.json() == []

    async def test_get_dosing_history_invalid_device(self, client):
        # Test getting history for non-existent device
        response = client.get("/api/v1/dosing/history/999")
        assert response.status_code == 404
        assert "Device not found" in response.json()["detail"]

    async def test_execute_dosing(self, client, test_session, test_dosing_device):
        # Create device
        device_response = client.post("/api/v1/devices/dosing", json=test_dosing_device)
        device_id = device_response.json()["id"]

        # Execute dosing
        response = client.post(f"/api/v1/dosing/execute/{device_id}")
        assert response.status_code == 200
        result = response.json()
        assert result["device_id"] == device_id
        assert "operation_id" in result
        assert result["status"] == "completed"

    async def test_cancel_dosing(self, client, test_session, test_dosing_device):
        # Create device
        device_response = client.post("/api/v1/devices/dosing", json=test_dosing_device)
        device_id = device_response.json()["id"]

        # Start dosing operation
        client.post(f"/api/v1/dosing/execute/{device_id}")

        # Cancel dosing
        response = client.post(f"/api/v1/dosing/cancel/{device_id}")
        assert response.status_code == 200
        assert response.json()["message"] == "Dosing operation cancelled"
    

@pytest.mark.asyncio
class TestSystemConfig:
    async def test_device_discovery(
        self,
        client: TestClient,
        mqtt_mock: MQTTPublisher
    ):
        # Initialize device discovery service with mock
        from app.services.device_discovery import get_device_discovery_service
        
        # Override the dependency
        app.dependency_overrides[get_device_discovery_service] = lambda: DeviceDiscoveryService.initialize(mqtt_mock)
        
        response = client.get("/api/v1/devices/discover")
        assert response.status_code == 200
        data = response.json()
        assert "devices" in data
        
        # Verify MQTT message was published
        assert len(mqtt_mock.published_messages) > 0
        published = mqtt_mock.published_messages[0]
        assert published["topic"] == "krishiverse/discovery"

        # Cleanup
        app.dependency_overrides.clear()
        DeviceDiscoveryService._instance = None
        DeviceDiscoveryService._mqtt_client = None