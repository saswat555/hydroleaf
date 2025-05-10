"""
Centralised runtime configuration.

• Reads a single `.env` file at project root (already loaded by dotenv).
• Fails fast if a mandatory variable is missing (e.g. DATABASE_URL).
• Casts booleans & integers safely.
• Exposes helpers for feature flags.
"""
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# 1.  Load .env early – variables already in the environment win               #
# --------------------------------------------------------------------------- #
ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=ENV_FILE, override=False)

# --------------------------------------------------------------------------- #
# 2.  Core settings                                                            #
# --------------------------------------------------------------------------- #
def _get_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}

def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:      # pragma: no cover – bad env var
        return default

ENVIRONMENT      = os.getenv("ENVIRONMENT", "production").lower()
DEBUG            = _get_bool("DEBUG", ENVIRONMENT != "production")
TESTING          = _get_bool("TESTING")
DEPLOYMENT_MODE  = os.getenv("DEPLOYMENT_MODE", "LAN").upper()            # LAN / CLOUD
ALLOWED_ORIGINS  = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]
HLS_TARGET_DURATION       = _get_int("HLS_TARGET_DURATION", 4)
HLS_PLAYLIST_LENGTH       = _get_int("HLS_PLAYLIST_LENGTH", 6)
# --------------------------------------------------------------------------- #
# 3.  Database                                                                 #
# --------------------------------------------------------------------------- #
if TESTING:
    DATABASE_URL = os.getenv("TEST_DATABASE_URL")
    if not DATABASE_URL:   # in CI you *must* supply a test DB
        raise RuntimeError("TEST_DATABASE_URL must be set when TESTING=1")
else:
    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not configured. "
            "Example: postgresql+asyncpg://user:pass@host:5432/dbname"
        )

# optional pool tuning
DB_POOL_SIZE      = _get_int("DB_POOL_SIZE", 20)
DB_MAX_OVERFLOW   = _get_int("DB_MAX_OVERFLOW", 20)

# --------------------------------------------------------------------------- #
# 4.  Misc feature flags / paths                                              #
# --------------------------------------------------------------------------- #
DATA_ROOT                 = os.getenv("CAM_DATA_ROOT", "./data")
RAW_DIR                   = os.getenv("CAM_RAW_DIR", "raw")
CLIPS_DIR                 = os.getenv("CAM_CLIPS_DIR", "clips")
PROCESSED_DIR             = os.getenv("CAM_PROCESSED_DIR", "processed")
RETENTION_DAYS            = _get_int("CAM_RETENTION_DAYS", 1)
OFFLINE_TIMEOUT           = _get_int("CAM_OFFLINE_TIMEOUT", 45)
BOUNDARY                  = os.getenv("CAM_BOUNDARY", "frame")
YOLO_MODEL_PATH           = os.getenv("YOLO_MODEL_PATH", "yolov5s.pt")
CAM_DETECTION_WORKERS     = _get_int("CAM_DETECTION_WORKERS", 4)
CAM_EVENT_GAP_SECONDS     = _get_int("CAM_EVENT_GAP_SECONDS", 2)
DETECTORS         = os.getenv("DETECTORS", "ssd,yolo").split(",")
API_V1_STR   = os.getenv("API_V1_STR", "/api/v1")
PROJECT_NAME = os.getenv("PROJECT_NAME", "Hydroleaf")
SESSION_KEY  = os.getenv("SESSION_KEY", "Hydroleaf_session")

# Ollama / LLM
USE_OLLAMA   = _get_bool("USE_OLLAMA", True)
OLLAMA_HOST  = os.getenv("OLLAMA_HOST", "http://localhost:11434")
MODEL_NAME_1_5B = os.getenv("MODEL_NAME_1_5B", "deepseek-r1:1.5b")
MODEL_NAME_7B   = os.getenv("MODEL_NAME_7B", "gemma")

# third-party keys
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")

# secrets
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is required for JWT / session signing")
RESET_DB = _get_bool("RESET_DB", False)

