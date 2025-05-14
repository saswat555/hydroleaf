# app/utils/camera_tasks.py

import os
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import cv2
from ultralytics import YOLO

from app.core.config import (
    DATA_ROOT,
    RAW_DIR,
    PROCESSED_DIR,
    OFFLINE_TIMEOUT,
    CAM_DETECTION_WORKERS,
    YOLO_MODEL_PATH,
)
from app.core.database import AsyncSessionLocal
from app.models import Camera
from app.utils.image_utils import clean_frame, is_day

logger = logging.getLogger(__name__)

# Suppress OpenCV internal logs
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")

# Thread pool for YOLO inference
_executor = ThreadPoolExecutor(max_workers=CAM_DETECTION_WORKERS)

# Load YOLO model once
try:
    _model = YOLO(YOLO_MODEL_PATH)
    _labels = _model.names
    logger.info("YOLO model loaded successfully")
except Exception as e:
    logger.error(f"Failed to load YOLO model from {YOLO_MODEL_PATH}: {e}")
    raise

def _ensure_dirs(cam_id: str):
    """
    Guarantee that both RAW_DIR and PROCESSED_DIR exist for this camera.
    """
    base = Path(DATA_ROOT) / cam_id
    for sub in (RAW_DIR, PROCESSED_DIR):
        (base / sub).mkdir(parents=True, exist_ok=True)

def _annotate(img):
    """
    Draw YOLO boxes & labels directly onto `img`.
    """
    res = _model(img, imgsz=640, conf=0.4, verbose=False)[0]
    for box, conf, cls in zip(res.boxes.xyxy, res.boxes.conf, res.boxes.cls):
        x1, y1, x2, y2 = map(int, box.tolist())
        label = f"{_labels[int(cls)]}:{conf:.2f}"
        cv2.rectangle(img, (x1, y1), (x2, y2), (0,255,0), 2)
        cv2.putText(img, label, (x1, y1-6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,0), 1)
    return img

async def encode_and_cleanup(cam_id: str):
    """
    For every raw .jpg in RAW_DIR:
      1) load → clean_frame(day/night) → YOLO annotate
      2) save to PROCESSED_DIR as `<ts>_processed.jpg`
      3) delete original
    """
    base    = Path(DATA_ROOT) / cam_id
    raw_dir = base / RAW_DIR
    proc_dir= base / PROCESSED_DIR
    _ensure_dirs(cam_id)

    for raw_path in sorted(raw_dir.glob("*.jpg")):
        try:
            img = cv2.imread(str(raw_path))
            if img is None:
                raw_path.unlink(missing_ok=True)
                continue

            # 1) cleanup
            cleaned = clean_frame(img, is_day(img))

            # 2) annotate (offload to thread)
            loop = asyncio.get_running_loop()
            annotated = await loop.run_in_executor(_executor, _annotate, cleaned)

            # 3) write processed
            out_file = proc_dir / f"{raw_path.stem}_processed.jpg"
            cv2.imwrite(str(out_file), annotated)

        except Exception as e:
            logger.exception(f"[camera_tasks] error processing {raw_path}: {e}")

        finally:
            # remove raw whether success or not
            raw_path.unlink(missing_ok=True)

async def offline_watcher(db_factory, interval_seconds: float = 30.0):
    """
    Periodically mark cameras as online/offline in the DB.
    """
    logger.info(f"Offline watcher every {interval_seconds}s")
    while True:
        await asyncio.sleep(interval_seconds)
        now = datetime.now(timezone.utc)
        async with db_factory() as sess:
            result = await sess.execute(
                __import__("sqlalchemy").future.select(Camera)
            )
            for cam in result.scalars().all():
                last = cam.last_seen or datetime(1970,1,1, tzinfo=timezone.utc)
                online = (now - last).total_seconds() <= OFFLINE_TIMEOUT
                if cam.is_online != online:
                    cam.is_online = online
                    logger.info(f"{cam.id} online={online}")
            await sess.commit()
