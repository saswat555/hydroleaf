# app/routers/device_comm.py

from __future__ import annotations

import asyncio
from datetime import datetime
import os
from pathlib import Path
from typing import Tuple
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import httpx
try:
    import semver  # type: ignore
except Exception:  # minimal fallback
    class _SemverFallback:  # pragma: no cover
        @staticmethod
        def compare(a: str, b: str) -> int:
            def tup(s: str): return tuple(int(p) for p in s.split("."))
            ta, tb = tup(a), tup(b)
            return (ta > tb) - (ta < tb)
        class VersionInfo:
            @staticmethod
            def isvalid(s: str) -> bool:
                try:
                    _ = [int(p) for p in s.split(".")]
                    return True
                except Exception:
                    return False
            @staticmethod
            def parse(s: str):
                return tuple(int(p) for p in s.split("."))
    semver = _SemverFallback()
from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Path as PathParam,
    Query,
    Request,
)
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import API_V1_STR
from app.core.database import get_db
from app.models import (
    Device,
    TaskStatus,
    SwitchState,
    Task,
    ValveState,
)
from app.schemas import SimpleDosingCommand, DeviceType
from app.dependencies import verify_device_token
# ─────────────────────────────────────────────────────────────────────────────
# Globals & helpers
# ─────────────────────────────────────────────────────────────────────────────
router = APIRouter(tags=["device_comm"])
bearer_scheme = HTTPBearer(auto_error=True)

async def _lease_once(db, device_id: str, max_tasks: int, lease_seconds: int) -> tuple[str | None, list[Task]]:
    """
    Try to lease up to `max_tasks`. Returns (lease_id, tasks).
    Uses SKIP LOCKED on Postgres; falls back to simple select+update on SQLite.
    """
    now = datetime.now(timezone.utc)
    await _requeue_expired(db, device_id)

    # pick eligible tasks
    stmt = (
        select(Task)
        .where(
            Task.device_id == device_id,
            Task.status == TaskStatus.PENDING,
            Task.available_at <= now,
        )
        .order_by(Task.priority.desc(), Task.id.asc())
        .limit(max_tasks)
    )

    # Try to take advisory row locks on Postgres; SQLite ignores with_for_update
    try:
        rows = await db.execute(stmt.with_for_update(skip_locked=True))
    except Exception:
        rows = await db.execute(stmt)

    tasks = rows.scalars().all()
    if not tasks:
        return None, []

    lease_id = uuid4().hex
    ttl = now + timedelta(seconds=lease_seconds)
    for t in tasks:
        t.status = TaskStatus.LEASED
        t.lease_id = lease_id
        t.leased_until = ttl
        t.attempts = (t.attempts or 0) + 1
    await db.commit()
    return lease_id, tasks


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

# ---- Pydantic DTOs ----
class EnqueueTask(BaseModel):
    device_id: str
    type: str = Field(..., description="e.g. 'pump', 'valve', 'switch_event'")
    parameters: dict = Field(default_factory=dict)
    priority: int = 100
    delay_seconds: int = 0   # schedule into the future (optional)

class LeaseRequest(BaseModel):
    device_id: str
    max_tasks: int = Field(1, ge=1, le=50)
    lease_seconds: int = Field(30, ge=5, le=600)
    wait_seconds: int = Field(25, ge=0, le=60)   # long-poll window

class TaskBrief(BaseModel):
    id: int
    type: str
    parameters: dict

class LeaseResponse(BaseModel):
    lease_id: str | None
    tasks: list[TaskBrief]

class TaskResult(BaseModel):
    id: int
    success: bool
    error: str | None = None
    requeue: bool = False    # if false + !success => FAILED

class AckRequest(BaseModel):
    device_id: str
    lease_id: str
    results: list[TaskResult]




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
    try:
        version, path = _find_latest_firmware(dtype)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No firmware available for {dtype}")

    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=f"{dtype}_{version}.bin",
    )


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
        status=TaskStatus.PENDING,
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
            status=TaskStatus.PENDING,
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
async def _requeue_expired(db, device_id: str) -> None:
    now = datetime.now(timezone.utc)
    await db.execute(
        update(Task)
        .where(
            Task.device_id == device_id,
            Task.status == TaskStatus.LEASED,
            Task.leased_until.isnot(None),
            Task.leased_until < now,
        )
        .values(status=TaskStatus.PENDING, lease_id=None, leased_until=None)
    )
    # no commit; caller does (same tx)

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
@router.get("/pending_tasks", summary="[DEPRECATED] Use /tasks/lease")
async def get_pending_tasks(
    device_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    token_device_id: str = Depends(verify_device_token),
):
    if token_device_id != device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")
    # Short, non-blocking lease attempt for backward compat
    lease_id, tasks = await _lease_once(db, device_id, 10, 20)
    return [t.parameters for t in tasks]  # same shape as before: list of param dicts

@router.post("/tasks", summary="[DEPRECATED] Enqueue a pump task")
async def enqueue_pump_legacy(
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
        status=TaskStatus.PENDING,
    )
    db.add(task); await db.commit(); await db.refresh(task)
    return {"message": "Pump task enqueued", "task": task.parameters, "task_id": task.id}


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

    dev_id = payload["device_id"]
    dtype = payload.get("type", "camera")
    fw_version = payload.get("version", "0.0.0")

    dev = await db.get(Device, dev_id)
    if dev:
        dev.last_seen = func.now()
        dev.firmware_version = fw_version
        await db.commit()

    # pending pump tasks
    q = await db.execute(
        select(Task).where(
            Task.device_id == dev_id, Task.status == TaskStatus.PENDING, Task.type == "pump"
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

@router.post("/tasks/enqueue", summary="Enqueue a task for a device")
async def enqueue_task(
    req: EnqueueTask,
    db: AsyncSession = Depends(get_db),
    _device_id: str = Depends(verify_device_token),   # token must belong to that device
):
    if _device_id != req.device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")

    task = Task(
        device_id=req.device_id,
        type=req.type,
        parameters=req.parameters,
        priority=req.priority,
        available_at=datetime.now(timezone.utc) + timedelta(seconds=req.delay_seconds or 0),
        status=TaskStatus.PENDING,
    )
    db.add(task)
    await db.commit(); await db.refresh(task)
    return {"task_id": task.id, "status": "queued"}

@router.post("/tasks/lease", response_model=LeaseResponse, summary="Lease tasks (long-poll)")
async def lease_tasks(
    req: LeaseRequest,
    db: AsyncSession = Depends(get_db),
    token_device_id: str = Depends(verify_device_token),
):
    if token_device_id != req.device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")

    # long-poll loop
    deadline = asyncio.get_running_loop().time() + req.wait_seconds
    while True:
        lease_id, tasks = await _lease_once(db, req.device_id, req.max_tasks, req.lease_seconds)
        if tasks or asyncio.get_running_loop().time() >= deadline:
            return LeaseResponse(
                lease_id=lease_id,
                tasks=[TaskBrief(id=t.id, type=t.type, parameters=t.parameters or {}) for t in tasks],
            )
        await asyncio.sleep(0.5)

@router.post("/tasks/ack", summary="Acknowledge leased tasks (complete/fail/requeue)")
async def ack_tasks(
    req: AckRequest,
    db: AsyncSession = Depends(get_db),
    token_device_id: str = Depends(verify_device_token),
):
    if token_device_id != req.device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")

    now = datetime.now(timezone.utc)
    for res in req.results:
        t: Task | None = await db.get(Task, res.id)
        if not t or t.device_id != req.device_id or t.lease_id != req.lease_id or t.status != TaskStatus.LEASED:
            # ignore stale/mismatched acks to stay idempotent
            continue

        if res.success:
            t.status = TaskStatus.COMPLETED
            t.lease_id = None
            t.leased_until = None
            t.error_message = None
        else:
            if res.requeue:
                t.status = TaskStatus.PENDING
                t.lease_id = None
                t.leased_until = None
                t.available_at = now + timedelta(seconds=3)  # small backoff
                t.error_message = (res.error or "")[:255]
            else:
                t.status = TaskStatus.FAILED
                t.lease_id = None
                t.leased_until = None
                t.error_message = (res.error or "")[:255]

    await db.commit()
    return {"ok": True}

class ExtendRequest(BaseModel):
    device_id: str
    lease_id: str
    extend_seconds: int = Field(30, ge=5, le=600)

@router.post("/tasks/extend", summary="Extend lease visibility timeout")
async def extend_lease(
    req: ExtendRequest,
    db: AsyncSession = Depends(get_db),
    token_device_id: str = Depends(verify_device_token),
):
    if token_device_id != req.device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")

    now = datetime.now(timezone.utc)
    await db.execute(
        update(Task)
        .where(Task.device_id == req.device_id, Task.lease_id == req.lease_id, Task.status == TaskStatus.LEASED)
        .values(leased_until=now + timedelta(seconds=req.extend_seconds))
    )
    await db.commit()
    return {"ok": True}
