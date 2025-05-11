from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from app.core.config import DATA_ROOT, PROCESSED_DIR
from app.schemas import CameraReportResponse, DeviceType
from app.services.device_discovery import get_connected_devices
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.core.database import get_db
from app.models import Device, Camera
from app.schemas import DeviceResponse  # re-use your existing pydantic schema
from app.dependencies import get_current_admin
from fastapi import Depends
router = APIRouter(prefix="/admin", tags=["admin"])


@router.get(
    "/devices/dosing",
    response_model=list[DeviceResponse],
    dependencies=[Depends(get_current_admin)],
    summary="List all dosing‐unit devices"
)
async def list_dosing_devices(db: AsyncSession = Depends(get_db)):
    q = await db.execute(select(Device).where(Device.type == DeviceType.DOSING_UNIT))
    return q.scalars().all()

@router.get(
    "/devices/valves",
    response_model=list[DeviceResponse],
    dependencies=[Depends(get_current_admin)],
    summary="List all valve‐controller devices"
)
async def list_valve_devices(db: AsyncSession = Depends(get_db)):
    q = await db.execute(select(Device).where(Device.type == DeviceType.VALVE_CONTROLLER))
    return q.scalars().all()

@router.get(
    "/cameras/list",
    response_model=list[CameraReportResponse],  # or define a CameraResponse if you like
    dependencies=[Depends(get_current_admin)],
    summary="List all registered cameras"
)
async def list_registered_cameras(db: AsyncSession = Depends(get_db)):
    from app.models import Camera
    q = await db.execute(select(Camera))
    cams = q.scalars().all()
    return [
        {
            "camera_id": cam.id,
            "is_online": cam.is_online,
            "last_seen": cam.last_seen,
            "frames_received": cam.frames_received,
            "clips_count": cam.clips_count
        }
        for cam in cams
    ]


@router.get(
    "/cameras/streams",
    dependencies=[Depends(get_current_admin)],
    summary="List all camera IDs and their last stream time"
)
async def list_camera_stream_times():
    root = Path(DATA_ROOT)
    if not root.exists():
        raise HTTPException(status_code=404, detail="Camera data directory not found")

    output = []
    for cam_dir in sorted(root.iterdir()):
        if not cam_dir.is_dir():
            continue

        # pick up any processed frame first
        proc = cam_dir / PROCESSED_DIR
        frames = list(proc.glob("*.jpg")) if proc.exists() else []
        # fallback to latest.jpg
        if not frames:
            latest = cam_dir / "latest.jpg"
            if latest.exists():
                frames = [latest]

        if not frames:
            continue

        newest = max(frames, key=lambda f: f.stat().st_mtime)
        ts = datetime.fromtimestamp(newest.stat().st_mtime, timezone.utc).isoformat()
        output.append({"camera_id": cam_dir.name, "last_stream_time": ts})

    return output