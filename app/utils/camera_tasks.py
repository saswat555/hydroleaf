import asyncio
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
from sqlalchemy import update
from sqlalchemy.future import select

from app.models import Camera
from app.core.config import (
    DATA_ROOT,
    RAW_DIR,
    CLIPS_DIR,
    RETENTION_DAYS,
    HLS_TARGET_DURATION,
    HLS_PLAYLIST_LENGTH,
    OFFLINE_TIMEOUT,
)
from app.core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

# ───────── SETTINGS ─────────
CLIP_DURATION = timedelta(minutes=10)
FPS = 20

# ───────── STATE ─────────
_writers: dict[str, dict]        = {}
_locks: dict[str, asyncio.Lock]  = {}

# ───────── YOLO MODEL ─────────
_MODEL_PATH = Path("models/yolov5s.onnx")
_NAMES_PATH = Path("models/coco.names")

_net = cv2.dnn.readNet(str(_MODEL_PATH))
_net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
_net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)

with open(_NAMES_PATH) as f:
    _LABELS = [l.strip() for l in f if l.strip()]


def _ensure_dirs(cam_id: str):
    base = Path(DATA_ROOT) / cam_id
    for sub in (RAW_DIR, CLIPS_DIR, "hls"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    # day/night folders
    (base / RAW_DIR / "day").mkdir(parents=True, exist_ok=True)
    (base / RAW_DIR / "night").mkdir(parents=True, exist_ok=True)


def _start_writer(cam_id: str, size: tuple[int,int], start_ts: datetime):
    clips_dir = Path(DATA_ROOT) / cam_id / CLIPS_DIR
    ts_ms = int(start_ts.timestamp() * 1000)
    path = clips_dir / f"{ts_ms}.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(str(path), fourcc, FPS, size)
    _writers[cam_id] = {"writer": vw, "start": start_ts, "path": path}
    logger.info(f"[Encoder] Started {path.name} for camera {cam_id}")


def _close_writer(cam_id: str):
    info = _writers.pop(cam_id, None)
    if not info:
        return
    info["writer"].release()
    path = info["path"]
    logger.info(f"[Encoder] Closed {path.name} for camera {cam_id}")

    hls_dir = path.parent.parent / "hls"
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", str(path),
            "-c", "copy",
            "-f", "hls",
            "-hls_time", str(HLS_TARGET_DURATION),
            "-hls_list_size", str(HLS_PLAYLIST_LENGTH),
            "-hls_flags", "delete_segments",
            str(hls_dir / "index.m3u8"),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info(f"[HLS] Segmented {path.name}")
    except Exception as e:
        logger.error(f"[HLS] Segmentation failed for {cam_id}: {e}")


def _detect_and_draw(img: np.ndarray) -> np.ndarray:
    """Run YOLO on the frame and draw boxes+labels."""
    h, w = img.shape[:2]
    blob = cv2.dnn.blobFromImage(img, 1/255, (640, 640), swapRB=True, crop=False)
    _net.setInput(blob)
    preds = _net.forward()[0]  # shape: Nx85 for YOLOv5

    boxes, confidences, classIDs = [], [], []
    for *xywh, conf, cls in preds:
        if conf > 0.4:
            cx, cy, bw, bh = xywh
            x = int((cx - bw/2) * w)
            y = int((cy - bh/2) * h)
            bw = int(bw * w)
            bh = int(bh * h)
            boxes.append([x, y, bw, bh])
            confidences.append(float(conf))
            classIDs.append(int(cls))

    idxs = cv2.dnn.NMSBoxes(boxes, confidences, 0.4, 0.5)
    if len(idxs):
        for i in idxs.flatten():
            x, y, bw, bh = boxes[i]
            label = f"{_LABELS[classIDs[i]]}:{confidences[i]:.2f}"
            cv2.rectangle(img, (x, y), (x + bw, y + bh), (0, 255, 0), 2)
            cv2.putText(img, label, (x, y - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    return img


async def encode_and_cleanup(cam_id: str):
    """
    1) Gather all raw frames (day/night),
    2) YOLO-detect & write to a rolling MP4 (10 min clips),
    3) Segment to HLS, update DB, and prune old clips.
    """
    lock = _locks.setdefault(cam_id, asyncio.Lock())
    if lock.locked():
        return

    async with lock:
        _ensure_dirs(cam_id)
        base = Path(DATA_ROOT) / cam_id / RAW_DIR
        day_dir = base / "day"
        night_dir = base / "night"

        # Collect (timestamp, path)
        frames: list[tuple[int, Path]] = []
        for d in (day_dir, night_dir):
            if not d.exists():
                continue
            for f in d.glob("*.jpg"):
                try:
                    ts = int(f.stem)
                    frames.append((ts, f))
                except ValueError:
                    continue

        frames.sort(key=lambda x: x[0])
        if not frames:
            return

        # Initialize writer if needed
        ts0, fp0 = frames[0]
        dt0 = datetime.fromtimestamp(ts0 / 1000, timezone.utc)
        img0 = cv2.imread(str(fp0))
        if img0 is None:
            fp0.unlink(missing_ok=True)
            return
        size = (img0.shape[1], img0.shape[0])

        if cam_id not in _writers:
            _start_writer(cam_id, size, dt0)

        info = _writers[cam_id]
        vw = info["writer"]
        clip_start = info["start"]

        # Process & write each frame
        for ts, fp in frames:
            dt = datetime.fromtimestamp(ts / 1000, timezone.utc)
            if dt - clip_start >= CLIP_DURATION:
                _close_writer(cam_id)
                _start_writer(cam_id, size, dt)
                info = _writers[cam_id]
                vw = info["writer"]
                clip_start = info["start"]

            img = cv2.imread(str(fp))
            if img is not None:
                det = _detect_and_draw(img)
                vw.write(det)
            fp.unlink(missing_ok=True)

        # Update hls_path in DB
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(Camera)
                .where(Camera.id == cam_id)
                .values(hls_path=f"hls/{cam_id}/index.m3u8")
            )
            await session.commit()

        # Prune old clips
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        clips_dir = Path(DATA_ROOT) / cam_id / CLIPS_DIR
        for c in clips_dir.glob("*.mp4"):
            if datetime.fromtimestamp(c.stat().st_mtime, timezone.utc) < cutoff:
                c.unlink(missing_ok=True)


async def offline_watcher(db_factory, interval_seconds: float = 30.0):
    """
    Every `interval_seconds`, mark cameras online/offline based on last_seen.
    """
    logger.info(f"Starting offline watcher every {interval_seconds}s")
    while True:
        await asyncio.sleep(interval_seconds)
        now = datetime.now(timezone.utc)
        async with db_factory() as session:
            result = await session.execute(select(Camera))
            cams = result.scalars().all()
            for cam in cams:
                last = cam.last_seen or datetime(1970,1,1, tzinfo=timezone.utc)
                online = (now - last).total_seconds() <= OFFLINE_TIMEOUT
                if cam.is_online != online:
                    cam.is_online = online
                    logger.info(f"Camera {cam.id} online={online}")
            await session.commit()
