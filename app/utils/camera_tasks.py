# app/utils/camera_tasks.py

import os
import asyncio
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import ffmpeg
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

# ── Suppress JPEG‐read warnings (set before any cv2.imread calls) ────────────
# OpenCV’s Python binding no longer exposes cv2.utils.logging; instead
# use the environment variable OPENCV_LOG_LEVEL=OFF to silence imread warnings. :contentReference[oaicite:0]{index=0}
os.environ.setdefault("OPENCV_LOG_LEVEL", "OFF")

# Clip rollover thresholds
CLIP_DURATION      = timedelta(seconds=30)
AUTO_CLOSE_DELAY   = timedelta(seconds=60)

# Thread pool for YOLO inference
_executor = ThreadPoolExecutor(max_workers=4)
_writers   = {}  # cam_id → {writer, start, path}
_locks     = {}  # cam_id → asyncio.Lock()

# Load YOLO model once at startup
try:
    _model = YOLO(str(Path("models") / "yolov5s.pt"))
    _labels = _model.names
    _detection_enabled = True
    logger.info("YOLO loaded")  # ultralytics documentation :contentReference[oaicite:1]{index=1}
except Exception as e:
    _model = None
    _labels = {}
    _detection_enabled = False
    logger.warning(f"YOLO init failed: {e}")

def _ensure_dirs(cam_id: str):
    base = Path(DATA_ROOT) / cam_id
    for sub in (RAW_DIR, CLIPS_DIR, "hls"):
        (base / sub).mkdir(parents=True, exist_ok=True)  # pathlib docs :contentReference[oaicite:2]{index=2}

def _open_writer(cam_id: str, size: tuple[int,int], start: datetime):
    clips = Path(DATA_ROOT) / cam_id / CLIPS_DIR
    ts = int(start.timestamp() * 1000)
    out = clips / f"{ts}.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(str(out), fourcc, FPS, size)  # VideoWriter usage :contentReference[oaicite:3]{index=3}
    _writers[cam_id] = {"writer": vw, "start": start, "path": out}
    logger.info(f"Started clip {out.name}")

def _close_writer(cam_id: str):
    info = _writers.pop(cam_id, None)
    if not info:
        return
    info["writer"].release()
    raw_path = info["path"]
    logger.info(f"Closed clip {raw_path.name}")

    # 1) Segment for HLS in background
    asyncio.create_task(_segment_hls(raw_path))
    # 2) Spawn CV re-encoding (YOLO overlay) in background
    asyncio.create_task(_generate_cv_version(raw_path))

async def _segment_hls(path: Path):
    if not shutil.which("ffmpeg"):  # ffmpeg binary check :contentReference[oaicite:4]{index=4}
        logger.warning("ffmpeg not found")
        return
    cam_id = path.parent.parent.name
    hls = Path(DATA_ROOT) / cam_id / "hls"
    hls.mkdir(parents=True, exist_ok=True)
    try:
        (
            ffmpeg.input(str(path))
            .output(
                str(hls / "index.m3u8"),
                format="hls",
                hls_time=HLS_TARGET_DURATION,
                hls_list_size=HLS_PLAYLIST_LENGTH,
                hls_flags="delete_segments",
                c="copy",
            )
            .overwrite_output()
            .run(quiet=True)
        )
        logger.info(f"HLS segmented {path.name}")  # ffmpeg-python docs :contentReference[oaicite:5]{index=5}
    except ffmpeg.Error as e:
        logger.error(f"HLS segmentation failed: {e}")

async def _generate_cv_version(raw_path: Path):
    stem = raw_path.stem
    cv_path = raw_path.parent / f"{stem}_cv.mp4"
    cap = cv2.VideoCapture(str(raw_path))  # VideoCapture usage :contentReference[oaicite:6]{index=6}
    if not cap.isOpened():
        logger.error(f"Failed to open raw clip: {raw_path}")
        return

    fps = cap.get(cv2.CAP_PROP_FPS) or FPS
    w   = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h   = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(str(cv_path), fourcc, fps, (w, h))

    loop = asyncio.get_running_loop()
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        cleaned = clean_frame(frame, is_day(frame))
        if _detection_enabled:
            cleaned = await loop.run_in_executor(_executor, _detect_sync, cleaned)  # ThreadPoolExecutor docs :contentReference[oaicite:7]{index=7}
        vw.write(cleaned)
    cap.release()
    vw.release()
    logger.info(f"Generated processed clip {cv_path.name}")

def _detect_sync(img):
    res = _model(img, imgsz=640, conf=0.4, verbose=False)[0]
    for box, conf, cls in zip(res.boxes.xyxy, res.boxes.conf, res.boxes.cls):
        x1, y1, x2, y2 = map(int, box.tolist())
        lbl = f"{_labels[int(cls)]}:{conf:.2f}"
        cv2.rectangle(img, (x1, y1), (x2, y2), (0,255,0), 2)
        cv2.putText(img, lbl, (x1, y1-6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,0), 1)
    return img

async def encode_and_cleanup(cam_id: str):
    lock = _locks.setdefault(cam_id, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        base = Path(DATA_ROOT) / cam_id
        raw  = base / RAW_DIR
        _ensure_dirs(cam_id)

        frames = sorted(
            [f for f in raw.glob("*.jpg") if f.stat().st_size > 1000],
            key=lambda p: int(p.stem),
        )
        if not frames:
            return

        ts0   = int(frames[0].stem)
        start = datetime.fromtimestamp(ts0/1000, timezone.utc)

        info = _writers.get(cam_id)
        if not info or (datetime.now(timezone.utc) - info["start"] >= AUTO_CLOSE_DELAY):
            _close_writer(cam_id)
            img0 = cv2.imread(str(frames[0]))
            if img0 is None:
                frames[0].unlink(missing_ok=True)
                return
            _open_writer(cam_id, (img0.shape[1], img0.shape[0]), start)

        vw = _writers[cam_id]["writer"]
        for f in frames:
            img = cv2.imread(str(f))
            f.unlink(missing_ok=True)
            if img is None:
                continue
            vw.write(img)
            tsf = datetime.fromtimestamp(int(f.stem)/1000, timezone.utc)
            if tsf - _writers[cam_id]["start"] >= CLIP_DURATION:
                _close_writer(cam_id)
                img0 = cv2.imread(str(f))
                if img0 is None:
                    continue
                _open_writer(cam_id, (img0.shape[1], img0.shape[0]), tsf)
                vw = _writers[cam_id]["writer"]

        # update HLS path :contentReference[oaicite:8]{index=8}
        async with AsyncSessionLocal() as sess:
            await sess.execute(
                update(Camera)
                .where(Camera.id == cam_id)
                .values(hls_path=f"hls/{cam_id}/index.m3u8")
            )
            await sess.commit()

        # prune old clips
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        for clip in (base / CLIPS_DIR).glob("*.mp4"):
            if datetime.fromtimestamp(clip.stat().st_mtime, timezone.utc) < cutoff:
                clip.unlink(missing_ok=True)

async def offline_watcher(db_factory, interval_seconds: float = 30.0):
    logger.info(f"Offline watcher every {interval_seconds}s")
    while True:
        await asyncio.sleep(interval_seconds)
        now = datetime.now(timezone.utc)
        async with db_factory() as sess:
            result = await sess.execute(future_select(Camera))
            for cam in result.scalars().all():
                last   = cam.last_seen or datetime(1970,1,1,tzinfo=timezone.utc)
                online = (now - last).total_seconds() <= OFFLINE_TIMEOUT
                if cam.is_online != online:
                    cam.is_online = online
                    logger.info(f"{cam.id} online={online}")
            await sess.commit()
