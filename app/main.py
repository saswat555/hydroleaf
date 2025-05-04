# app/main.py

import os
import time
import logging
import asyncio
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn

from app.core.config import ENVIRONMENT, ALLOWED_ORIGINS, SESSION_KEY, API_V1_STR
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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ─── Application Lifespan ──────────────────────────────────────────────────────
RESET_DB = os.getenv("RESET_DB", "false").lower() in ("1","true","yes")
@asynccontextmanager
async def lifespan(app):
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
templates = Jinja2Templates(directory="app/templates")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# ─── Request Logging Middleware ───────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    try:
        # ── NEW: who is calling? ───────────────────────────────────────
        client_ip = request.headers.get("x-forwarded-for", request.client.host)
        device_id = request.query_params.get("device_id")  # may be None

        response = await call_next(request)
        latency = time.time() - start

        # neat one‑liner: METHOD PATH • ip=… • device_id=… • code •  ms
        logger.info(
            "%s %s • ip=%s • device_id=%s • %d • %.1f ms",
            request.method,
            request.url.path,
            client_ip,
            device_id or "-",
            response.status_code,
            latency * 1000,
        )

        response.headers.update({
             "X-Process-Time": f"{latency:.3f}",
             "X-API-Version": app.version,
         })
        return response
    except Exception as exc:
        logger.error(f"Unhandled error during request: {exc}", exc_info=True)
        raise

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
    return {
        "system": sys,
        "database": db,
        "timestamp": datetime.now(timezone.utc),
    }

# ─── Override Camera Upload Endpoints ─────────────────────────────────────────
app.add_api_route("/upload/{camera_id}/day", upload_day_frame, methods=["POST"])
app.add_api_route("/upload/{camera_id}/night", upload_night_frame, methods=["POST"])

# ─── Exception Handlers ───────────────────────────────────────────────────────
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

# ─── Include Routers ──────────────────────────────────────────────────────────

# routers that declare their own prefixes internally:
app.include_router(auth_router,            prefix=f"{API_V1_STR}/auth")
app.include_router(users_router)                                  # already @router(prefix="/api/v1/users")
app.include_router(admin_users_router)                            # already @router(prefix="/admin/users")
app.include_router(subscriptions_router)                          # already @router(prefix="/api/v1/subscriptions")
app.include_router(admin_subscriptions_router)                    # already @router(prefix="/admin")

# the rest use main.py prefixes:
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

# ─── Startup Tasks ───────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_tasks():
    asyncio.create_task(
        offline_watcher(
            db_factory=get_db,
            interval_seconds=30,
        )
    )
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
