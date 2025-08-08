# app/services/farm_service.py

import logging
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models import Farm, User

logger = logging.getLogger(__name__)

async def create_farm(owner_id: int, payload, db: AsyncSession) -> Farm:
    """
    Create a new farm belonging to the given owner (user/admin).
    Accepts either a Pydantic model or a plain dict for `payload`.
    """
    data = payload.model_dump() if hasattr(payload, "model_dump") else dict(payload)
    new_farm = Farm(owner_id=owner_id, **data)
    await db.add(new_farm)
    await db.commit()
    await db.refresh(new_farm)
    return new_farm


async def list_farms_for_user(user_id: int, db: AsyncSession) -> list[Farm]:
    """
    Return all farms owned by a specific user.
    """
    result = await db.execute(select(Farm).where(Farm.user_id == user_id))
    return result.scalars().all()


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


async def share_farm_with_user(farm_id: int, user_id: int, db: AsyncSession) -> dict:
    """
    Share a farm with another user: creates a row in the
    `farm_shares` association table.
    """
    # 1) ensure farm exists
    farm = await db.get(Farm, farm_id)
    if not farm:
        raise HTTPException(status_code=404, detail="Farm not found")

    # 2) ensure user exists
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # 3) avoid duplicate shares
    if user not in farm.shared_users:
        farm.shared_users.append(user)
        # SQLAlchemy knows to INSERT into farm_shares
        db.add(farm)
        await db.commit()

    return {"farm_id": farm.id, "user_id": user.id}
