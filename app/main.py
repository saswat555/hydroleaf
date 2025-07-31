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

from app.core.config import ENVIRONMENT, ALLOWED_ORIGINS, SESSION_KEY, API_V1_STR, RESET_DB
from app.core.database import engine, Base, get_db, init_db, check_db_connection

# routers
from app.routers.auth import router as auth_router
from app.routers.users import router as users_router
from app.routers.admin_users import router as admin_users_router
from app.routers.subscriptions import router as subscriptions_router
from app.routers.admin_subscriptions import router as admin_subscriptions_router
from app.routers.admin_subscription_plans import router as admin_subscription_plans_router
from app.routers.devices import router as devices_router
from app.routers.dosing import router as dosing_router
from app.routers.config import router as config_router
from app.routers.farms import router as farms_router
from app.routers.plants import router as plants_router
from app.routers.supply_chain import router as supply_chain_router
from app.routers.cloud import router as cloud_router
from app.routers.admin import router as admin_router
from app.routers.device_comm import router as device_comm_router
from app.routers.cameras import router as cameras_router
from app.routers.admin_clips import router as admin_clips_router

from app.utils.camera_tasks import offline_watcher
from app.utils.camera_queue import camera_queue

# ─── Logging Setup ─────────────────────────────────────────────────────────────

log_path = Path("logs.txt")
log_path.parent.mkdir(parents=True, exist_ok=True)

formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
file_handler = RotatingFileHandler(str(log_path), maxBytes=5 * 1024 * 1024, backupCount=3)
file_handler.setFormatter(formatter)
logging.basicConfig(level=logging.INFO, handlers=[console_handler, file_handler])
logger = logging.getLogger(__name__)

# ─── Application Lifespan ──────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.start_time = time.time()
    # (re)create tables
    await init_db()
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

# ─── Request Logging ──────────────────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    ip = request.headers.get("x-forwarded-for", request.client.host)
    device_id = request.query_params.get("device_id", "-")
    try:
        resp = await call_next(request)
    except Exception as e:
        logger.error(f"Error on {request.method} {request.url.path}: {e}", exc_info=True)
        raise
    latency = (time.time() - start) * 1000
    logger.info(
        "%s %s • ip=%s • device_id=%s • %d • %.1fms",
        request.method, request.url.path, ip, device_id, resp.status_code, latency,
    )
    resp.headers["X-Process-Time"] = f"{latency/1000:.3f}"
    resp.headers["X-API-Version"] = app.version
    return resp

# ─── Health Endpoints ─────────────────────────────────────────────────────────
@app.get(f"{API_V1_STR}/health")
async def health_check():
    return {
        "status": "healthy",
        "version": app.version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "environment": ENVIRONMENT,
        "uptime": time.time() - app.state.start_time,
    }

@app.get(f"{API_V1_STR}/health/database")
async def database_health():
    return await check_db_connection()

@app.get(f"{API_V1_STR}/health/system")
async def system_health():
    return {
        "system": await health_check(),
        "database": await database_health(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# ─── Exception Handlers ───────────────────────────────────────────────────────
@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    logger.warning(f"HTTPException {exc.detail} on {request.url.path}")
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
app.include_router(auth_router, prefix=f"{API_V1_STR}/auth", tags=["auth"])
app.include_router(users_router)                # already carries its own prefix
app.include_router(admin_users_router)
app.include_router(subscriptions_router)
app.include_router(admin_subscriptions_router)
app.include_router(admin_subscription_plans_router)

app.include_router(devices_router,      prefix=f"{API_V1_STR}/devices",      tags=["devices"])
app.include_router(dosing_router,       prefix=f"{API_V1_STR}/dosing",       tags=["dosing"])
app.include_router(config_router,       prefix=f"{API_V1_STR}/config",       tags=["config"])
app.include_router(plants_router,       prefix=f"{API_V1_STR}/plants",       tags=["plants"])
app.include_router(farms_router,        prefix=f"{API_V1_STR}/farms",        tags=["farms"])
app.include_router(supply_chain_router, prefix=f"{API_V1_STR}/supply_chain", tags=["supply_chain"])
app.include_router(cloud_router,        prefix=f"{API_V1_STR}/cloud",        tags=["cloud"])
app.include_router(device_comm_router,  prefix=f"{API_V1_STR}/device_comm",  tags=["device_comm"])
app.include_router(cameras_router,      prefix=f"{API_V1_STR}/cameras",      tags=["cameras"])
app.include_router(admin_clips_router)
app.include_router(admin_router)

# ─── Startup / Shutdown Tasks ─────────────────────────────────────────────────
@app.on_event("startup")
async def startup_tasks():
    asyncio.create_task(offline_watcher(db_factory=get_db, interval_seconds=30))
    camera_queue.start_workers()

@app.on_event("shutdown")
async def shutdown_cleanup():
    from app.routers.cameras import _clip_writers
    for info in _clip_writers.values():
        try:
            info["writer"].release()
        except:
            pass

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 3000)),
        log_level=os.getenv("LOG_LEVEL", "info"),
        reload=os.getenv("DEBUG", "false").lower() == "true",
    )
