    # app/routers/users.py
from fastapi import APIRouter, HTTPException, Depends
from app.dependencies import get_current_user
from app.models import User
from app.core.database import get_db
from app.schemas import UserResponse, UserUpdate  # reuse the already defined UserUpdate from schemas.py
from sqlalchemy.future import select

router = APIRouter(prefix="/api/v1/users", tags=["users"])

@router.get("/me", response_model=UserResponse)
async def get_my_profile(current_user: User = Depends(get_current_user)):
    return current_user

@router.put("/me", response_model=UserResponse)
async def update_my_profile(update: UserUpdate, db=Depends(get_db), current_user: User = Depends(get_current_user)):
    # Allow updating only permitted fields (e.g. email).
    if update.email:
        current_user.email = update.email
    # Do NOT allow role change by the user.
    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)
    return current_user
