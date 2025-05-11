# app/routers/cameras.py

from datetime import datetime, timedelta, timezone
import mimetypes
import asyncio
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
    CAM_EVENT_GAP_SECONDS,
    DATA_ROOT,
    RAW_DIR,
    CLIPS_DIR,
    BOUNDARY,
    PROCESSED_DIR,
)
from app.core.database import get_db
from app.dependencies import get_current_admin, verify_camera_token
from app.models import Camera, DetectionRecord, DeviceCommand
from app.schemas import CameraReportResponse, DetectionRange
from app.utils.camera_tasks import encode_and_cleanup
from app.utils.camera_queue import camera_queue

router = APIRouter()
ws_clients: dict[str, list[WebSocket]] = defaultdict(list)


async def _process_upload(
    camera_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession,
    day_flag: bool,
) -> dict:
    # 1) Validate content-type
    ct = request.headers.get("content-type", "")
    if not ct.startswith("image/"):
        raise HTTPException(415, "Expected image/jpeg")

    # 2) Read & decode
    data = await request.body()
    arr = np.frombuffer(data, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(400, "Invalid JPEG")

    # 3) Enhance
    try:
        proc = _enhance_day(frame) if day_flag else _enhance_night(frame)
        ok, buf = cv2.imencode(".jpg", proc)
        data = buf.tobytes() if ok else data
    except Exception:
        pass

    # 4) Atomic write
    base = Path(DATA_ROOT) / camera_id
    raw_dir = base / RAW_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)
    latest = base / "latest.jpg"

    ts = int(time.time() * 1000)
    tmp = raw_dir / f"{ts}.jpg.tmp"
    tmp.write_bytes(data)
    tmp.rename(raw_dir / f"{ts}.jpg")

    t2 = base / "latest.jpg.tmp"
    t2.write_bytes(data)
    t2.rename(latest)

    # 5) DB
    cam = await db.get(Camera, camera_id)
    if not cam:
        cam = Camera(id=camera_id, name=camera_id)
        db.add(cam)
    cam.is_online = True
    cam.last_seen = datetime.utcnow()
    await db.commit()

    # 6) Schedule encode & cleanup
    loop = asyncio.get_running_loop()
    loop.create_task(encode_and_cleanup(camera_id))

    # 7) Schedule detection
    loop.create_task(camera_queue.enqueue(camera_id, latest))

    # 8) Push to WS
    for ws in list(ws_clients[camera_id]):
        try:
            await ws.send_bytes(data)
        except:
            ws_clients[camera_id].remove(ws)

    return {"ok": True, "ts": ts, "mode": "day" if day_flag else "night"}


@router.post("/upload/{camera_id}/day", dependencies=[Depends(verify_camera_token)])
async def upload_day_frame(
    camera_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    return await _process_upload(camera_id, request, background_tasks, db, True)


@router.post("/upload/{camera_id}/night", dependencies=[Depends(verify_camera_token)])
async def upload_night_frame(
    camera_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    return await _process_upload(camera_id, request, background_tasks, db, False)


@router.get("/stream/{camera_id}", dependencies=[Depends(get_current_admin)])
def stream(
    camera_id: str,
    mode: str = Query(
        "mjpeg",
        regex="^(mjpeg|poll)$",
        description="`mjpeg` or single-frame `poll`",
    ),
):
    cam_dir = Path(DATA_ROOT) / camera_id
    if not cam_dir.exists():
        raise HTTPException(404, "Camera not found")

    if mode == "poll":
        proc = cam_dir / PROCESSED_DIR
        if proc.exists():
            frames = sorted(proc.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
            path = frames[0] if frames else cam_dir / "latest.jpg"
        else:
            path = cam_dir / "latest.jpg"
        if not path.exists():
            raise HTTPException(404, "No frame")
        return FileResponse(path, media_type="image/jpeg")

    async def gen():
        last = None
        while True:
            proc = cam_dir / PROCESSED_DIR
            if proc.exists():
                frames = sorted(proc.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
                path = frames[-1] if frames else cam_dir / "latest.jpg"
            else:
                path = cam_dir / "latest.jpg"

            if path.exists():
                m = path.stat().st_mtime_ns
                if m != last:
                    last = m
                    b = path.read_bytes()
                    yield (
                        f"--{BOUNDARY}\r\n"
                        f"Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(b)}\r\n\r\n"
                    ).encode() + b + b"\r\n"
            await asyncio.sleep(0.03)

    return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}")


@router.get("/still/{camera_id}", dependencies=[Depends(get_current_admin)])
def still(camera_id: str):
    base = Path(DATA_ROOT) / camera_id
    proc = base / PROCESSED_DIR
    if proc.exists():
        frames = sorted(proc.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
        path = frames[0] if frames else base / "latest.jpg"
    else:
        path = base / "latest.jpg"
    if not path.exists():
        raise HTTPException(404, "No image")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/api/clips/{camera_id}")
def list_clips(camera_id: str):
    clip_dir = Path(DATA_ROOT) / camera_id / CLIPS_DIR
    clips = sorted(clip_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for c in clips:
        ts = int(c.stem)
        out.append(
            {
                "filename": c.name,
                "datetime": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(),
                "size_mb": round(c.stat().st_size / 1024**2, 2),
            }
        )
    return JSONResponse(out)


@router.get("/clips/{camera_id}/{clip_name}")
def serve_clip(camera_id: str, clip_name: str):
    path = Path(DATA_ROOT) / camera_id / CLIPS_DIR / clip_name
    if not path.exists():
        raise HTTPException(404, "Not found")
    mime = mimetypes.guess_type(clip_name)[0] or "video/mp4"
    return FileResponse(path, media_type=mime)


@router.get("/api/status/{camera_id}")
async def cam_status(camera_id: str, db: AsyncSession = Depends(get_db)):
    c = await db.get(Camera, camera_id)
    if not c:
        raise HTTPException(404, "Unknown camera")
    return {"is_online": c.is_online, "last_seen": c.last_seen}


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
    q = await db.execute(
        select(DetectionRecord).where(DetectionRecord.camera_id == camera_id).order_by(DetectionRecord.timestamp)
    )
    recs = q.scalars().all()
    grouped: dict[str, list[dict]] = {}
    gap = timedelta(seconds=CAM_EVENT_GAP_SECONDS)
    for r in recs:
        lst = grouped.setdefault(r.object_name, [])
        if not lst or r.timestamp - lst[-1]["end"] > gap:
            lst.append({"start": r.timestamp, "end": r.timestamp})
        else:
            lst[-1]["end"] = r.timestamp

    dets = [
        DetectionRange(object_name=obj, start_time=seg["start"], end_time=seg["end"])
        for obj, segs in grouped.items()
        for seg in segs
    ]
    return CameraReportResponse(camera_id=camera_id, detections=dets)


@router.websocket("/ws/stream/{camera_id}")
async def ws_stream(websocket: WebSocket, camera_id: str):
    await websocket.accept()
    ws_clients[camera_id].append(websocket)
    try:
        while True:
            await asyncio.sleep(30)
    finally:
        ws_clients[camera_id].remove(websocket)


def _enhance_day(frame):
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    cl = clahe.apply(l)
    merged = cv2.merge((cl, a, b))
    enh = cv2.cvtColor(merged, cv2.COLOR_LAB2BGR)
    return cv2.fastNlMeansDenoisingColored(enh, None, 4, 4, 7, 21)


def _enhance_night(frame):
    gamma = 0.5
    table = (np.arange(256) / 255.0) ** (1 / gamma) * 255
    bright = cv2.LUT(frame, table.astype("uint8"))
    gray = cv2.cvtColor(bright, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    eq = clahe.apply(gray)
    eq_bgr = cv2.cvtColor(eq, cv2.COLOR_GRAY2BGR)
    merged = cv2.addWeighted(bright, 0.7, eq_bgr, 0.3, 0)
    den = cv2.fastNlMeansDenoisingColored(merged, None, 10, 10, 7, 21)
    blur = cv2.GaussianBlur(den, (0, 0), sigmaX=3, sigmaY=3)
    return cv2.addWeighted(den, 1.5, blur, -0.5, 0)
