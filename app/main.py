from fastapi import FastAPI, HTTPException, status, Request
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.logger import logger as fastapi_logger
from pydantic import BaseModel
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
import logging
import os
import time
from sqlalchemy import text
import asyncio
from app.routers import devices, dosing, config, plants, supply_chain, cloud
from app.routers.heartbeat import router as heartbeat_router
from app.routers.admin import router as admin_router
from app.core.database import (
    init_db, 
    check_db_connection,
    get_table_stats,
    get_migration_status
)
from app.simulated_esp import simulated_esp_app 
# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Pydantic models for responses
class HealthResponse(BaseModel):
    status: str
    version: str
    timestamp: datetime
    environment: str
    uptime: float

class TableInfo(BaseModel):
    existing: List[str]
    missing: List[str]
    status: str

class MigrationInfo(BaseModel):
    status: str
    existing_tables: List[str]
    missing_tables: List[str]
    error: Optional[str] = None

class DatabaseHealthResponse(BaseModel):
    status: str
    type: str
    timestamp: datetime
    last_check: Optional[datetime] = None
    error: Optional[str] = None
    migrations: MigrationInfo
    tables: Dict[str, Any]

    class Config:
        arbitrary_types_allowed = True

class SystemHealthResponse(BaseModel):
    system: Dict[str, Any]
    database: Dict[str, Any]
    timestamp: datetime
    api_version: str
    environment: str

    class Config:
        arbitrary_types_allowed = True

# Global variables
START_TIME = time.time()
API_VERSION = "1.0.0"

# Application lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    logger.info("Starting Krishiverse API")
    try:
        await init_db()
        yield
        logger.info("Shutting down Krishiverse API")
    except Exception as e:
        logger.error(f"Error during application lifecycle: {e}")
        raise

# Create FastAPI application
app = FastAPI(
    title="Krishiverse API",
    description="API for managing IoT devices and automated dosing in hydroponic systems",
    version=API_VERSION,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/simulated_esp", simulated_esp_app)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log request details"""
    start_time = time.time()
    client_ip = request.client.host if request.client else "unknown"
    
    logger.info(f"Request started: {request.method} {request.url.path} from {client_ip}")
    
    try:
        response = await call_next(request)
        process_time = time.time() - start_time
        
        logger.info(
            f"Request completed: {request.method} {request.url.path} "
            f"Status: {response.status_code} "
            f"Duration: {process_time:.3f}s "
            f"Client: {client_ip}"
        )
        
        response.headers["X-Process-Time"] = str(process_time)
        response.headers["X-API-Version"] = API_VERSION
        
        return response
    except Exception as e:
        logger.error(f"Request failed: {request.method} {request.url.path} Error: {e}")
        raise

# Health check endpoints
@app.get("/api/v1/health", response_model=HealthResponse)
async def health_check():
    """Basic health check endpoint"""
    try:
        uptime = time.time() - START_TIME
        return {
            "status": "healthy",
            "version": API_VERSION,
            "timestamp": datetime.now(timezone.utc),
            "environment": os.getenv("ENVIRONMENT", "development"),
            "uptime": uptime
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="System health check failed"
        )

@app.get("/api/v1/health/database", response_model=DatabaseHealthResponse)
async def database_health_check():
    """Database health check endpoint"""
    try:
        db_info = await check_db_connection()
        migration_info = await get_migration_status()
        table_stats = await get_table_stats()
        
        current_time = datetime.now(timezone.utc)
        
        return {
            "status": "healthy" if db_info["status"] == "connected" else "unhealthy",
            "type": "sqlite",
            "timestamp": current_time,
            "last_check": current_time,
            "error": db_info.get("error"),
            "migrations": {
                "status": migration_info["status"],
                "existing_tables": migration_info.get("existing_tables", []),
                "missing_tables": migration_info.get("missing_tables", []),
                "error": migration_info.get("error")
            },
            "tables": {
                "counts": table_stats.get("counts", {}),
                "status": table_stats.get("status", "unknown")
            }
        }
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return {
            "status": "error",
            "type": "sqlite",
            "timestamp": datetime.now(timezone.utc),
            "last_check": datetime.now(timezone.utc),
            "error": str(e),
            "migrations": {
                "status": "error",
                "existing_tables": [],
                "missing_tables": [],
                "error": str(e)
            },
            "tables": {
                "counts": {},
                "status": "error"
            }
        }

@app.get("/api/v1/health/all", response_model=SystemHealthResponse)
async def system_health_check():
    """Complete system health check"""
    try:
        system = await health_check()
        database = await database_health_check()
        
        return {
            "system": system,
            "database": database,
            "timestamp": datetime.now(timezone.utc),
            "api_version": API_VERSION,
            "environment": os.getenv("ENVIRONMENT", "development")
        }
    except Exception as e:
        logger.error(f"System health check failed: {e}")
        return {
            "system": {"status": "error", "error": str(e)},
            "database": {"status": "unknown"},
            "timestamp": datetime.now(timezone.utc),
            "api_version": API_VERSION,
            "environment": os.getenv("ENVIRONMENT", "development")
        }

# Error handlers
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Handle HTTP exceptions"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "path": request.url.path
        }
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle general exceptions"""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "Internal server error",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "path": request.url.path
        }
    )

# Include routers
app.include_router(devices.router, prefix="/api/v1/devices", tags=["devices"])
app.include_router(dosing.router, prefix="/api/v1/dosing", tags=["dosing"])
app.include_router(config.router, prefix="/api/v1/config", tags=["config"])
app.include_router(plants.router, prefix="/api/v1/plants", tags=["plants"]) 
app.include_router(supply_chain.router, prefix="/api/v1/supply_chain", tags=["supply_chain"])
app.include_router(cloud.router, prefix="/api/v1", tags=["cloud"])
app.include_router(heartbeat_router)
app.include_router(admin_router)
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=bool(os.getenv("DEBUG", "True")),
        log_level=os.getenv("LOG_LEVEL", "info")
    )
