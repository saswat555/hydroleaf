# app/routers/device_comm.py

import os
from pathlib import Path
from typing import Tuple

import httpx
import semver
from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Path as PathParam,
    Query,
    Request,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import API_V1_STR
from app.core.database import get_db
from app.dependencies import verify_dosing_device_token, verify_valve_device_token
from app.dependencies import verify_switch_device_token
from app.models import SwitchState
from app.models import Device, Task, ValveState
from app.schemas import SimpleDosingCommand, DeviceType

router = APIRouter(tags=["device_comm"])


def find_latest_firmware(device_type: str) -> Tuple[str, str]:
    """
    Scan `firmware/<device_type>/<version>/firmware.bin` folders
    and return the latest version and path to its .bin.
    """
    base = os.path.join("firmware", device_type)
    if not os.path.isdir(base):
        raise FileNotFoundError(f"No firmware folder for device type '{device_type}'")
    versions = [
        d for d in os.listdir(base)
        if os.path.isdir(os.path.join(base, d)) and semver.VersionInfo.isvalid(d)
    ]
    if not versions:
        raise FileNotFoundError(f"No versioned firmware found under {base}")
    latest = str(max(versions, key=semver.VersionInfo.parse))
    binpath = os.path.join(base, latest, "firmware.bin")
    if not os.path.isfile(binpath):
        raise FileNotFoundError(f"Missing firmware.bin in {base}/{latest}")
    return latest, binpath


@router.get(
    "/update",
    summary="Check for firmware update (dosing device)",
    dependencies=[Depends(verify_dosing_device_token)],
)
async def check_for_update(
    request: Request,
    device_id: str = Query(..., description="MAC ID of this dosing device"),
    db: AsyncSession = Depends(get_db),
):
    # 1) Lookup
    result = await db.execute(select(Device).where(Device.mac_id == device_id))
    device = result.scalar_one_or_none()
    current = device.firmware_version if device else "0.0.0"
    dtype = device.type.value if device else "camera"

    # 2) Latest on disk
    try:
        latest, _ = find_latest_firmware(dtype)
    except FileNotFoundError:
        latest = current

    # 3) Compare
    available = semver.compare(latest, current) > 0

    # 4) Download URL
    base = str(request.base_url).rstrip("/")
    url = f"{base}{API_V1_STR}/device_comm/update/pull?device_id={device_id}"

    return {
        "current_version": current,
        "latest_version": latest,
        "update_available": available,
        "download_url": url,
    }


@router.get(
    "/update/pull",
    summary="Download the latest firmware (dosing device)",
    dependencies=[Depends(verify_dosing_device_token)],
)
async def pull_firmware(
    device_id: str = Query(..., description="MAC ID of this dosing device"),
    db: AsyncSession = Depends(get_db),
):
    # Lookup device type again
    result = await db.execute(select(Device).where(Device.mac_id == device_id))
    device = result.scalar_one_or_none()
    dtype = device.type.value if device else "camera"

    try:
        version, path = find_latest_firmware(dtype)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Firmware not found")

    filename = f"{dtype}_{version}.bin"
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
    )


class ValveEventPayload(BaseModel):
    device_id: str
    valve_id: int
    state: str  # "on" or "off"
class SwitchEventPayload(BaseModel):
    device_id: str
    channel: int
    state: str  # "on" or "off"
@router.post(
    "/switch_event",
    summary="Record a switch toggle event (smart switch)",
    dependencies=[Depends(verify_switch_device_token)],
)
async def switch_event(
    payload: SwitchEventPayload,
    db: AsyncSession = Depends(get_db),
):
    # store a Task log
    task = Task(
        device_id=payload.device_id,
        type="switch_event",
        parameters={"channel": payload.channel, "state": payload.state},
        status="received",
    )
    db.add(task)
    # update cached state
    ss = await db.get(SwitchState, payload.device_id)
    if ss:
        ss.states[str(payload.channel)] = payload.state
    else:
        ss = SwitchState(device_id=payload.device_id,
                         states={str(payload.channel): payload.state})
        db.add(ss)
    await db.commit()
    return {"message": "Switch event recorded"}
@router.post(
    "/switch/{device_id}/toggle",
    summary="Toggle a single switch channel",
    dependencies=[Depends(verify_switch_device_token)],
)
async def toggle_switch(
    device_id: str = PathParam(..., description="MAC ID of the smart switch"),
    body: dict = Body(..., media_type="application/json"),
    db: AsyncSession = Depends(get_db),
):
    channel = body.get("channel")
    if not isinstance(channel, int) or not (1 <= channel <= 8):
        raise HTTPException(status_code=400, detail="Invalid channel (must be 1–8)")

    device = await db.get(Device, device_id)
    if not device or device.type != DeviceType.SMART_SWITCH:
        raise HTTPException(status_code=404, detail="Smart switch not found")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{device.http_endpoint.rstrip('/')}/toggle",
            json={"channel": channel},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()

    # enqueue a Task record
    task = Task(
        device_id=device_id,
        type="switch",
        parameters={"channel": channel, "new_state": data.get("new_state")},
    )
    db.add(task)
    await db.commit()

    return data
@router.post(
    "/valve_event",
    summary="Record a valve toggle event (valve controller)",
    dependencies=[Depends(verify_valve_device_token)],
)
async def valve_event(
    payload: ValveEventPayload,
    db: AsyncSession = Depends(get_db),
):
    task = Task(
        device_id=payload.device_id,
        type="valve_event",
        parameters={"valve_id": payload.valve_id, "state": payload.state},
        status="received",
    )
    db.add(task)
    await db.commit()
    vs = await db.get(ValveState, payload.device_id)
    if vs:
        vs.states[str(payload.valve_id)] = payload.state
    else:
        vs = ValveState(device_id=payload.device_id,
                        states={ str(payload.valve_id): payload.state })
        db.add(vs)
    await db.commit()
    return {"message": "Valve event recorded"}


@router.get(
    "/valve/{device_id}/state",
    summary="Fetch current valve states",
    dependencies=[Depends(verify_valve_device_token)],
)
async def get_valve_state(
    device_id: str = PathParam(..., description="MAC ID of the valve controller"),
    db: AsyncSession = Depends(get_db),
):
    device = await db.get(Device, device_id)
    if not device or device.type != DeviceType.VALVE_CONTROLLER:
        raise HTTPException(status_code=404, detail="Valve controller not found")

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{device.http_endpoint.rstrip('/')}/state", timeout=5)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            # fallback to last‐saved DB state
            vs = await db.get(ValveState, device_id)
            if not vs:
                raise HTTPException(status_code=503, detail="Device unreachable, no cached state")
            return {
                "device_id": device_id,
                "valves": [
                    {"id": int(k), "state": v}
                    for k, v in vs.states.items()
                ]
            }


@router.post(
    "/valve/{device_id}/toggle",
    summary="Toggle a single valve",
    dependencies=[Depends(verify_valve_device_token)],
)
async def toggle_valve(
    device_id: str = PathParam(..., description="MAC ID of the valve controller"),
    body: dict = Body(..., media_type="application/json"),
    db: AsyncSession = Depends(get_db),
):
    valve_id = body.get("valve_id")
    if not isinstance(valve_id, int) or not (1 <= valve_id <= 4):
        raise HTTPException(status_code=400, detail="Invalid valve_id (must be 1–4)")

    device = await db.get(Device, device_id)
    if not device or device.type != DeviceType.VALVE_CONTROLLER:
        raise HTTPException(status_code=404, detail="Valve controller not found")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{device.http_endpoint.rstrip('/')}/toggle",
            json={"valve_id": valve_id},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()

    task = Task(
        device_id=device_id,
        type="valve",
        parameters={"valve_id": valve_id, "new_state": data.get("new_state")},
    )
    db.add(task)
    await db.commit()

    return data


@router.get(
    "/pending_tasks",
    summary="Get pending pump tasks (dosing device)",
    dependencies=[Depends(verify_dosing_device_token)],
)
async def get_pending_tasks(
    device_id: str = Query(..., description="MAC ID of this dosing device"),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Task).where(
            Task.device_id == device_id,
            Task.status == "pending",
            Task.type == "pump",
        )
    )
    return [t.parameters for t in result.scalars().all()]


@router.post(
    "/heartbeat",
    summary="Device heartbeat (returns pump tasks & OTA info)",
    dependencies=[Depends(verify_dosing_device_token)],
)
async def heartbeat(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.json()
    mac = payload.get("device_id")
    dtype = payload.get("type")
    ver = payload.get("version")

    # Update last_seen & firmware_version
    device = await db.scalar(select(Device).where(Device.mac_id == mac))
    if device:
        device.last_seen = func.now()
        device.firmware_version = ver
        await db.commit()

    # Collect pending pump tasks
    q = await db.execute(
        select(Task).where(
            Task.device_id == mac,
            Task.status == "pending",
            Task.type == "pump",
        )
    )
    tasks = [t.parameters for t in q.scalars().all()]

    # OTA check
    try:
        latest_ver, _ = find_latest_firmware(dtype)
        available = semver.compare(latest_ver, ver) > 0
    except Exception:
        latest_ver, available = ver, False

    return {
        "status": "ok",
        "status_message": "All systems nominal",
        "tasks": tasks,
        "update": {
            "current": ver,
            "latest": latest_ver,
            "available": available,
        },
    }


@router.post(
    "/tasks",
    summary="Enqueue a dosing task",
    dependencies=[Depends(verify_dosing_device_token)],
)
async def enqueue_pump(
    body: SimpleDosingCommand,
    device_id: str = Query(..., description="MAC ID of this dosing device"),
    db: AsyncSession = Depends(get_db),
):
    task = Task(
        device_id=device_id,
        type="pump",
        parameters={"pump": body.pump, "amount": body.amount},
        status="pending",
    )
    db.add(task)
    await db.commit()
    return {"message": "Pump task enqueued", "task": task.parameters}

@router.get(
    "/switch/{device_id}/state",
    summary="Fetch current switch states",
    dependencies=[Depends(verify_switch_device_token)],
)
async def get_switch_state(
    device_id: str = PathParam(..., description="MAC ID of the smart switch"),
    db: AsyncSession = Depends(get_db),
):
    device = await db.get(Device, device_id)
    if not device or device.type != DeviceType.SMART_SWITCH:
        raise HTTPException(status_code=404, detail="Smart switch not found")

    # Try the live device first
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{device.http_endpoint.rstrip('/')}/state", timeout=5)
            resp.raise_for_status()
            return resp.json()
    except Exception:
        # Fallback to cached DB state
        ss = await db.get(SwitchState, device_id)
        if not ss:
            raise HTTPException(status_code=503, detail="Device unreachable, no cached state")
        return {
            "device_id": device_id,
            "switches": [
                {"channel": int(k), "state": v}
                for k, v in ss.states.items()
            ]
        }

