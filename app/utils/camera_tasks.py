# app/utils/camera_tasks.py

import os
import asyncio
import logging
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
from sqlalchemy import update
from sqlalchemy.future import select as future_select
from ultralytics import YOLO

from app.models import Camera
from app.core.config import (
    DATA_ROOT,
    RAW_DIR,
    CLIPS_DIR,
    RETENTION_DAYS,
    HLS_TARGET_DURATION,
    HLS_PLAYLIST_LENGTH,
    OFFLINE_TIMEOUT,
    FPS,
)
from app.core.database import AsyncSessionLocal
from app.utils.image_utils import clean_frame, is_day

logger = logging.getLogger(__name__)

# Suppress OpenCV internal logs
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")

# Clip roll-over settings
CLIP_DURATION = timedelta(minutes=10)
AUTO_CLOSE_DELAY = timedelta(minutes=15)

# Thread pool for YOLO inference
_executor = ThreadPoolExecutor(max_workers=4)
_writers = {}  # cam_id -> {writer, start, path}
_locks = {}    # cam_id -> asyncio.Lock()

# Load YOLO model once
try:
    _model = YOLO(str(Path("models") / "yolov5s.pt"))
    _labels = _model.names
    _detection_enabled = True
    logger.info("YOLO model loaded successfully")
except Exception as e:
    _model = None
    _labels = {}
    _detection_enabled = False
    logger.warning(f"YOLO init failed: {e}")


def _ensure_dirs(cam_id: str):
    base = Path(DATA_ROOT) / cam_id
    for sub in (RAW_DIR, CLIPS_DIR, "hls"):
        (base / sub).mkdir(parents=True, exist_ok=True)


def _open_writer(cam_id: str, size: tuple[int, int], start: datetime):
    clips_dir = Path(DATA_ROOT) / cam_id / CLIPS_DIR
    ts = int(start.timestamp() * 1000)
    # write with MP4 container (mp4v)
    out_path = clips_dir / f"{ts}.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, FPS, size)
    if not writer.isOpened():
        logger.error(f"Failed to open MP4 VideoWriter for {out_path}")
        return
    _writers[cam_id] = {"writer": writer, "start": start, "path": out_path}
    logger.info(f"Started new clip {out_path.name}")


def _close_writer(cam_id: str):
    info = _writers.pop(cam_id, None)
    if not info:
        return
    info["writer"].release()
    raw_path = info["path"]
    logger.info(f"Closed clip {raw_path.name}")
    # segment to HLS and generate CV version asynchronously
    asyncio.create_task(_segment_hls(raw_path))
    asyncio.create_task(_generate_cv_version(raw_path))


async def _segment_hls(raw_path: Path):
    cam_id = raw_path.parent.parent.name
    hls_dir = Path(DATA_ROOT) / cam_id / "hls"
    hls_dir.mkdir(parents=True, exist_ok=True)
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg not found in PATH")
        return
    cmd = [
        "ffmpeg", "-y", "-i", str(raw_path),
        "-c", "copy",
        "-f", "hls",
        "-hls_time", str(HLS_TARGET_DURATION),
        "-hls_list_size", str(HLS_PLAYLIST_LENGTH),
        "-hls_flags", "delete_segments",
        str(hls_dir / "index.m3u8"),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info(f"HLS segmented {raw_path.name}")
    except subprocess.CalledProcessError as e:
        logger.error(f"HLS segmentation failed for {raw_path.name}: {e}")


async def _generate_cv_version(raw_path: Path):
    await asyncio.sleep(60)
    stem = raw_path.stem
    cv_path = raw_path.parent / f"{stem}_cv.mp4"
    cap = cv2.VideoCapture(str(raw_path))
    if not cap.isOpened():
        logger.error(f"Cannot open raw clip for CV: {raw_path}")
        return
    fps = cap.get(cv2.CAP_PROP_FPS) or FPS
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(str(cv_path), fourcc, fps, (w, h))
    loop = asyncio.get_running_loop()
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        cleaned = clean_frame(frame, is_day(frame))
        if _detection_enabled:
            cleaned = await loop.run_in_executor(_executor, _detect_sync, cleaned)
        vw.write(cleaned)
    cap.release()
    vw.release()
    logger.info(f"Generated CV clip {cv_path.name}")


def _detect_sync(img):
    res = _model(img, imgsz=640, conf=0.4, verbose=False)[0]
    for box, conf, cls in zip(res.boxes.xyxy, res.boxes.conf, res.boxes.cls):
        x1, y1, x2, y2 = map(int, box.tolist())
        label = f"{_labels[int(cls)]}:{conf:.2f}"
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(img, label, (x1, y1 - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    return img


async def encode_and_cleanup(cam_id: str):
    lock = _locks.setdefault(cam_id, asyncio.Lock())
    if lock.locked():
        return

    async with lock:
        base = Path(DATA_ROOT) / cam_id
        raw_dir = base / RAW_DIR
        _ensure_dirs(cam_id)

        # Gather all raw frames
        frames = sorted(raw_dir.rglob("*.jpg"), key=lambda p: int(p.stem))
        if not frames:
            return

        # Determine clip start time
        ts0 = int(frames[0].stem)
        start = datetime.fromtimestamp(ts0 / 1000, timezone.utc)
        info = _writers.get(cam_id)

        # If no open writer or clip aged out, close and reopen
        if not info or (datetime.now(timezone.utc) - info["start"] >= AUTO_CLOSE_DELAY):
            _close_writer(cam_id)

            first = frames[0]
            if not first.exists():
                logger.warning("First raw frame missing, skipping clip start: %s", first)
                return

            try:
                raw_bytes = first.read_bytes()
                arr = np.frombuffer(raw_bytes, dtype=np.uint8)
                img0 = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img0 is None:
                    raise ValueError("cv2.imdecode returned None")
                size = (img0.shape[1], img0.shape[0])
                _open_writer(cam_id, size, start)
                info = _writers.get(cam_id)
                if not info:
                    return
            except Exception as e:
                logger.exception("Error opening writer for %s: %s", first, e)
                first.unlink(missing_ok=True)
                return

        vw = info["writer"]

        # Append each frame to the writer
        for f in frames:
            try:
                if not f.exists():
                    logger.warning("Raw frame disappeared, skipping: %s", f)
                    continue
                raw_bytes = f.read_bytes()
                arr = np.frombuffer(raw_bytes, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                f.unlink(missing_ok=True)
                if img is None:
                    logger.warning("Could not decode frame, skipping: %s", f)
                    continue

                vw.write(img)

                # Roll clip if duration exceeded
                tsf = datetime.fromtimestamp(int(f.stem) / 1000, timezone.utc)
                if tsf - info["start"] >= CLIP_DURATION:
                    _close_writer(cam_id)
                    # open next clip
                    size = (img.shape[1], img.shape[0])
                    _open_writer(cam_id, size, tsf)
                    info = _writers.get(cam_id)
                    vw = info["writer"]
            except Exception as e:
                logger.exception("Error processing frame %s: %s", f, e)
                f.unlink(missing_ok=True)

        # Update HLS path in the DB
        try:
            async with AsyncSessionLocal() as sess:
                await sess.execute(
                    update(Camera)
                    .where(Camera.id == cam_id)
                    .values(hls_path=f"hls/{cam_id}/index.m3u8")
                )
                await sess.commit()
        except Exception:
            logger.exception("Failed to update HLS path for camera %s", cam_id)

        # Prune old MP4 clips
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        for clip in (base / CLIPS_DIR).glob("*.mp4"):
            try:
                if datetime.fromtimestamp(clip.stat().st_mtime, timezone.utc) < cutoff:
                    clip.unlink(missing_ok=True)
                    logger.info("Pruned old clip %s", clip.name)
            except Exception as e:
                logger.warning("Could not prune clip %s: %s", clip, e)

async def offline_watcher(db_factory, interval_seconds: float = 30.0):
    logger.info(f"Offline watcher every {interval_seconds}s")
    while True:
        await asyncio.sleep(interval_seconds)
        now = datetime.now(timezone.utc)
        async with db_factory() as sess:
            result = await sess.execute(future_select(Camera))
            for cam in result.scalars().all():
                last = cam.last_seen or datetime(1970, 1, 1, tzinfo=timezone.utc)
                online = (now - last).total_seconds() <= OFFLINE_TIMEOUT
                if cam.is_online != online:
                    cam.is_online = online
                    logger.info(f"{cam.id} online={online}")
            await sess.commit()
