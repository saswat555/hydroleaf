# app/routers/auth.py

import os
import datetime
from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from jose import jwt
from passlib.context import CryptContext

from app.core.database import get_db
from app.models import User
from app.schemas import AuthResponse, UserCreate, UserResponse

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
SECRET_KEY = os.getenv("SECRET_KEY", "your-default-secret")
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
    # 1) Fetch normal user
    user = await db.scalar(select(User).where(User.email == form_data.username))

    # 2) If not a normal user, try Admin
    if user is None:
        from app.models import Admin
        user = await db.scalar(select(Admin).where(Admin.email == form_data.username))

    # 3) Validate credentials
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    # 4) Create JWT
    expire = datetime.datetime.utcnow() + datetime.timedelta(
        hours=ACCESS_TOKEN_EXPIRE_HOURS
    )
    token_payload = {"user_id": user.id, "role": user.role, "exp": expire}
    token = jwt.encode(token_payload, SECRET_KEY, algorithm=ALGORITHM)

    return AuthResponse(
        access_token=token,
        token_type="bearer",
        user=UserResponse.from_orm(user),
    )


@router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def signup(
    user_create: UserCreate,
    db: AsyncSession = Depends(get_db),
):
    # Single transaction for user + profile creation
    async with db.begin():
        # 1) Ensure email not already taken
        existing = await db.scalar(select(User).where(User.email == user_create.email))
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered",
            )

        # 2) Create User
        hashed_pw = get_password_hash(user_create.password)
        user = User(
            email=user_create.email,
            hashed_password=hashed_pw,
            role="user",
        )
        db.add(user)
        await db.flush()  # populates user.id

        # 3) Create Profile
        from app.models import UserProfile

        profile = UserProfile(
            user_id=user.id,
            first_name=user_create.first_name,
            last_name=user_create.last_name,
            phone=user_create.phone,
            address=user_create.address,
            city=user_create.city,
            state=user_create.state,
            country=user_create.country,
            postal_code=user_create.postal_code,
        )
        db.add(profile)
        # All flush/commit will happen at exit of the `with` block

    # 4) Issue JWT
    expire = datetime.datetime.utcnow() + datetime.timedelta(
        hours=ACCESS_TOKEN_EXPIRE_HOURS
    )
    token_payload = {"user_id": user.id, "role": user.role, "exp": expire}
    token = jwt.encode(token_payload, SECRET_KEY, algorithm=ALGORITHM)

    return AuthResponse(
        access_token=token,
        token_type="bearer",
        user=UserResponse.from_orm(user),
    )
