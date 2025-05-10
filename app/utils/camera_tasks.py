import asyncio
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
from sqlalchemy.future import select
from sqlalchemy import update
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
)
from app.core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)
_encode_locks: dict[str, asyncio.Lock] = {}


def _encode_and_cleanup_sync(cam_id: str):
    """
    Synchronously encode raw/processed frames into clips, segment HLS,
    and prune any files older than RETENTION_DAYS.
    """
    cam_dir = Path(DATA_ROOT) / cam_id
    raw_dir = cam_dir / RAW_DIR
    proc_dir = cam_dir / PROCESSED_DIR
    clips_dir = cam_dir / CLIPS_DIR
    hls_dir = cam_dir / "hls"

    # Ensure directories
    for d in (raw_dir, proc_dir, clips_dir, hls_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 1) Group frames into fixed-size clips
    CLIP_MS = 10 * 60 * 1000
    # prefer processed frames if present
    input_dir = proc_dir if proc_dir.exists() and any(proc_dir.glob("*.jpg")) else raw_dir
    frames = sorted(input_dir.glob("*.jpg"), key=lambda f: int(f.stem))
    buckets: dict[int, list[Path]] = {}
    for f in frames:
        period = int(f.stem) // CLIP_MS
        buckets.setdefault(period, []).append(f)

    # 2) Encode each full bucket
    for period, group in buckets.items():
        if len(group) < 2:
            continue
        start_ts = int(group[0].stem)
        end_ts = int(group[-1].stem)
        if end_ts - start_ts < CLIP_MS:
            continue
        clip_file = clips_dir / f"{period * CLIP_MS}.mp4"
        if not clip_file.exists():
            # initialize video writer
            first_img = cv2.imread(str(group[0]))
            if first_img is None:
                continue
            h, w = first_img.shape[:2]
            vw = cv2.VideoWriter(str(clip_file), cv2.VideoWriter_fourcc(*"mp4v"), 20, (w, h))
            for img_path in group:
                img = cv2.imread(str(img_path))
                if img is not None:
                    vw.write(img)
                # remove frame after writing
                img_path.unlink(missing_ok=True)
            vw.release()
        # HLS segmentation
        try:
            subprocess.run([
                "ffmpeg", "-y",
                "-i", str(clip_file),
                "-c", "copy",
                "-f", "hls",
                "-hls_time", str(HLS_TARGET_DURATION),
                "-hls_list_size", str(HLS_PLAYLIST_LENGTH),
                "-hls_flags", "delete_segments",
                str(hls_dir / "index.m3u8"),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            logger.error(f"HLS segmentation failed for {cam_id}: {e}")

    # 3) Prune stale files
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    # raw frames
    for f in raw_dir.glob("*.jpg"):
        if datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) < cutoff:
            f.unlink(missing_ok=True)
    # processed frames
    if proc_dir.exists():
        for f in proc_dir.glob("*.jpg"):
            if datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) < cutoff:
                f.unlink(missing_ok=True)
    # clips
    for c in clips_dir.glob("*.mp4"):
        if datetime.fromtimestamp(c.stat().st_mtime, timezone.utc) < cutoff:
            c.unlink(missing_ok=True)
    # latest snapshot
    latest = cam_dir / "latest.jpg"
    if latest.exists() and datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc) < cutoff:
        latest.unlink(missing_ok=True)


async def encode_and_cleanup(cam_id: str):
    """
    Async entrypoint: ensure single-run per camera, update DB with HLS path.
    """
    lock = _encode_locks.setdefault(cam_id, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        # run CPU-bound work in thread
        await asyncio.to_thread(_encode_and_cleanup_sync, cam_id)
        # update hls_path in database
        hls_path = f"hls/{cam_id}/index.m3u8"
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(Camera)
                .where(Camera.id == cam_id)
                .values(hls_path=hls_path)
            )
            await session.commit()


async def offline_watcher(db_factory, interval_seconds: float = 5.0):
    """
    Periodically mark cameras online/offline based on last_seen and OFFLINE_TIMEOUT.
    """
    logger.info(f"Starting offline watcher (interval={interval_seconds}s)")
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
                    logger.info(f"Camera {cam.id} online status changed to {online}")
            await session.commit()
