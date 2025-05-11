import asyncio
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
from sqlalchemy import update
from sqlalchemy.future import select

from ultralytics import YOLO
import requests

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
_writers: dict[str, dict]       = {}
_locks: dict[str, asyncio.Lock] = {}

# ───────── MODEL CONFIG ─────────
_MODEL_DIR     = Path("models")
_WEIGHTS_PATH  = _MODEL_DIR / "yolov5s.pt"
_WEIGHTS_URL   = "https://github.com/ultralytics/yolov5/releases/download/v6.0/yolov5s.pt"

model: YOLO | None = None
_LABELS: dict[int,str] = {}
_detection_enabled = False

def _download_file(url: str, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"[Model] Downloading {url} → {dest}")
    resp = requests.get(url, stream=True)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(1024*1024):
            f.write(chunk)
    logger.info(f"[Model] Download complete: {dest.name}")

def _init_model():
    global model, _LABELS, _detection_enabled
    try:
        if not _WEIGHTS_PATH.exists():
            _download_file(_WEIGHTS_URL, _WEIGHTS_PATH)

        model = YOLO(str(_WEIGHTS_PATH))
        _LABELS = model.names  # dict idx→name
        _detection_enabled = True
        logger.info("[Model] ultralytics YOLOv5s loaded, detection enabled")
    except Exception as e:
        logger.warning(f"❌ Failed to initialize YOLO model ({e}), detection disabled")
        model = None
        _detection_enabled = False

# initialize on import
_init_model()


def _ensure_dirs(cam_id: str):
    base = Path(DATA_ROOT) / cam_id
    for sub in (RAW_DIR, CLIPS_DIR, "hls"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    (base / RAW_DIR / "day").mkdir(parents=True, exist_ok=True)
    (base / RAW_DIR / "night").mkdir(parents=True, exist_ok=True)


def _start_writer(cam_id: str, size: tuple[int,int], start_ts: datetime):
    clips_dir = Path(DATA_ROOT) / cam_id / CLIPS_DIR
    ts_ms = int(start_ts.timestamp() * 1000)
    path = clips_dir / f"{ts_ms}.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(str(path), fourcc, FPS, size)
    _writers[cam_id] = {"writer": vw, "start": start_ts, "path": path}
    logger.info(f"[Encoder] Started clip {path.name} for camera {cam_id}")


def _close_writer(cam_id: str):
    info = _writers.pop(cam_id, None)
    if not info:
        return
    info["writer"].release()
    path = info["path"]
    logger.info(f"[Encoder] Closed clip {path.name} for camera {cam_id}")

    # HLS segmentation
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
    """
    Run ultralytics YOLO model on img and overlay boxes+labels.
    """
    if not _detection_enabled or model is None:
        return img

    results = model(img, imgsz=640, conf=0.4, verbose=False)[0]
    # results.boxes.xyxy, results.boxes.conf, results.boxes.cls
    for box, conf, cls in zip(results.boxes.xyxy, results.boxes.conf, results.boxes.cls):
        x1, y1, x2, y2 = map(int, box.tolist())
        label = f"{_LABELS[int(cls)]}:{float(conf):.2f}"
        cv2.rectangle(img, (x1, y1), (x2, y2), (0,255,0), 2)
        cv2.putText(img, label, (x1, y1-6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,0), 1)
    return img


async def encode_and_cleanup(cam_id: str):
    """
    1) Read raw frames (day/night) for cam_id
    2) Draw YOLO detections, append to 10-min MP4 at 20 FPS
    3) Segment to HLS, update DB, prune old clips
    """
    lock = _locks.setdefault(cam_id, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        _ensure_dirs(cam_id)
        raw_base = Path(DATA_ROOT) / cam_id / RAW_DIR
        dirs = [raw_base / "day", raw_base / "night"]

        # collect and sort
        frames: list[tuple[int,Path]] = []
        for d in dirs:
            if not d.exists(): continue
            for f in d.glob("*.jpg"):
                try:
                    ts = int(f.stem)
                    frames.append((ts, f))
                except:
                    continue
        frames.sort(key=lambda x: x[0])
        if not frames:
            return

        # init writer
        ts0, fp0 = frames[0]
        dt0 = datetime.fromtimestamp(ts0/1000, timezone.utc)
        img0 = cv2.imread(str(fp0))
        if img0 is None:
            fp0.unlink(missing_ok=True)
            return
        size = (img0.shape[1], img0.shape[0])
        if cam_id not in _writers:
            _start_writer(cam_id, size, dt0)

        info       = _writers[cam_id]
        vw         = info["writer"]
        clip_start = info["start"]

        # process each
        for ts, fp in frames:
            dt = datetime.fromtimestamp(ts/1000, timezone.utc)
            if dt - clip_start >= CLIP_DURATION:
                _close_writer(cam_id)
                _start_writer(cam_id, size, dt)
                info       = _writers[cam_id]
                vw         = info["writer"]
                clip_start = info["start"]

            img = cv2.imread(str(fp))
            if img is not None:
                out = _detect_and_draw(img)
                vw.write(out)
            fp.unlink(missing_ok=True)

        # update HLS path in DB
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(Camera)
                .where(Camera.id == cam_id)
                .values(hls_path=f"hls/{cam_id}/index.m3u8")
            )
            await session.commit()

        # prune old
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        clips_dir = Path(DATA_ROOT) / cam_id / CLIPS_DIR
        for c in clips_dir.glob("*.mp4"):
            if datetime.fromtimestamp(c.stat().st_mtime, timezone.utc) < cutoff:
                c.unlink(missing_ok=True)


async def offline_watcher(db_factory, interval_seconds: float = 30.0):
    """
    Periodically mark cameras online/offline based on last_seen.
    """
    logger.info(f"Starting offline watcher every {interval_seconds}s")
    while True:
        await asyncio.sleep(interval_seconds)
        now = datetime.now(timezone.utc)
        async with db_factory() as session:
            result = await session.execute(select(Camera))
            cams = result.scalars().all()
            for cam in cams:
                last   = cam.last_seen or datetime(1970,1,1,tzinfo=timezone.utc)
                online = (now - last).total_seconds() <= OFFLINE_TIMEOUT
                if cam.is_online != online:
                    cam.is_online = online
                    logger.info(f"Camera {cam.id} online={online}")
            await session.commit()
