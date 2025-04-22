# app/routers/heartbeat.py

from fastapi import APIRouter, Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from app.core.config import DEPLOYMENT_MODE
from app.services.device_discovery import update_device
from app.dependencies import get_current_device
from app.core.database import get_db
from app.models import Device, Task

router = APIRouter()

@router.post(
    "/heartbeat",
    dependencies=[Depends(get_current_device)],  # authenticate deviceKey → ActivationKey → Device → Subscription
)
async def heartbeat(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    # 1) Parse payload
    payload    = await request.json()
    device_id  = payload.get("device_id")
    fw_version = payload.get("version")
    client_ip  = request.client.host

    # 2) Presence tracking (for your LAN/CLOUD discovery)
    if DEPLOYMENT_MODE == "CLOUD" and device_id:
        update_device(device_id, client_ip)

    # 3) Update the Device row (last_seen + firmware_version)
    result = await db.execute(select(Device).where(Device.mac_id == device_id))
    device = result.scalar_one_or_none()
    if device:
        device.last_seen        = func.now()
        device.firmware_version = fw_version
        await db.commit()

    # 4) Pull all pending “pump” tasks
    q = await db.execute(
        select(Task).where(
            Task.device_id == device_id,
            Task.status    == "pending",
            Task.type      == "pump",
        )
    )
    tasks = [t.parameters for t in q.scalars().all()]

    # 5) Return exactly what the firmware is expecting
    return {
        "status":         "ok",
        "status_message": "All systems nominal",
        "tasks":          tasks,
    }
