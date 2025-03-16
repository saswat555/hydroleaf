from fastapi import APIRouter
import asyncio
from app.services.device_discovery import get_connected_devices
from app.services.ping import ping_host

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
