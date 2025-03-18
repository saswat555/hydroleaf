# app/routers/farms.py
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List
from app.core.database import get_db
from app.models import Farm, User
from app.schemas import FarmCreate, FarmResponse
from app.dependencies import get_current_user

router = APIRouter(prefix="/api/v1/farms", tags=["farms"])

@router.post("/", response_model=FarmResponse)
async def create_farm(farm: FarmCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    new_farm = Farm(user_id=current_user.id, name=farm.name, location=farm.location)
    db.add(new_farm)
    await db.commit()
    await db.refresh(new_farm)
    return new_farm

@router.get("/", response_model=List[FarmResponse])
async def list_farms(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    result = await db.execute(select(Farm).where(Farm.user_id == current_user.id))
    return result.scalars().all()

@router.get("/{farm_id}", response_model=FarmResponse)
async def get_farm(farm_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    farm = await db.get(Farm, farm_id)
    if not farm or farm.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Farm not found")
    return farm

@router.delete("/{farm_id}")
async def delete_farm(farm_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    farm = await db.get(Farm, farm_id)
    if not farm or farm.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Farm not found")
    await db.delete(farm)
    await db.commit()
    return {"detail": "Farm deleted successfully"}
