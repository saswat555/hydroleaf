from datetime import datetime, timedelta, timezone
import mimetypes
import asyncio
import time
from pathlib import Path
from collections import defaultdict

import numpy as np
import cv2
from fastapi import APIRouter, Depends, Request, BackgroundTasks, HTTPException, Query, WebSocket
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from starlette.exceptions import ClientDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import (
    CAM_EVENT_GAP_SECONDS,
    DATA_ROOT,
    RAW_DIR,
    CLIPS_DIR,
    PROCESSED_DIR,
    BOUNDARY,
    RETENTION_DAYS,
)
from app.core.database import get_db
from app.dependencies import get_current_admin, verify_camera_token
from app.models import Camera, DetectionRecord, DeviceCommand
from app.schemas import CameraReportResponse, DetectionRange
from app.utils.camera_tasks import encode_and_cleanup
from app.utils.camera_queue import camera_queue

router = APIRouter()
# WebSocket clients per camera
ws_clients: dict[str, list[WebSocket]] = defaultdict(list)

async def _process_upload(
    camera_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession,
    day_flag: bool
) -> dict:
    # 1) Validate content-type
    ct = request.headers.get("content-type", "")
    if not ct.startswith("image/"):
        raise HTTPException(status_code=415, detail="Unsupported Media Type; expected image/jpeg")

    # 2) Stream & accumulate
    body = bytearray()
    try:
        async for chunk in request.stream():
            body.extend(chunk)
    except ClientDisconnect:
        raise HTTPException(status_code=499, detail="Client disconnected during upload")

    if not body:
        raise HTTPException(status_code=400, detail="Empty request body")

    # 3) Decode JPEG
    arr = np.frombuffer(body, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(status_code=400, detail="Invalid JPEG data")

    # 4) Enhance
    try:
        proc = _enhance_day(frame) if day_flag else _enhance_night(frame)
        ok, buf = cv2.imencode(".jpg", proc)
        image_bytes = buf.tobytes() if ok else body
    except Exception:
        image_bytes = body

    # 5) Atomic file writes
    base = Path(DATA_ROOT) / camera_id
    raw_dir = base / RAW_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)
    latest_file = base / "latest.jpg"

    ts = int(time.time() * 1000)
    tmp1 = raw_dir / f"{ts}.jpg.tmp"
    final1 = raw_dir / f"{ts}.jpg"
    tmp1.write_bytes(image_bytes)
    tmp1.rename(final1)

    tmp2 = base / "latest.jpg.tmp"
    tmp2.write_bytes(image_bytes)
    tmp2.rename(latest_file)

    # 6) DB update/insert
    camera = await db.get(Camera, camera_id)
    if not camera:
        camera = Camera(id=camera_id, name=camera_id)
        db.add(camera)
    camera.is_online = True
    camera.last_seen = datetime.now(timezone.utc)
    await db.commit()

    # 7) Schedule tasks
    loop = asyncio.get_running_loop()
    loop.create_task(encode_and_cleanup(camera_id))
    loop.create_task(camera_queue.enqueue(camera_id, latest_file))

    # 8) Broadcast over WebSocket
    for ws in list(ws_clients.get(camera_id, [])):
        try:
            await ws.send_bytes(image_bytes)
        except Exception:
            ws_clients[camera_id].remove(ws)

    return {"ok": True, "ts": ts, "mode": "day" if day_flag else "night"}


# Upload endpoints for cameras\:@router.post("/upload/{camera_id}/day", dependencies=[Depends(verify_camera_token)])
async def upload_day_frame(
    camera_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
) -> dict:
    return await _process_upload(camera_id, request, background_tasks, db, day_flag=True)


@router.post("/upload/{camera_id}/night", dependencies=[Depends(verify_camera_token)])
async def upload_night_frame(
    camera_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
) -> dict:
    return await _process_upload(camera_id, request, background_tasks, db, day_flag=False)


# Admin-only: list all cameras and their stats
@router.get("/cameras/list", dependencies=[Depends(get_current_admin)])
async def list_cameras(db: AsyncSession = Depends(get_db)):
    cams = []
    base_root = Path(DATA_ROOT)
    for d in sorted(base_root.iterdir()):
        if not d.is_dir():
            continue
        cam_id = d.name
        # DB record
        cam = await db.get(Camera, cam_id)
        is_online = bool(cam and cam.is_online)
        last_seen = cam.last_seen if cam else None
        # frame count
        proc_dir = d / PROCESSED_DIR
        try:
            frames_received = len(list(proc_dir.glob("*.jpg"))) if proc_dir.exists() else 0
        except Exception:
            frames_received = 0
        # clips count
        clips_dir = d / CLIPS_DIR
        try:
            clips_count = len(list(clips_dir.glob("*.mp4"))) if clips_dir.exists() else 0
        except Exception:
            clips_count = 0
        cams.append({
            "camera_id": cam_id,
            "is_online": is_online,
            "last_seen": last_seen,
            "frames_received": frames_received,
            "clips_count": clips_count,
        })
    return JSONResponse(cams)


# Admin-only: list and serve clips
@router.get("/cameras/{camera_id}/clips", dependencies=[Depends(get_current_admin)])
async def admin_list_clips(camera_id: str):
    clip_dir = Path(DATA_ROOT) / camera_id / CLIPS_DIR
    if not clip_dir.exists():
        return JSONResponse([])
    clips = sorted(clip_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    out = []
    for c in clips:
        try:
            ts = int(c.stem)
            out.append({
                "filename": c.name,
                "datetime": datetime.fromtimestamp(ts/1000, timezone.utc).isoformat(),
                "size_mb": round(c.stat().st_size / 1024**2, 2)
            })
        except Exception:
            continue
    return JSONResponse(out)


@router.get(
    "/cameras/{camera_id}/clips/{clip_name}/play",
    dependencies=[Depends(get_current_admin)]
)
def admin_play_clip(camera_id: str, clip_name: str):
    clip = Path(DATA_ROOT) / camera_id / CLIPS_DIR / clip_name
    if not clip.exists():
        raise HTTPException(404, "Clip not found")
    mime = mimetypes.guess_type(clip_name)[0] or "video/mp4"
    # direct FileResponse; browser can play natively
    return FileResponse(clip, media_type=mime, filename=clip_name)


@router.get(
    "/cameras/{camera_id}/clips/{clip_name}/download",
    dependencies=[Depends(get_current_admin)]
)
def admin_download_clip(camera_id: str, clip_name: str):
    clip = Path(DATA_ROOT) / camera_id / CLIPS_DIR / clip_name
    if not clip.exists():
        raise HTTPException(404, "Clip not found")
    mime = mimetypes.guess_type(clip_name)[0] or "application/octet-stream"
    return FileResponse(clip, media_type=mime, filename=clip_name)


# Streaming (poll or MJPEG)
@router.get("/stream/{camera_id}", dependencies=[Depends(get_current_admin)])
def stream(
    camera_id: str,
    mode: str = Query(
        "mjpeg",
        pattern="^(mjpeg|poll)$",
        description="`mjpeg` for live MJPEG (~20 FPS), `poll` for single-frame snapshot"
    )
):
    base = Path(DATA_ROOT) / camera_id
    if not base.exists():
        raise HTTPException(404, "Camera not found")

    # Poll: latest processed or raw
    if mode == "poll":
        proc = base / PROCESSED_DIR
        img = None
        if proc.exists():
            jpgs = sorted(proc.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
            if jpgs:
                img = jpgs[0]
        if not img or not img.exists():
            img = base / "latest.jpg"
        if not img.exists():
            raise HTTPException(404, "Image not found")
        return FileResponse(img, media_type="image/jpeg")

    # MJPEG streaming
    async def gen():
        last_mtime = None
        while True:
            try:
                proc = base / PROCESSED_DIR
                if proc.exists():
                    jpgs = sorted(proc.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
                    img_path = jpgs[-1] if jpgs else base / "latest.jpg"
                else:
                    img_path = base / "latest.jpg"
                if img_path.exists():
                    mtime = img_path.stat().st_mtime_ns
                    if mtime != last_mtime:
                        last_mtime = mtime
                        data = img_path.read_bytes()
                        header = (
                            f"--{BOUNDARY}\r\n"
                            f"Content-Type: image/jpeg\r\n"
                            f"Content-Length: {len(data)}\r\n\r\n"
                        )
                        yield header.encode() + data + b"\r\n"
                await asyncio.sleep(0.03)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(1)

    return StreamingResponse(
        gen(),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}"
    )


# Single-frame still
@router.get("/still/{camera_id}", dependencies=[Depends(get_current_admin)])
def still(camera_id: str):
    base = Path(DATA_ROOT) / camera_id
    proc = base / PROCESSED_DIR
    p = None
    if proc.exists():
        jpgs = sorted(proc.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
        if jpgs:
            p = jpgs[0]
    if not p or not p.exists():
        p = base / "latest.jpg"
    if not p.exists():
        raise HTTPException(404, "Image not found")
    return FileResponse(p, media_type="image/jpeg")


# Public API: list clips (if needed)
@router.get("/api/clips/{camera_id}")
def api_list_clips(camera_id: str):
    clip_dir = Path(DATA_ROOT) / camera_id / CLIPS_DIR
    if not clip_dir.exists():
        return JSONResponse([])
    clips = sorted(clip_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for c in clips:
        ts = None
        try:
            ts = int(c.stem)
        except ValueError:
            continue
        out.append({
            "filename": c.name,
            "datetime": datetime.fromtimestamp(ts/1000, timezone.utc).isoformat(),
            "size_mb": round(c.stat().st_size / 1024**2, 2)
        })
    return JSONResponse(out)


@router.get("/api/status/{camera_id}")
async def cam_status(camera_id: str, db: AsyncSession = Depends(get_db)):
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


# Internal helpers

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


# WebSocket for live push\:@router.websocket("/ws/stream/{camera_id}")
async def ws_stream(websocket: WebSocket, camera_id: str):
    await websocket.accept()
    ws_clients[camera_id].append(websocket)
    try:
        while True:
            await asyncio.sleep(30)
    except asyncio.CancelledError:
        pass
    finally:
        ws_clients[camera_id].remove(websocket)
