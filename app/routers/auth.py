# app/routers/auth.py

import os
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, noload
from passlib.context import CryptContext
import jwt
from app.core.database import get_db
from app.core.config import SECRET_KEY
from app.models import User, Admin, UserProfile
from app.schemas import AuthResponse, UserCreate, UserResponse

router = APIRouter(tags=["Auth"])

# password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT settings
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = int(os.getenv("ACCESS_TOKEN_EXPIRE_HOURS", "1"))

# (Optional safety)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


async def _get_account_by_email(
    email: str, db: AsyncSession
) -> User | Admin | None:
    """
    Try to load a User; if none, try to load an Admin.
    Explicitly disable loading of farms & farm_shares,
    but eagerly load profile if present.
    """
    stmt_user = (
        select(User)
        .options(
            noload(User.farms),
            noload(User.shared_farms),    # ← use your actual relationship name
            selectinload(User.profile),
        )
        .where(User.email == email)
    )
    res = await db.execute(stmt_user)
    user = res.scalars().first()
    if user:
        return user

    stmt_admin = (
        select(Admin)
        .options(noload(Admin.cloud_keys))  # if Admin has any heavy relationships
        .where(Admin.email == email)
    )
    res2 = await db.execute(stmt_admin)
    return res2.scalars().first()


def _create_access_token(user_id: int, role: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    to_encode = {"user_id": user_id, "role": role, "exp": expire}
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


@router.post("/login", response_model=AuthResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    account = await _get_account_by_email(form_data.username, db)

    if not account or not verify_password(form_data.password, account.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Issue JWT
    token = _create_access_token(account.id, account.role)

    # Build response user object
    # UserResponse.from_orm will pull just the fields your schema defines
    return AuthResponse(
        access_token=token,
        token_type="bearer",
        user=UserResponse.from_orm(account),
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
    # 1) Prevent duplicate in users or admins
    existing = await _get_account_by_email(user_create.email, db)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    # 2) Create User + nested profile
    hashed_pw = get_password_hash(user_create.password)
    user = User(email=user_create.email, hashed_password=hashed_pw, role="user")

    # pick nested profile fields if present, else top-level
    raw = {}
    if user_create.profile:
        raw = user_create.profile.dict(exclude_unset=True)

    profile = UserProfile(
        first_name=raw.get("first_name", user_create.first_name),
        last_name=raw.get("last_name", user_create.last_name),
        phone=raw.get("phone", user_create.phone),
        address=raw.get("address", user_create.address),
        city=raw.get("city", user_create.city),
        state=raw.get("state", user_create.state),
        country=raw.get("country", user_create.country),
        postal_code=raw.get("postal_code", user_create.postal_code),
    )
    user.profile = profile

    # 3) Persist
    db.add(user)
    await db.commit()

    # 3.1) re-fetch *only* the new user + its profile (no farms)
    stmt = (
        select(User)
        .options(
            noload(User.farms),
            noload(User.shared_farms),
            selectinload(User.profile),
        )
        .where(User.id == user.id)
    )
    res = await db.execute(stmt)
    user = res.scalars().first()

    # 4) Issue JWT
    token = _create_access_token(user.id, user.role)

    return AuthResponse(
        access_token=token,
        token_type="bearer",
        user=UserResponse.from_orm(user),
    )
