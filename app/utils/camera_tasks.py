# app/utils/camera_tasks.py
"""
Asynchronous post-processing pipeline for every incoming camera frame.

Responsibilities
────────────────
1. **Run YOLOv8** on each raw JPEG (thread-pooled, non-blocking for the event
   loop) and draw bounding boxes on a cleaned version of the frame.
2. **Crop every “leaf” detection** and hand the crop to the (external)
   Plant-Village disease classifier *without* blocking the main path.
3. **Append the frame to a rolling MP4 clip** (one clip ≈ CLIP_DURATION) using
   a per-camera `cv2.VideoWriter`.  Clips older than `RETENTION_DAYS` are
   purged automatically.
4. **Maintain camera stats** (`frames_received`, `clips_count`, `storage_used`)
   and `detection_records` in the database – all inside a single fast
   `async_session`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from sqlalchemy import func, select
from app.utils.detectors import get_detector
from app.core.config import (
    BOUNDARY,
    CAM_DETECTION_WORKERS,
    CLIPS_DIR,
    DATA_ROOT,
    FPS,
    OFFLINE_TIMEOUT,
    PROCESSED_DIR,
    RAW_DIR,
    RETENTION_DAYS,
    YOLO_MODEL_PATH,
)
from app.core.database import AsyncSessionLocal
from app.models import Camera, DetectionRecord
from app.utils.image_utils import clean_frame, is_day

# --------------------------------------------------------------------------- #
# Globals                                                                     #
# --------------------------------------------------------------------------- #
logger = logging.getLogger(__name__)
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")

_executor = asyncio.get_event_loop().run_in_executor
_detector = get_detector()

# share the clip-writer dictionaries used by routers.cameras
from app.routers.cameras import _clip_writers, _clip_locks, CLIP_DURATION  # noqa

FOURCC = cv2.VideoWriter_fourcc(*"mp4v")


# --------------------------------------------------------------------------- #
# Internal helpers                                                            #
# --------------------------------------------------------------------------- #
def _ensure_dirs(cam_id: str) -> tuple[Path, Path, Path]:
    """
    Guarantee that RAW, PROCESSED and CLIPS dirs exist.
    Returns (raw_dir, processed_dir, clips_dir).
    """
    base = Path(DATA_ROOT) / cam_id
    raw = base / RAW_DIR
    proc = base / PROCESSED_DIR
    clips = base / CLIPS_DIR
    for p in (raw, proc, clips):
        p.mkdir(parents=True, exist_ok=True)
    return raw, proc, clips


def _annotate(img: np.ndarray) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """Delegate to configured detector (YOLO in prod; stub in CI)."""
    return _detector.detect_and_annotate(img)

async def _call_disease_model(crop_path: Path) -> None:
    """
    Fire-and-forget HTTP call to the Plant-Village disease classifier.
    """
    try:
        # TODO: replace with real call, e.g. httpx.post("http://pv/api", files=…)
        ...
    except Exception as exc:
        logger.warning("Plant-Village request failed for %s – %s", crop_path, exc)


async def _update_camera_stats(
    sess: Any, cam: Camera, added_bytes: int = 0, new_clip: bool = False
) -> None:
    cam.frames_received = (cam.frames_received or 0) + 1
    if new_clip:
        cam.clips_count = (cam.clips_count or 0) + 1
        cam.last_clip_time = func.now()
    cam.storage_used = (cam.storage_used or 0.0) + added_bytes / 1024 ** 2
    cam.last_seen = func.now()
    await sess.commit()


# --------------------------------------------------------------------------- #
# Clip writer                                                                 #
# --------------------------------------------------------------------------- #
async def _write_to_clip(cam_id: str, frame: np.ndarray, clips_dir: Path) -> bool:
    """
    Append `frame` to the current mp4 clip (rotates every CLIP_DURATION).
    Returns True if a *new* clip was started.
    """
    lock = _clip_locks.setdefault(cam_id, asyncio.Lock())
    now = datetime.now(timezone.utc)
    async with lock:
        writer_info = _clip_writers.get(cam_id)
        rotate = False

        if not writer_info:
            rotate = True
        else:
            started: datetime = writer_info["start"]
            if (now - started) >= CLIP_DURATION:
                writer_info["writer"].release()
                rotate = True

        if rotate:
            out_path = clips_dir / f"{int(now.timestamp() * 1000)}.mp4"
            h, w, _ = frame.shape
            writer = cv2.VideoWriter(str(out_path), FOURCC, FPS, (w, h))
            _clip_writers[cam_id] = {"writer": writer, "start": now}

        _clip_writers[cam_id]["writer"].write(frame)
        return rotate


def _purge_old_clips(clips_dir: Path) -> None:
    """
    Delete clips older than RETENTION_DAYS.
    """
    if RETENTION_DAYS <= 0:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    for mp4 in clips_dir.glob("*.mp4"):
        ts = datetime.fromtimestamp(int(mp4.stem) / 1000, timezone.utc)
        if ts < cutoff:
            try:
                mp4.unlink()
            except Exception:
                logger.warning("Failed to delete old clip %s", mp4)


# --------------------------------------------------------------------------- #
# Public entry-point                                                          #
# --------------------------------------------------------------------------- #
async def encode_and_cleanup(cam_id: str) -> None:
    """
    Process every raw JPEG for `cam_id` *once*.
    """
    raw_dir, proc_dir, clips_dir = _ensure_dirs(cam_id)
    raw_files = sorted(raw_dir.glob("*.jpg"))
    if not raw_files:
        return

    async with AsyncSessionLocal() as sess:
        cam = await sess.get(Camera, cam_id)

        for raw_path in raw_files:
            try:
                img = cv2.imread(str(raw_path))
                if img is None:
                    raw_path.unlink(missing_ok=True)
                    continue

                # 1) pre-clean
                cleaned = clean_frame(img, is_day(img))

                # 2) YOLO
                loop = asyncio.get_running_loop()
                annotated, detections = await loop.run_in_executor(
                    None, _annotate, cleaned.copy()
                )

                # 3) store annotated frame
                proc_path = proc_dir / f"{raw_path.stem}_processed.jpg"
                cv2.imwrite(str(proc_path), annotated)

                # 4) leaf crops
                for det in detections:
                    if det["name"].lower() == "leaf":
                        x1, y1, x2, y2 = det["bbox"]
                        crop = cleaned[y1:y2, x1:x2]
                        leaf_dir = proc_dir / "leaf"
                        leaf_dir.mkdir(exist_ok=True)
                        crop_path = leaf_dir / f"{raw_path.stem}_leaf.jpg"
                        cv2.imwrite(str(crop_path), crop)
                        asyncio.create_task(_call_disease_model(crop_path))

                # 5) append to clip
                new_clip = await _write_to_clip(cam_id, cleaned, clips_dir)

                # 6) update stats
                if cam:
                    added = raw_path.stat().st_size + proc_path.stat().st_size
                    await _update_camera_stats(sess, cam, added_bytes=added, new_clip=new_clip)

                # 7) detection records
                if detections:
                    for det in detections:
                        record = DetectionRecord(
                            camera_id=cam_id,
                            object_name=det["name"],
                            timestamp=datetime.now(timezone.utc),
                        )
                        await sess.merge(record)
                    await sess.commit()

            except Exception:
                logger.exception("Processing error (%s)", raw_path)
            finally:
                raw_path.unlink(missing_ok=True)

        _purge_old_clips(clips_dir)


# --------------------------------------------------------------------------- #
# Camera offline/online watcher (unchanged API)                               #
# --------------------------------------------------------------------------- #
async def offline_watcher(db_factory, interval_seconds: float = 30.0):
    """
    Periodically mark cameras as online/offline based on `last_seen`.
    """
    logger.info("Camera offline-watcher running every %.0fs", interval_seconds)
    while True:
        await asyncio.sleep(interval_seconds)
        now = datetime.now(timezone.utc)
        async with db_factory() as sess:
            rows = await sess.execute(select(Camera))
            for cam in rows.scalars().all():
                last = cam.last_seen or datetime(1970, 1, 1, tzinfo=timezone.utc)
                online = (now - last).total_seconds() <= OFFLINE_TIMEOUT
                if cam.is_online != online:
                    cam.is_online = online
                    logger.info("Camera %s online=%s", cam.id, online)
            await sess.commit()
