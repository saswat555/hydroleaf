# app/routers/device_comm.py

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Tuple
from uuid import uuid4
from datetime import datetime, timedelta, timezone

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
    Response,
    status as http_status,
)
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import API_V1_STR, TESTING
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
# Router
# ─────────────────────────────────────────────────────────────────────────────
router = APIRouter(tags=["device_comm"])

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _has_result_payload_column() -> bool:
    # avoid hard failing if migration hasn't run yet
    return hasattr(Task, "result_payload")

async def _authz_optional_device(request: Request, db: AsyncSession, expected_device_id: str) -> None:
    """
    Enforce device token in production; allow missing token in tests.
    If an Authorization header *is* present, we validate it either way.
    """
    auth = request.headers.get("authorization", "")
    if not auth:
        if TESTING:
            return
        raise HTTPException(status_code=401, detail="Missing bearer token")
    try:
        scheme, token = auth.split(" ", 1)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    creds = HTTPAuthorizationCredentials(scheme=scheme, credentials=token)
    device_id_from_token = await verify_device_token(creds=creds, db=db)
    if device_id_from_token != expected_device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")

async def _requeue_expired(db: AsyncSession, device_id: str) -> None:
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

async def _lease_once(db: AsyncSession, device_id: str, max_tasks: int, lease_seconds: int) -> tuple[str | None, list[Task]]:
    """
    Try to lease up to `max_tasks`. Returns (lease_id, tasks).
    Uses SKIP LOCKED on Postgres; falls back gracefully on SQLite.
    """
    now = datetime.now(timezone.utc)
    await _requeue_expired(db, device_id)

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

def _public_status(t: TaskStatus) -> str:
    if t == TaskStatus.COMPLETED:
        return "done"
    if t == TaskStatus.FAILED:
        return "error"
    if t == TaskStatus.CANCELLED:
        return "cancelled"
    return "queued"

async def _update_cached_state_for_result(device_id: str, kind: str, payload: dict, db: AsyncSession):
    """
    Update cached state tables when a toggle completes.
    """
    if kind in ("valve_toggle", "valve"):
        vid = payload.get("valve_id")
        new_state = payload.get("new_state")
        if isinstance(vid, int) and new_state in ("on", "off"):
            vs = await db.get(ValveState, device_id)
            if not vs:
                vs = ValveState(device_id=device_id, states={})
                db.add(vs)
            vs.states[str(vid)] = new_state
            await db.flush()
    if kind in ("switch_toggle", "switch"):
        ch = payload.get("channel")
        new_state = payload.get("new_state")
        if isinstance(ch, int) and new_state in ("on", "off"):
            ss = await db.get(SwitchState, device_id)
            if not ss:
                ss = SwitchState(device_id=device_id, states={})
                db.add(ss)
            ss.states[str(ch)] = new_state
            await db.flush()

# ─────────────────────────────────────────────────────────────────────────────
# DTOs
# ─────────────────────────────────────────────────────────────────────────────
class ValveEventPayload(BaseModel):
    device_id: str
    valve_id: int
    state: str  # "on" | "off"

class SwitchEventPayload(BaseModel):
    device_id: str
    channel: int
    state: str  # "on" | "off"

class EnqueueTask(BaseModel):
    device_id: str
    type: str = Field(..., description="e.g. 'pump', 'valve', 'switch_event'")
    parameters: dict = Field(default_factory=dict)
    priority: int = 100
    delay_seconds: int = 0

class LeaseRequest(BaseModel):
    device_id: str
    max_tasks: int = Field(1, ge=1, le=50)
    lease_seconds: int = Field(30, ge=5, le=600)
    wait_seconds: int = Field(25, ge=0, le=60)

class TaskBrief(BaseModel):
    id: str
    type: str
    parameters: dict

class LeaseResponse(BaseModel):
    lease_id: str | None
    tasks: list[TaskBrief]

class TaskResult(BaseModel):
    id: str
    success: bool
    error: str | None = None
    requeue: bool = False

class AckRequest(BaseModel):
    device_id: str
    lease_id: str
    results: list[TaskResult]

# Simple queue (compat)
class SimpleRequest(BaseModel):
    device_id: str
    kind: str = Field(..., description="e.g., 'read_sensors', 'pump', 'valve_toggle', 'switch_toggle', 'cancel_dosing'")
    payload: dict = Field(default_factory=dict)

class SimpleResult(BaseModel):
    status: str = Field(..., description="'ok' | 'error'")
    payload: dict = Field(default_factory=dict)

class ExtendRequest(BaseModel):
    device_id: str
    lease_id: str
    extend_seconds: int = Field(30, ge=5, le=600)

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

# ─────────────────────────────────────────────────────────────────────────────
# Device → cloud events (switch/valve)
# ─────────────────────────────────────────────────────────────────────────────
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

# ─────────────────────────────────────────────────────────────────────────────
# Cloud → device helpers (switch/valve)
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
            raise HTTPException(status_code=503, detail="Device unreachable and no cached state")
        return {
            "device_id": device_id,
            "switches": [{"channel": int(k), "state": v} for k, v in ss.states.items()],
        }

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
# Pump-task legacy helpers (back-compat)
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/pending_tasks", summary="[DEPRECATED] Use /tasks/lease")
async def get_pending_tasks(
    device_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    token_device_id: str = Depends(verify_device_token),
):
    if token_device_id != device_id:
        raise HTTPException(status_code=401, detail="Token/device mismatch")
    lease_id, tasks = await _lease_once(db, device_id, 10, 20)
    return [t.parameters for t in tasks]

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
# Heartbeat
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

    q = await db.execute(
        select(Task).where(
            Task.device_id == dev_id, Task.status == TaskStatus.PENDING, Task.type == "pump"
        )
    )
    tasks = [t.parameters for t in q.scalars().all()]

    try:
        latest, _ = _find_latest_firmware(dtype)
        available = semver.compare(latest, fw_version) > 0
    except Exception:
        latest, available = fw_version, False

    return {
        "status": "ok",
        "status_message": "All systems nominal",
        "tasks": tasks,
        "update": {"current": fw_version, "latest": latest, "available": available},
    }

# ─────────────────────────────────────────────────────────────────────────────
# Modern leasing queue (SaaS)
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/tasks/enqueue", summary="Enqueue a task for a device")
async def enqueue_task(
    req: EnqueueTask,
    db: AsyncSession = Depends(get_db),
    _device_id: str = Depends(verify_device_token),
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
                t.available_at = now + timedelta(seconds=3)
                t.error_message = (res.error or "")[:255]
            else:
                t.status = TaskStatus.FAILED
                t.lease_id = None
                t.leased_until = None
                t.error_message = (res.error or "")[:255]
    await db.commit()
    return {"ok": True}

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

# ─────────────────────────────────────────────────────────────────────────────
# Simple test-compat queue (public API used by tests)
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/request")
async def enqueue_simple_request(req: SimpleRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Enqueue a one-off task and return its id + public status.
    Auth: required in prod; optional in tests.
    """
    await _authz_optional_device(request, db, expected_device_id=req.device_id)
    task = Task(
        device_id=req.device_id,
        type=req.kind,
        parameters=req.payload or {},
        status=TaskStatus.PENDING,
        priority=100,
        available_at=datetime.now(timezone.utc),
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return {"id": task.id, "status": _public_status(task.status)}

@router.get("/tasks/{task_id}")
async def get_simple_task(task_id: str, db: AsyncSession = Depends(get_db)):
    """
    Return task status and any posted result payload.
    """
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    payload = None
    if _has_result_payload_column():
        payload = task.result_payload
    if payload is None and isinstance(task.parameters, dict) and "_result" in task.parameters:
        payload = task.parameters.get("_result")

    return {"id": task.id, "status": _public_status(task.status), "payload": payload}

@router.post("/tasks/{task_id}/result")
async def post_simple_result(
    task_id: str,
    body: SimpleResult,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Device posts result for a task. Updates cached states when relevant.
    Auth: required in prod; optional in tests.
    """
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    await _authz_optional_device(request, db, expected_device_id=task.device_id)

    # Store result in result_payload (preferred) or parameters["_result"] fallback
    if _has_result_payload_column():
        setattr(task, "result_payload", body.payload or {})
    else:
        params = dict(task.parameters or {})
        params["_result"] = body.payload or {}
        task.parameters = params

    if body.status.lower() == "ok":
        task.status = TaskStatus.COMPLETED
    elif body.status.lower() == "error":
        task.status = TaskStatus.FAILED
    else:
        task.status = TaskStatus.FAILED
        task.error_message = f"Unknown status '{body.status}'"

    try:
        # try to reflect state changes in caches
        payload_for_cache = (body.payload or {})
        await _update_cached_state_for_result(task.device_id, task.type, payload_for_cache, db)
    finally:
        await db.commit()
        await db.refresh(task)

    return {"id": task.id, "status": _public_status(task.status)}

@router.get("/device_state/{device_id}")
async def get_cached_device_state(device_id: str, db: AsyncSession = Depends(get_db)):
    """
    Return last-known cached state for valve/switch devices (204 if none).
    """
    vs = await db.get(ValveState, device_id)
    if vs and vs.states:
        return {"device_id": device_id, "valves": [{"id": int(k), "state": v} for k, v in vs.states.items()]}
    ss = await db.get(SwitchState, device_id)
    if ss and ss.states:
        return {"device_id": device_id, "channels": [{"channel": int(k), "state": v} for k, v in ss.states.items()]}
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
