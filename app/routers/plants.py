from fastapi import APIRouter, Depends, HTTPException
from fastapi.logger import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from app.schemas import DosingOperation, PlantCreate, PlantDosingResponse, PlantResponse, SensorReading
from app.core.database import get_db
from app.services.plant_service import (
    get_all_plants,
    get_plant_by_id,
    create_plant,
    delete_plant
)
from app.models import Plant

router = APIRouter()

@router.get("/", response_model=List[PlantResponse])
async def fetch_all_plants(db: AsyncSession = Depends(get_db)):
    """Retrieve all plant profiles"""
    plants = await get_all_plants(db)
    return plants 

@router.get("/{plant_id}", response_model=PlantResponse)
async def fetch_plant(plant_id: int, db: AsyncSession = Depends(get_db)):
    """Retrieve a plant by ID."""
    return await get_plant_by_id(plant_id, db)

@router.post("/", response_model=PlantResponse)
async def add_plant(plant: PlantCreate, db: AsyncSession = Depends(get_db)):
    """Create a new plant."""
    return await create_plant(plant, db)

@router.delete("/{plant_id}")
async def remove_plant(plant_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a plant by ID."""
    return await delete_plant(plant_id, db)

@router.post("/execute-dosing/{plant_id}", response_model=PlantDosingResponse)
async def execute_dosing(plant_id: int, db: AsyncSession = Depends(get_db)):
    """
    Execute a dosing operation by checking the latest sensor readings and applying the correct amount of nutrients.
    
    **Note:** This endpoint expects the Plant object to have dosing parameters
    (`target_ph_min`, `target_ph_max`, `target_tds_min`, and `target_tds_max`). 
    If these are not configured, the endpoint returns a 400 error.
    """
    plant = await db.get(Plant, plant_id)
    if not plant:
        raise HTTPException(status_code=404, detail="Plant Profile not found")
    
    # Ensure the plant has dosing parameters.
    for attr in ("target_ph_min", "target_ph_max", "target_tds_min", "target_tds_max"):
        if not hasattr(plant, attr):
            raise HTTPException(status_code=400, detail="Plant dosing parameters not configured")
    
    target_ph_min = getattr(plant, "target_ph_min")
    target_ph_max = getattr(plant, "target_ph_max")
    target_tds_min = getattr(plant, "target_tds_min")
    target_tds_max = getattr(plant, "target_tds_max")
    
    # Get latest sensor readings for the plant's location.
    readings_result = await db.execute(
        select(SensorReading)
        .where(SensorReading.location == plant.location)
        .order_by(SensorReading.timestamp.desc())
    )
    latest_readings = readings_result.scalars().all()
    if not latest_readings:
        raise HTTPException(status_code=400, detail="No sensor readings available")
    
    # Extract pH and TDS values.
    ph = next((r.value for r in latest_readings if r.reading_type == "ph"), None)
    tds = next((r.value for r in latest_readings if r.reading_type == "tds"), None)
    if ph is None or tds is None:
        raise HTTPException(status_code=400, detail="Missing pH or TDS readings")
    
    # Determine dosing actions based on the plant’s dosing parameters.
    actions = []
    if ph < target_ph_min:
        actions.append({"pump": 1, "dose_ml": 10, "reasoning": "Increase pH"})
    elif ph > target_ph_max:
        actions.append({"pump": 2, "dose_ml": 10, "reasoning": "Decrease pH"})
    
    if tds < target_tds_min:
        actions.append({"pump": 3, "dose_ml": 5, "reasoning": "Increase nutrients"})
    elif tds > target_tds_max:
        actions.append({"pump": 4, "dose_ml": 5, "reasoning": "Decrease nutrients"})
    
    return {"plant_id": plant_id, "actions": actions}
