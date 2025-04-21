# app/routers/cameras.py

from datetime import datetime, timezone
import mimetypes, asyncio
from pathlib import Path
import time
from fastapi import APIRouter, Depends, Request, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from requests import Session
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.models import Camera, UserCamera, User
import asyncio
from app.utils.camera_tasks import encode_and_cleanup
from app.core.config import DATA_ROOT, RAW_DIR, CLIPS_DIR, BOUNDARY
import numpy as np
import cv2
from app.utils.image_utils import is_day, clean_frame

router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

def current_user(request: Request, db=Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    if not uid: return None
    return db.query(User).get(uid)

@router.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

async def _process_upload(
    camera_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession,
    day_flag: bool
) -> dict:
    """
    Shared logic for ingesting a JPEG frame:
    - Validate content-type
    - Decode JPEG
    - Clean frame (day_flag determines mode)
    - Save raw and latest files
    - Update camera record
    - Schedule encoding
    """
    # Validate Content-Type
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="Unsupported Media Type; expected image/jpeg")

    # Read raw bytes
    raw_bytes = await request.body()

    # Decode JPEG
    arr = np.frombuffer(raw_bytes, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(status_code=400, detail="Invalid JPEG data")

    # Clean frame according to day/night
    try:
        cleaned = clean_frame(frame, day_flag)
        ok, buf = cv2.imencode(".jpg", cleaned)
        image_bytes = buf.tobytes() if ok else raw_bytes
    except Exception:
        image_bytes = raw_bytes

    # Prepare directories
    base_dir = Path(DATA_ROOT) / camera_id
    raw_dir = base_dir / RAW_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)
    latest_file = base_dir / "latest.jpg"

    # Write files
    timestamp = int(time.time() * 1000)
    (raw_dir / f"{timestamp}.jpg").write_bytes(image_bytes)
    latest_file.write_bytes(image_bytes)

    # Update DB record asynchronously
    camera = await db.get(Camera, camera_id)
    if not camera:
        camera = Camera(id=camera_id, name=camera_id)
        db.add(camera)
    camera.is_online = True
    camera.last_seen = datetime.utcnow()
    await db.commit()

    # Schedule background encoding
        # Schedule background encoding in a fresh event loop
    def _encode_wrapper(cam_id: str):
        # this will spin up its own event loop to call the async function
        asyncio.run(encode_and_cleanup(cam_id))

    background_tasks.add_task(_encode_wrapper, camera_id)


    return {"ok": True, "ts": timestamp, "mode": "day" if day_flag else "night"}

@router.post("/upload/{camera_id}/day")
async def upload_day_frame(
    camera_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
) -> dict:
    """Upload and clean a frame in daytime mode"""
    return await _process_upload(camera_id, request, background_tasks, db, day_flag=True)

@router.post("/upload/{camera_id}/night")
async def upload_night_frame(
    camera_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
) -> dict:
    """Upload and clean a frame in nighttime mode"""
    return await _process_upload(camera_id, request, background_tasks, db, day_flag=False)

@router.get("/stream/{camera_id}")
def mjpeg_stream(camera_id: str):
    cam_dir = Path(DATA_ROOT)/camera_id
    if not cam_dir.exists():
        raise HTTPException(404, "Camera not found")
    async def gen():
        last = 0
        while True:
            img = cam_dir/"latest.jpg"
            if img.exists():
                m = img.stat().st_mtime_ns
                if m != last:
                    last = m
                    data = img.read_bytes()
                    yield (
                        f"--{BOUNDARY}\r\n"
                        f"Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(data)}\r\n\r\n"
                    ).encode() + data + f"\r\n--{BOUNDARY}\r\n".encode()
            await asyncio.sleep(0.05)
    return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}")

@router.get("/still/{camera_id}")
def still(camera_id: str):
    p = Path(DATA_ROOT)/camera_id/"latest.jpg"
    if not p.exists(): raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg")

@router.get("/api/clips/{camera_id}")
def list_clips(camera_id: str):
    clips = sorted((Path(DATA_ROOT)/camera_id/CLIPS_DIR).glob("*.mp4"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for c in clips:
        ts = int(c.stem)
        dt = datetime.fromtimestamp(ts/1000, timezone.utc).isoformat()
        size_mb = round(c.stat().st_size/1024/1024,2)
        # …duration via cv2 if needed…
        out.append({"filename":c.name,"datetime":dt,"size_mb":size_mb})
    return JSONResponse(out)

@router.get("/clips/{camera_id}/{clip_name}")
def serve_clip(camera_id: str, clip_name: str):
    clip = Path(DATA_ROOT)/camera_id/CLIPS_DIR/clip_name
    if not clip.exists(): raise HTTPException(404)
    return FileResponse(clip, media_type=mimetypes.guess_type(clip_name)[0] or "video/mp4")

@router.get("/api/status/{camera_id}")
def cam_status(camera_id: str, db=Depends(get_db)):
    cam = db.query(Camera).get(camera_id)
    if not cam: raise HTTPException(404)
    return {"is_online":cam.is_online, "last_seen":cam.last_seen}

# … plus assign‑camera HTML form & POST handler …
