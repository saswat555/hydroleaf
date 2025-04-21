
# app/utils/camera_tasks.py
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
import cv2
from sqlalchemy.future import select
from app.models import Camera
from app.core.config import DATA_ROOT, RAW_DIR, CLIPS_DIR, RETENTION_DAYS, OFFLINE_TIMEOUT
from app.core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)
encode_locks: dict[str, asyncio.Lock] = {}

def _encode_and_cleanup_sync(cam_id: str):
    cam_dir = Path(DATA_ROOT) / cam_id
    raw_dir = cam_dir / RAW_DIR
    clips_dir = cam_dir / CLIPS_DIR
    clips_dir.mkdir(parents=True, exist_ok=True)
    CLIP_MS = 15 * 60 * 1000
    files = sorted(raw_dir.glob("*.jpg"), key=lambda f: int(f.stem))
    buckets: dict[int, list[Path]] = {}
    for f in files:
        period = int(f.stem) // CLIP_MS
        buckets.setdefault(period, []).append(f)
    for period, group in buckets.items():
        timestamps = sorted(int(f.stem) for f in group)
        if timestamps[-1] - timestamps[0] < CLIP_MS:
            continue
        clip_start = period * CLIP_MS
        clip_path = clips_dir / f"{clip_start}.mp4"
        if clip_path.exists():
            continue
        first = cv2.imread(str(group[0]))
        h, w = first.shape[:2]
        vw = cv2.VideoWriter(
            str(clip_path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (w, h)
        )
        for imgf in group:
            im = cv2.imread(str(imgf))
            if im is not None:
                vw.write(im)
            imgf.unlink(missing_ok=True)
        vw.release()
    # prune old clips
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    for clip in clips_dir.glob("*.mp4"):
        if datetime.fromtimestamp(clip.stat().st_mtime, timezone.utc) < cutoff:
            clip.unlink(missing_ok=True)

async def encode_and_cleanup(cam_id: str):
    lock = encode_locks.setdefault(cam_id, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        await asyncio.to_thread(_encode_and_cleanup_sync, cam_id)

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
