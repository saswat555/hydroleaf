# app/routers/device_comm.py
"""
Runtime-side communication between Hydroleaf devices and the cloud.

This version uses the *unified* `device_tokens` table introduced together with
the refactored `cloud.py`:

• Any device authenticates once via `/cloud/authenticate` → gets a bearer token.
• All subsequent calls attach that token in `Authorization: Bearer …`.
• A single dependency – `verify_device_token()` – validates the token and
  returns the corresponding `device_id`.

The router keeps the same external API shape, but all token checks are now
centralised and type-agnostic.
"""

from __future__ import annotations

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
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import API_V1_STR
from app.core.database import get_db
from app.models import (
    Device,
    DeviceToken,
    DeviceType,
    SwitchState,
    Task,
    ValveState,
)
from app.schemas import SimpleDosingCommand

# ─────────────────────────────────────────────────────────────────────────────
# Globals & helpers
# ─────────────────────────────────────────────────────────────────────────────
router = APIRouter(tags=["device_comm"])
bearer_scheme = HTTPBearer(auto_error=True)


def _find_latest_firmware(device_type: str) -> Tuple[str, str]:
    """
    Return (version, path) for the newest firmware in firmware/<type>/<ver>/.
    """
    base = os.path.join("firmware", device_type)
    if not os.path.isdir(base):
        raise FileNotFoundError(f"No firmware folder for '{device_type}'")
    versions = [
        d
        for d in os.listdir(base)
        if os.path.isdir(os.path.join(base, d)) and semver.VersionInfo.isvalid(d)
    ]
    if not versions:
        raise FileNotFoundError(f"No firmware versions under {base}")
    latest = str(max(versions, key=semver.VersionInfo.parse))
    bin_path = os.path.join(base, latest, "firmware.bin")
    if not os.path.isfile(bin_path):
        raise FileNotFoundError(f"Missing firmware.bin for {device_type} {latest}")
    return latest, bin_path


async def verify_device_token(
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> str:
    """
    Validate `Authorization: Bearer <token>` and return **device_id**.

    Any endpoint that needs the device id simply adds:
        token_device_id: str = Depends(verify_device_token)
    """
    token = creds.credentials
    rec = await db.scalar(select(DeviceToken).where(DeviceToken.token == token))
    if not rec:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid device token"
        )
    return rec.device_id


# ─────────────────────────────────────────────────────────────────────────────
# Firmware OTA
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/update", summary="Check for firmware update")
async def check_for_update(
    request: Request,
    device_id: str = Query(..., description="ID of this device"),
    db: AsyncSession = Depends(get_db),
    token_device_id: str = Depends(verify_device_token),
):
    if token_device_id != device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")

    dev = await db.get(Device, device_id)
    current = dev.firmware_version if dev else "0.0.0"
    dtype = dev.type.value if dev else "camera"

    try:
        latest, _ = _find_latest_firmware(dtype)
    except FileNotFoundError:
        latest = current

    download_url = (
        f"{str(request.base_url).rstrip('/')}{API_V1_STR}"
        f"/device_comm/update/pull?device_id={device_id}"
    )
    return {
        "current_version": current,
        "latest_version": latest,
        "update_available": semver.compare(latest, current) > 0,
        "download_url": download_url,
    }


@router.get("/update/pull", summary="Download latest firmware")
async def pull_firmware(
    device_id: str = Query(..., description="ID of this device"),
    db: AsyncSession = Depends(get_db),
    token_device_id: str = Depends(verify_device_token),
):
    if token_device_id != device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")

    dev = await db.get(Device, device_id)
    dtype = dev.type.value if dev else "camera"
    version, path = _find_latest_firmware(dtype)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=f"{dtype}_{version}.bin",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Switch & valve telemetry helpers
# ─────────────────────────────────────────────────────────────────────────────
class ValveEventPayload(BaseModel):
    device_id: str
    valve_id: int
    state: str  # "on" | "off"


class SwitchEventPayload(BaseModel):
    device_id: str
    channel: int
    state: str  # "on" | "off"


# ── Smart-switch event from device ───────────────────────────────────────────
@router.post("/switch_event", summary="Device → cloud switch event")
async def switch_event(
    payload: SwitchEventPayload,
    db: AsyncSession = Depends(get_db),
    token_device_id: str = Depends(verify_device_token),
):
    if token_device_id != payload.device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")

    task = Task(
        device_id=payload.device_id,
        type="switch_event",
        parameters={"channel": payload.channel, "state": payload.state},
        status="received",
    )
    db.add(task)

    ss = await db.get(SwitchState, payload.device_id)
    if not ss:
        ss = SwitchState(device_id=payload.device_id, states={})
        db.add(ss)
    ss.states[str(payload.channel)] = payload.state
    await db.commit()
    return {"message": "Switch event recorded"}


# ── Switch toggle (cloud → device) ───────────────────────────────────────────
@router.post("/switch/{device_id}/toggle", summary="Toggle a switch channel")
async def toggle_switch(
    device_id: str = PathParam(..., description="ID of the smart switch"),
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    token_device_id: str = Depends(verify_device_token),
):
    if token_device_id != device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")

    channel = body.get("channel")
    if not isinstance(channel, int) or not (1 <= channel <= 8):
        raise HTTPException(400, "Channel must be 1–8")

    device = await db.get(Device, device_id)
    if not device or device.type != DeviceType.SMART_SWITCH:
        raise HTTPException(404, "Smart switch not found")

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{device.http_endpoint.rstrip('/')}/toggle",
            json={"channel": channel},
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()

    db.add(
        Task(
            device_id=device_id,
            type="switch",
            parameters={"channel": channel, "new_state": data.get("new_state")},
        )
    )
    await db.commit()
    return data


# ── Valve controller event ──────────────────────────────────────────────────
@router.post("/valve_event", summary="Device → cloud valve event")
async def valve_event(
    payload: ValveEventPayload,
    db: AsyncSession = Depends(get_db),
    token_device_id: str = Depends(verify_device_token),
):
    if token_device_id != payload.device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")

    db.add(
        Task(
            device_id=payload.device_id,
            type="valve_event",
            parameters={"valve_id": payload.valve_id, "state": payload.state},
            status="received",
        )
    )
    vs = await db.get(ValveState, payload.device_id)
    if not vs:
        vs = ValveState(device_id=payload.device_id, states={})
        db.add(vs)
    vs.states[str(payload.valve_id)] = payload.state
    await db.commit()
    return {"message": "Valve event recorded"}


# ── Valve helpers (cloud → device) ──────────────────────────────────────────
@router.get("/valve/{device_id}/state", summary="Fetch valve states")
async def get_valve_state(
    device_id: str = PathParam(...),
    db: AsyncSession = Depends(get_db),
    token_device_id: str = Depends(verify_device_token),
):
    if token_device_id != device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")

    dev = await db.get(Device, device_id)
    if not dev or dev.type != DeviceType.VALVE_CONTROLLER:
        raise HTTPException(404, "Valve controller not found")

    # live call first
    try:
        async with httpx.AsyncClient() as cli:
            r = await cli.get(f"{dev.http_endpoint.rstrip('/')}/state", timeout=5)
            r.raise_for_status()
            return r.json()
    except Exception:
        vs = await db.get(ValveState, device_id)
        if not vs:
            raise HTTPException(503, "Device unreachable and no cached state")
        return {
            "device_id": device_id,
            "valves": [{"id": int(k), "state": v} for k, v in vs.states.items()],
        }


@router.post("/valve/{device_id}/toggle", summary="Toggle a valve")
async def toggle_valve(
    device_id: str = PathParam(...),
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    token_device_id: str = Depends(verify_device_token),
):
    if token_device_id != device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")

    valve_id = body.get("valve_id")
    if not isinstance(valve_id, int) or not (1 <= valve_id <= 4):
        raise HTTPException(400, "valve_id must be 1–4")

    dev = await db.get(Device, device_id)
    if not dev or dev.type != DeviceType.VALVE_CONTROLLER:
        raise HTTPException(404, "Valve controller not found")

    async with httpx.AsyncClient() as cli:
        r = await cli.post(
            f"{dev.http_endpoint.rstrip('/')}/toggle",
            json={"valve_id": valve_id},
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()

    db.add(
        Task(
            device_id=device_id,
            type="valve",
            parameters={"valve_id": valve_id, "new_state": data.get("new_state")},
        )
    )
    await db.commit()
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Pump-task helpers (dosing units)
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/pending_tasks", summary="List pending pump tasks")
async def get_pending_tasks(
    device_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    token_device_id: str = Depends(verify_device_token),
):
    if token_device_id != device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")

    rows = await db.execute(
        select(Task).where(
            Task.device_id == device_id,
            Task.status == "pending",
            Task.type == "pump",
        )
    )
    return [t.parameters for t in rows.scalars().all()]


@router.post("/tasks", summary="Enqueue a pump task")
async def enqueue_pump(
    body: SimpleDosingCommand,
    device_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    token_device_id: str = Depends(verify_device_token),
):
    if token_device_id != device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")

    task = Task(
        device_id=device_id,
        type="pump",
        parameters={"pump": body.pump, "amount": body.amount},
        status="pending",
    )
    db.add(task)
    await db.commit()
    return {"message": "Pump task enqueued", "task": task.parameters}


# ─────────────────────────────────────────────────────────────────────────────
# Heart-beat
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/heartbeat", summary="Device heartbeat")
async def heartbeat(
    request: Request,
    db: AsyncSession = Depends(get_db),
    token_device_id: str = Depends(verify_device_token),
):
    payload = await request.json()
    if payload.get("device_id") != token_device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")

    mac = payload["device_id"]
    dtype = payload.get("type", "camera")
    fw_version = payload.get("version", "0.0.0")

    dev = await db.scalar(select(Device).where(Device.mac_id == mac))
    if dev:
        dev.last_seen = func.now()
        dev.firmware_version = fw_version
        await db.commit()

    # pending pump tasks
    q = await db.execute(
        select(Task).where(
            Task.device_id == mac, Task.status == "pending", Task.type == "pump"
        )
    )
    tasks = [t.parameters for t in q.scalars().all()]

    # OTA check
    try:
        latest, _ = _find_latest_firmware(dtype)
        available = semver.compare(latest, fw_version) > 0
    except Exception:
        latest, available = fw_version, False

    return {
        "status": "ok",
        "status_message": "All systems nominal",
        "tasks": tasks,
        "update": {
            "current": fw_version,
            "latest": latest,
            "available": available,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Switch state helper
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/switch/{device_id}/state", summary="Fetch switch states")
async def get_switch_state(
    device_id: str = PathParam(...),
    db: AsyncSession = Depends(get_db),
    token_device_id: str = Depends(verify_device_token),
):
    if token_device_id != device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")

    dev = await db.get(Device, device_id)
    if not dev or dev.type != DeviceType.SMART_SWITCH:
        raise HTTPException(404, "Smart switch not found")

    try:
        async with httpx.AsyncClient() as cli:
            r = await cli.get(f"{dev.http_endpoint.rstrip('/')}/state", timeout=5)
            r.raise_for_status()
            return r.json()
    except Exception:
        ss = await db.get(SwitchState, device_id)
        if not ss:
            raise HTTPException(
                status_code=503,
                detail="Device unreachable and no cached state",
            )
        return {
            "device_id": device_id,
            "switches": [
                {"channel": int(k), "state": v} for k, v in ss.states.items()
            ],
        }
