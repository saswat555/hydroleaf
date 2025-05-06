from fastapi import APIRouter, Depends
from app.schemas import CameraReportResponse, DeviceType
from app.services.device_discovery import get_connected_devices
from app.services.ping import ping_host
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.core.database import get_db
from app.models import Device, Camera
from app.schemas import DeviceResponse  # re-use your existing pydantic schema
from app.dependencies import get_current_admin
from fastapi import Depends
router = APIRouter(prefix="/admin", tags=["admin"])

@router.get("/devices")
async def list_connected_devices():
    devices = get_connected_devices()
    results = {}
    # For each registered device, ping its IP and return status along with last seen time.
    for device_id, info in devices.items():
        ip = info["ip"]
        reachable = await ping_host(ip)
        results[device_id] = {
            "ip": ip,
            "reachable": reachable,
            "last_seen": info["last_seen"]
        }
    return results
@router.get(
    "/devices/dosing",
    response_model=list[DeviceResponse],
    dependencies=[Depends(get_current_admin)],
    summary="List all dosing‐unit devices"
)
async def list_dosing_devices(db: AsyncSession = Depends(get_db)):
    q = await db.execute(select(Device).where(Device.type == DeviceType.DOSING_UNIT))
    return q.scalars().all()

@router.get(
    "/devices/valves",
    response_model=list[DeviceResponse],
    dependencies=[Depends(get_current_admin)],
    summary="List all valve‐controller devices"
)
async def list_valve_devices(db: AsyncSession = Depends(get_db)):
    q = await db.execute(select(Device).where(Device.type == DeviceType.VALVE_CONTROLLER))
    return q.scalars().all()

@router.get(
    "/cameras/list",
    response_model=list[CameraReportResponse],  # or define a CameraResponse if you like
    dependencies=[Depends(get_current_admin)],
    summary="List all registered cameras"
)
async def list_registered_cameras(db: AsyncSession = Depends(get_db)):
    from app.models import Camera
    q = await db.execute(select(Camera))
    cams = q.scalars().all()
    return [
        {
            "camera_id": cam.id,
            "is_online": cam.is_online,
            "last_seen": cam.last_seen,
            "frames_received": cam.frames_received,
            "clips_count": cam.clips_count
        }
        for cam in cams
    ]