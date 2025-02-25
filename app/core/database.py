import logging
from typing import Dict, List, AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import StaticPool
from sqlalchemy import text
from datetime import datetime

from app.core.config import DATABASE_URL

logger = logging.getLogger(__name__)

# Create engine for SQLite
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)

# Create session factory
AsyncSessionLocal = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)

# Create declarative base
Base = declarative_base()

async def init_db() -> bool:
    """Initialize database"""
    try:
        async with engine.begin() as conn:
            # await conn.run_sync(Base.metadata.drop_all)
            # await conn.run_sync(Base.metadata.create_all)
            
            # Verify tables were created
            result = await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            )
            tables = [row[0] for row in result.fetchall()]
            logger.info(f"Created tables: {tables}")
            
        return True
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        raise

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Get database session"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

async def check_db_connection() -> Dict:
    """Check SQLite database connection and status"""
    try:
        async with AsyncSessionLocal() as session:
            # Test basic connectivity
            result = await session.execute(text("SELECT 1"))
            value = result.scalar()
            
            # Get table information
            tables_result = await session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            )
            existing_tables = [row[0] for row in tables_result.fetchall()]
            
            # Expected tables
            expected_tables = [
                'devices',
                'dosing_profiles',
                'sensor_readings',
                'dosing_operations'
            ]
            
            missing_tables = set(expected_tables) - set(existing_tables)
            
            return {
                "status": "connected",
                "type": "sqlite",
                "tables": {
                    "existing": existing_tables,
                    "missing": list(missing_tables),
                    "status": "complete" if not missing_tables else "incomplete"
                },
                "error": None
            }
            
    except Exception as e:
        logger.error(f"Database connection check failed: {e}")
        return {
            "status": "error",
            "type": "sqlite",
            "tables": None,
            "error": str(e)
        }

async def get_table_stats() -> Dict:
    """Get row counts for SQLite tables"""
    try:
        async with AsyncSessionLocal() as session:
            # Get list of actual tables
            tables_result = await session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            )
            tables = [row[0] for row in tables_result.fetchall()]
            
            stats = {}
            for table in tables:
                count_result = await session.execute(text(f"SELECT COUNT(*) FROM {table}"))
                count = count_result.scalar()
                stats[table] = count
                
            return {
                "status": "success",
                "counts": stats,
                "error": None
            }
    except Exception as e:
        logger.error(f"Error getting table statistics: {e}")
        return {
            "status": "error",
            "counts": {},
            "error": str(e)
        }

async def get_migration_status() -> Dict:
    """Get SQLite database migration status"""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            )
            existing_tables = [row[0] for row in result.fetchall()]
            
            expected_tables = [
                'devices',
                'dosing_profiles',
                'sensor_readings',
                'dosing_operations'
            ]
            
            missing_tables = set(expected_tables) - set(existing_tables)
            
            return {
                "status": "ok" if not missing_tables else "incomplete",
                "existing_tables": existing_tables,
                "missing_tables": list(missing_tables),
                "error": None
            }
    except Exception as e:
        logger.error(f"Error checking migration status: {e}")
        return {
            "status": "error",
            "existing_tables": [],
            "missing_tables": [],
            "error": str(e)
        }

async def cleanup_db() -> bool:
    """Cleanup database connections"""
    try:
        await engine.dispose()
        logger.info("Database connections cleaned up successfully")
        return True
    except Exception as e:
        logger.error(f"Error during database cleanup: {e}")
        return False