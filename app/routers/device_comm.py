from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse
import httpx
import semver
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DEPLOYMENT_MODE
from app.core.database import get_db
from app.dependencies import get_current_device
from app.models import Device, Task
from pydantic import BaseModel

import os

from app.schemas import DeviceType, SimpleDosingCommand
from app.services.device_controller import DeviceController

router = APIRouter(prefix="/api/v1/device_comm", tags=["device_comm"])
 


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
class ValveEventPayload(BaseModel):
    device_id: str
    valve_id: int
    state: str  # "on" or "off"

@router.post("/valve_event", summary="Receive a valve toggle event from device")
async def valve_event(
    payload: ValveEventPayload,
    db: AsyncSession = Depends(get_db)
):
    # Store it as a pending task (or log for admin)
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


def find_latest_firmware(device_type: str) -> tuple[str,str]:
    """
    Scan ./firmware/<device_type> for version folders,
    return (latest_version, path_to_bin).
    """
    base = os.path.join("firmware", device_type)
    if not os.path.isdir(base):
        raise FileNotFoundError(f"No firmware folder for device type '{device_type}'")
    versions = [d for d in os.listdir(base)
                if os.path.isdir(os.path.join(base, d)) and semver.VersionInfo.isvalid(d)]
    if not versions:
        raise FileNotFoundError(f"No versioned firmware found under {base}")
    latest = str(max(versions, key=semver.VersionInfo.parse))
    binpath = os.path.join(base, latest, "firmware.bin")
    if not os.path.isfile(binpath):
        raise FileNotFoundError(f"Missing firmware.bin in {base}/{latest}")
    return latest, binpath

@router.get("/pending_tasks")
async def get_pending_tasks(
    device_id: str = Query(..., description="MAC ID or device identifier"),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Task).where(Task.device_id == device_id, Task.status == "pending")
    )
    return [t.to_dict() for t in result.scalars().all()]

@router.post("/heartbeat", dependencies=[Depends(get_current_device)])
async def heartbeat(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.json()
    mac  = payload["device_id"]
    dtype= payload["type"]
    ver  = payload["version"]

    # 1) update last_seen & firmware_version…
    device = await db.scalar(select(Device).where(Device.mac_id == mac))
    device.last_seen = func.now()
    device.firmware_version = ver
    await db.commit()

    # 2) collect pending pump tasks
    result = await db.execute(
        select(Task).where(Task.device_id == mac, Task.status == "pending", Task.type == "pump")
    )
    tasks = [t.parameters for t in result.scalars().all()]

    # 3) check for OTA
    try:
        latest_ver, _ = find_latest_firmware(dtype)
        update_available = semver.compare(latest_ver, ver) > 0
    except Exception:
        latest_ver, update_available = ver, False

    return {
      "status":         "ok",
      "status_message": "All systems nominal",     # or any custom message
      "tasks":          tasks,                    # e.g. [{ "pump":1, "amount":50 }, …]
      "update": {
         "current":   ver,
         "latest":    latest_ver,
         "available": update_available
      }
    }


@router.get("/update")
async def check_for_update(device_id: str = Query(...), db: AsyncSession = Depends(get_db)):
    device = await db.scalar(select(Device).where(Device.mac_id == device_id))
    if not device:
        raise HTTPException(404, "Device not registered")
    latest, _ = find_latest_firmware(device.type.value)
    available = semver.compare(latest, device.firmware_version or "0.0.0") > 0
    return {"version": latest, "update_available": available}

@router.get("/update/pull")
async def pull_firmware(device_id: str = Query(...), db: AsyncSession = Depends(get_db)):
    device = await db.scalar(select(Device).where(Device.mac_id == device_id))
    latest, path = find_latest_firmware(device.type.value)
    return FileResponse(path, media_type="application/octet-stream", filename=f"{device.type.value}-{latest}.bin")


@router.post("/tasks", summary="Enqueue a dosing task")
async def enqueue_pump(
    body: SimpleDosingCommand,                # from your schemas: { pump:int, amount:float }
    device_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    task = Task(
      device_id = device_id,
      type      = "pump",
      parameters= { "pump": body.pump, "amount": body.amount },
      status    = "pending"
    )
    db.add(task)
    await db.commit()
    return {"message": "Pump task enqueued", "task": task.to_dict()}
