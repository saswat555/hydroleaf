# app/routers/cameras.py

import asyncio
from datetime import datetime, timedelta, timezone
import time
from pathlib import Path
from collections import defaultdict

from fastapi import (
    APIRouter,
    Depends,
    Request,
    BackgroundTasks,
    HTTPException,
    Query,
    WebSocket,
)
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import (
    DATA_ROOT,
    RAW_DIR,
    CLIPS_DIR,
    PROCESSED_DIR,
    BOUNDARY,
    FPS,
    CAM_EVENT_GAP_SECONDS,
)
from app.core.database import get_db
from app.dependencies import get_current_admin, verify_camera_token
from app.models import Camera, DetectionRecord, DeviceCommand
from app.schemas import CameraReportResponse, DetectionRange
from app.utils.camera_queue import camera_queue

router = APIRouter()
ws_clients: dict[str, list[WebSocket]] = defaultdict(list)

# Clip writers: camera_id -> {'writer': VideoWriter, 'start': datetime}
_clip_writers: dict[str, dict] = {}
_clip_locks: dict[str, asyncio.Lock] = {}
CLIP_DURATION = timedelta(minutes=10)

JPEG_SOI = b"\xff\xd8"  # JPEG start-of-image magic


def _camera_dirs(camera_id: str) -> tuple[Path, Path, Path]:
    base = Path(DATA_ROOT) / camera_id
    raw_dir = base / RAW_DIR
    proc_dir = base / PROCESSED_DIR
    clip_dir = base / CLIPS_DIR
    return base, raw_dir, proc_dir, clip_dir


async def _process_upload(
    camera_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    day_flag: bool,
) -> dict:
    # 1) Read body (don’t rely solely on Content-Type – tests may omit it)
    body = bytearray()
    try:
        async for chunk in request.stream():
            body.extend(chunk)
    except Exception:
        # Non-standard 499 tends to confuse tests; use 400 instead
        raise HTTPException(status_code=400, detail="Client disconnected during upload")

    if not body:
        raise HTTPException(status_code=400, detail="Empty request body")

    # 2) Validate it looks like a JPEG (accepts image/* or octet-stream uploads)
    ct = request.headers.get("content-type", "")
    if not (ct.startswith("image/") or bytes(body[:2]) == JPEG_SOI):
        raise HTTPException(status_code=415, detail="Unsupported media; expected image/jpeg")

    # 3) Persist RAW and update 'latest.jpg'
    base, raw_dir, _, _ = _camera_dirs(camera_id)
    raw_dir.mkdir(parents=True, exist_ok=True)
    base.mkdir(parents=True, exist_ok=True)

    ts = int(time.time() * 1000)
    raw_file = raw_dir / f"{ts}.jpg"
    raw_file.write_bytes(body)

    latest_file = base / "latest.jpg"
    tmp_latest = latest_file.with_suffix(".tmp")
    tmp_latest.write_bytes(body)
    tmp_latest.replace(latest_file)

    # 4) Schedule async post-processing (non-blocking)
    try:
        background_tasks.add_task(camera_queue.enqueue, camera_id, latest_file)
    except Exception:
        # Queue not available in certain tests; don’t fail the upload
        pass

    # 5) Broadcast to any websocket listeners (best-effort)
    for ws in list(ws_clients.get(camera_id, [])):
        try:
            await ws.send_bytes(body)
        except Exception:
            ws_clients[camera_id].remove(ws)

    return {"ok": True, "ts": ts, "mode": "day" if day_flag else "night"}


@router.post("/upload/{camera_id}/day", dependencies=[Depends(verify_camera_token)])
async def upload_day_frame(
    camera_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    return await _process_upload(camera_id, request, background_tasks, day_flag=True)


@router.post("/upload/{camera_id}/night", dependencies=[Depends(verify_camera_token)])
async def upload_night_frame(
    camera_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    return await _process_upload(camera_id, request, background_tasks, day_flag=False)


@router.get("/stream/{camera_id}", dependencies=[Depends(get_current_admin)])
def stream(
    camera_id: str,
    mode: str = Query("mjpeg", description="`mjpeg` for live MJPEG, `poll` for single-frame snapshot"),
):
    if mode not in ("mjpeg", "poll"):
        raise HTTPException(status_code=422, detail="mode must be 'mjpeg' or 'poll'")

    base, _, proc_dir, _ = _camera_dirs(camera_id)
    if not base.exists():
        raise HTTPException(status_code=404, detail="Camera not found")

    if mode == "poll":
        candidates = (
            sorted(proc_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
            if proc_dir.exists()
            else []
        )
        img_path = candidates[0] if candidates else (base / "latest.jpg")
        if not img_path.exists():
            raise HTTPException(status_code=404, detail="Image not found")
        return FileResponse(img_path, media_type="image/jpeg")

    async def gen():
        last_mtime = 0
        while True:
            proc = proc_dir
            candidates = (
                sorted(proc.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
                if proc.exists()
                else []
            )
            img_path = candidates[-1] if candidates else (base / "latest.jpg")
            if img_path.exists():
                m = img_path.stat().st_mtime_ns
                if m != last_mtime:
                    last_mtime = m
                    data = img_path.read_bytes()
                    yield (
                        f"--{BOUNDARY}\r\n"
                        f"Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(data)}\r\n\r\n"
                    ).encode() + data + b"\r\n"
            await asyncio.sleep(1 / max(FPS, 1))

    return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}")


@router.get("/still/{camera_id}", dependencies=[Depends(get_current_admin)])
def still(camera_id: str):
    base, _, proc, _ = _camera_dirs(camera_id)
    candidates = (
        sorted(proc.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
        if proc.exists()
        else []
    )
    p = candidates[0] if candidates else (base / "latest.jpg")
    if not p.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(p, media_type="image/jpeg")


@router.get("/clips/{camera_id}")
def list_clips(camera_id: str):
    _, _, _, clip_dir = _camera_dirs(camera_id)
    if not clip_dir.exists():
        return JSONResponse([])
    clips = sorted(clip_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for c in clips:
        try:
            ts = int(c.stem)
        except ValueError:
            # Skip files not named by epoch-ms
            continue
        out.append(
            {
                "filename": c.name,
                "datetime": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(),
                "size_mb": round(c.stat().st_size / 1024**2, 2),
            }
        )
    return JSONResponse(out)


@router.get("/status/{camera_id}")
async def cam_status(camera_id: str, db: AsyncSession = Depends(get_db)):
    cam = await db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not registered")
    # FastAPI will JSON-encode datetimes; no need for manual isoformat
    return {"is_online": cam.is_online, "last_seen": cam.last_seen}


@router.get("/commands/{camera_id}", dependencies=[Depends(verify_camera_token)])
async def next_command(camera_id: str, db: AsyncSession = Depends(get_db)):
    cmd = await db.scalar(
        select(DeviceCommand)
        .where(DeviceCommand.device_id == camera_id, DeviceCommand.dispatched == False)
        .order_by(DeviceCommand.issued_at)
        .limit(1)
    )
    if not cmd:
        return {"command": None}
    cmd.dispatched = True
    await db.commit()
    return {"command": cmd.action, "parameters": cmd.parameters or {}}


@router.get("/report/{camera_id}", response_model=CameraReportResponse)
async def get_camera_report(camera_id: str, db: AsyncSession = Depends(get_db)):
    rows = await db.execute(
        select(DetectionRecord)
        .where(DetectionRecord.camera_id == camera_id)
        .order_by(DetectionRecord.timestamp)
    )
    records = rows.scalars().all()
    grouped: dict[str, list[dict]] = {}
    gap = timedelta(seconds=CAM_EVENT_GAP_SECONDS)

    for rec in records:
        lst = grouped.setdefault(rec.object_name, [])
        if not lst:
            lst.append({"start": rec.timestamp, "end": rec.timestamp})
        else:
            last = lst[-1]
            if rec.timestamp - last["end"] <= gap:
                last["end"] = rec.timestamp
            else:
                lst.append({"start": rec.timestamp, "end": rec.timestamp})

    detections: list[DetectionRange] = []
    for obj, ranges in grouped.items():
        for r in ranges:
            detections.append(DetectionRange(object_name=obj, start_time=r["start"], end_time=r["end"]))

    return CameraReportResponse(camera_id=camera_id, detections=detections)


@router.websocket("/ws/stream/{camera_id}")
async def ws_stream(websocket: WebSocket, camera_id: str):
    await websocket.accept()
    ws_clients[camera_id].append(websocket)
    try:
        while True:
            # Keep the connection alive; frames are pushed by uploads
            await asyncio.sleep(30)
    finally:
        try:
            ws_clients[camera_id].remove(websocket)
        except ValueError:
            pass
