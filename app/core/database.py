# app/core/database.py

import logging
from datetime import datetime
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.orm import declarative_base

from app.core.config import (
    DATABASE_URL,
    TEST_DATABASE_URL,
    TESTING,
    DB_POOL_SIZE,
    DB_MAX_OVERFLOW,
    RESET_DB,
)

logger = logging.getLogger(__name__)

# ─── Build the correct URL ────────────────────────────────────────────────────
DB_URL = TEST_DATABASE_URL if TESTING and TEST_DATABASE_URL else DATABASE_URL
if not DB_URL.startswith("postgresql+asyncpg://"):
    raise RuntimeError(f"DB_URL must start with postgresql+asyncpg://, got {DB_URL}")

# ─── Engine ───────────────────────────────────────────────────────────────────
engine = create_async_engine(
    DB_URL,
    pool_pre_ping=True,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    future=True,
)

# ─── Base declarative class ──────────────────────────────────────────────────
Base = declarative_base()

# ─── Sessionmaker exposed for direct import in tests ─────────────────────────
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

# ─── FastAPI dependency ──────────────────────────────────────────────────────
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Yield a database session, committing on success and rolling back on error.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except:
            await session.rollback()
            raise

# ─── (Re)create all tables at startup ────────────────────────────────────────
async def init_db() -> None:
    async with engine.begin() as conn:
        if RESET_DB:
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

# ─── Health‑check for /health/database ───────────────────────────────────────
async def check_db_connection() -> dict[str, str]:
    """
    Simple check: run SELECT 1.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "connected", "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        logger.error("Database health check failed", exc_info=True)
        return {"status": "error", "error": str(e)}
