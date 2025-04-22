# app/dependencies.py
from datetime import datetime, timezone
import os
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.future import select
from app.models import ActivationKey, Subscription, SubscriptionPlan, User
from app.core.database import get_db
bearer_scheme = HTTPBearer() 
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

async def get_current_user(token: str = Depends(oauth2_scheme), db=Depends(get_db)):
    SECRET_KEY = os.getenv("SECRET_KEY", "your-default-secret")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        user_id = payload.get("user_id")
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials"
            )
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found"
            )
        return user
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials"
        )


async def get_current_admin(user: User = Depends(get_current_user)):
    """
    Dependency that verifies the current user is an admin.
    """
    if user.role != "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required"
        )
    return user


async def get_current_device(
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db=Depends(get_db),
):
    key = creds.credentials
    ak = await db.scalar(select(ActivationKey).where(ActivationKey.key == key))
    if not ak or not ak.redeemed:
        raise HTTPException(status_code=401, detail="Invalid or un‑redeemed device key")
    device = await db.get(Device, ak.redeemed_device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    # check subscription
    now = datetime.now(timezone.utc)
    sub = await db.scalar(
      select(Subscription)
      .where(
        Subscription.device_id == device.id,
        Subscription.active == True,
        Subscription.start_date <= now,
        Subscription.end_date >= now
      )
    )
    if not sub:
        raise HTTPException(status_code=403, detail="No active subscription")
    plan = await db.get(SubscriptionPlan, sub.plan_id)
    if device.type.value not in plan.device_types:
        raise HTTPException(status_code=403, detail="Plan does not cover this device type")
    return device