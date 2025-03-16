from fastapi import APIRouter, HTTPException, Depends
from fastapi.logger import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List
from datetime import datetime, UTC
from pydantic import BaseModel
from app.schemas import DeviceType 
from app.core.database import get_db
from app.schemas import (
    DosingOperation,
    DosingProfileResponse,
    DosingProfileCreate
)
from app.models import Device, DosingProfile
from app.services.dose_manager import execute_dosing_operation, cancel_dosing_operation
router = APIRouter()

@router.post("/execute/{device_id}", response_model=DosingOperation)
async def execute_dosing(
    device_id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    Execute a dosing operation for a device using its HTTP endpoint.
    """
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if device.type != DeviceType.DOSING_UNIT:
        raise HTTPException(status_code=400, detail="Device is not a dosing unit")
    
    try:
        # Use the device's HTTP endpoint and pump configurations to execute the dosing operation
        result = await execute_dosing_operation(device_id, device.http_endpoint, device.pump_configurations)
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
    """
    Cancel an active dosing operation for a device.
    """
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    
    try:
        result = await cancel_dosing_operation(device_id, device.http_endpoint)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error cancelling dosing operation: {exc}")


@router.get("/history/{device_id}", response_model=List[DosingOperation])
async def get_dosing_history(
    device_id: int,
    session: AsyncSession = Depends(get_db)
):
    """
    Retrieve the dosing history for a device.
    """
    try:
        result = await session.execute(
            select(Device).where(Device.id == device_id)
        )
        device = result.scalar_one_or_none()
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")

        # Import the DosingOperation model from app.models to query the history
        from app.models import DosingOperation as ModelDosingOperation
        result = await session.execute(
            select(ModelDosingOperation)
            .where(ModelDosingOperation.device_id == device_id)
            .order_by(ModelDosingOperation.timestamp.desc())
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
    """
    Create a new dosing profile for a dosing device.
    """
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

# New endpoint to handle the LLM dosing flow
class LlmDosingRequest(BaseModel):
    sensor_data: dict
    plant_profile: dict

@router.post("/llm-request")
async def llm_dosing_request(
    device_id: int,
    request: LlmDosingRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Process a dosing request using sensor data and plant profile to generate a dosing plan via LLM.
    """
    try:
        # Verify device exists
        device = await db.get(Device, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")

        # Process the dosing request
        from app.services.llm import process_dosing_request
        result,raw = await process_dosing_request(device_id, request.sensor_data, request.plant_profile, db)

        return result,raw

    except HTTPException as he:
        raise he  # Allow already handled errors to propagate correctly

    except Exception as exc:
        logger.exception(f"Unexpected error in /llm-request: {exc}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

class llmPlaningRequest(BaseModel):
    sensor_data: dict
    plant_profile: dict
    query: str

@router.post("/llm-plan")
async def llm_plan(
    device_id: int,
    request: llmPlaningRequest,
    db: AsyncSession= Depends(get_db)
): 
    """
    PROCESS A DOSING PLAN ACCORDING TO GIVEN REGION CLIMATE
    """

    try:
        # Verify device exists
        device = await db.get(Device, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")

        # Process the dosing request
        from app.services.llm import process_sensor_plan
        result= await process_sensor_plan(device_id, request.sensor_data, request.plant_profile, request.query, db)

        return result

    except HTTPException as he:
        raise he  # Allow already handled errors to propagate correctly

    except Exception as exc:
        logger.exception(f"Unexpected error in /llm-request: {exc}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

