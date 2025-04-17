from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DEPLOYMENT_MODE
from app.core.database import get_db
from app.models import Device, Task

import os

router = APIRouter()


@router.get("/pending_tasks")
async def get_pending_tasks(
    device_id: str = Query(..., description="MAC ID or identifier of the device"),
    db: AsyncSession = Depends(get_db)
):
    # Dynamic query (optional)
    result = await db.execute(
        select(Task).where(Task.device_id == device_id, Task.status == "pending")
    )
    tasks = result.scalars().all()

    return tasks


@router.get("/update")
async def check_for_update(
    device_id: str = Query(..., description="MAC ID or unique device identifier"),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Device).where(Device.mac_id == device_id))
    device = result.scalar_one_or_none()

    latest_version = "2.2.0"
    update_available = device and device.version != latest_version
    return {
        "version": latest_version,
        "update_available": update_available
    }


@router.get("/update/pull")
async def pull_firmware(
    device_id: str = Query(..., description="MAC ID or device identifier")
):
    firmware_path = "firmware/firmware.bin"
    if not os.path.exists(firmware_path):
        raise HTTPException(status_code=404, detail="Firmware file not found.")
    return FileResponse(
        firmware_path,
        media_type="application/octet-stream",
        filename="firmware.bin"
    )
