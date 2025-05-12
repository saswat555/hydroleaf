# app/main.py

import os
import time
import logging
import asyncio
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn

from app.routers.admin_clips import router as admin_clips_router
from app.core.config import ENVIRONMENT, ALLOWED_ORIGINS, SESSION_KEY, API_V1_STR, RESET_DB
from app.core.database import engine, Base, get_db, check_db_connection

from app.routers import (
    auth_router,
    users_router,
    admin_users_router,
    subscriptions_router,
    admin_subscriptions_router,
    devices_router,
    dosing_router,
    config_router,
    farms_router,
    plants_router,
    supply_chain_router,
    cloud_router,
    admin_router,
    device_comm_router,
    cameras_router,
)
from app.routers.cameras import upload_day_frame, upload_night_frame
from app.utils.camera_tasks import offline_watcher
from app.utils.camera_queue import camera_queue

# ─── Logging Setup ─────────────────────────────────────────────────────────────

# Ensure logs directory exists
log_path = Path("logs.txt")
log_path.parent.mkdir(parents=True, exist_ok=True)

# Console formatter
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

# Rotating file handler
file_handler = RotatingFileHandler(
    filename=str(log_path),
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
)
file_handler.setFormatter(formatter)

# Root logger configuration
logging.basicConfig(level=logging.INFO, handlers=[console_handler, file_handler])
logger = logging.getLogger(__name__)

# ─── Application Lifespan ──────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # record startup time for /health
    app.state.start_time = time.time()

    async with engine.begin() as conn:
        if RESET_DB:
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    yield

# ─── Instantiate FastAPI ──────────────────────────────────────────────────────
app = FastAPI(
    title="Hydroleaf API",
    version=os.getenv("API_VERSION", "1.0.0"),
    docs_url=f"{API_V1_STR}/docs",
    redoc_url=None,
    openapi_url=f"{API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

# ─── Middleware ────────────────────────────────────────────────────────────────
app.add_middleware(SessionMiddleware, secret_key=SESSION_KEY)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/hls", StaticFiles(directory=os.getenv("CAM_DATA_ROOT", "./data")), name="hls")
templates = Jinja2Templates(directory="app/templates")

# ─── Request Logging Middleware ───────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    client_ip = request.headers.get("x-forwarded-for", request.client.host)
    device_id = request.query_params.get("device_id", "-")

    try:
        response = await call_next(request)
    except Exception as exc:
        logger.error(f"Unhandled error handling request {request.method} {request.url.path}: {exc}", exc_info=True)
        raise

    latency = (time.time() - start_time) * 1000
    logger.info(
        "%s %s • ip=%s • device_id=%s • %d • %.1fms",
        request.method,
        request.url.path,
        client_ip,
        device_id,
        response.status_code,
        latency,
    )

    response.headers["X-Process-Time"] = f"{latency/1000:.3f}"
    response.headers["X-API-Version"] = app.version
    return response

# ─── Health Endpoints ─────────────────────────────────────────────────────────
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
    return await check_db_connection()

@app.get(f"{API_V1_STR}/health/system")
async def system_health():
    sys = await health_check()
    db = await database_health()
    return {"system": sys, "database": db, "timestamp": datetime.now(timezone.utc)}

from app.dependencies import verify_camera_token

app.add_api_route(
    "/upload/{camera_id}/day",
    upload_day_frame,
    methods=["POST"],
    dependencies=[Depends(verify_camera_token)],
)
app.add_api_route(
    "/upload/{camera_id}/night",
    upload_night_frame,
    methods=["POST"],
    dependencies=[Depends(verify_camera_token)],
)
# ─── Exception Handlers ───────────────────────────────────────────────────────
@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    logger.warning(f"HTTPException: {exc.detail} on {request.url.path}")
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
    logger.error(f"Unhandled exception on {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "Internal server error",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "path": request.url.path,
        },
    )

# ─── Include Routers ──────────────────────────────────────────────────────────
app.include_router(auth_router,            prefix=f"{API_V1_STR}/auth")
app.include_router(users_router)                                  # already prefixed
app.include_router(admin_users_router)                            # already prefixed
app.include_router(subscriptions_router)                          # already prefixed
app.include_router(admin_subscriptions_router)                    # already prefixed

app.include_router(devices_router,       prefix=f"{API_V1_STR}/devices",       tags=["devices"])
app.include_router(dosing_router,        prefix=f"{API_V1_STR}/dosing",        tags=["dosing"])
app.include_router(config_router,        prefix=f"{API_V1_STR}/config",        tags=["config"])
app.include_router(plants_router,        prefix=f"{API_V1_STR}/plants",        tags=["plants"])
app.include_router(farms_router,         prefix=f"{API_V1_STR}/farms",         tags=["farms"])
app.include_router(supply_chain_router,  prefix=f"{API_V1_STR}/supply_chain",  tags=["supply_chain"])
app.include_router(cloud_router,         prefix=f"{API_V1_STR}/cloud",         tags=["cloud"])
app.include_router(admin_router,         prefix="/admin",                      tags=["admin"])
app.include_router(device_comm_router,   prefix=f"{API_V1_STR}/device_comm",   tags=["device_comm"])
app.include_router(cameras_router,       prefix=f"{API_V1_STR}/cameras",       tags=["cameras"])
app.include_router(admin_clips_router)

# ─── Startup Tasks ───────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_tasks():
    asyncio.create_task(offline_watcher(db_factory=get_db, interval_seconds=30))
    camera_queue.start_workers()

# ─── Main Entrypoint ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 3000)),
        log_level=os.getenv("LOG_LEVEL", "info"),
        reload=os.getenv("DEBUG", "false").lower() == "true",
    )
