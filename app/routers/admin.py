# app/routers/admin.py

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.config import DATA_ROOT, PROCESSED_DIR, RAW_DIR
from app.core.database import get_db
from app.dependencies import get_current_admin
from app.models import Device, Camera, DosingDeviceToken, SwitchDeviceToken, ValveDeviceToken
from app.schemas import DeviceResponse, CameraReportResponse, DeviceType

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(get_current_admin)]
)

@router.get(
    "/devices/dosing",
    response_model=list[DeviceResponse],
    summary="List all dosing‐unit devices"
)
async def list_dosing_devices(db: AsyncSession = Depends(get_db)):
    q = await db.execute(
        select(Device).where(Device.type == DeviceType.DOSING_UNIT)
    )
    return q.scalars().all()

@router.get(
    "/devices/valves",
    response_model=list[DeviceResponse],
    summary="List all valve‐controller devices"
)
async def list_valve_devices(db: AsyncSession = Depends(get_db)):
    q = await db.execute(
        select(Device).where(Device.type == DeviceType.VALVE_CONTROLLER)
    )
    return q.scalars().all()

@router.get(
    "/devices/switches",
    response_model=list[DeviceResponse],
    summary="List all smart‐switch devices"
)
async def list_switch_devices(db: AsyncSession = Depends(get_db)):
    q = await db.execute(
        select(Device).where(Device.type == DeviceType.SMART_SWITCH)
    )
    return q.scalars().all()
@router.get(
    "/cameras/list",
    response_model=list[CameraReportResponse],
    summary="List all registered cameras"
)
async def list_registered_cameras(db: AsyncSession = Depends(get_db)):
    """
    List all camera IDs that have a folder under DATA_ROOT.
    """
    root = Path(DATA_ROOT)
    if not root.exists():
        raise HTTPException(404, "Camera data root not found")
    cameras = []
    # any directory under DATA_ROOT is a camera
    for cam_dir in sorted(root.iterdir()):
        if not cam_dir.is_dir():
            continue
        cameras.append(CameraReportResponse(
            camera_id=cam_dir.name,
            detections=[],
        ))
    return cameras


@router.get(
    "/cameras/streams",
    summary="List all camera IDs and their last stream time"
)
async def list_camera_stream_times():
    """
    For each camera folder under DATA_ROOT, return its most recent processed frame timestamp
    (or latest.jpg if no processed frames exist).
    """
    root = Path(DATA_ROOT)
    if not root.exists():
        raise HTTPException(404, "Camera data root not found")

    output = []
    for cam_dir in sorted(root.iterdir()):
        if not cam_dir.is_dir():
            continue

        # try processed frames first
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
        output.append({
            "camera_id": cam_dir.name,
            "last_stream_time": ts
        })

    return output

@router.get(
    "/devices/all",
    response_model=list[DeviceResponse],
    summary="List every registered device"
)
async def list_all_devices(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Device))
    return result.scalars().all()

# ─── NEW: List devices that have a *device token* (i.e. have authenticated) ─
@router.get(
    "/devices/authenticated",
    summary="List devices which hold an active device token"
)
async def list_authenticated_devices(db: AsyncSession = Depends(get_db)):
    async def fetch_tokens(model):
        rows = (await db.execute(select(model))).scalars().all()
        return [
            {
                "device_id": row.device_id,
                "token":      row.token,
                "issued_at":  row.issued_at.isoformat() if isinstance(row.issued_at, datetime) else row.issued_at
            }
            for row in rows
        ]

    dosing_tokens  = await fetch_tokens(DosingDeviceToken)
    valve_tokens   = await fetch_tokens(ValveDeviceToken)
    switch_tokens  = await fetch_tokens(SwitchDeviceToken)

    return {
        "dosing_unit_tokens": dosing_tokens,
        "valve_controller_tokens": valve_tokens,
        "smart_switch_tokens": switch_tokens,
    }

@router.get(
    "/images",
    summary="List all frames between two timestamps"
)
async def list_images(
    start: datetime = Query(..., description="ISO start time"),
    end:   datetime = Query(..., description="ISO end time")
):
    """
    Returns a list of { camera_id, filename, timestamp, processed:bool } for every
    frame in RAW_DIR whose timestamp ∈ [start, end].
    """
    out = []
    root = Path(DATA_ROOT)
    for cam_dir in sorted(root.iterdir()):
        if not cam_dir.is_dir(): continue
        raw = cam_dir / RAW_DIR
        for img in raw.glob("*.jpg"):
            ts = datetime.fromtimestamp(int(img.stem)/1000, tz=start.tzinfo)
            if start <= ts <= end:
                proc = cam_dir / PROCESSED_DIR / img.name
                out.append({
                    "camera_id":  cam_dir.name,
                    "filename":   img.name,
                    "timestamp":  ts,
                    "processed":  proc.exists(),
                })
    return out

@router.get(
    "/images/{camera_id}/{filename}",
    summary="Download a frame (processed if available)"
)
async def download_image(camera_id: str, filename: str):
    base = Path(DATA_ROOT) / camera_id
    proc = base / PROCESSED_DIR / filename
    raw  = base / RAW_DIR       / filename
    if proc.exists():
        return FileResponse(proc, media_type="image/jpeg", filename=filename)
    if raw.exists():
        return FileResponse(raw, media_type="image/jpeg", filename=filename)
    raise HTTPException(404, "Image not found")

router.post(
    "/devices/{device_id}/switch/toggle",
    summary="(Admin) Toggle smart switch channel",
)
async def admin_toggle_switch(
    device_id: str,
    channel:    int = Body(..., embed=True),
    db: AsyncSession = Depends(get_db)
):
    """
    Admin endpoint that uses the device’s own HTTP endpoint to toggle a channel.
    """
    dev = await db.get(Device, device_id)
    if not dev or dev.type != DeviceType.SMART_SWITCH:
        raise HTTPException(404, "Smart switch not found")
    # forward the toggle
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{dev.http_endpoint.rstrip('/')}/toggle", json={"channel":channel})
        r.raise_for_status()
        data = r.json()
    return data