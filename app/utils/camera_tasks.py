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
_locks: dict[str, asyncio.Lock] = {}

# maintain separate writers for raw and cv streams
_writers_raw: dict[str, dict] = {}
_writers_cv: dict[str, dict]  = {}

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


def _open_writers(cam_id: str, size: tuple[int, int], start: datetime):
    """
    Create two VideoWriters: one for raw frames, one for CV-annotated frames.
    """
    clips = Path(DATA_ROOT) / cam_id / CLIPS_DIR
    ts = int(start.timestamp() * 1000)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")

    # raw
    raw_path = clips / f"{ts}.mp4"
    vw_raw = cv2.VideoWriter(str(raw_path), fourcc, FPS, size)
    _writers_raw[cam_id] = {"writer": vw_raw, "start": start, "path": raw_path}

    # cv annotated
    cv_path = clips / f"{ts}_cv.mp4"
    vw_cv = cv2.VideoWriter(str(cv_path), fourcc, FPS, size)
    _writers_cv[cam_id] = {"writer": vw_cv, "start": start, "path": cv_path}

    logger.info(f"Started clips {raw_path.name} & {cv_path.name}")


def _close_writers(cam_id: str):
    """
    Close both raw and cv writers, segment raw for HLS.
    """
    raw_info = _writers_raw.pop(cam_id, None)
    cv_info  = _writers_cv.pop(cam_id, None)

    if raw_info:
        raw_info["writer"].release()
        path = raw_info["path"]
        logger.info(f"Closed raw clip {path.name}")
        asyncio.create_task(_segment_hls(path, cam_id))

    if cv_info:
        cv_info["writer"].release()
        path = cv_info["path"]
        logger.info(f"Closed processed clip {path.name}")

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

        # open or rotate writers
        raw_info = _writers_raw.get(cam_id)
        if not raw_info or datetime.now(timezone.utc) - raw_info["start"] >= AUTO_CLOSE_DELAY:
            _close_writers(cam_id)
            _open_writers(cam_id, size, start)
        raw_info = _writers_raw[cam_id]
        cv_info  = _writers_cv[cam_id]

        vw_raw = raw_info["writer"]
        vw_cv  = cv_info["writer"]

        for f in frames:
            img = cv2.imread(str(f))
            f.unlink(missing_ok=True)
            if img is None:
                continue

            # write raw
            vw_raw.write(img)
            # process & write cv
            cleaned = clean_frame(img, is_day(img))
            ann     = await _detect(cleaned)
            vw_cv.write(ann)

            tsf = datetime.fromtimestamp(int(f.stem) / 1000, timezone.utc)
            if tsf - raw_info["start"] >= CLIP_DURATION:
                _close_writers(cam_id)
                _open_writers(cam_id, size, tsf)
                raw_info = _writers_raw[cam_id]
                cv_info  = _writers_cv[cam_id]
                vw_raw, vw_cv = raw_info["writer"], cv_info["writer"]

        # update camera.hls_path only for raw stream
        async with AsyncSessionLocal() as sess:
            await sess.execute(
                update(Camera)
                .where(Camera.id == cam_id)
                .values(hls_path=f"hls/{cam_id}/index.m3u8")
            )
            await sess.commit()

        # prune both raw & cv clips
        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        for suffix in ["", "_cv"]:
            for c in (base / CLIPS_DIR).glob(f"*{suffix}.mp4"):
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