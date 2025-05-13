import asyncio
from datetime import datetime, timedelta, timezone
import time
from pathlib import Path
from collections import defaultdict

import numpy as np
import cv2
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


async def _process_upload(
    camera_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    day_flag: bool,
) -> dict:
    # 1) Validate Content-Type
    ct = request.headers.get("content-type", "")
    if not ct.startswith("image/"):
        raise HTTPException(status_code=415, detail="Unsupported Media Type; expected image/jpeg")

    # 2) Read body
    body = bytearray()
    try:
        async for chunk in request.stream():
            body.extend(chunk)
    except Exception:
        raise HTTPException(status_code=499, detail="Client disconnected during upload")
    if not body:
        raise HTTPException(status_code=400, detail="Empty request body")

    # 3) Save raw JPEG
    base = Path(DATA_ROOT) / camera_id
    raw_dir = base / RAW_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    raw_file = raw_dir / f"{ts}.jpg"
    raw_file.write_bytes(body)
    latest_file = base / "latest.jpg"
    tmp_latest = latest_file.with_suffix('.jpg.tmp')
    tmp_latest.write_bytes(body)
    tmp_latest.rename(latest_file)

    # 4) Direct clip writing (no enhancement)
    arr = np.frombuffer(body, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(status_code=400, detail="Invalid JPEG data")
    now = datetime.now(timezone.utc)

    # Thread-safe clip rollover & write
    lock = _clip_locks.setdefault(camera_id, asyncio.Lock())
    async with lock:
        info = _clip_writers.get(camera_id)
        # if no writer yet, or clip has reached duration, start new
        if not info or (now - info['start']) >= CLIP_DURATION:
            if info:
                info['writer'].release()
            clip_dir = base / CLIPS_DIR
            clip_dir.mkdir(parents=True, exist_ok=True)
            clip_ts = int(now.timestamp() * 1000)
            out_path = clip_dir / f"{clip_ts}.mp4"
            h, w = frame.shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            writer = cv2.VideoWriter(str(out_path), fourcc, FPS, (w, h))
            if not writer.isOpened():
                raise HTTPException(status_code=500, detail="Failed to open video writer")
            _clip_writers[camera_id] = {'writer': writer, 'start': now}
        # write the current frame
        _clip_writers[camera_id]['writer'].write(frame)

    # 5) Enqueue for YOLO processing only
    background_tasks.add_task(camera_queue.enqueue, camera_id, latest_file)

    # 6) Broadcast to WebSocket clients
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
    mode: str = Query(
        "mjpeg",
        pattern="^(mjpeg|poll)$",
        description="`mjpeg` for live MJPEG, `poll` for single-frame snapshot",
    ),
):
    cam_dir = Path(DATA_ROOT) / camera_id
    if not cam_dir.exists():
        raise HTTPException(status_code=404, detail="Camera not found")

    if mode == "poll":
        proc = cam_dir / PROCESSED_DIR
        img_path = (
            sorted(proc.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)[0]
            if proc.exists() and any(proc.glob("*.jpg"))
            else cam_dir / "latest.jpg"
        )
        if not img_path.exists():
            raise HTTPException(status_code=404, detail="Image not found")
        return FileResponse(img_path, media_type="image/jpeg")

    async def gen():
        last_mtime = 0
        while True:
            proc = Path(DATA_ROOT) / camera_id / PROCESSED_DIR
            img_path = (
                sorted(proc.glob("*.jpg"), key=lambda p: p.stat().st_mtime)[-1]
                if proc.exists() and any(proc.glob("*.jpg"))
                else Path(DATA_ROOT) / camera_id / "latest.jpg"
            )
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
            await asyncio.sleep(1 / FPS)

    return StreamingResponse(
        gen(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}"
    )


@router.get("/still/{camera_id}", dependencies=[Depends(get_current_admin)])
def still(camera_id: str):
    base = Path(DATA_ROOT) / camera_id
    proc = base / PROCESSED_DIR
    p = (
        sorted(proc.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)[0]
        if proc.exists() and any(proc.glob("*.jpg"))
        else base / "latest.jpg"
    )
    if not p.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(p, media_type="image/jpeg")


@router.get("/api/clips/{camera_id}")
def list_clips(camera_id: str):
    clip_dir = Path(DATA_ROOT) / camera_id / CLIPS_DIR
    clips = sorted(clip_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for c in clips:
        ts = int(c.stem)
        out.append({
            "filename": c.name,
            "datetime": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(),
            "size_mb": round(c.stat().st_size / 1024**2, 2),
        })
    return JSONResponse(out)


@router.get("/api/status/{camera_id}")
async def cam_status(camera_id: str, db: AsyncSession = Depends(get_db)):
    cam = await db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not registered")
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


@router.get("/api/report/{camera_id}", response_model=CameraReportResponse)
async def get_camera_report(camera_id: str, db: AsyncSession = Depends(get_db)):
    records = (await db.execute(
        select(DetectionRecord).where(DetectionRecord.camera_id == camera_id).order_by(DetectionRecord.timestamp)
    )).scalars().all()
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
            detections.append(
                DetectionRange(object_name=obj, start_time=r["start"], end_time=r["end"])
            )
    return CameraReportResponse(camera_id=camera_id, detections=detections)


@router.websocket("/ws/stream/{camera_id}")
async def ws_stream(websocket: WebSocket, camera_id: str):
    await websocket.accept()
    ws_clients[camera_id].append(websocket)
    try:
        while True:
            await asyncio.sleep(30)
    finally:
        ws_clients[camera_id].remove(websocket)