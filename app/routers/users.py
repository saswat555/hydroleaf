# app/routers/users.py
from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.dependencies import get_current_user
from app.models import User, UserProfile, Admin
from app.schemas import UserResponse, UserUpdate

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.get("/me", response_model=UserResponse)
async def get_my_profile(current_user: User = Depends(get_current_user)):
    """
    Return the authenticated user's profile (including nested UserProfile).
    """
    return current_user


async def _email_taken(db: AsyncSession, email: str, exclude_user_id: int) -> bool:
    """
    Check if `email` is already used by another User or any Admin.
    """
    # Another user with this email?
    user_q = await db.execute(
        select(User).where(User.email == email, User.id != exclude_user_id)
    )
    if user_q.scalar_one_or_none():
        return True

    # Any admin with this email?
    admin_q = await db.execute(select(Admin).where(Admin.email == email))
    if admin_q.scalar_one_or_none():
        return True

    return False


@router.put("/me", response_model=UserResponse)
async def update_my_profile(
    update: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Allow a user to update their own email and profile fields.
    - Email change is validated for uniqueness across Users and Admins.
    - Role changes are NOT allowed.
    - If the user has no profile yet, create a blank one before applying updates.
    """
    # 1) Email update (if provided)
    if update.email:
        if await _email_taken(db, update.email, exclude_user_id=current_user.id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered",
            )
        current_user.email = update.email

    # 2) Ensure a profile row exists
    if current_user.profile is None:
        current_user.profile = UserProfile(user_id=current_user.id)

    # 3) Apply allowed profile fields (explicitly excluding email/role)
    profile_updates = update.model_dump(exclude_unset=True, exclude={"email", "role"})
    for field, value in profile_updates.items():
        setattr(current_user.profile, field, value)

    # 4) Persist
    db.add(current_user)
    await db.commit()
    # Refresh to return the latest state (profile is already attached)
    await db.refresh(current_user)

    return current_user
