# tests/conftest.py

import os
import logging
import pytest
import pytest_asyncio
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient
from datetime import datetime, UTC
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import AsyncSessionLocal, init_db
# Set test environment before imports
os.environ["TESTING"] = "1"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test.db"
from app.main import app
from app.core.database import Base, get_db
from app.services.mqtt import MQTTPublisher

logger = logging.getLogger(__name__)

# Test database setup
TEST_DB_URL = "sqlite+aiosqlite:///./test.db"
test_engine = create_async_engine(
    TEST_DB_URL,
    echo=False,
    future=True
)
@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    await init_db()
    yield

TestingSessionLocal = sessionmaker(
    test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)

@pytest_asyncio.fixture(scope="session")
async def setup_test_db():
    """Initialize test database"""
    try:
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        yield
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    except Exception as e:
        logger.error(f"Error setting up test database: {e}")
        raise
@pytest_asyncio.fixture
async def test_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.rollback()
        finally:
            await session.close()

class MockMQTTPublisher:
    def __init__(self):
        self.connected = True
        self.published_messages = []
        self.subscribed_topics = {}
        self.client = self

    def publish(self, topic: str, payload: dict, qos: int = 1):
        if isinstance(payload, dict) and 'timestamp' not in payload:
            payload['timestamp'] = datetime.now(UTC).isoformat()
        
        self.published_messages.append({
            "topic": topic,
            "payload": payload,
            "qos": qos
        })
        return [0, 1]

    def subscribe(self, topic: str, callback=None):
        self.subscribed_topics[topic] = callback
        return [0, 1]

    def cleanup(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

@pytest.fixture
def mqtt_mock():
    return MockMQTTPublisher()

@pytest.fixture
def override_get_db(test_session: AsyncSession):
    """Override get_db dependency"""
    async def _override_get_db():
        try:
            yield test_session
        finally:
            pass  # Session cleanup is handled by test_session fixture
    return _override_get_db

@pytest.fixture
def client(override_get_db, mqtt_mock: MockMQTTPublisher) -> TestClient:
    """Create test client with mocked dependencies"""
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[MQTTPublisher] = lambda: mqtt_mock

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()

# Test data fixtures
@pytest.fixture
def test_dosing_device():
    return {
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
    
@pytest.fixture
def test_sensor_device():
    return {
        "name": "Test pH/TDS Sensor",
        "type": "ph_tds_sensor",
        "mqtt_topic": "krishiverse/devices/test_sensor",
        "location_description": "Test Location",
        "sensor_parameters": {
            "ph_calibration": "7.0",
            "tds_calibration": "500"
        }
    }

@pytest.fixture(autouse=True)
def setup_device_discovery(mqtt_mock):
    """Setup device discovery service with mock MQTT client"""
    from app.services.device_discovery import DeviceDiscoveryService
    DeviceDiscoveryService.initialize(mqtt_mock)
    yield
    DeviceDiscoveryService._instance = None
    DeviceDiscoveryService._mqtt_client = None
    
@pytest.fixture
async def test_session():
    async with AsyncSessionLocal() as session:
        yield session
        await session.rollback()
        
@pytest.fixture
def test_dosing_profile(test_dosing_device):
    return {
        "device_id": None,  # Will be set during test
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