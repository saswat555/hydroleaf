# tests/conftest.py
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
os.environ.setdefault("CAM_SKIP_ANNOTATE", "1")
os.environ.setdefault("CAM_DATA_ROOT", str(ROOT / ".camdata_test"))
(ROOT / ".camdata_test").mkdir(exist_ok=True, parents=True)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from sqlalchemy.engine.url import make_url
import jwt as _jwt
import uuid

# ─────────────────────────────────────────────────────────────────────────────
# Patch out JWT signature checks in tests
# ─────────────────────────────────────────────────────────────────────────────
_orig_jwt_decode = _jwt.decode
def _decode_no_key(token, key=None, algorithms=None, options=None, **kwargs):
    return _orig_jwt_decode(
        token,
        key or "",
        algorithms=algorithms,
        options={**(options or {}), "verify_signature": False},
        **kwargs,
    )
_jwt.decode = _decode_no_key

# Import config so TESTING=True picks up TEST_DATABASE_URL
import app.core.config  # noqa: F401

# Grab the test‐DB URL
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:1234@localhost:5432/hydroleaf_test",
)

# Build a *sync* URL for psycopg2 work (alembic, DDL, truncation)
_sync_url = make_url(TEST_DB_URL)
if "+asyncpg" in _sync_url.drivername:
    _sync_url = _sync_url.set(drivername="postgresql+psycopg2")
SYNC_DB_URL = str(_sync_url)

# ─────────────────────────────────────────────────────────────────────────────
# Fresh DB each test run: DROP DATABASE …; CREATE DATABASE …; apply schema
# Prefer Alembic if available; otherwise fallback to Base.metadata.create_all.
# ─────────────────────────────────────────────────────────────────────────────
def _recreate_database(sync_url: str) -> None:
    url = make_url(sync_url)
    dbname = url.database
    # connect to 'postgres' maintenance DB with same creds
    admin_url = url.set(database="postgres")
    admin_engine = create_engine(
        str(admin_url),
        future=True,
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",  # needed for DROP/CREATE DATABASE
    )
    with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        # terminate existing connections (works without superuser in >=PG9.2 if you own the DB)
        conn.execute(
            text(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = :d AND pid <> pg_backend_pid()
                """
            ),
            {"d": dbname},
        )
        # drop & create (must be autocommit)
        conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{dbname}"')
        conn.exec_driver_sql(f'CREATE DATABASE "{dbname}" ENCODING \'UTF8\' TEMPLATE template1')
    admin_engine.dispose()

def _apply_schema_with_alembic_or_metadata(sync_url: str) -> None:
    # Try Alembic first (use local alembic.ini if present)
    ini = ROOT / "alembic.ini"
    if ini.exists():
        try:
            from alembic import command
            from alembic.config import Config
            cfg = Config(str(ini))
            # Force alembic to use the test DB URL
            cfg.set_main_option("sqlalchemy.url", sync_url)
            # Quiet alembic logger noise in CI
            cfg.attributes["configure_logger"] = False
            command.upgrade(cfg, "head")
            return
        except Exception as e:  # fall back to metadata if alembic hiccups
            print(f"[conftest] Alembic upgrade failed ({e}); falling back to metadata.create_all()")
    # Fallback: create all tables from models
    from app.core.database import Base
    eng = create_engine(sync_url, future=True, poolclass=NullPool)
    with eng.begin() as conn:
        Base.metadata.create_all(bind=conn)
    eng.dispose()

# Recreate DB & apply schema *before* any test engine connects
_recreate_database(SYNC_DB_URL)
_apply_schema_with_alembic_or_metadata(SYNC_DB_URL)

# ─────────────────────────────────────────────────────────────────────────────
# Async engine + sessionmaker for tests (no pooling)
# ─────────────────────────────────────────────────────────────────────────────
_test_engine = create_async_engine(TEST_DB_URL, echo=False, future=True, poolclass=NullPool)
TestSessionLocal = async_sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)

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
    # (Schema already applied above.) Still ensure metadata exists in case of fallback.
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Dependency override for get_db
    async def _override_get_db() -> AsyncSession:
        async with TestSessionLocal() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db

    # Make all admin routes succeed in builds that import the generic dependency
    try:
        # Use the canonical dependency module so test overrides match other tests
        from app.dependencies import get_current_admin
        app.dependency_overrides[get_current_admin] = lambda: True
    except Exception:
        pass

    yield
    # Engines are disposed below

# Create a synchronous engine for table truncation (created AFTER schema)
_sync_engine = create_engine(SYNC_DB_URL, future=True, poolclass=NullPool)

@pytest.fixture(autouse=True)
def _truncate_tables_after_each_test():
    yield
    # Truncate all tables to reset state (no superuser assumptions)
    with _sync_engine.begin() as conn:
        for tbl in reversed(Base.metadata.sorted_tables):
            # Schema-qualified name if present
            fullname = f'{tbl.schema}."{tbl.name}"' if tbl.schema else f'"{tbl.name}"'
            conn.execute(text(f"TRUNCATE TABLE {fullname} RESTART IDENTITY CASCADE"))

@pytest.fixture(scope="session", autouse=True)
def _dispose_engines_at_end():
    # Dispose engines when the entire session ends
    yield
    try:
        _sync_engine.dispose()
    finally:
        try:
            _test_engine.sync_engine.dispose()
        except Exception:
            pass

# ───── Sign-up helper used by many tests ─────
@pytest.fixture
async def signed_up_user(async_client):
    """
    Returns (user_id, user_payload, auth_headers).
    The tests only use the headers, but we keep the shape as a triple.
    """
    email = f"test_{uuid.uuid4().hex[:12]}@example.com"
    payload = {
        "email": email,
        "password": "Pass!234",
        "first_name": "Test",
        "last_name": "User",
        "phone": "9999999999",
        "address": "1 St",
        "city": "Bengaluru",
        "state": "KA",
        "country": "IN",
        "postal_code": "560001",
    }
    r = await async_client.post("/api/v1/auth/signup", json=payload)
    assert r.status_code == 201, f"Signup failed: {r.status_code} {r.text}"
    data = r.json()

    # tolerate slightly different response shapes
    token = data.get("access_token") or data.get("token") or data.get("accessToken")
    assert token, f"Signup response missing token: {data}"
    headers = {"Authorization": f"Bearer {token}"}

    user_obj = data.get("user") or {}
    user_id = user_obj.get("id") or data.get("user_id")
    return user_id, user_obj, headers

# ───── Virtual IoT services ─────
@pytest.fixture(scope="session", autouse=True)
def virtual_iot_services():
    # start four FastAPI-based device emulators on localhost:8001–8004
    _virtual_iot.start_virtual_iot()
    yield
    _virtual_iot.stop_virtual_iot()

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
