# app/routers/auth.py

import os
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from jose import jwt
from passlib.context import CryptContext

from app.core.database import get_db
from app.core.config import SECRET_KEY
from app.models import User, Admin, UserProfile
from app.schemas import AuthResponse, UserCreate, UserResponse

router = APIRouter(tags=["auth"])

# password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT settings
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = int(os.getenv("ACCESS_TOKEN_EXPIRE_HOURS", "1"))


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


@router.post("/login", response_model=AuthResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    # 1) Try to find a regular user
    result = await db.execute(select(User).where(User.email == form_data.username))
    user = result.scalars().first()

    # 2) If not found, try an admin account
    if not user:
        result = await db.execute(select(Admin).where(Admin.email == form_data.username))
        user = result.scalars().first()

    # 3) Verify credentials
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 4) Load related profile (if any)
    await db.refresh(user)

    # 5) Create JWT
    expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {"user_id": user.id, "role": user.role, "exp": expire}
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

    return AuthResponse(
        access_token=token,
        token_type="bearer",
        user=UserResponse.from_orm(user),
    )


@router.post(
    "/signup",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
)
async def signup(
    user_create: UserCreate,
    db: AsyncSession = Depends(get_db),
):
    # 1) Prevent duplicate emails
    result = await db.execute(select(User).where(User.email == user_create.email))
    if result.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    # 2) Build the User + profile
    hashed_pw = get_password_hash(user_create.password)
    user = User(email=user_create.email, hashed_password=hashed_pw, role="user")
    profile = UserProfile(
        first_name=user_create.first_name,
        last_name=user_create.last_name,
        phone=user_create.phone,
        address=user_create.address,
        city=user_create.city,
        state=user_create.state,
        country=user_create.country,
        postal_code=user_create.postal_code,
    )
    user.profile = profile

    # 3) Persist
    db.add(user)
    await db.commit()
    await db.refresh(user)

    # 4) Issue JWT
    expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {"user_id": user.id, "role": user.role, "exp": expire}
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

    return AuthResponse(
        access_token=token,
        token_type="bearer",
        user=UserResponse.from_orm(user),
    )
