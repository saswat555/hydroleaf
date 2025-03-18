from fastapi import APIRouter, HTTPException, Depends, Query
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List
import logging
from app.dependencies import get_current_user  
logger = logging.getLogger(__name__)
from app.schemas import (
    DosingDeviceCreate,
    SensorDeviceCreate,
    DeviceResponse,
    DeviceType,
)
from app.models import Device, User
from app.core.database import get_db
# UPDATED: Removed old DeviceDiscoveryService imports and use DeviceController instead.
from app.services.device_controller import DeviceController
from app.services.llm import getSensorData

router = APIRouter()

@router.get("/discover", summary="Check if a device is connected")
async def check_device_connection(
    ip: str = Query(..., description="IP address of the device to validate")
):
    """
    Validate connectivity of a device at a specific IP address using the unified device controller.
    """
    controller = DeviceController(device_ip=ip)
    device_info = await controller.discover()

    # 🔍 Debugging: Log the response from discover()
    logger.info(f"Device discovery response for {ip}: {device_info}")

    # 🔥 Fix: Ensure empty responses trigger a 404
    if not device_info or not isinstance(device_info, dict) or "device_id" not in device_info:
        raise HTTPException(status_code=404, detail="No device found at the provided IP")

    # Map device_info keys to a formatted response.
    formatted_device = {
        "id": device_info.get("device_id"),
        "name": device_info.get("device_id"),  # Adjust this if the device sends a proper name.
        "type": device_info.get("type"),
        "status": device_info.get("status"),
        "version": device_info.get("version"),
        "ip": device_info.get("ip")
    }
    return formatted_device

# app/routers/devices.py
@router.post("/dosing", response_model=DeviceResponse)
async def create_dosing_device(
    device: DosingDeviceCreate,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Create a new dosing device with an HTTP endpoint.
    The endpoint calls the device’s discovery method.
    """
    try:
        # Normalize the endpoint: if it doesn't start with http, prepend a base URL.
        endpoint = device.http_endpoint
        if not endpoint.startswith("http"):
            endpoint = f"http://localhost/{endpoint}"
        
        # Use the normalized endpoint for discovery.
        controller = DeviceController(device_ip=endpoint)
        discovered_device = await controller.discover()
        if not discovered_device:
            raise HTTPException(
                status_code=500, 
                detail="Could not discover any device at the given endpoint"
            )
        # Check if the device is already registered.
        existing = await session.execute(
            select(Device).where(Device.mac_id == device.mac_id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Device already registered")

        # Use the discovered device's name if available.
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
        raise HTTPException(
            status_code=500,
            detail=f"Error creating dosing device: {str(e)}"
        )


@router.post("/sensor", response_model=DeviceResponse)
async def create_sensor_device(
    device: SensorDeviceCreate,
    session: AsyncSession = Depends(get_db)
):
    """Register a new sensor device with an HTTP endpoint."""
    try:
        new_device = Device(
            mac_id=device.mac_id,  # Ensure mac_id is set!
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
        raise HTTPException(
            status_code=500,
            detail=f"Error creating sensor device: {str(e)}"
        )

@router.get("", response_model=List[DeviceResponse], summary="List all devices")
async def list_devices(db: AsyncSession = Depends(get_db)):
    """Retrieve all registered devices."""
    result = await db.execute(select(Device))
    return result.scalars().all()

@router.get("/{device_id}", response_model=DeviceResponse, summary="Get device details")
async def get_device(device_id: int, db: AsyncSession = Depends(get_db)):
    """Get details of a specific device."""
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device

@router.get("/sensoreading/{device_id}")
async def getSensorReadings(device_id: int, db: AsyncSession = Depends(get_db)):
    """
    Get details of a specific device and fetch its sensor readings.
    """
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()

    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    sensor_data = await getSensorData(device)
    
    return sensor_data

@router.get("/discover-all", summary="Automatically discover all connected devices")
async def discover_all_devices(db: AsyncSession = Depends(get_db)):
    """
    Retrieve all registered devices and send a GET request to each device's /discovery endpoint.
    Returns a list of devices that responded successfully.
    """
    result = await db.execute(select(Device))
    devices = result.scalars().all()
    discovered_devices = []
    
    async with httpx.AsyncClient(timeout=5) as client:
        for device in devices:
            try:
                # Use the device's http_endpoint appended with "/discovery"
                url = device.http_endpoint.rstrip("/") + "/discovery"
                response = await client.get(url)
                if response.status_code == 200:
                    data = response.json()
                    data["ip"] = device.http_endpoint  # Optionally include the endpoint
                    discovered_devices.append(data)
            except Exception as e:
                # Log error and skip device if request fails
                logger.error(f"Error discovering device {device.id} at {device.http_endpoint}: {e}")
                continue
    return discovered_devices
