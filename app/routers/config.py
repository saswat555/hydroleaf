# app/routers/config.py

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List
from sqlalchemy import select, func  
# Update these imports to use the correct paths
from app.core.database import get_db
from app.schemas import DosingProfileCreate, DosingProfileResponse, DeviceType, PlantCreate, PlantResponse
from app.models import Device, DosingOperation, DosingProfile, Plant, SensorReading
from app.services.device_discovery import discover_devices

router = APIRouter()


@router.get("/system-info", summary="Get system information")
async def get_system_info(db: AsyncSession = Depends(get_db)):
    """Get system configuration and status"""
    # Get actual device counts from database
    dosing_count = await db.scalar(
        select(func.count()).select_from(Device).where(Device.type == "dosing_unit")
    )
    sensor_count = await db.scalar(
        select(func.count()).select_from(Device).where(Device.type.in_(["ph_tds_sensor", "environment_sensor"]))
    )
    
    return {
        "version": "1.0.0",
        "device_count": {
            "dosing": dosing_count or 0,
            "sensors": sensor_count or 0
        }
    }

@router.post("/dosing-profile", response_model=DosingProfileResponse)
async def create_dosing_profile(
    profile: DosingProfileCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create a new dosing profile for a device"""
    # Verify device exists
    result = await db.execute(
        select(Device).where(Device.id == profile.device_id)
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    if device.type != "dosing_unit":
        raise HTTPException(
            status_code=400,
            detail="Dosing profiles can only be created for dosing units"
        )

    # Create the new profile from the request payload
    new_profile = DosingProfile(**profile.model_dump())
    db.add(new_profile)
    try:
        await db.commit()
        await db.refresh(new_profile)
        # Fix: Ensure updated_at is a valid datetime value
        if new_profile.updated_at is None:
            new_profile.updated_at = new_profile.created_at
        return new_profile
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Error creating dosing profile: {exc}"
        )
        
@router.get("/dosing-profiles/{device_id}", response_model=List[DosingProfileResponse])
async def get_device_profiles(
    device_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get all dosing profiles for a device"""
    # First verify device exists
    device = await db.scalar(
        select(Device).where(Device.id == device_id)
    )
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    result = await db.execute(
        select(DosingProfile)
        .where(DosingProfile.device_id == device_id)
        .order_by(DosingProfile.created_at.desc())
    )
    profiles = result.scalars().all()
    return profiles

@router.delete("/dosing-profiles/{profile_id}")
async def delete_dosing_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Delete a dosing profile"""
    profile = await db.get(DosingProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    
    try:
        await db.delete(profile)
        await db.commit()
        return {"message": "Profile deleted successfully"}
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Error deleting profile: {exc}"
        )
        
        
@router.get("/system-info")
async def get_system_info(db: AsyncSession = Depends(get_db)):
    """Get system configuration and status"""
    # Get device counts
    dosing_count = await db.scalar(
        select(func.count()).select_from(Device).where(Device.type == DeviceType.DOSING_UNIT)
    )
    sensor_count = await db.scalar(
        select(func.count()).select_from(Device).where(
            Device.type.in_([DeviceType.PH_TDS_SENSOR, DeviceType.ENVIRONMENT_SENSOR])
        )
    )
    
    return {
        "version": "1.0.0",
        "device_count": {
            "dosing": dosing_count or 0,
            "sensors": sensor_count or 0
        }
    }