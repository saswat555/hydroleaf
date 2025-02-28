from fastapi import APIRouter, Depends, HTTPException
from fastapi.logger import logger
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from app.schemas import DosingOperation, PlantCreate, PlantResponse, SensorReading
from app.core.database import get_db
from app.services.plant_service import (
    get_all_plants,
    get_plant_by_id,
    create_plant,
    delete_plant
)
from app.models import Device, Plant

router = APIRouter()

@router.get("/plants", response_model=List[PlantResponse])
async def fetch_all_plants(db: AsyncSession = Depends(get_db)):
    """Retrieve all plant profiles"""
    plants = await get_all_plants(db)
    return plants 


@router.get("/plants/{plant_id}", response_model=PlantResponse)
async def fetch_plant(plant_id: int, db: AsyncSession = Depends(get_db)):
    """Retrieve a plant by ID."""
    return await get_plant_by_id(plant_id, db)

@router.post("/plants", response_model=PlantResponse)
async def add_plant(plant: PlantCreate, db: AsyncSession = Depends(get_db)):
    """Create a new plant."""
    return await create_plant(plant, db)

@router.delete("/plants/{plant_id}")
async def remove_plant(plant_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a plant by ID."""
    return await delete_plant(plant_id, db)


@router.post("/execute-dosing/{plant_id}", response_model=DosingOperation)
async def execute_dosing(
    plant_id: int, db: AsyncSession = Depends(get_db)
):
    """
    Execute a dosing operation by checking the latest sensor readings and applying the correct amount of nutrients.
    """
    plant = await db.get(Plant, plant_id)
    if not plant:
        raise HTTPException(status_code=404, detail="Plant Profile not found")

    # Get latest sensor readings for the plant's location
    readings = await db.execute(
        select(SensorReading)
        .where(SensorReading.location == plant.location)
        .order_by(SensorReading.timestamp.desc())
    )
    latest_readings = readings.scalars().all()
    if not latest_readings:
        raise HTTPException(status_code=400, detail="No sensor readings available")

    # Extract pH and TDS values
    ph = next((r.value for r in latest_readings if r.reading_type == "ph"), None)
    tds = next((r.value for r in latest_readings if r.reading_type == "tds"), None)
    if ph is None or tds is None:
        raise HTTPException(status_code=400, detail="Missing pH or TDS readings")

    # Determine dosing based on the plant profile
    actions = []
    if ph < plant.target_ph_min:
        actions.append({"pump": 1, "dose_ml": 10, "reasoning": "Increase pH"})
    elif ph > plant.target_ph_max:
        actions.append({"pump": 2, "dose_ml": 10, "reasoning": "Decrease pH"})

    if tds < plant.target_tds_min:
        actions.append({"pump": 3, "dose_ml": 5, "reasoning": "Increase nutrients"})
    elif tds > plant.target_tds_max:
        actions.append({"pump": 4, "dose_ml": 5, "reasoning": "Decrease nutrients"})

    return {"plant_id": plant_id, "actions": actions}
