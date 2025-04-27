import os
from dotenv import load_dotenv

# Load any .env file
load_dotenv()

# ─── Environment ──────────────────────────────────────────────────────────────
ENVIRONMENT     = os.getenv("ENVIRONMENT", "development")
TESTING         = os.getenv("TESTING", "0") == "1"
DEPLOYMENT_MODE = os.getenv("DEPLOYMENT_MODE", "LAN").upper()  # "LAN" or "CLOUD"

# ─── CORS ─────────────────────────────────────────────────────────────────────
# comma‑separated or "*" for all
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

# ─── Database ─────────────────────────────────────────────────────────────────
# if TESTING and TEST_DATABASE_URL is set, use it; otherwise fallback to DATABASE_URL
DATABASE_URL      = os.getenv("TEST_DATABASE_URL") if TESTING else os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./Hydroleaf.db")

# ─── Camera storage & tasks ───────────────────────────────────────────────────
DATA_ROOT      = os.getenv("CAM_DATA_ROOT", "./data")
RAW_DIR        = os.getenv("CAM_RAW_DIR", "raw")
CLIPS_DIR      = os.getenv("CAM_CLIPS_DIR", "clips")
RETENTION_DAYS = int(os.getenv("CAM_RETENTION_DAYS", "2"))
OFFLINE_TIMEOUT= int(os.getenv("CAM_OFFLINE_TIMEOUT", "45"))
BOUNDARY       = os.getenv("CAM_BOUNDARY", "frame")
PROCESSED_DIR            = os.getenv("CAM_PROCESSED_DIR", "processed")
YOLO_MODEL_PATH          = os.getenv("YOLO_MODEL_PATH", "yolov5s.pt")    # path to weights
CAM_DETECTION_WORKERS    = int(os.getenv("CAM_DETECTION_WORKERS", "4"))
CAM_EVENT_GAP_SECONDS    = int(os.getenv("CAM_EVENT_GAP_SECONDS", "2"))  # gap to merge events

# ─── API ──────────────────────────────────────────────────────────────────────
API_V1_STR   = os.getenv("API_V1_STR", "/api/v1")
PROJECT_NAME = os.getenv("PROJECT_NAME", "Hydroleaf")
SESSION_KEY = os.getenv("SESSION_KEY", "Hydroleaf_session")