# app/routers/auth.py
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import OAuth2PasswordRequestForm
from app.models import User
from app.core.database import get_db
from sqlalchemy.future import select
import jwt  # This is from PyJWT
import os
import datetime
from passlib.context import CryptContext
from app.schemas import UserCreate, UserResponse 

router = APIRouter()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
SECRET_KEY = os.getenv("SECRET_KEY", "your-default-secret")
ALGORITHM = "HS256"

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)
@router.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db=Depends(get_db)):
    # 1) Try the normal User table
    user = await db.scalar(select(User).where(User.email == form_data.username))
    # 2) If not found, fall back to Admins
    if user is None:
        from app.models import Admin
        user = await db.scalar(select(Admin).where(Admin.email == form_data.username))

    # 3) Verify credentials
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Invalid credentials")

    # 4) Issue JWT
    token_data = {
        "user_id": user.id,
        "role":    user.role,
        "exp":     datetime.datetime.utcnow() + datetime.timedelta(hours=1)
    }
    token = jwt.encode(token_data, SECRET_KEY, algorithm=ALGORITHM)
    return {"access_token": token, "token_type": "bearer"}


@router.post("/signup", response_model=UserResponse)
async def signup(user_create: UserCreate, db=Depends(get_db)):
    # Check if email exists
    result = await db.execute(select(User).where(User.email == user_create.email))
    if result.unique().scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")
    
    hashed_pw = get_password_hash(user_create.password)
    user = User(email=user_create.email, hashed_password=hashed_pw, role="user")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    token_data = {
      "user_id": user.id,
      "role":    user.role,
      "exp":     datetime.datetime.utcnow() + datetime.timedelta(hours=1)
    }
    token = jwt.encode(token_data, SECRET_KEY, algorithm=ALGORITHM)
    from app.models import UserProfile
    profile = UserProfile(
        user_id    = user.id,
        first_name = user_create.first_name,
        last_name  = user_create.last_name,
        phone      = user_create.phone,
        address    = user_create.address,
        city       = user_create.city,
        state      = user_create.state,
        country    = user_create.country,
        postal_code= user_create.postal_code
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    await db.refresh(user)
    return {
      "access_token": token,
      "token_type":   "bearer",
      "user":         UserResponse.from_orm(user),
    }

