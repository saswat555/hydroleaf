# app/main.py

import os
import time
import logging
import asyncio
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.config import ENVIRONMENT, ALLOWED_ORIGINS, SESSION_KEY, API_V1_STR, TESTING
from app.core.database import init_db, check_db_connection, get_db
from app.schemas import HealthCheck, DatabaseHealthCheck, FullHealthCheck

# ─── Routers ──────────────────────────────────────────────────────────────────
from app.routers.auth import router as auth_router
from app.routers.devices import router as devices_router
from app.routers.farms import router as farms_router
from app.routers.payments import router as payments_router
from app.routers.subscriptions import router as subscriptions_router
from app.routers.cameras import router as cameras_router
from app.routers.device_comm import router as device_comm_router
from app.routers.cloud import router as cloud_router
from app.routers.plants import router as plants_router
from app.routers.admin import router as admin_router
from app.routers.admin_users import router as admin_users_router
from app.routers.admin_subscription_plans import router as admin_plans_router
from app.routers.admin_clips import router as admin_clips_router
from app.routers.dosing import router as dosing_router
from app.routers.config import router as config_router
from app.routers.users import router as users_router
from app.routers.supply_chain import router as supply_chain_router

# Admin-only
from app.routers.admin_subscriptions import router as admin_subscriptions_router


# ─── Logging Setup ─────────────────────────────────────────────────────────────
log_path = Path("logs.txt")
log_path.parent.mkdir(exist_ok=True)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
console_handler = logging.StreamHandler();  console_handler.setFormatter(formatter)
file_handler    = RotatingFileHandler(str(log_path), maxBytes=5_000_000, backupCount=3)
file_handler.setFormatter(formatter)
logging.basicConfig(level=logging.INFO, handlers=[console_handler, file_handler])
logger = logging.getLogger(__name__)

# ─── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Hydroleaf API",
    version=os.getenv("API_VERSION", "1.0.0"),
    docs_url=f"{API_V1_STR}/docs",
    redoc_url=None,
    openapi_url=f"{API_V1_STR}/openapi.json",
)

# ─── Redirect banner paths to the real docs ───────────────────────────────────
@app.get("/docs", include_in_schema=False)
def _redirect_docs():
    return RedirectResponse(url=f"{API_V1_STR}/docs")

@app.get("/openapi.json", include_in_schema=False)
def _redirect_openapi():
    return RedirectResponse(url=f"{API_V1_STR}/openapi.json")

# ─── Middlewares ───────────────────────────────────────────────────────────────
app.add_middleware(SessionMiddleware, secret_key=SESSION_KEY)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Static Files & Templates ─────────────────────────────────────────────────
_static_dir = Path("app/static"); _static_dir.mkdir(parents=True, exist_ok=True)
_hls_dir    = Path(os.getenv("CAM_DATA_ROOT", "./data")); _hls_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
app.mount("/hls",    StaticFiles(directory=str(_hls_dir)),   name="hls")
templates = Jinja2Templates(directory="app/templates")

# ─── Request-logging Middleware ───────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    ip = request.headers.get("x-forwarded-for", request.client.host)
    device_id = request.query_params.get("device_id", "-")
    try:
        response = await call_next(request)
    except Exception as e:
        logger.error(f"Error on {request.method} {request.url.path}: {e}", exc_info=True)
        raise
    ms = (time.time() - start) * 1000
    logger.info("%s %s • ip=%s • device_id=%s • %d • %.1fms",
                request.method, request.url.path, ip, device_id, response.status_code, ms)
    response.headers["X-Process-Time"] = f"{ms/1000:.3f}"
    response.headers["X-API-Version"] = app.version
    return response

# ─── Startup / Shutdown ────────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup():
    app.state.start_time = time.time()
    if not TESTING:
        await init_db()
        # import heavy CV/YOLO only when we actually run them
        from app.utils.camera_tasks import offline_watcher
        from app.utils.camera_queue import camera_queue
        asyncio.create_task(offline_watcher(db_factory=get_db, interval_seconds=30))
        camera_queue.start_workers()

@app.on_event("shutdown")
async def on_shutdown():
    from app.routers.cameras import _clip_writers
    for info in _clip_writers.values():
        try:
            info["writer"].release()
        except:
            pass

# ─── Health Endpoints ─────────────────────────────────────────────────────────
@app.get(f"{API_V1_STR}/health", response_model=HealthCheck)
async def health_check():
    return HealthCheck(
        status="healthy", version=app.version,
        timestamp=datetime.now(timezone.utc),
        environment=ENVIRONMENT,
        uptime=time.time() - app.state.start_time,
    )

@app.get(f"{API_V1_STR}/health/database", response_model=DatabaseHealthCheck)
async def database_health():
    s = await check_db_connection()
    return DatabaseHealthCheck(
        status=s["status"], type="database",
        timestamp=datetime.now(timezone.utc),
        last_test=s.get("timestamp"),
    )

@app.get(f"{API_V1_STR}/health/system", response_model=FullHealthCheck)
async def system_health():
    sys = await health_check()
    db  = await database_health()
    return FullHealthCheck(system=sys, database=db,
                           timestamp=datetime.now(timezone.utc))

# ─── Exception Handlers ───────────────────────────────────────────────────────
@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    logger.warning(f"HTTPException {exc.detail} on {request.url.path}")
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail,
                 "timestamp": datetime.now(timezone.utc).isoformat(),
                 "path": request.url.path},
    )

@app.exception_handler(Exception)
async def exc_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception on {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error",
                 "timestamp": datetime.now(timezone.utc).isoformat(),
                 "path": request.url.path},
    )

# ─── Include Routers ──────────────────────────────────────────────────────────
app.include_router(auth_router,        prefix=f"{API_V1_STR}/auth",        tags=["Auth"])
app.include_router(device_comm_router, prefix=f"{API_V1_STR}/device_comm", tags=["Device Comm"])
app.include_router(cloud_router,       prefix=f"{API_V1_STR}/cloud",       tags=["Cloud"])
# These already define /api/v1/... inside the files – include as-is
app.include_router(payments_router)
app.include_router(subscriptions_router)
app.include_router(cameras_router,     prefix=f"{API_V1_STR}/cameras",     tags=["Cameras"])
app.include_router(admin_subscriptions_router, prefix=f"{API_V1_STR}")
app.include_router(admin_clips_router, prefix=f"{API_V1_STR}") 
# Routers without internal prefixes – mount them under /api/v1
app.include_router(devices_router,     prefix=f"{API_V1_STR}/devices",     tags=["Devices"])
app.include_router(farms_router,       prefix=f"{API_V1_STR}/farms",       tags=["Farms"])
app.include_router(plants_router,      prefix=f"{API_V1_STR}/plants",      tags=["Plants"])
app.include_router(dosing_router,      prefix=f"{API_V1_STR}/dosing",      tags=["Dosing"])
app.include_router(config_router,      prefix=f"{API_V1_STR}/config",      tags=["Config"])
app.include_router(supply_chain_router, prefix=f"{API_V1_STR}/supply_chain", tags=["Supply Chain"])

# Admin routers already have /admin... inside; expose them under /api/v1
app.include_router(admin_router,        prefix=f"{API_V1_STR}")
app.include_router(admin_users_router,  prefix=f"{API_V1_STR}")
app.include_router(admin_plans_router,  prefix=f"{API_V1_STR}")

# Users router already carries /api/v1/users in the file; include as-is
app.include_router(users_router)
# ─── Run the App ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="localhost",
        port=int(os.getenv("PORT", 8000)),
        log_level=os.getenv("LOG_LEVEL", "info"),
        reload=os.getenv("DEBUG", "false").lower() == "true",
    )
