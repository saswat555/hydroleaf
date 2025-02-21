from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List
from datetime import datetime, UTC

from app.schemas import (
    DosingDeviceCreate,
    SensorDeviceCreate,
    DeviceResponse,
    DeviceType,
    DosingOperation
)
from app.models import Device
from app.core.database import get_db
from app.services.mqtt import MQTTPublisher
from app.services.device_discovery import (
    DeviceDiscoveryService,
    get_device_discovery_service,
    discover_devices
)

router = APIRouter()
# Initialize MQTT with error handling
try:
    mqtt_publisher = MQTTPublisher()
except Exception as e:
    import logging
    logging.warning(f"MQTT initialization failed: {e}. Some features may be limited.")
    mqtt_publisher = None

# Rest of your router code remains the same...

@router.get("/discover", summary="Discover available devices")
async def discover_network_devices(
    discovery_service: DeviceDiscoveryService = Depends(get_device_discovery_service)
):
    """Discover devices on the network"""
    return await discovery_service.scan_network()

@router.post("/dosing", response_model=DeviceResponse)
async def create_dosing_device(
    device: DosingDeviceCreate,
    session: AsyncSession = Depends(get_db)
):
    """Create a new dosing device"""
    try:
        new_device = Device(
            name=device.name,
            type=DeviceType.DOSING_UNIT,
            mqtt_topic=device.mqtt_topic,
            location_description=device.location_description,
            pump_configurations=[pump.model_dump() for pump in device.pump_configurations],
            is_active=True
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
    """Register a new sensor device"""
    try:
        new_device = Device(
            name=device.name,
            type=device.type,
            mqtt_topic=device.mqtt_topic,
            location_description=device.location_description,
            sensor_parameters=device.sensor_parameters,
            is_active=True
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
    """Get all registered devices"""
    result = await db.execute(select(Device))
    return result.scalars().all()

@router.get("/{device_id}", response_model=DeviceResponse, summary="Get device details")
async def get_device(device_id: int, db: AsyncSession = Depends(get_db)):
    """Get details of a specific device"""
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device