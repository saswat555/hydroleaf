# tests/conftest.py
"""
Pytest fixtures for Hydroleaf

▪︎ Spins‑up a dedicated Postgres DB (TEST_DATABASE_URL) for the whole test run
▪︎ Overrides FastAPI’s DB dependency to use that same Session
▪︎ Truncates every table after each test
▪︎ Provides an httpx.AsyncClient + a deterministic DeviceController mock
▪︎ Logs the result of each test to test_logs.txt, including detailed failure reasons
"""

import os
import sys
import datetime as _dt
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# ───────────────────────── paths / env ──────────────────────────
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Force TESTING early
os.environ["TESTING"] = "1"
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:1234@localhost:5432/hydroleaf",
)

# ───────────────────────── database setup ───────────────────────
from app.core.database import Base, AsyncSessionLocal  # the real one
from app.main import app
from app.core.database import get_db

# make a test‐only engine + sessionmaker
_test_engine = create_async_engine(TEST_DB_URL, echo=False, future=True)
TestSessionLocal = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)

# __CRITICAL__: point the app’s AsyncSessionLocal at our TestSessionLocal
import app.core.database as _db_mod
_db_mod.AsyncSessionLocal = TestSessionLocal

# also override the get_db dependency
@pytest.fixture(scope="session", autouse=True)
async def _setup_db_and_overrides():
    # create all tables once
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def _override_get_db() -> AsyncSession:
        async with TestSessionLocal() as session:
            try:
                yield session
                await session.commit()
            except:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db

    # make admin routes always succeed
    try:
        from app.routers.admin_subscriptions import get_current_admin
        app.dependency_overrides[get_current_admin] = lambda: True
    except ImportError:
        pass

    yield
    # teardown handled by table‐truncation fixture below

# sync engine for truncation
_SYNC_DB_URL = TEST_DB_URL.replace(
    "postgresql+asyncpg://", "postgresql+psycopg2://"
)
_sync_engine = create_engine(_SYNC_DB_URL, future=True)

@pytest.fixture(autouse=True)
def _truncate_tables_after_each_test():
    yield
    # wipe everything
    with _sync_engine.begin() as conn:
        conn.execute(text("SET session_replication_role = replica;"))
        for tbl in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f'TRUNCATE TABLE "{tbl.name}" RESTART IDENTITY CASCADE'))
        conn.execute(text("SET session_replication_role = DEFAULT;"))

# ─────────────────── Device‑controller mock ────────────────────
class MockController:
    def __init__(self, device_ip: str, request_timeout: float = 10.0):
        self.device_ip = device_ip

    async def discover(self):
        suffix_map = {
            "dosing":    {"device_id": "dev-dosing", "name": "Mock Dosing", "type": "dosing_unit"},
            "sensor":    {"device_id": "dev-sensor", "name": "Mock Sensor", "type": "ph_tds_sensor"},
            "valve":     {"device_id": "dev-valve",  "name": "Mock Valve",  "type": "valve_controller"},
            "switch":    {"device_id": "dev-switch", "name": "Mock Switch", "type": "smart_switch"},
        }
        for suf, payload in suffix_map.items():
            if self.device_ip.endswith(suf):
                return {**payload, "ip": self.device_ip}
        return None

@pytest.fixture(autouse=True)
def _patch_device_controller(monkeypatch, request):
    # only for endpoints that use async_client
    if "async_client" in request.fixturenames:
        import app.services.device_controller as dc_mod
        monkeypatch.setattr(dc_mod, "DeviceController", MockController)

# ─────────────────── camera data root helper ────────────────────
@pytest.fixture(autouse=True)
def _temp_cam_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CAM_DATA_ROOT", str(tmp_path))
    from importlib import reload
    reload(__import__("app.core.config"))
    yield

# ───────────────────────── httpx AsyncClient ─────────────────────────
@pytest.fixture
async def async_client():
    async with AsyncClient(app=app, base_url="http://testserver") as client:
        yield client

# ─────────────────────── result‑logging plugin ────────────────────────
_LOG_PATH = ROOT / "test_logs.txt"

def pytest_sessionstart(session):
    with _LOG_PATH.open("w", encoding="utf-8") as fp:
        fp.write(f"Test run started: {_dt.datetime.utcnow().isoformat()}Z\n")
        fp.write("=" * 70 + "\n")

def pytest_runtest_logreport(report):
    if report.when != "call":
        return

    outcome = "PASSED" if report.passed else "FAILED" if report.failed else "SKIPPED"
    ts = _dt.datetime.utcnow().isoformat() + "Z"
    with _LOG_PATH.open("a", encoding="utf-8") as fp:
        fp.write(f"{ts} | {report.nodeid} | {outcome} | {report.duration:.2f}s\n")
        if report.failed:
            fp.write("--- Failure details below ---\n")
            longrepr = getattr(report, "longreprtext", None) or str(report.longrepr)
            fp.write(longrepr)
            if not longrepr.endswith("\n"):
                fp.write("\n")
            fp.write("-" * 70 + "\n")
