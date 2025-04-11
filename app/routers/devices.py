import json
import os
import ipaddress
import asyncio
import socket
import httpx
import logging
from fastapi import APIRouter, HTTPException, Depends, Query, Request,  WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.config import DEPLOYMENT_MODE  # e.g. "LAN" or "CLOUD"
from app.models import Device, User
from app.dependencies import get_current_user
from app.core.database import get_db
from app.services.device_controller import DeviceController
from app.services.llm import getSensorData
from app.schemas import (
    DosingDeviceCreate,
    SensorDeviceCreate,
    DeviceResponse,
    DeviceType,
    CamRegisterRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter()

cam_registry: dict[str, str] = {}
latest_frames = {}
ws_connections = {}

def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

def default_subnet_from_ip(local_ip: str) -> str:
    parts = local_ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
    return "192.168.1.0/24"

async def discover_cloud_device(device: Device, client: httpx.AsyncClient) -> dict:
    url = device.http_endpoint.rstrip("/") + "/discovery"
    try:
        response = await asyncio.wait_for(client.get(url), timeout=2.0)
        if response.status_code == 200:
            data = response.json()
            data["ip"] = device.http_endpoint
            return data
    except Exception as e:
        logger.error(f"Cloud discovery error for device {device.id} at {device.http_endpoint}: {e}")
    return None

async def discover_lan_device(ip: str, port: str, client: httpx.AsyncClient) -> dict:
    url = f"http://{ip}:{port}/discovery"
    try:
        response = await asyncio.wait_for(client.get(url), timeout=2.0)
        if response.status_code == 200:
            data = response.json()
            data["ip"] = ip
            return data
    except Exception as e:
        logger.debug(f"No response from {ip}:{port} - {e}")
    return None

@router.get("/discover-all", summary="Discover devices with progress updates")
async def discover_all_devices(db: AsyncSession = Depends(get_db)):
    async def event_generator():
        discovered_devices = []
        eventCount = 0  # Count every SSE event sent
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0)) as client:
            if DEPLOYMENT_MODE.upper() == "LAN":
                local_ip = get_local_ip()
                subnet = os.getenv("LAN_SUBNET", default_subnet_from_ip(local_ip))
                port = os.getenv("LAN_PORT", "80")
                network = ipaddress.ip_network(subnet, strict=False)
                ips = [str(ip) for ip in network.hosts()]
                # If you want a fixed target for LAN mode, force total_ips to 256:
                total_ips = len(ips)  # Or: total_ips = len(ips) if you prefer the real count
                logger.info(f"LAN mode: scanning {total_ips} IPs in subnet {subnet} on port {port}")

                sem = asyncio.Semaphore(20)
                async def sem_discover(ip: str):
                    async with sem:
                        return await discover_lan_device(ip, port, client)
                tasks = [asyncio.create_task(sem_discover(ip)) for ip in ips]
                for task in asyncio.as_completed(tasks):
                    eventCount += 1  # Increment for every event (each IP tested)
                    try:
                        result = await task
                    except Exception as exc:
                        logger.error(f"Error in LAN discovery task: {exc}")
                        result = None
                    if result:
                        discovered_devices.append(result)
                    yield f"data: {json.dumps({'eventCount': eventCount, 'total': total_ips})}\n\n"
            else:
                result = await db.execute(select(Device))
                devices = result.scalars().all()
                total_devices = len(devices)
                logger.info(f"CLOUD mode: found {total_devices} registered devices")
                sem = asyncio.Semaphore(20)
                async def sem_discover_cloud(device: Device):
                    async with sem:
                        return await discover_cloud_device(device, client)
                tasks = [asyncio.create_task(sem_discover_cloud(device)) for device in devices]
                for task in asyncio.as_completed(tasks):
                    eventCount += 1
                    try:
                        result = await task
                    except Exception as exc:
                        logger.error(f"Error in CLOUD discovery task: {exc}")
                        result = None
                    if result:
                        discovered_devices.append(result)
                    yield f"data: {json.dumps({'eventCount': eventCount, 'total': total_devices})}\n\n"
        # Final event: send the full discovered devices list.
        yield f"data: {json.dumps({'discovered_devices': discovered_devices})}\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ---------- Additional Endpoints ----------
@router.get("/discover", summary="Check if a device is connected")
async def check_device_connection(
    ip: str = Query(..., description="IP address of the device to validate")
):
    controller = DeviceController(device_ip=ip)
    device_info = await controller.discover()
    logger.info(f"Discovery response for {ip}: {device_info}")
    if not device_info or not isinstance(device_info, dict) or "device_id" not in device_info:
        raise HTTPException(status_code=404, detail="No device found at the provided IP")
    formatted_device = {
        "id": device_info.get("device_id"),
        "name": device_info.get("name", device_info.get("device_id")),
        "type": device_info.get("type"),
        "status": device_info.get("status"),
        "version": device_info.get("version"),
        "ip": device_info.get("ip")
    }
    return formatted_device

@router.post("/dosing", response_model=DeviceResponse)
async def create_dosing_device(
    device: DosingDeviceCreate,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        endpoint = device.http_endpoint
        if not endpoint.startswith("http"):
            endpoint = f"http://localhost/{endpoint}"
        controller = DeviceController(device_ip=endpoint)
        discovered_device = await controller.discover()
        if not discovered_device:
            raise HTTPException(status_code=500, detail="Device discovery failed at the given endpoint")
        existing = await session.execute(select(Device).where(Device.mac_id == device.mac_id))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Device already registered")
        new_device = Device(
            name=discovered_device.get("name", device.name),
            user_id=current_user.id,
            mac_id=device.mac_id,
            type=DeviceType.DOSING_UNIT,
            http_endpoint=endpoint,
            location_description=device.location_description or "",
            pump_configurations=[p.model_dump() for p in device.pump_configurations],
            is_active=True,
            farm_id=device.farm_id
        )
        session.add(new_device)
        await session.commit()
        await session.refresh(new_device)
        return new_device
    except Exception as e:
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"Error creating dosing device: {e}")

@router.post("/sensor", response_model=DeviceResponse)
async def create_sensor_device(
    device: SensorDeviceCreate,
    session: AsyncSession = Depends(get_db)
):
    try:
        new_device = Device(
            mac_id=device.mac_id,
            name=device.name,
            type=device.type,
            http_endpoint=device.http_endpoint,
            location_description=device.location_description,
            sensor_parameters=device.sensor_parameters,
            is_active=True,
            farm_id=device.farm_id
        )
        session.add(new_device)
        await session.commit()
        await session.refresh(new_device)
        return new_device
    except Exception as e:
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"Error creating sensor device: {e}")

@router.get("", response_model=list[DeviceResponse], summary="List all devices")
async def list_devices(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Device))
    return result.scalars().all()

@router.get("/{device_id}", response_model=DeviceResponse, summary="Get device details")
async def get_device(device_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device

@router.get("/sensoreading/{device_id}")
async def get_sensor_readings(device_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    sensor_data = await getSensorData(device)
    return sensor_data


@router.get("/device/{device_id}/version", summary="Get device version")
async def get_device_version(device_id: int, db: AsyncSession = Depends(get_db)):
    try:
        # Fetch the device from the database
        result = await db.execute(select(Device).where(Device.id == device_id))
        device = result.scalar_one_or_none()
        
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        
        controller = DeviceController(device_ip=device.http_endpoint)
        device_version = await controller.get_version()
        
        if not device_version:
            raise HTTPException(status_code=500, detail="Failed to retrieve device version")
        
        return {"device_id": device_id, "version": device_version}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching device version: {e}")
    

@router.post("/register_cam", summary="Register an ESP32-CAM")
async def register_cam(request: CamRegisterRequest):
    cam_registry[request.cam_id] = request.ip
    logger.info(f"Registered cam {request.cam_id} with IP {request.ip}")
    return {"message": "Camera registered", "cam_id": request.cam_id, "ip": request.ip}

@router.get("/get_cams", summary="List all registered cameras")
async def get_cams():
    return cam_registry

@router.get("/stream/{cam_id}", summary="Proxy MJPEG stream from an ESP32-CAM")
async def stream_cam(cam_id: str):
    ip = cam_registry.get(cam_id)
    if not ip:
        raise HTTPException(status_code=404, detail="Camera not registered")
    return {
        "cam_id": cam_id,
        "stream_url": f"http://{ip}:81/stream"
    }

@router.post("/upload_mjpeg", summary="Receive and process MJPEG stream from ESP32 master")
async def upload_mjpeg_stream(request: Request, cam_id: str = Query(..., description="Camera ID, e.g. 'cam_1'")):
    """
    This endpoint receives a continuous MJPEG stream forwarded by the ESP32 master.
    It reads chunks from the request stream, detects JPEG frame boundaries (assumed here to be marked by the word "frame"),
    and whenever a new JPEG frame is extracted, it stores it and dispatches it to connected WebSocket clients.
    """
    logger.info(f"Started receiving stream for {cam_id}")
    # We assume a known boundary; often the MJPEG stream uses something like "--frame"
    boundary = b"--123456789000000000000987654321"
    buffer = b""
    try:
        async for chunk in request.stream():
            logger.info(f"Received chunk of size {len(chunk)}")
            buffer += chunk
            logger.info(f"Received chunk of buffer {len(buffer )}")
            while boundary in buffer:
                # Split by boundary; the first part may be incomplete so take the part in between boundaries.
                parts = buffer.split(boundary)
                if len(parts) < 30:
                    break
                frame_chunk = parts[1]
                logger.info(f"Received chunk of frame_chunke {frame_chunk}")
                # Optionally, you might strip header data here—this example assumes frame_chunk is the JPEG data.
                latest_frames[cam_id] = frame_chunk
                logger.info(f"Stream ended for {latest_frames[cam_id]}")
                # Dispatch to all connected WebSocket clients
                if cam_id in ws_connections:
                    for ws in ws_connections[cam_id]:
                        try:
                            await ws.send_bytes(frame_chunk)
                        except Exception as e:
                            logger.error(f"Error sending frame to WebSocket: {e}")
                # Remove the processed frame from the buffer; keep the last part (which might be incomplete)
                buffer = boundary.join(parts[2:])
        logger.info(f"Stream ended for {cam_id}")
        return JSONResponse(content={"cam_id": cam_id, "status": "stream ended"})
    except Exception as e:
        logger.error(f"Error while streaming for camera {cam_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
@router.get("/cam/stream_mjpeg", summary="Show MJPEG stream from ESP32")
async def stream_mjpeg(request: Request, cam_id: str = Query(..., description="Camera ID, e.g. 'cam_1'")):
    boundary = b"--123456789000000000000987654321"

    async def stream_generator():
        while True:
            if await request.is_disconnected():
                break
            if cam_id in latest_frames:
                frame = latest_frames[cam_id]
                header = (
                    f"{boundary}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n"
                )
                yield header.encode("utf-8")
                yield frame
                yield "\r\n".encode("utf-8")
            # adjust sleep to control frame rate
            await asyncio.sleep(0.05)
    
    return StreamingResponse(
        stream_generator(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}"
    )
