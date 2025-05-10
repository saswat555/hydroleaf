# app/utils/camera_tasks.py
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
    PROCESSED_DIR,
    RAW_DIR,
    CLIPS_DIR,
    RETENTION_DAYS,
    OFFLINE_TIMEOUT,
    HLS_TARGET_DURATION,
    HLS_PLAYLIST_LENGTH,
)
from app.core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)
encode_locks: dict[str, asyncio.Lock] = {}


def _encode_and_cleanup_sync(cam_id: str):
    cam_dir = Path(DATA_ROOT) / cam_id
    raw_dir = cam_dir / RAW_DIR
    clips_dir = cam_dir / CLIPS_DIR
    clips_dir.mkdir(parents=True, exist_ok=True)
    hls_dir = cam_dir / "hls"
    hls_dir.mkdir(parents=True, exist_ok=True)

    # Group raw frames into 15‑minute buckets
    CLIP_MS = 20 * 1000  
    proc_dir = cam_dir / PROCESSED_DIR
    input_dir = proc_dir if proc_dir.exists() else raw_dir
    files = sorted(input_dir.glob("*.jpg"), key=lambda f: int(f.stem))
    buckets: dict[int, list[Path]] = {}
    for f in files:
        period = int(f.stem) // CLIP_MS
        buckets.setdefault(period, []).append(f)

    # For each full bucket, write MP4 and segment to HLS
    for period, group in buckets.items():
        timestamps = sorted(int(f.stem) for f in group)
        if timestamps[-1] - timestamps[0] < CLIP_MS:
            continue
        clip_start = period * CLIP_MS
        clip_path = clips_dir / f"{clip_start}.mp4"
        if not clip_path.exists():
            first = cv2.imread(str(group[0]))
            if first is None:
                continue
            h, w = first.shape[:2]
            vw = cv2.VideoWriter(
                str(clip_path), cv2.VideoWriter_fourcc(*"mp4v"), 20, (w, h)
            )
            for imgf in group:
                im = cv2.imread(str(imgf))
                if im is not None:
                    vw.write(im)
                imgf.unlink(missing_ok=True)
            vw.release()

        # HLS segmentation (sliding window)
        try:
            subprocess.run([
                "ffmpeg", "-y",
                "-i", str(clip_path),
                "-c", "copy",
                "-f", "hls",
                "-hls_time", str(HLS_TARGET_DURATION),
                "-hls_list_size", str(HLS_PLAYLIST_LENGTH),
                "-hls_flags", "delete_segments",
                str(hls_dir / "index.m3u8")
            ], check=True)
        except Exception as e:
            logger.error(f"HLS segmentation failed for {cam_id}: {e}")

    # Prune raw frames & clips older than retention
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    for frame_file in raw_dir.glob("*.jpg"):
        if datetime.fromtimestamp(frame_file.stat().st_mtime, timezone.utc) < cutoff:
            frame_file.unlink(missing_ok=True)
    for clip in clips_dir.glob("*.mp4"):
        if datetime.fromtimestamp(clip.stat().st_mtime, timezone.utc) < cutoff:
            clip.unlink(missing_ok=True)
    if proc_dir.exists():
        for p in proc_dir.glob("*.jpg"):
            if datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) < cutoff:
                p.unlink(missing_ok=True)


async def encode_and_cleanup(cam_id: str):
    # Ensure one worker per camera
    lock = encode_locks.setdefault(cam_id, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        # Run CPU‑bound work in thread
        await asyncio.to_thread(_encode_and_cleanup_sync, cam_id)
        # Update hls_path in DB
        hls_relative = f"hls/{cam_id}/index.m3u8"
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(Camera)
                .where(Camera.id == cam_id)
                .values(hls_path=hls_relative)
            )
            await session.commit()


async def offline_watcher(db_factory, interval_seconds: float = 5.0):
    logger.info(f"Starting offline watcher, interval={interval_seconds}s")
    while True:
        await asyncio.sleep(interval_seconds)
        now = datetime.now(timezone.utc)
        async with db_factory() as session:
            result = await session.execute(select(Camera))
            cams = result.scalars().all()
            for cam in cams:
                last_seen = cam.last_seen or datetime.fromtimestamp(0, timezone.utc)
                is_online = (now - last_seen).total_seconds() <= OFFLINE_TIMEOUT
                if cam.is_online != is_online:
                    cam.is_online = is_online
                    logger.info(f"Camera {cam.id} online status changed: {is_online}")
            await session.commit()
