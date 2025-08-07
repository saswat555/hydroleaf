# app/routers/admin.py
"""
Admin-only endpoints for Hydroleaf Cloud.

Changes in this revision
────────────────────────
• Uses the *unified* `device_tokens` table – no more separate tables for each
  device type.
• Keeps the previous functionality (device & camera listings, image download,
  remote switch toggle) but modernises a few rough edges.
"""

from __future__ import annotations

import httpx
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DATA_ROOT, PROCESSED_DIR, RAW_DIR
from app.core.database import get_db
from app.dependencies import get_current_admin
from app.models import Device, DeviceToken
from app.schemas import DeviceResponse, CameraReportResponse, DeviceType

# ─────────────────────────────────────────────────────────────────────────────
router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(get_current_admin)],
)
# ─────────────────────────────────────────────────────────────────────────────
# Device listings
# ─────────────────────────────────────────────────────────────────────────────
@router.get(
    "/devices/dosing",
    response_model=List[DeviceResponse],
    summary="List all dosing-unit devices",
)
async def list_dosing_devices(db: AsyncSession = Depends(get_db)):
    rows = await db.execute(select(Device).where(Device.type == DeviceType.DOSING_UNIT))
    return rows.scalars().all()


@router.get(
    "/devices/valves",
    response_model=List[DeviceResponse],
    summary="List all valve-controller devices",
)
async def list_valve_devices(db: AsyncSession = Depends(get_db)):
    rows = await db.execute(
        select(Device).where(Device.type == DeviceType.VALVE_CONTROLLER)
    )
    return rows.scalars().all()


@router.get(
    "/devices/switches",
    response_model=List[DeviceResponse],
    summary="List all smart-switch devices",
)
async def list_switch_devices(db: AsyncSession = Depends(get_db)):
    rows = await db.execute(
        select(Device).where(Device.type == DeviceType.SMART_SWITCH)
    )
    return rows.scalars().all()


@router.get(
    "/devices/all",
    response_model=List[DeviceResponse],
    summary="List every registered device (all types)",
)
async def list_all_devices(db: AsyncSession = Depends(get_db)):
    rows = await db.execute(select(Device))
    return rows.scalars().all()


# ─────────────────────────────────────────────────────────────────────────────
# Tokens
# ─────────────────────────────────────────────────────────────────────────────
@router.get(
    "/devices/authenticated",
    summary="List devices that currently hold a device token",
)
async def list_authenticated_devices(db: AsyncSession = Depends(get_db)):
    rows = await db.execute(select(DeviceToken))
    return [
        {
            "device_id": tok.device_id,
            "token": tok.token,
            "issued_at": tok.issued_at.isoformat()
            if isinstance(tok.issued_at, datetime)
            else tok.issued_at,
        }
        for tok in rows.scalars().all()
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Camera helpers
# ─────────────────────────────────────────────────────────────────────────────
@router.get(
    "/cameras/list",
    response_model=List[CameraReportResponse],
    summary="List every camera that has stored frames",
)
async def list_registered_cameras():
    root = Path(DATA_ROOT)
    if not root.exists():
        raise HTTPException(404, "Camera data root not found")

    cameras: list[CameraReportResponse] = []
    for cam_dir in sorted(root.iterdir()):
        if not cam_dir.is_dir():
            continue
        cameras.append(
            CameraReportResponse(camera_id=cam_dir.name, detections=[])
        )
    return cameras


@router.get(
    "/cameras/streams",
    summary="Last streamed frame time for each camera",
)
async def list_camera_stream_times():
    root = Path(DATA_ROOT)
    if not root.exists():
        raise HTTPException(404, "Camera data root not found")

    output: list[dict] = []
    for cam_dir in sorted(root.iterdir()):
        if not cam_dir.is_dir():
            continue

        processed = cam_dir / PROCESSED_DIR
        frames = list(processed.glob("*.jpg")) if processed.exists() else []

        if not frames:
            latest = cam_dir / "latest.jpg"
            if latest.exists():
                frames = [latest]

        if not frames:
            continue

        newest = max(frames, key=lambda f: f.stat().st_mtime)
        ts = datetime.fromtimestamp(
            newest.stat().st_mtime, timezone.utc
        ).isoformat()
        output.append({"camera_id": cam_dir.name, "last_stream_time": ts})

    return output


# ─────────────────────────────────────────────────────────────────────────────
# Image search & download
# ─────────────────────────────────────────────────────────────────────────────
@router.get(
    "/images",
    summary="List every frame captured between two timestamps",
)
async def list_images(
    start: datetime = Query(..., description="Start (ISO-8601)"),
    end: datetime = Query(..., description="End (ISO-8601)"),
):
    root = Path(DATA_ROOT)
    out: list[dict] = []

    for cam_dir in sorted(root.iterdir()):
        if not cam_dir.is_dir():
            continue
        raw = cam_dir / RAW_DIR
        for img in raw.glob("*.jpg"):
            ts = datetime.fromtimestamp(int(img.stem) / 1000, tz=start.tzinfo)
            if start <= ts <= end:
                processed = cam_dir / PROCESSED_DIR / img.name
                out.append(
                    {
                        "camera_id": cam_dir.name,
                        "filename": img.name,
                        "timestamp": ts,
                        "processed": processed.exists(),
                    }
                )
    return out


@router.get(
    "/images/{camera_id}/{filename}",
    summary="Download a raw or processed frame",
)
async def download_image(camera_id: str, filename: str):
    base = Path(DATA_ROOT) / camera_id
    processed = base / PROCESSED_DIR / filename
    raw = base / RAW_DIR / filename

    if processed.exists():
        return FileResponse(processed, media_type="image/jpeg", filename=filename)
    if raw.exists():
        return FileResponse(raw, media_type="image/jpeg", filename=filename)
    raise HTTPException(404, "Image not found")


# ─────────────────────────────────────────────────────────────────────────────
# Remote actions – smart switch only
# ─────────────────────────────────────────────────────────────────────────────
@router.post(
    "/devices/{device_id}/switch/toggle",
    summary="(Admin) Toggle a smart-switch channel",
)
async def admin_toggle_switch(
    device_id: str,
    channel: int = Body(..., embed=True, ge=1, le=8),
    db: AsyncSession = Depends(get_db),
):
    """
    Sends a *direct* HTTP request to a smart-switch's local endpoint.  
    Primarily used for diagnostics and emergency actions.
    """
    dev = await db.get(Device, device_id)
    if not dev or dev.type != DeviceType.SMART_SWITCH:
        raise HTTPException(404, "Smart switch not found")

    # forward the toggle
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{dev.http_endpoint.rstrip('/')}/toggle", json={"channel": channel}
        )
        r.raise_for_status()
        return r.json()
