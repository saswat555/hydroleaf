from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from fastapi.responses import FileResponse
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DEPLOYMENT_MODE
from app.core.database import get_db
from app.models import Device, Task
from pydantic import BaseModel

import os

from app.schemas import DeviceType
from app.services.device_controller import DeviceController

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
router = APIRouter(prefix="/api/v1/device_comm", tags=["device_comm"])

class ValveEventPayload(BaseModel):
    device_id: str
    valve_id: int
    state: str  # "on" or "off"

@router.post("/valve_event", summary="Receive a valve toggle event from device")
async def valve_event(
    payload: ValveEventPayload,
    db: AsyncSession = Depends(get_db)
):
    task = Task(
        device_id=payload.device_id,
        type="valve_event",
        parameters={"valve_id": payload.valve_id, "state": payload.state},
        status="received"
    )
    db.add(task)
    await db.commit()
    return {"message": "Valve event recorded"}

@router.get("/valve/{device_id}/state", summary="Fetch current valve states")
async def get_valve_state(
    device_id: str = Path(..., description="MAC ID of the valve controller"),
    db: AsyncSession = Depends(get_db),
):
    device = await db.get(Device, device_id)
    if not device or device.type != DeviceType.VALVE_CONTROLLER:
        raise HTTPException(status_code=404, detail="Valve controller not found")
    # call the device's /state endpoint
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{device.http_endpoint.rstrip('/')}/state", timeout=5)
        resp.raise_for_status()
        return resp.json()

@router.post("/valve/{device_id}/toggle", summary="Toggle a single valve")
async def toggle_valve(
    device_id: str = Path(..., description="MAC ID of the valve controller"),
    body: dict = Body(..., media_type="application/json"),
    db: AsyncSession = Depends(get_db),
):
    """
    Body: { "valve_id": 1 }
    """
    valve_id = body.get("valve_id")
    if not isinstance(valve_id, int) or not (1 <= valve_id <= 4):
        raise HTTPException(status_code=400, detail="Invalid valve_id (must be 1–4)")
    device = await db.get(Device, device_id)
    if not device or device.type != DeviceType.VALVE_CONTROLLER:
        raise HTTPException(status_code=404, detail="Valve controller not found")
    # forward to hardware
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{device.http_endpoint.rstrip('/')}/toggle",
            json={"valve_id": valve_id},
            timeout=5
        )
        resp.raise_for_status()
        data = resp.json()
    # also enqueue in our Task table for audit/logging if you like
    task = Task(device_id=device_id, type="valve", parameters={"valve_id": valve_id, "new_state": data.get("new_state")})
    db.add(task)
    await db.commit()
    return data