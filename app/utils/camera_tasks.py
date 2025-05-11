# app/routers/cameras.py

from datetime import datetime, timedelta, timezone
import mimetypes
import asyncio
import time
import os
from pathlib import Path

import numpy as np
import cv2
from fastapi import APIRouter, Depends, Request, BackgroundTasks, HTTPException, Query, WebSocket
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from collections import defaultdict

from app.core.config import CAM_EVENT_GAP_SECONDS, DATA_ROOT, RAW_DIR, CLIPS_DIR, BOUNDARY, PROCESSED_DIR
from app.core.database import get_db
from app.dependencies import get_current_admin, verify_camera_token
from app.models import Camera, DetectionRecord, DeviceCommand
from app.schemas import CameraReportResponse, DetectionRange
from app.utils.camera_tasks import encode_and_cleanup
from app.utils.camera_queue import camera_queue

router = APIRouter()
# WebSocket clients mapping for live push
ws_clients: dict[str, list[WebSocket]] = defaultdict(list)


def _process_upload(
    camera_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession,
    day_flag: bool
) -> dict:
    # 1) Validate content-type
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        raise HTTPException(415, "Unsupported Media Type; expected image/jpeg")

    # 2) Read & decode
    raw_bytes = await request.body()
    arr = np.frombuffer(raw_bytes, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(400, "Invalid JPEG data")

    # 3) Day/night enhancement
    try:
        processed = _enhance_day(frame) if day_flag else _enhance_night(frame)
        ok, buf = cv2.imencode(".jpg", processed)
        image_bytes = buf.tobytes() if ok else raw_bytes
    except Exception:
        image_bytes = raw_bytes

    # 4) Save files atomically
    base_dir = Path(DATA_ROOT) / camera_id
    raw_dir = base_dir / RAW_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)
    latest_file = base_dir / "latest.jpg"

    ts = int(time.time() * 1000)
    tmp_raw = raw_dir / f"{ts}.jpg.tmp"
    final_raw = raw_dir / f"{ts}.jpg"
    tmp_raw.write_bytes(image_bytes)
    tmp_raw.rename(final_raw)

    tmp_latest = base_dir / "latest.jpg.tmp"
    tmp_latest.write_bytes(image_bytes)
    tmp_latest.rename(latest_file)

    # 5) Update DB
    camera = await db.get(Camera, camera_id)
    if not camera:
        camera = Camera(id=camera_id, name=camera_id)
        db.add(camera)
    camera.is_online = True
    camera.last_seen = datetime.utcnow()
    await db.commit()

    # 6) Schedule encoding & cleanup
    loop = asyncio.get_running_loop()
    loop.create_task(encode_and_cleanup(camera_id))

    # 7) Schedule YOLO detection
    loop.create_task(camera_queue.enqueue(camera_id, Path(latest_file)))

    # 8) Push raw JPEG to any connected WebSocket clients
    for ws in list(ws_clients.get(camera_id, [])):
        try:
            await ws.send_bytes(image_bytes)
        except Exception:
            ws_clients[camera_id].remove(ws)

    return {"ok": True, "ts": ts, "mode": "day" if day_flag else "night"}


@router.post(
    "/upload/{camera_id}/day",
    dependencies=[Depends(verify_camera_token)]
)
async def upload_day_frame(
    camera_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
) -> dict:
    return await _process_upload(camera_id, request, background_tasks, db, day_flag=True)


@router.post(
    "/upload/{camera_id}/night",
    dependencies=[Depends(verify_camera_token)]
)
async def upload_night_frame(
    camera_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
) -> dict:
    return await _process_upload(camera_id, request, background_tasks, db, day_flag=False)


@router.get(
    "/stream/{camera_id}",
    dependencies=[Depends(get_current_admin)]
)
def stream(
    camera_id: str,
    mode: str = Query(
        "mjpeg",
        regex="^(mjpeg|poll)$",
        description="`mjpeg` for live MJPEG (~20 FPS), `poll` for single-frame snapshot"
    )
):
    cam_dir = Path(DATA_ROOT) / camera_id
    if not cam_dir.exists():
        raise HTTPException(404, "Camera not found")

    # Poll mode: return a single JPEG
    if mode == "poll":
        proc = cam_dir / PROCESSED_DIR
        img_path = (
            sorted(proc.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)[0]
            if proc.exists() and any(proc.glob("*.jpg"))
            else cam_dir / "latest.jpg"
        )
        if not img_path.exists():
            raise HTTPException(404, "Image not found")
        return FileResponse(img_path, media_type="image/jpeg")

    # MJPEG mode: multipart stream
    async def gen():
        last_mtime = 0
        while True:
            proc = cam_dir / PROCESSED_DIR
            img_path = (
                sorted(proc.glob("*.jpg"), key=lambda p: p.stat().st_mtime)[-1]
                if proc.exists() and any(proc.glob("*.jpg"))
                else cam_dir / "latest.jpg"
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
            await asyncio.sleep(0.03)

    return StreamingResponse(
        gen(),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}"
    )


@router.get(
    "/still/{camera_id}",
    dependencies=[Depends(get_current_admin)]
)
def still(camera_id: str):
    base = Path(DATA_ROOT) / camera_id
    proc = base / PROCESSED_DIR
    p = (
        sorted(proc.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)[0]
        if proc.exists() and any(proc.glob("*.jpg"))
        else base / "latest.jpg"
    )
    if not p.exists():
        raise HTTPException(404, "Image not found")
    return FileResponse(p, media_type="image/jpeg")


@router.get("/api/clips/{camera_id}")
def list_clips(camera_id: str):
    clip_dir = Path(DATA_ROOT) / camera_id / CLIPS_DIR
    clips = sorted(
        clip_dir.glob("*.mp4"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )
    out = []
    for c in clips:
        ts = int(c.stem)
        out.append({
            "filename": c.name,
            "datetime": datetime.fromtimestamp(ts/1000, timezone.utc).isoformat(),
            "size_mb": round(c.stat().st_size / 1024**2, 2)
        })
    return JSONResponse(out)


@router.get("/clips/{camera_id}/{clip_name}")
def serve_clip(camera_id: str, clip_name: str):
    clip = Path(DATA_ROOT) / camera_id / CLIPS_DIR / clip_name
    if not clip.exists():
        raise HTTPException(404, "Clip not found")
    mime = mimetypes.guess_type(clip_name)[0] or "video/mp4"
    return FileResponse(clip, media_type=mime)


@router.get("/api/status/{camera_id}")
async def cam_status(
    camera_id: str,
    db: AsyncSession = Depends(get_db)
):
    cam = await db.get(Camera, camera_id)
    if not cam:
        raise HTTPException(404, "Camera not registered")
    return {"is_online": cam.is_online, "last_seen": cam.last_seen}


@router.get(
    "/commands/{camera_id}",
    dependencies=[Depends(verify_camera_token)]
)
async def next_command(
    camera_id: str,
    db: AsyncSession = Depends(get_db)
):
    cmd = await db.scalar(
        select(DeviceCommand)
        .where(
            DeviceCommand.device_id == camera_id,
            DeviceCommand.dispatched == False
        )
        .order_by(DeviceCommand.issued_at)
        .limit(1)
    )
    if not cmd:
        return {"command": None}
    cmd.dispatched = True
    await db.commit()
    return {"command": cmd.action, "parameters": cmd.parameters or {}}


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers for day & night enhancement
# ─────────────────────────────────────────────────────────────────────────────

def _enhance_day(frame: np.ndarray) -> np.ndarray:
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    cl = clahe.apply(l)
    merged = cv2.merge((cl, a, b))
    enhanced = cv2.cvtColor(merged, cv2.COLOR_LAB2BGR)
    return cv2.fastNlMeansDenoisingColored(enhanced, None, 4, 4, 7, 21)

def _enhance_night(frame: np.ndarray) -> np.ndarray:
    gamma = 0.5
    inv_gamma = 1.0 / gamma
    table = (np.arange(256) / 255.0) ** inv_gamma * 255
    bright = cv2.LUT(frame, table.astype("uint8"))
    gray = cv2.cvtColor(bright, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    eq = clahe.apply(gray)
    eq_bgr = cv2.cvtColor(eq, cv2.COLOR_GRAY2BGR)
    merged = cv2.addWeighted(bright, 0.7, eq_bgr, 0.3, 0)
    denoised = cv2.fastNlMeansDenoisingColored(merged, None, 10, 10, 7, 21)
    blur = cv2.GaussianBlur(denoised, (0, 0), sigmaX=3, sigmaY=3)
    return cv2.addWeighted(denoised, 1.5, blur, -0.5, 0)


@router.get(
    "/api/report/{camera_id}",
    response_model=CameraReportResponse
)
async def get_camera_report(
    camera_id: str,
    db: AsyncSession = Depends(get_db)
):
    q = await db.execute(
        select(DetectionRecord)
        .where(DetectionRecord.camera_id == camera_id)
        .order_by(DetectionRecord.timestamp)
    )
    records = q.scalars().all()
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
                DetectionRange(
                    object_name=obj,
                    start_time=r["start"],
                    end_time=r["end"],
                )
            )

    return CameraReportResponse(camera_id=camera_id, detections=detections)


@router.websocket("/ws/stream/{camera_id}")
async def ws_stream(websocket: WebSocket, camera_id: str):
    await websocket.accept()
    ws_clients[camera_id].append(websocket)
    try:
        while True:
            await asyncio.sleep(30)  # keep-alive ping
    finally:
        ws_clients[camera_id].remove(websocket)
