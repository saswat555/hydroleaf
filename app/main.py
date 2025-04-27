# app/main.py
import os
import time
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from app.utils.camera_queue import camera_queue
from fastapi import FastAPI, HTTPException, Request, status
from fastapi import BackgroundTasks, Query, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import uvicorn

from app.core.config import (
    ENVIRONMENT, OFFLINE_TIMEOUT, ALLOWED_ORIGINS,
    SESSION_KEY, API_V1_STR
)
from app.core.database import init_db, AsyncSessionLocal, get_db, engine, Base
from app.routers import (
    devices_router, dosing_router, config_router,
    farms_router, plants_router, supply_chain_router,
    cloud_router, users_router, admin_users_router,
    auth_router, device_comm_router,
    admin_router, cameras_router, subscriptions_router, admin_subscriptions_router
)
from app.routers.cameras import upload_day_frame, upload_night_frame
from app.utils.camera_tasks import offline_watcher
from app.routers.cameras import _process_upload
from app.core.database import get_db
# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Application lifespan for migrations & startup
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Hydroleaf API - ensuring database schema...")
    # Verify Alembic config
    if not Path("alembic.ini").is_file() or not Path("alembic").is_dir():
        raise RuntimeError(
            "Alembic configuration missing: please run `alembic init alembic` and set up migrations."
        )

    # 1) Run Alembic migrations
    try:
        await asyncio.to_thread(init_db)
        logger.info("Alembic migrations complete")
    except Exception as e:
        logger.error(f"Migration step failed: {e}")
        raise

    # 2) Auto-create any missing tables
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("SQLAlchemy metadata.create_all complete")
    except Exception as e:
        logger.error(f"Auto-create tables failed: {e}")
        raise

    # Mark startup complete
    app.state.start_time = time.time()
    yield
    logger.info("Shutting down Hydroleaf API")

# Instantiate app
app = FastAPI(
    title="Hydroleaf API",
    version=os.getenv("API_VERSION", "1.0.0"),
    docs_url=f"{API_V1_STR}/docs",
    redoc_url=None,
    openapi_url=f"{API_V1_STR}/openapi.json",
    lifespan=lifespan
)

# Middleware
app.add_middleware(SessionMiddleware, secret_key=SESSION_KEY)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")

# --- Logging middleware (fixed) ---
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    try:
        response = await call_next(request)
        latency = time.time() - start
        response.headers.update({
            "X-Process-Time": f"{latency:.3f}",
            "X-API-Version": app.version,
        })
        return response
    except Exception as exc:
        logger.error(f"Unhandled error during request: {exc}", exc_info=True)
        raise

# Health endpoints
@app.get(f"{API_V1_STR}/health")
async def health_check():
    uptime = time.time() - app.state.start_time
    return {
        "status": "healthy",
        "version": app.version,
        "timestamp": datetime.now(timezone.utc),
        "environment": ENVIRONMENT,
        "uptime": uptime,
    }

@app.get(f"{API_V1_STR}/health/database")
async def database_health():
    now = datetime.now(timezone.utc)
    try:
        session = await get_db().__anext__()
        await session.close()
    except Exception as e:
        return {"status": "error", "detail": str(e)}
    return {"status": "ok", "timestamp": now}

@app.get(f"{API_V1_STR}/health/system")
async def system_health():
    sys = await health_check()
    db = await database_health()
    return {
        "system": sys,
        "database": db,
        "timestamp": datetime.now(timezone.utc),
        "api_version": app.version,
        "environment": ENVIRONMENT,
    }

# Camera upload endpoints (override default router paths)
app.add_api_route(
    "/upload/{camera_id}/day",
    upload_day_frame,
    methods=["POST"]
)
app.add_api_route(
    "/upload/{camera_id}/night",
    upload_night_frame,
    methods=["POST"]
)

# Exception handlers
@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "path": request.url.path,
        },
    )

@app.exception_handler(Exception)
async def exc_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "Internal server error",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "path": request.url.path,
        },
    )

# Include routers
app.include_router(admin_users_router, prefix=f"{API_V1_STR}/admin/users", tags=["admin-users"])
app.include_router(auth_router, prefix=f"{API_V1_STR}/auth", tags=["auth"])
app.include_router(devices_router, prefix=f"{API_V1_STR}/devices", tags=["devices"])
app.include_router(dosing_router, prefix=f"{API_V1_STR}/dosing", tags=["dosing"])
app.include_router(config_router, prefix=f"{API_V1_STR}/config", tags=["config"])
app.include_router(plants_router, prefix=f"{API_V1_STR}/plants", tags=["plants"])
app.include_router(farms_router, prefix=f"{API_V1_STR}/farms", tags=["farms"])
app.include_router(supply_chain_router, prefix=f"{API_V1_STR}/supply_chain", tags=["supply_chain"])
app.include_router(cloud_router, prefix=f"{API_V1_STR}/cloud", tags=["cloud"])
app.include_router(admin_router, prefix="/admin", tags=["admin"])
app.include_router(device_comm_router, prefix=f"{API_V1_STR}/device_comm", tags=["device_comm"])
app.include_router(cameras_router, prefix=f"{API_V1_STR}/cameras", tags=["cameras"])
app.include_router(subscriptions_router, prefix=f"{API_V1_STR}/subscriptions", tags=["subscriptions"])
app.include_router(admin_subscriptions_router, prefix="/admin/subscriptions", tags=["admin-subscriptions"])# Startup tasks
@app.on_event("startup")
async def startup_tasks():
    asyncio.create_task(
        offline_watcher(
            db_factory=AsyncSessionLocal,
            interval_seconds=OFFLINE_TIMEOUT / 2,
        )
    )
    asyncio.create_task(offline_watcher(db_factory=AsyncSessionLocal, interval_seconds=OFFLINE_TIMEOUT/2))
    # 2) start YOLO detection workers
    camera_queue.start_workers()
@app.post("/upload", summary="Legacy camera firmware: send a frame")
async def legacy_camera_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    camera_id: str = Query(..., description="Camera ID"),
    db: AsyncSession = Depends(get_db),
):
    # firmware signals night mode by header X-Night: true
    day_flag = request.headers.get("X-Night", "").lower() != "true"
    try:
        return await _process_upload(camera_id, request, background_tasks, db, day_flag=day_flag)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}")
# Run server
if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        log_level=os.getenv("LOG_LEVEL", "info"),
        reload=os.getenv("DEBUG", "false").lower() == "true"
    )
