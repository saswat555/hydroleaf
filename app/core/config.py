"""
Hydroleaf – centralised runtime configuration.

• Reads a single “.env” at project‑root (already loaded by python‑dotenv).
• Fails fast when a mandatory variable (DATABASE_URL, SECRET_KEY …) is missing.
• All helpers (_get_bool/_get_int) are safe against bad input.
• Every setting that other modules import is declared **once** here.
"""
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

# ──────────────────────────────────────────────
# 1.  Early .env loading – vars already in the
#     environment always win over .env values.
# ──────────────────────────────────────────────
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(dotenv_path=ENV_FILE, override=False)

# ──────────────────────────────────────────────
# 2.  Helpers
# ──────────────────────────────────────────────
def _get_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {
        "1", "true", "yes", "on"
    }


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# ──────────────────────────────────────────────
# 3.  Environment basics
# ──────────────────────────────────────────────
ENVIRONMENT   = os.getenv("ENVIRONMENT", "production").lower()
DEBUG         = _get_bool("DEBUG", ENVIRONMENT != "production")
TESTING       = _get_bool("TESTING")
DEPLOYMENT_MODE = os.getenv("DEPLOYMENT_MODE", "LAN").upper()           # LAN / CLOUD
RESET_DB      = _get_bool("RESET_DB")                                   # ← **used by app.main**
API_V1_STR    = os.getenv("API_V1_STR", "/api/v1")
PROJECT_NAME  = os.getenv("PROJECT_NAME", "Hydroleaf")
SESSION_KEY   = os.getenv("SESSION_KEY", "Hydroleaf_session")
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]

# ──────────────────────────────────────────────
# 4.  Database
# ──────────────────────────────────────────────
if TESTING:
    DATABASE_URL = os.getenv("TEST_DATABASE_URL")
    if not DATABASE_URL:
        raise RuntimeError("When TESTING=1 you must set TEST_DATABASE_URL")
else:
    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not configured "
            "(e.g. postgresql+asyncpg://user:pass@host:5432/dbname)"
        )

DB_POOL_SIZE    = _get_int("DB_POOL_SIZE", 20)
DB_MAX_OVERFLOW = _get_int("DB_MAX_OVERFLOW", 20)

# ──────────────────────────────────────────────
# 5.  Camera / HLS settings
# ──────────────────────────────────────────────
DATA_ROOT         = os.getenv("CAM_DATA_ROOT", "./data")
RAW_DIR           = os.getenv("CAM_RAW_DIR", "raw")
CLIPS_DIR         = os.getenv("CAM_CLIPS_DIR", "clips")
PROCESSED_DIR     = os.getenv("CAM_PROCESSED_DIR", "processed")
HLS_TARGET_DURATION = _get_int("HLS_TARGET_DURATION", 4)
HLS_PLAYLIST_LENGTH = _get_int("HLS_PLAYLIST_LENGTH", 6)
FPS               = _get_int("CAM_FPS", 15)
RETENTION_DAYS    = _get_int("CAM_RETENTION_DAYS", 1)
OFFLINE_TIMEOUT   = _get_int("CAM_OFFLINE_TIMEOUT", 45)
BOUNDARY          = os.getenv("CAM_BOUNDARY", "frame")
YOLO_MODEL_PATH   = os.getenv("YOLO_MODEL_PATH", "yolov5s.pt")
CAM_DETECTION_WORKERS = _get_int("CAM_DETECTION_WORKERS", 4)
CAM_EVENT_GAP_SECONDS  = _get_int("CAM_EVENT_GAP_SECONDS", 2)
DETECTORS         = [d.strip() for d in os.getenv("DETECTORS", "ssd,yolo").split(",")]

# ──────────────────────────────────────────────
# 6.  LLM / Ollama / OpenAI
# ──────────────────────────────────────────────
def _default_ollama_use() -> bool:
    explicit = os.getenv("USE_OLLAMA")
    if explicit is not None:
        return _get_bool("USE_OLLAMA")
    return TESTING   # fall back to Ollama in unit‑tests for speed

USE_OLLAMA     = _default_ollama_use()
# Both names exported – some modules expect one, others the other.
OLLAMA_HOST    = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_URL     = os.getenv("OLLAMA_URL", f"{OLLAMA_HOST.rstrip('/')}/api/generate")
MODEL_NAME_1_5B = os.getenv("MODEL_NAME_1_5B", "deepseek-r1:1.5b")
MODEL_NAME_7B   = os.getenv("MODEL_NAME_7B", "gemma")
GPT_MODEL       = os.getenv("GPT_MODEL", "gpt-3.5-turbo")

# ──────────────────────────────────────────────
# 7.  Third‑party API keys
# ──────────────────────────────────────────────
SERPER_API_KEY  = os.getenv("SERPER_API_KEY", "")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")

# ──────────────────────────────────────────────
# 8.  Secrets (JWT/signing)
# ──────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    if TESTING:
        SECRET_KEY = "hydroleaf‑test‑secret"
    else:
        raise RuntimeError("SECRET_KEY is required for JWT / session signing")

# ──────────────────────────────────────────────
# 9.  Public re‑exports (helps with `from config import *`)
# ──────────────────────────────────────────────
__all__ = [
    # env
    "ENVIRONMENT", "DEBUG", "TESTING", "DEPLOYMENT_MODE", "RESET_DB",
    # URLs / paths
    "API_V1_STR", "PROJECT_NAME", "SESSION_KEY", "ALLOWED_ORIGINS",
    # DB
    "DATABASE_URL", "DB_POOL_SIZE", "DB_MAX_OVERFLOW",
    # Camera / HLS
    "DATA_ROOT", "RAW_DIR", "CLIPS_DIR", "PROCESSED_DIR",
    "HLS_TARGET_DURATION", "HLS_PLAYLIST_LENGTH", "FPS",
    "RETENTION_DAYS", "OFFLINE_TIMEOUT", "BOUNDARY",
    "YOLO_MODEL_PATH", "CAM_DETECTION_WORKERS", "CAM_EVENT_GAP_SECONDS",
    "DETECTORS",
    # LLM
    "USE_OLLAMA", "OLLAMA_HOST", "OLLAMA_URL",
    "MODEL_NAME_1_5B", "MODEL_NAME_7B", "GPT_MODEL",
    # APIs / keys
    "SERPER_API_KEY", "OPENAI_API_KEY",
    # secrets
    "SECRET_KEY",
]
