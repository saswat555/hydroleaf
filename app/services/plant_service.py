import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from app.models import Plant
logger = logging.getLogger(__name__)

async def get_all_plants(db: AsyncSession):
    """Retrieve all plants from the database."""
    try:
        logger.info("Fetching plants from database...")

        # Fetch plants
        result = await db.execute(select(Plant))
        plants = result.scalars().all()

        # ✅ Return an empty list instead of an error if no plants exist
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

async def create_plant(plant_data, db: AsyncSession):
    """Create a new plant."""
    new_plant = Plant(**plant_data.model_dump())
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
