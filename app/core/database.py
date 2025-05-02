# app/core/database.py
"""PostgreSQL‑only async DB layer with *safe multi‑worker bootstrap*.

Key points
----------
* Refuses to start if `DATABASE_URL` is **not** `postgresql+asyncpg://…`.
* Uses a **PostgreSQL advisory lock** so only **one worker** runs
  `metadata.create_all()` – avoids the duplicate‑sequence race you just hit.
* Exposes:  `engine`, `AsyncSessionLocal`, `Base`, `get_db()` dependency,
  `init_db()`, `check_db_connection()`, and `cleanup_db()`.
* In production you should normally run Alembic migrations; the bootstrap
  helper is for local dev / CI or the very first deploy.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import AsyncGenerator, Dict

from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

from app.core.config import DATABASE_URL, DB_POOL_SIZE, DB_MAX_OVERFLOW

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 1.  Validate DATABASE_URL – we only support Postgres via asyncpg             #
# --------------------------------------------------------------------------- #
url = make_url(DATABASE_URL)
if not url.drivername.startswith("postgresql+asyncpg"):
    raise RuntimeError(
        "Hydroleaf is configured for PostgreSQL only.  "
        f"Invalid driver in DATABASE_URL: {url.drivername}"
    )

# --------------------------------------------------------------------------- #
# 2.  Engine & session factory                                                #
# --------------------------------------------------------------------------- #
engine = create_async_engine(
    DATABASE_URL,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    future=True,
    pool_pre_ping=True,
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)

# Base that every model shares
Base = declarative_base()

# --------------------------------------------------------------------------- #
# 3.  FastAPI session dependency                                              #
# --------------------------------------------------------------------------- #
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a transactional session and guarantee proper cleanup."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:  # pragma: no cover
            await session.rollback()
            raise
        finally:
            await session.close()

# --------------------------------------------------------------------------- #
# 4.  Boot‑time schema helper with advisory lock                              #
# --------------------------------------------------------------------------- #
_ADVISORY_KEY = 0x6A7971  # arbitrary constant <= 2^31‑1

async def init_db(create: bool = True) -> None:
    """Ensure the schema exists (dev / first‑run).

    Uses `pg_advisory_lock` so that when several Uvicorn workers start at the
    same time **only one** will run `metadata.create_all()`; the others wait
    for the lock, see that the tables already exist, and move on.
    """
    if not create:
        return  # migrations only, skip auto‑create

    async with engine.begin() as conn:
        # Acquire global lock (blocks until available)
        await conn.execute(text("SELECT pg_advisory_lock(:k)").bindparams(k=_ADVISORY_KEY))
        try:
            await conn.run_sync(Base.metadata.create_all)
            logger.info("DB schema ensured (create_all checkfirst)")
        finally:
            # Always release lock so hot‑reload works in dev
            await conn.execute(text("SELECT pg_advisory_unlock(:k)").bindparams(k=_ADVISORY_KEY))

# --------------------------------------------------------------------------- #
# 5.  Health helpers                                                          #
# --------------------------------------------------------------------------- #
async def check_db_connection() -> Dict[str, str]:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "connected", "timestamp": datetime.utcnow().isoformat()}
    except Exception as exc:  # pragma: no cover
        logger.error("DB health‑check failed", exc_info=True)
        return {"status": "error", "error": str(exc)}

async def cleanup_db() -> None:
    await engine.dispose()
