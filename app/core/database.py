"""
Asynchronous SQLAlchemy + FastAPI data‑layer, designed for:

• PostgreSQL in production  (driver: ``postgresql+asyncpg``)
• SQLite in unit‑tests      (driver: ``sqlite+aiosqlite``)

Key points
──────────
✔  Fast start‑up retry loop (Postgres may come up a bit later in Docker)
✔  No pooled connections when ``TESTING=1`` (avoids event‑loop clashes)
✔  Safe session dependency that always commits / rolls back
✔  Optional schema bootstrap with advisory‑lock (multi‑worker safe)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import AsyncGenerator, Dict, Tuple

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import NullPool

from app.core.config import (
    DATABASE_URL,
    DB_POOL_SIZE,
    DB_MAX_OVERFLOW,
    RESET_DB,
    TESTING,
)

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 1.  Validate & parse DATABASE_URL
# ─────────────────────────────────────────────────────────────────────────────
url: URL = make_url(DATABASE_URL)

PG_BACKEND     = url.drivername.startswith("postgresql+asyncpg")
SQLITE_BACKEND = url.drivername.startswith("sqlite+aiosqlite")

if not (PG_BACKEND or SQLITE_BACKEND):
    raise RuntimeError(
        "Unsupported SQLAlchemy driver. "
        "Use 'postgresql+asyncpg' (production) or 'sqlite+aiosqlite' (tests).\n"
        f"Provided: {url.drivername}"
    )

if SQLITE_BACKEND and not TESTING:
    raise RuntimeError("SQLite backend is allowed **only** when TESTING=1")

# ─────────────────────────────────────────────────────────────────────────────
# 2.  Engine factory with retry (handles slow DB start‑ups)
# ─────────────────────────────────────────────────────────────────────────────
_MAX_RETRY_SEC = 15.0   # bail after 15 s
_INITIAL_DELAY = 0.75   # first retry after 750 ms


def _create_engine() -> Tuple:
    """Return ``(engine, is_sqlite)``."""
    kw: dict = {
        "future": True,
        "pool_pre_ping": True,
        # NOTE: ``echo`` comes from SQLALCHEMY env variable if you need it
    }

    # Never share DBAPI connections between event‑loops in tests → NullPool
    if TESTING:
        kw["poolclass"] = NullPool
    elif PG_BACKEND:
        kw |= {"pool_size": DB_POOL_SIZE, "max_overflow": DB_MAX_OVERFLOW}
    else:  # SQLite in production would land here (but we disallow it above)
        kw["poolclass"] = NullPool

    return create_async_engine(DATABASE_URL, **kw), SQLITE_BACKEND


def _engine_with_retry() -> Tuple:
    engine, is_sqlite = _create_engine()

    async def _check() -> None:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))

    async def _wait_until_ready() -> None:
        delay, total = _INITIAL_DELAY, 0.0
        while True:
            try:
                await _check()
                return
            except Exception as exc:
                if total >= _MAX_RETRY_SEC or is_sqlite:
                    log.error("Database connection failed: %s", exc)
                    raise
                log.warning("DB not ready, retrying in %.1fs… (%s)", delay, exc)
                await asyncio.sleep(delay)
                total += delay
                delay = min(delay * 1.7, 5.0)     # tame exponential back‑off

    # When imported outside a running loop we can block; inside PyTest we can’t.
    try:
        asyncio.get_event_loop().run_until_complete(_wait_until_ready())
    except RuntimeError:  # no running loop – happens under pytest‑asyncio
        asyncio.create_task(_wait_until_ready())

    return engine, is_sqlite


engine, _USING_SQLITE = _engine_with_retry()

# ─────────────────────────────────────────────────────────────────────────────
# 3.  Session factory & declarative base
# ─────────────────────────────────────────────────────────────────────────────
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)

Base = declarative_base()

# ─────────────────────────────────────────────────────────────────────────────
# 4.  FastAPI dependency
# ─────────────────────────────────────────────────────────────────────────────
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency that wraps each request in a transaction.

    • Commit on success
    • Roll back on exception
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

# ─────────────────────────────────────────────────────────────────────────────
# 5.  Optional schema bootstrap (dev / CI)
# ─────────────────────────────────────────────────────────────────────────────
_ADVISORY_KEY = 0x6A7971  # chosen at random


async def _ensure_postgres_schema() -> None:
    async with engine.begin() as conn:
        # Advisory‑lock prevents race‑condition when multiple workers start
        await conn.execute(text("SELECT pg_advisory_lock(:k)").bindparams(k=_ADVISORY_KEY))
        try:
            if RESET_DB:
                await conn.run_sync(Base.metadata.drop_all)
                log.info("Existing schema dropped (RESET_DB=1).")
            await conn.run_sync(Base.metadata.create_all)
            log.info("Schema verified/created (Postgres).")
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)").bindparams(k=_ADVISORY_KEY))


async def init_db(*, auto_create: bool | None = None) -> None:
    """
    Create tables automatically when:

    • ``TESTING=1``  (default for unit‑tests)
    • ``auto_create=True`` is passed explicitly
    """
    if auto_create is None:
        auto_create = TESTING

    if not auto_create:
        log.info("init_db(auto_create=False) – skipping create_all()")
        return

    if _USING_SQLITE:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        log.info("SQLite schema ensured.")
    else:
        await _ensure_postgres_schema()

# ─────────────────────────────────────────────────────────────────────────────
# 6.  Health helpers & cleanup
# ─────────────────────────────────────────────────────────────────────────────
async def check_db_connection() -> Dict[str, str]:
    """Lightweight readiness probe used by /health endpoint."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "connected", "timestamp": datetime.utcnow().isoformat()}
    except Exception as exc:  # pragma: no cover
        log.error("DB health‑check failed", exc_info=True)
        return {"status": "error", "error": str(exc)}


async def cleanup_db() -> None:
    """Dispose engine – useful between individual pytest cases."""
    await engine.dispose()


__all__ = (
    "engine",
    "AsyncSessionLocal",
    "Base",
    "get_db",
    "init_db",
    "check_db_connection",
    "cleanup_db",
)
