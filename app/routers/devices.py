import json
import os
import ipaddress
import asyncio
import socket
import httpx
import logging
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse
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
)

logger = logging.getLogger(__name__)
router = APIRouter()

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
