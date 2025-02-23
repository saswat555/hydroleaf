import os
import logging
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from app.core.database import init_db, Base, get_db
from app.main import app
from datetime import datetime, timezone

# Set test environment variables BEFORE app imports occur
os.environ["TESTING"] = "1"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test.db"

TEST_DB_URL = os.environ["DATABASE_URL"]
test_engine = create_async_engine(
    TEST_DB_URL,
    echo=False,
    future=True
)

TestingSessionLocal = sessionmaker(
    test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)

# Ensure that the test database is initialized (run once per session)
@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_test_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

# Provide a test session fixture
@pytest_asyncio.fixture
async def test_session() -> AsyncSession:
    async with TestingSessionLocal() as session:
        yield session
        await session.rollback()

# Override the get_db dependency so that our app uses the test database.
@pytest_asyncio.fixture(autouse=True)
async def override_get_db_fixture(test_session: AsyncSession):
    async def _override_get_db():
        try:
            yield test_session
        finally:
            pass
    app.dependency_overrides[get_db] = _override_get_db
    yield
    app.dependency_overrides.clear()

# Test data fixtures
@pytest.fixture
def test_dosing_device():
    return {
        "name": "Test Dosing Unit",
        "type": "dosing_unit",
        "http_endpoint": f"krishiverse/devices/test_dosing_{int(datetime.now(timezone.utc).timestamp()*1000)}",
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
        "http_endpoint": f"krishiverse/devices/test_sensor_{int(datetime.now(timezone.utc).timestamp()*1000)}",
        "location_description": "Test Location",
        "sensor_parameters": {
            "ph_calibration": "7.0",
            "tds_calibration": "500"
        }
    }
