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

# clip length and auto‐close delay
CLIP_DURATION = timedelta(seconds=30)
AUTO_CLOSE_DELAY = timedelta(seconds=60)

_executor = ThreadPoolExecutor(max_workers=4)
_writers: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}

# load YOLO
try:
    _model = YOLO(str(Path("models") / "yolov5s.pt"))
    _labels = _model.names
    _detection_enabled = True
    logger.info("YOLO loaded")
except Exception as e:
    _model = None
    _labels = {}
    _detection_enabled = False
    logger.warning(f"YOLO init failed: {e}")


def _ensure_dirs(cam_id: str):
    base = Path(DATA_ROOT) / cam_id
    for sub in (RAW_DIR, PROCESSED_DIR, CLIPS_DIR, "hls"):
        (base / sub).mkdir(parents=True, exist_ok=True)


def _open_writer(cam_id: str, size: tuple[int, int], start: datetime):
    clips = Path(DATA_ROOT) / cam_id / CLIPS_DIR
    ts = int(start.timestamp() * 1000)
    out = clips / f"{ts}.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(str(out), fourcc, FPS, size)
    _writers[cam_id] = {"writer": vw, "start": start, "path": out}
    logger.info(f"Started clip {out.name}")
    return _writers[cam_id]


def _close_writer(cam_id: str):
    info = _writers.pop(cam_id, None)
    if not info:
        return
    vw, path = info["writer"], info["path"]
    vw.release()
    logger.info(f"Closed clip {path.name}")
    asyncio.create_task(_segment_hls(path, cam_id))


async def _segment_hls(path: Path, cam_id: str):
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg not found")
        return
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
        logger.info(f"HLS segmented {path.name}")
    except ffmpeg.Error as e:
        logger.error(f"HLS failed: {e}")


def _detect_sync(img: np.ndarray) -> np.ndarray:
    if not _detection_enabled:
        return img
    res = _model(img, imgsz=640, conf=0.4, verbose=False)[0]
    for box, conf, cls in zip(res.boxes.xyxy, res.boxes.conf, res.boxes.cls):
        x1, y1, x2, y2 = map(int, box.tolist())
        label = f"{_labels[int(cls)]}:{conf:.2f}"
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(img, label, (x1, y1 - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    return img


async def _detect(img: np.ndarray) -> np.ndarray:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _detect_sync, img)


async def encode_and_cleanup(cam_id: str):
    lock = _locks.setdefault(cam_id, asyncio.Lock())
    if lock.locked():
        return

    async with lock:
        base = Path(DATA_ROOT) / cam_id
        raw = base / RAW_DIR
        _ensure_dirs(cam_id)

        frames = sorted(raw.glob("*.jpg"), key=lambda p: int(p.stem))
        if not frames:
            return

        ts0 = int(frames[0].stem)
        start = datetime.fromtimestamp(ts0 / 1000, timezone.utc)
        img0 = cv2.imread(str(frames[0]))
        if img0 is None:
            frames[0].unlink(missing_ok=True)
            return
        size = (img0.shape[1], img0.shape[0])

        info = _writers.get(cam_id)
        if not info:
            info = _open_writer(cam_id, size, start)
        elif datetime.now(timezone.utc) - info["start"] >= AUTO_CLOSE_DELAY:
            _close_writer(cam_id)
            info = _open_writer(cam_id, size, start)

        writer = info["writer"]

        for f in frames:
            img = cv2.imread(str(f))
            f.unlink(missing_ok=True)
            if img is None:
                continue

            cleaned = clean_frame(img, is_day(img))
            ann = await _detect(cleaned)
            writer.write(ann)

            tsf = datetime.fromtimestamp(int(f.stem) / 1000, timezone.utc)
            if tsf - info["start"] >= CLIP_DURATION:
                _close_writer(cam_id)
                info = _open_writer(cam_id, size, tsf)
                writer = info["writer"]

        # update camera.hls_path
        async with AsyncSessionLocal() as sess:
            await sess.execute(
                update(Camera)
                .where(Camera.id == cam_id)
                .values(hls_path=f"hls/{cam_id}/index.m3u8")
            )
            await sess.commit()

        # prune
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        for c in (base / CLIPS_DIR).glob("*.mp4"):
            if datetime.fromtimestamp(c.stat().st_mtime, timezone.utc) < cutoff:
                c.unlink(missing_ok=True)


async def offline_watcher(db_factory, interval_seconds: float = 30.0):
    logger.info(f"Offline watcher every {interval_seconds}s")
    while True:
        await asyncio.sleep(interval_seconds)
        now = datetime.now(timezone.utc)
        async with db_factory() as sess:
            res = await sess.execute(select(Camera))
            for cam in res.scalars().all():
                last = cam.last_seen or datetime(1970, 1, 1, tzinfo=timezone.utc)
                online = (now - last).total_seconds() <= OFFLINE_TIMEOUT
                if cam.is_online != online:
                    cam.is_online = online
                    logger.info(f"{cam.id} online={online}")
            await sess.commit()
