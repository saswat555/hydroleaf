import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from app.models import Farm, Plant
logger = logging.getLogger(__name__)

async def get_all_plants(db: AsyncSession):
    """Retrieve all plants from the database."""
    try:
        logger.info("Fetching plants from database...")

        # Fetch plants
        result = await db.execute(select(Plant))
        plants = result.scalars().all()

        if not plants:
            logger.info("No plants found, returning an empty list.")
            return []

        logger.info(f"Fetched {len(plants)} plants from the database")
        return plants

    except Exception as e:
        logger.error(f"Database query failed: {str(e)}")
        return []


async def get_plant_by_id(plant_id: int, db: AsyncSession):
    """Retrieve a specific plant by ID."""
    plant = await db.get(Plant, plant_id)
    if not plant:
        raise HTTPException(status_code=404, detail="Plant not found")
    return plant

async def create_plant(payload, db: AsyncSession, farm_id: int | None = None):
    """
    Create a new plant.  Accepts either a PlantCreate Pydantic model or a dict.

    If `farm_id` is provided, verify that the farm exists and assign it to the Plant.
    """
    # Convert Pydantic model to dict if necessary
    data = payload.model_dump() if hasattr(payload, "model_dump") else dict(payload)
    # Check farm existence if farm_id supplied
    if farm_id is not None:
        farm = await db.get(Farm, farm_id)
        if not farm:
            raise HTTPException(status_code=404, detail="Farm not found")
        data["farm_id"] = farm_id

    new_plant = Plant(**data)
    db.add(new_plant)
    await db.commit()
    await db.refresh(new_plant)
    return new_plant

async def delete_plant(plant_id: int, db: AsyncSession):
    """Delete a plant by ID."""
    plant = await db.get(Plant, plant_id)
    if not plant:
        raise HTTPException(status_code=404, detail="Plant not found")
    await db.delete(plant)
    await db.commit()
    return {"message": "Plant deleted successfully"}

async def list_plants_by_farm(farm_id: int, db: AsyncSession) -> list[Plant]:
    """
    Retrieve all plants belonging to a given farm.
    """
    try:
        result = await db.execute(
            select(Plant).where(Plant.farm_id == farm_id)
        )
        plants = result.scalars().all()
        return plants
    except Exception as e:
        logger.error(f"Failed to list plants for farm {farm_id}: {e}")
        return []

async def list_plants_by_farm(farm_id: int, db: AsyncSession):
    """
    (Stub) List all plants for a given farm.
    Eventually this should filter by Plant.farm_id, but for now
    it simply returns all plants so the import and signature exist.
    """
    return await get_all_plants(db)
