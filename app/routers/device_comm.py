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
from app.dependencies import (
    verify_dosing_device_token,
    verify_valve_device_token,
    verify_switch_device_token,
    get_ota_authorized_device,
)
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
    summary="Check for firmware update for any authorized device",
)
async def check_for_update(
    request: Request,
    current_device: Device = Depends(get_ota_authorized_device),
    db: AsyncSession = Depends(get_db), # db is already available via get_ota_authorized_device, but keeping for clarity if direct db ops are needed
):
    # Device is already fetched and authorized by get_ota_authorized_device
    # current_device.mac_id is the device identifier
    # current_device.type.value is the device type
    # current_device.firmware_version is the current firmware version

    current_version = current_device.firmware_version if current_device.firmware_version else "0.0.0"
    device_type = current_device.type.value

    # 2) Latest on disk
    try:
        latest, _ = find_latest_firmware(device_type)
    except FileNotFoundError:
        latest = current_version

    # 3) Compare
    available = semver.compare(latest, current_version) > 0

    # 4) Download URL
    base = str(request.base_url).rstrip("/")
    # The pull endpoint will also use get_ota_authorized_device, so no need to pass device_id as query param
    url = f"{base}{API_V1_STR}/device_comm/update/pull" 

    return {
        "current_version": current_version,
        "latest_version": latest,
        "update_available": available,
        "download_url": url,
    }


@router.get(
    "/update/pull",
    summary="Download the latest firmware for any authorized device",
)
async def pull_firmware(
    current_device: Device = Depends(get_ota_authorized_device),
    # db: AsyncSession = Depends(get_db), # db is available via dependency if needed
):
    # Device is already fetched and authorized by get_ota_authorized_device
    device_type = current_device.type.value

    try:
        version, path = find_latest_firmware(device_type)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Firmware not found for device type {device_type}")

    filename = f"{device_type}_{version}.bin"
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
    summary="Device heartbeat (returns pump tasks & OTA info for any authorized device)",
)
async def heartbeat(
    request: Request,
    current_device: Device = Depends(get_ota_authorized_device), # Authorizes and fetches device
    db: AsyncSession = Depends(get_db), # db is already available via get_ota_authorized_device
):
    payload = await request.json()
    payload_device_id = payload.get("device_id")
    payload_type = payload.get("type")
    payload_version = payload.get("version")

    # Verification step
    if payload_device_id != current_device.mac_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Payload device_id '{payload_device_id}' does not match authenticated device '{current_device.mac_id}'",
        )
    if payload_type != current_device.type.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Payload type '{payload_type}' does not match authenticated device type '{current_device.type.value}'",
        )

    # Use current_device from token for DB operations and reliable data
    # The firmware version from DB (before update) is current_device.firmware_version
    # The device type from DB is current_device.type.value

    # Update last_seen & firmware_version in DB with payload version
    current_device.last_seen = func.now() # Use func.now() for database timestamp
    current_device.firmware_version = payload_version # Update DB with version from heartbeat payload
    db.add(current_device) # Add to session for commit
    await db.commit()
    await db.refresh(current_device) # Refresh to get updated state if needed elsewhere

    # Collect pending pump tasks (if applicable for this device type)
    # This part might need adjustment if not all devices have 'pump' tasks
    tasks = []
    if current_device.type == DeviceType.DOSING_UNIT: # Example: only dosing units have pump tasks
        q = await db.execute(
            select(Task).where(
                Task.device_id == current_device.mac_id, # Use authenticated device_id
                Task.status == "pending",
                Task.type == "pump", # Assuming 'pump' is a valid task type
            )
        )
        tasks = [t.parameters for t in q.scalars().all()]

    # OTA check using device data from token (current state before this heartbeat)
    # and comparing with latest available firmware for its type.
    # The 'current_firmware_for_ota_check' is what the device *had* before this heartbeat reported a new version.
    # However, standard practice is to report based on the version the device *currently claims to have*.
    current_firmware_for_ota_check = payload_version # Use the version reported in THIS heartbeat for OTA decision
    device_type_for_ota_check = current_device.type.value

    try:
        latest_ver, _ = find_latest_firmware(device_type_for_ota_check)
        update_available = semver.compare(latest_ver, current_firmware_for_ota_check) > 0
    except FileNotFoundError: # No firmware path for this device type
        latest_ver = current_firmware_for_ota_check # No update if specific firmware type not found
        update_available = False
    except Exception: # Other errors like invalid version format in files
        latest_ver = current_firmware_for_ota_check
        update_available = False


    return {
        "status": "ok",
        "status_message": "All systems nominal",
        "tasks": tasks, # Return tasks relevant to the device
        "update": {
            "current": current_firmware_for_ota_check, # Version device reports now
            "latest": latest_ver,
            "available": update_available,
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

