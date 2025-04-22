import time
import uuid
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models import Device


# In‑memory registry: key=device_id, value=dict(ip=<ip>, last_seen=<timestamp>)
_connected_devices = {}

def update_device(device_id: str, ip: str) -> None:
    _connected_devices[device_id] = {"ip": ip, "last_seen": time.time()}

def get_connected_devices() -> dict:
    now = time.time()
    # Only return devices seen in the last 60 seconds (adjust as needed)
    return {device_id: info for device_id, info in _connected_devices.items() if now - info["last_seen"] < 60}

async def assign_unique_device_key(
        device_id: int,
        db:AsyncSession
    ) -> dict:
    """
    Assign a unique key to a device based on its device_id and store it in the database.
    """
    unique_key = str(uuid.uuid4())

    try:
        result = await db.execute(select(Device).filter(Device.id == device_id))
        device = result.scalar_one_or_none()

        if not device:
            raise HTTPException(status_code=404, detail="Device not found")

        device.unique_key = unique_key
        await db.commit()

        return {"device_id": device_id, "unique_key": unique_key}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error assigning unique key: {str(exc)}")
