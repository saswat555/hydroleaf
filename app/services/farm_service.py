# app/services/farm_service.py

import logging
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models import Farm

logger = logging.getLogger(__name__)

async def create_farm(owner_id: int, farm_data, db: AsyncSession) -> Farm:
    """
    Create a new farm belonging to the given owner (user/admin).
    `farm_data` is a Pydantic model with .model_dump() returning:
      { name: str, location: str, latitude: float, longitude: float }
    """
    payload = farm_data.model_dump()
    new_farm = Farm(user_id=owner_id, **payload)
    db.add(new_farm)
    await db.commit()
    await db.refresh(new_farm)
    return new_farm

async def list_farms_for_user(user_id: int, db: AsyncSession) -> list[Farm]:
    """
    Return all farms owned by a specific user.
    """
    result = await db.execute(select(Farm).where(Farm.user_id == user_id))
    farms = result.scalars().all()
    return farms

async def get_farm_by_id(farm_id: int, db: AsyncSession) -> Farm:
    """
    Fetch a single farm by its PK.  404 if not found.
    """
    farm = await db.get(Farm, farm_id)
    if not farm:
        raise HTTPException(status_code=404, detail="Farm not found")
    return farm

async def delete_farm(farm_id: int, db: AsyncSession) -> dict:
    """
    Delete a farm by ID.  404 if missing.
    Returns {"message": "Farm deleted successfully"} on success.
    """
    farm = await db.get(Farm, farm_id)
    if not farm:
        raise HTTPException(status_code=404, detail="Farm not found")
    await db.delete(farm)
    await db.commit()
    return {"message": "Farm deleted successfully"}


async def share_farm_with_user(farm_id: int, owner_id: int, sub_user_id: int, db: AsyncSession):
    """
    Stub for future farm-sharing functionality.
    Tests expect this symbol to exist; implementation will come later.
    """
    farm = await db.get(Farm, farm_id)
    if not farm or farm.user_id != owner_id:
        raise HTTPException(status_code=404, detail="Farm not found or access denied")
    # TODO: implement real sharing (e.g. via association table)
    raise HTTPException(status_code=501, detail="Not implemented")