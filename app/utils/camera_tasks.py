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

# Clip length and frame rate
CLIP_DURATION = timedelta(minutes=10)
FPS = 20

# State for each camera
_writers: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}

def _ensure_dirs(cam_id: str):
    base = Path(DATA_ROOT) / cam_id
    for sub in (RAW_DIR, CLIPS_DIR, "hls"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    # separate day/night raw folders
    (base / RAW_DIR / "day").mkdir(parents=True, exist_ok=True)
    (base / RAW_DIR / "night").mkdir(parents=True, exist_ok=True)

def _start_writer(cam_id: str, h: int, w: int, start_ts: datetime):
    """Begin a new MP4 file for this camera."""
    clips_dir = Path(DATA_ROOT) / cam_id / CLIPS_DIR
    ts_ms = int(start_ts.timestamp() * 1000)
    path = clips_dir / f"{ts_ms}.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(str(path), fourcc, FPS, (w, h))
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
    except Exception as e:
        logger.error(f"HLS segmentation failed for {cam_id}: {e}")

def _process_image(img: np.ndarray, mode: str) -> np.ndarray:
    """Add border, timestamp, and enhance for day/night."""
    # 1) Black border
    b = 5
    img = cv2.copyMakeBorder(img, b, b, b, b, cv2.BORDER_CONSTANT, value=(0,0,0))
    # 2) Overlay server timestamp
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    cv2.putText(img, now, (b+10, b+30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2)
    # 3) CLAHE enhancement
    if mode == "night":
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
        eq = clahe.apply(gray)
        img = cv2.cvtColor(eq, cv2.COLOR_GRAY2BGR)
    else:  # day
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        l, a, b_ = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
        cl = clahe.apply(l)
        lab = cv2.merge((cl, a, b_))
        img = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    return img

async def encode_and_cleanup(cam_id: str):
    """
    Grab all raw frames (day/night), stream them into the active writer,
    rollover every 10 min, segment for HLS, update DB, and prune old clips.
    """
    lock = _locks.setdefault(cam_id, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        _ensure_dirs(cam_id)
        base = Path(DATA_ROOT) / cam_id / RAW_DIR
        day_dir = base / "day"
        night_dir = base / "night"

        # Collect (timestamp, path, mode)
        frames: list[tuple[int, Path, str]] = []
        for d, mode in ((day_dir, "day"), (night_dir, "night")):
            if d.exists():
                for f in d.glob("*.jpg"):
                    try:
                        ts = int(f.stem)
                        frames.append((ts, f, mode))
                    except ValueError:
                        continue
        frames.sort(key=lambda x: x[0])
        if not frames:
            return

        # Initialize writer if needed
        ts0, fp0, _ = frames[0]
        dt0 = datetime.fromtimestamp(ts0/1000, timezone.utc)
        img0 = cv2.imread(str(fp0))
        if img0 is None:
            fp0.unlink(missing_ok=True)
            return
        h, w = img0.shape[:2]
        if cam_id not in _writers:
            _start_writer(cam_id, h, w, dt0)

        writer_info = _writers[cam_id]
        vw = writer_info["writer"]
        clip_start = writer_info["start"]

        # Write & process each frame
        for ts, fp, mode in frames:
            dt = datetime.fromtimestamp(ts/1000, timezone.utc)
            # Rollover?
            if dt - clip_start >= CLIP_DURATION:
                _close_writer(cam_id)
                _start_writer(cam_id, h, w, dt)
                writer_info = _writers[cam_id]
                vw = writer_info["writer"]
                clip_start = writer_info["start"]

            img = cv2.imread(str(fp))
            if img is not None:
                proc = _process_image(img, mode)
                vw.write(proc)
            fp.unlink(missing_ok=True)

        # Update camera record with new HLS path
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
                last = cam.last_seen or datetime.fromtimestamp(0, timezone.utc)
                online = (now - last).total_seconds() <= OFFLINE_TIMEOUT
                if cam.is_online != online:
                    cam.is_online = online
                    logger.info(f"Camera {cam.id} online={online}")
            await session.commit()
