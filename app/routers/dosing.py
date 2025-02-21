# app/routers/dosing.py

from fastapi import APIRouter, HTTPException, Depends # type: ignore
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from datetime import datetime, UTC
from app.core.database import get_db
from app.schemas import DosingOperation, DosingProfileResponse, DosingProfileCreate
from app.services.dose_manager import execute_dosing_operation, cancel_dosing_operation
from app.models import Device, DosingProfile
from sqlalchemy import select
router = APIRouter()

@router.post("/execute/{device_id}", response_model=DosingOperation)
async def execute_dosing(
    device_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Execute dosing operation for a device"""
    # Get device and its active profile
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    if device.type != "dosing_unit":
        raise HTTPException(status_code=400, detail="Device is not a dosing unit")
    
    try:
        result = await execute_dosing_operation(device_id, device.pump_configurations)
        return result
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Error executing dosing operation: {exc}"
        )

@router.post("/cancel/{device_id}")
async def cancel_dosing(
    device_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Cancel active dosing operation"""
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    try:
        await cancel_dosing_operation(device_id)
        return {"message": "Dosing operation cancelled"}
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Error cancelling dosing operation: {exc}"
        )

@router.get("/history/{device_id}", response_model=List[DosingOperation])
async def get_dosing_history(
    device_id: int,
    session: AsyncSession = Depends(get_db)
):
    """Get dosing history for a device"""
    try:
        # First verify device exists
        result = await session.execute(
            select(Device).where(Device.id == device_id)
        )
        device = result.scalar_one_or_none()
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")

        # Get dosing history
        result = await session.execute(
            select(models.DosingOperation)
            .where(models.DosingOperation.device_id == device_id)
            .order_by(models.DosingOperation.timestamp.desc())
        )
        operations = result.scalars().all()
        return operations
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching dosing history: {str(e)}"
        )

@router.post("/profile", response_model=DosingProfileResponse)
async def create_dosing_profile(
    profile: DosingProfileCreate,
    db: AsyncSession = Depends(get_db)
):
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

    now = datetime.now(UTC)
    new_profile = DosingProfile(
        **profile.model_dump(),
        created_at=now,
        updated_at=now
    )
    
    db.add(new_profile)
    await db.commit()
    await db.refresh(new_profile)
    return new_profile