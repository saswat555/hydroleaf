# app/utils/camera_tasks.py
import asyncio
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import ffmpeg
import numpy as np
from sqlalchemy import update
from sqlalchemy.future import select
from ultralytics import YOLO

from app.models import Camera
from app.core.config import (
    DATA_ROOT,
    RAW_DIR,
    PROCESSED_DIR,
    CLIPS_DIR,
    RETENTION_DAYS,
    HLS_TARGET_DURATION,
    HLS_PLAYLIST_LENGTH,
    OFFLINE_TIMEOUT,
    FPS,
)
from app.core.database import AsyncSessionLocal
from app.utils.image_utils import is_day, clean_frame

logger = logging.getLogger(__name__)

# ───────── SETTINGS ─────────
CLIP_DURATION    = timedelta(seconds=30)
AUTO_CLOSE_DELAY = timedelta(seconds=60)

# ───────── THREAD POOL ─────────
_executor = ThreadPoolExecutor(max_workers=4)

# ───────── STATE ─────────
_writers: dict[str, dict]       = {}
_locks:   dict[str, asyncio.Lock] = {}

# ───────── YOLO MODEL ─────────
try:
    _model = YOLO(str(Path("models") / "yolov5s.pt"))
    _labels = _model.names
    _detection_enabled = True
    logger.info("YOLOv5 loaded, detection enabled")
except Exception as e:
    _model = None
    _labels = {}
    _detection_enabled = False
    logger.warning(f"YOLO init failed ({e}), detection disabled")


def _ensure_dirs(cam_id: str):
    base = Path(DATA_ROOT) / cam_id
    for sub in (RAW_DIR, PROCESSED_DIR, CLIPS_DIR, "hls"):
        (base / sub).mkdir(parents=True, exist_ok=True)


def _open_writer(cam_id: str, size: tuple[int,int], start_ts: datetime):
    clips_dir = Path(DATA_ROOT) / cam_id / CLIPS_DIR
    ts_ms = int(start_ts.timestamp() * 1000)
    path = clips_dir / f"{ts_ms}.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(str(path), fourcc, FPS, size)
    _writers[cam_id] = {"writer": vw, "start": start_ts, "path": path}
    logger.info(f"[Encoder] Started clip {path.name} for camera {cam_id}")
    return _writers[cam_id]


def _close_writer(cam_id: str):
    info = _writers.pop(cam_id, None)
    if not info:
        return
    vw, path = info["writer"], info["path"]
    vw.release()
    logger.info(f"[Encoder] Closed clip {path.name} for camera {cam_id}")
    asyncio.create_task(_segment_hls(path, cam_id))


async def _segment_hls(path: Path, cam_id: str):
    """Run HLS segmentation via ffmpeg-python, if ffmpeg is available."""
    if not shutil.which("ffmpeg"):
        logger.warning(f"'ffmpeg' not on PATH, skipping HLS for {cam_id}")
        return

    hls_dir = Path(DATA_ROOT) / cam_id / "hls"
    hls_dir.mkdir(parents=True, exist_ok=True)
    try:
        (
            ffmpeg
            .input(str(path))
            .output(
                str(hls_dir / "index.m3u8"),
                format="hls",
                hls_time=HLS_TARGET_DURATION,
                hls_list_size=HLS_PLAYLIST_LENGTH,
                hls_flags="delete_segments",
                c="copy"
            )
            .overwrite_output()
            .run(quiet=True)
        )
        logger.info(f"[HLS] Segmented {path.name} for camera {cam_id}")
    except ffmpeg.Error as e:
        logger.error(f"[HLS] Segmentation failed for {cam_id}: {e}")


def _detect_and_draw_sync(img: np.ndarray) -> np.ndarray:
    """Blocking YOLO inference + overlay, run in thread."""
    if not _detection_enabled:
        return img
    results = _model(img, imgsz=640, conf=0.4, verbose=False)[0]
    for box, conf, cls in zip(results.boxes.xyxy, results.boxes.conf, results.boxes.cls):
        x1, y1, x2, y2 = map(int, box.tolist())
        label = f"{_labels[int(cls)]}:{conf:.2f}"
        cv2.rectangle(img, (x1, y1), (x2, y2), (0,255,0), 2)
        cv2.putText(img, label, (x1, y1-6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,0), 1)
    return img


async def _detect_and_draw(img: np.ndarray) -> np.ndarray:
    return await asyncio.get_event_loop().run_in_executor(_executor, _detect_and_draw_sync, img)


async def encode_and_cleanup(cam_id: str):
    lock = _locks.setdefault(cam_id, asyncio.Lock())
    if lock.locked():
        return

    async with lock:
        base    = Path(DATA_ROOT) / cam_id
        raw_dir = base / RAW_DIR
        _ensure_dirs(cam_id)

        # Gather all new frames
        frames = sorted(raw_dir.glob("*.jpg"), key=lambda f: int(f.stem))
        if not frames:
            return

        # Initialize or rollover writer
        ts0 = int(frames[0].stem)
        dt0 = datetime.fromtimestamp(ts0/1000, timezone.utc)
        img0 = cv2.imread(str(frames[0]))
        if img0 is None:
            frames[0].unlink(missing_ok=True)
            return
        size = (img0.shape[1], img0.shape[0])

        info = _writers.get(cam_id)
        if not info:
            info = _open_writer(cam_id, size, dt0)
        elif datetime.now(timezone.utc) - info["start"] >= AUTO_CLOSE_DELAY:
            _close_writer(cam_id)
            info = _open_writer(cam_id, size, dt0)

        writer = info["writer"]

        # Process each frame
        for f in frames:
            img = cv2.imread(str(f))
            f.unlink(missing_ok=True)
            if img is None:
                continue

            # enhance
            try:
                cleaned = clean_frame(img, is_day(img))
            except Exception as e:
                logger.warning(f"[Cleaner] {f.name}: {e}")
                cleaned = img

            # detect & write
            annotated = await _detect_and_draw(cleaned)
            writer.write(annotated)

            # check 30s clip duration
            if datetime.fromtimestamp(int(f.stem)/1000, timezone.utc) - info["start"] >= CLIP_DURATION:
                _close_writer(cam_id)
                info = _open_writer(cam_id, size, datetime.fromtimestamp(int(f.stem)/1000, timezone.utc))
                writer = info["writer"]

        # Commit latest HLS path to DB
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(Camera)
                .where(Camera.id == cam_id)
                .values(hls_path=f"hls/{cam_id}/index.m3u8")
            )
            await session.commit()

        # Prune old clips
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        for c in (base / CLIPS_DIR).glob("*.mp4"):
            if datetime.fromtimestamp(c.stat().st_mtime, timezone.utc) < cutoff:
                c.unlink(missing_ok=True)


async def offline_watcher(db_factory, interval_seconds: float = 30.0):
    logger.info(f"Offline watcher running every {interval_seconds}s")
    while True:
        await asyncio.sleep(interval_seconds)
        now = datetime.now(timezone.utc)
        async with db_factory() as session:
            result = await session.execute(select(Camera))
            for cam in result.scalars().all():
                last = cam.last_seen or datetime(1970,1,1,tzinfo=timezone.utc)
                online = (now - last).total_seconds() <= OFFLINE_TIMEOUT
                if cam.is_online != online:
                    cam.is_online = online
                    logger.info(f"Camera {cam.id} online={online}")
            await session.commit()
