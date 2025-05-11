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
    FPS as CONFIGURED_FPS
)
from app.core.database import AsyncSessionLocal
from app.utils.image_utils import is_day, clean_frame

logger = logging.getLogger(__name__)

# ───────── SETTINGS ─────────
# roll clips every 30 seconds
CLIP_DURATION = timedelta(seconds=30)
# if writer stays open past 60 seconds, force close
AUTO_CLOSE_DURATION = timedelta(seconds=60)
FPS = CONFIGURED_FPS or 20

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
        _LABELS = model.names
        _detection_enabled = True
        logger.info("[Model] YOLOv5s loaded, detection enabled")
    except Exception as e:
        logger.warning(f"❌ YOLO init failed ({e}), detection disabled")
        model = None
        _detection_enabled = False

# initialize on import
_init_model()

def _ensure_dirs(cam_id: str):
    base = Path(DATA_ROOT) / cam_id
    for sub in (RAW_DIR, CLIPS_DIR, "hls", "processed"):
        (base / sub).mkdir(parents=True, exist_ok=True)

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
    writer = info['writer']
    writer.release()
    path = info['path']
    logger.info(f"[Encoder] Closed clip {path.name} for camera {cam_id}")

    # HLS segmentation
    hls_dir = Path(DATA_ROOT) / cam_id / "hls"
    hls_dir.mkdir(parents=True, exist_ok=True)
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
    if not _detection_enabled or model is None:
        return img
    results = model(img, imgsz=640, conf=0.4, verbose=False)[0]
    for box, conf, cls in zip(results.boxes.xyxy, results.boxes.conf, results.boxes.cls):
        x1, y1, x2, y2 = map(int, box.tolist())
        label = f"{_LABELS[int(cls)]}:{float(conf):.2f}"
        cv2.rectangle(img, (x1, y1), (x2, y2), (0,255,0), 2)
        cv2.putText(img, label, (x1, y1-6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,0), 1)
    return img

async def encode_and_cleanup(cam_id: str):
    lock = _locks.setdefault(cam_id, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        base = Path(DATA_ROOT) / cam_id
        raw_dir = base / RAW_DIR
        proc_dir = base / "processed"
        _ensure_dirs(cam_id)

        # collect and sort raw frames
        frames = []
        for f in raw_dir.glob("*.jpg"):
            try:
                ts = int(f.stem)
                frames.append((ts, f))
            except:
                continue
        frames.sort(key=lambda x: x[0])
        if not frames:
            return

        # initialize writer if needed
        ts0, fp0 = frames[0]
        dt0 = datetime.fromtimestamp(ts0/1000, timezone.utc)
        img0 = cv2.imread(str(fp0))
        if img0 is None:
            fp0.unlink(missing_ok=True)
            return
        size = (img0.shape[1], img0.shape[0])
        if cam_id not in _writers:
            _start_writer(cam_id, size, dt0)

        info = _writers[cam_id]
        vw = info['writer']
        clip_start = info['start']

        # process frames
        for ts, fp in frames:
            dt = datetime.fromtimestamp(ts/1000, timezone.utc)

            # clip rollover at 30s
            if dt - clip_start >= CLIP_DURATION:
                _close_writer(cam_id)
                _start_writer(cam_id, size, dt)
                info = _writers[cam_id]
                vw = info['writer']
                clip_start = info['start']

            img = cv2.imread(str(fp))
            if img is None:
                fp.unlink(missing_ok=True)
                continue

            # cleaning and enhancement
            try:
                day = is_day(img)
                cleaned = clean_frame(img, day)
            except Exception as e:
                logger.warning(f"[Cleaner] failed for {fp.name}: {e}")
                cleaned = img

            # detection overlay
            annotated = _detect_and_draw(cleaned)

            vw.write(annotated)
            fp.unlink(missing_ok=True)

        # force-close if writer open >60s
        info = _writers.get(cam_id)
        if info and (datetime.now(timezone.utc) - info["start"] >= AUTO_CLOSE_DURATION):
            _close_writer(cam_id)

        # commit HLS path in DB
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(Camera)
                .where(Camera.id == cam_id)
                .values(hls_path=f"hls/{cam_id}/index.m3u8")
            )
            await session.commit()

        # prune old clips
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        clips_dir = base / CLIPS_DIR
        for c in clips_dir.glob("*.mp4"):
            if datetime.fromtimestamp(c.stat().st_mtime, timezone.utc) < cutoff:
                c.unlink(missing_ok=True)

async def offline_watcher(db_factory, interval_seconds: float = 30.0):
    logger.info(f"Offline watcher every {interval_seconds}s")
    while True:
        await asyncio.sleep(interval_seconds)
        now = datetime.now(timezone.utc)
        async with db_factory() as session:
            result = await session.execute(select(Camera))
            cams = result.scalars().all()
            for cam in cams:
                last = cam.last_seen or datetime(1970,1,1,tzinfo=timezone.utc)
                online = (now - last).total_seconds() <= OFFLINE_TIMEOUT
                if cam.is_online != online:
                    cam.is_online = online
                    logger.info(f"Camera {cam.id} online={online}")
            await session.commit()
