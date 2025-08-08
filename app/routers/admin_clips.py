# app/routers/admin_clips.py
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pathlib import Path
from datetime import datetime, timezone
from app.core.config import DATA_ROOT, CLIPS_DIR
from app.dependencies import get_current_admin

router = APIRouter(
    prefix="/admin/cameras",
    tags=["admin-cameras"],
    dependencies=[Depends(get_current_admin)]
)

def _clip_metadata(p: Path):
    ts = int(p.stem)
    return {
        "filename": p.name,
        "datetime": datetime.fromtimestamp(ts/1000, timezone.utc).isoformat(),
        "size_mb": round(p.stat().st_size / 1024**2, 2)
    }

@router.get("/clips", summary="List all clips for all cameras")
async def list_all_clips():
    root = Path(DATA_ROOT)
    out: dict[str, list] = {}
    for cam_dir in root.iterdir():
        clip_dir = cam_dir / CLIPS_DIR
        if cam_dir.is_dir() and clip_dir.exists():
            clips = sorted(clip_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
            out[cam_dir.name] = [_clip_metadata(c) for c in clips]
    return JSONResponse(out)

@router.get("/{camera_id}/clips", summary="List clips for one camera")
async def list_clips(camera_id: str):
    clip_dir = Path(DATA_ROOT) / camera_id / CLIPS_DIR
    if not clip_dir.exists():
        raise HTTPException(404, "Camera or clips folder not found")
    clips = sorted(clip_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [ _clip_metadata(c) for c in clips ]

@router.get("/{camera_id}/clips/{clip_name}/play", 
            response_class=HTMLResponse,
            summary="Embed HTML5 player for a clip")
async def play_clip(camera_id: str, clip_name: str):
    video_url = f"/api/v1/admin/cameras/{camera_id}/clips/{clip_name}/download"
    html = f"""
    <html><body>
      <video controls autoplay style="max-width:100%">
        <source src="{video_url}" type="video/mp4">
        Your browser does not support HTML5 video.
      </video>
    </body></html>
    """
    return HTMLResponse(html)

@router.get("/{camera_id}/clips/{clip_name}/download", 
            summary="Download or stream the raw MP4")
async def download_clip(camera_id: str, clip_name: str):
    clip = Path(DATA_ROOT) / camera_id / CLIPS_DIR / clip_name
    if not clip.exists():
        raise HTTPException(404, "Clip not found")
    return FileResponse(clip, media_type="video/mp4", filename=clip_name)

@router.delete("/{camera_id}/clips/{clip_name}", 
               summary="Delete a specific clip")
async def delete_clip(camera_id: str, clip_name: str):
    clip = Path(DATA_ROOT) / camera_id / CLIPS_DIR / clip_name
    if not clip.exists():
        raise HTTPException(404, "Clip not found")
    clip.unlink()
    return {"message": "Clip deleted successfully"}
