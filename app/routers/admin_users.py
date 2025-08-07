# app/routers/admin_users.py
from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import joinedload
from typing import List
import datetime
import os
import jwt
from app.models import User, Admin
from app.core.config import SECRET_KEY
from app.core.database import get_db
from app.dependencies import get_current_admin
from app.schemas import UserResponse, UserUpdate

router = APIRouter(prefix="/admin/users", tags=["admin", "users"])

@router.get("/", response_model=List[UserResponse])
async def list_users(
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    """Admin-only: list all users (eager-load devices & profile)."""
    stmt = (
        select(User)
        .options(
            joinedload(User.devices),
            joinedload(User.profile),
        )
    )
    result = await db.execute(stmt)
    return result.unique().scalars().all()
    

@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin)
):
    """Admin-only: get one user."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user

@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    user_update: UserUpdate,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin)
):
    """Admin-only: update a user's email or role."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    if user_update.email is not None:
        user.email = user_update.email
    if user_update.role is not None:
        user.role = user_update.role

    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user

@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin)
):
    """
    Admin-only endpoint to delete a user.
    """
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    await db.delete(user)
    await db.commit()
    return {"detail": "User deleted successfully"}

@router.post("/impersonate/{user_id}")
async def impersonate_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin)
):
    """Admin-only: return a JWT for the target user."""
    user = await db.get(Admin, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    ALGORITHM = "HS256"
    token_data = {
        "user_id": user.id,
        "role": user.role,
        "impersonated_by": admin.id,  # For audit purposes
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=1)
    }
    token = jwt.encode(token_data, SECRET_KEY, algorithm=ALGORITHM)
    return {
        "access_token": token,
        "token_type": "bearer",
        "impersonated_user": user.email
    }
