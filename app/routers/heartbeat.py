from fastapi import APIRouter, Request
from app.core.config import DEPLOYMENT_MODE
from app.services.device_discovery import update_device

router = APIRouter()

@router.post("/heartbeat")
async def heartbeat(request: Request):
    data = await request.json()
    device_id = data.get("device_id")
    client_ip = request.client.host  # IP address of the connecting device
    if DEPLOYMENT_MODE == "CLOUD" and device_id:
        update_device(device_id, client_ip)
    return {"status": "ok"}
