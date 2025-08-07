# tests/conftest.py
"""
Pytest fixtures for Hydroleaf:

- Loads .env and forces TESTING=1
- Spins up a dedicated Postgres DB (TEST_DATABASE_URL)
- Overrides FastAPI’s DB dependency to use that same session
- Truncates every table after each test
- Provides an httpx.AsyncClient + a deterministic DeviceController mock
- Logs the result of each test to test_logs.txt
"""

import os
import sys
import datetime as _dt
from pathlib import Path
from dotenv import load_dotenv
import tests.virtual_iot as _virtual_iot

# 1) Load .env from project root, then force TESTING
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)
os.environ["TESTING"] = "1"

# 2) Ensure project root is on sys.path
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 3) Now import FastAPI app, database, etc.
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
import jwt as _jwt
import uuid
# patch out JWT signature checks in tests
_orig_jwt_decode = _jwt.decode
def _decode_no_key(token, key=None, algorithms=None, options=None, **kwargs):
    return _orig_jwt_decode(token, key or "", algorithms=algorithms,
                            options={**(options or {}), "verify_signature": False}, **kwargs)
_jwt.decode = _decode_no_key

# import the config so that TESTING=True picks up TEST_DATABASE_URL
import app.core.config  # noqa: F401

# Grab the test‐DB URL
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:1234@localhost:5432/hydroleaf_test",
)

# Async engine + sessionmaker for tests (no pooling)
_test_engine = create_async_engine(
    TEST_DB_URL, echo=False, future=True, poolclass=NullPool
)
TestSessionLocal = async_sessionmaker(
    _test_engine, class_=AsyncSession, expire_on_commit=False
)

# Override the app’s AsyncSessionLocal
import app.core.database as _db_mod
_db_mod.AsyncSessionLocal = TestSessionLocal

# Import application and dependency
from app.main import app
from app.core.database import Base, get_db

def pytest_configure(config):
    """
    If pytest-cov is available, enable coverage of the 'app' package
    and show missing lines in the terminal report by default.
    """
    cov = config.pluginmanager.getplugin("cov")
    if cov:
        config.option.cov_source = ["app"]
        config.option.cov_report = ["term-missing"]
        
@pytest.fixture(scope="session", autouse=True)
async def _setup_db_and_overrides():
    # Create tables once
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Dependency override for get_db
    async def _override_get_db() -> AsyncSession:
        async with TestSessionLocal() as session:
            try:
                yield session
                await session.commit()
            except:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db

    # Make all admin routes succeed
    try:
        from app.routers.admin_subscriptions import get_current_admin
        app.dependency_overrides[get_current_admin] = lambda: True
    except ImportError:
        pass

    yield
    # teardown is handled by table‐truncation fixture below

# Create a synchronous engine for table truncation
_SYNC_DB_URL = TEST_DB_URL.replace(
    "postgresql+asyncpg://", "postgresql+psycopg2://"
)
_sync_engine = create_engine(_SYNC_DB_URL, future=True)

@pytest.fixture(autouse=True)
def _truncate_tables_after_each_test():
    yield
    # Truncate all tables to reset state
    with _sync_engine.begin() as conn:
        conn.execute(text("SET session_replication_role = replica;"))
        for tbl in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f'TRUNCATE TABLE "{tbl.name}" RESTART IDENTITY CASCADE'))
        conn.execute(text("SET session_replication_role = DEFAULT;"))


# ───── Virtual IoT services ─────

@pytest.fixture(scope="session", autouse=True)
def virtual_iot_services():
    # start four FastAPI-based device emulators on localhost:8001–8004
    _virtual_iot.start_virtual_iot()
    yield
    _virtual_iot.stop_virtual_iot()

# ───── Camera data root helper ─────
@pytest.fixture(autouse=True)
def _temp_cam_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CAM_DATA_ROOT", str(tmp_path))
    # reload config so HLS paths update
    from importlib import reload
    reload(__import__("app.core.config"))
    yield

# ───── Async HTTP client ─────
@pytest.fixture
async def async_client():
    async with AsyncClient(app=app, base_url="http://testserver") as client:
        yield client

# ───── Test‐result logging ─────
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
        fp.write(f"{ts} | {report.nodeid} | {outcome} | {getattr(report,'duration',0):.2f}s\n")
        if report.failed:
            fp.write("--- Failure details below ---\n")
            longrepr = getattr(report, "longreprtext", None) or str(report.longrepr)
            fp.write(f"{longrepr}\n")
            if cap := getattr(report, "capstderr", None):
                fp.write(f"--- stderr ---\n{cap}\n")
            fp.write("-" * 70 + "\n")
