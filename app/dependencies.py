# app/dependencies.py
from datetime import datetime, timezone
import os
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from app.models import ActivationKey, CameraToken, Device, DosingDeviceToken, Subscription, SubscriptionPlan, SwitchDeviceToken, User, Admin, ValveDeviceToken
from app.core.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession

bearer_scheme = HTTPBearer() 
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")
ALGORITHM = "HS256"
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
        user = result.unique().scalar_one_or_none()
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

async def get_current_admin(
    token: str = Depends(oauth2_scheme),
    db=Depends(get_db),
):
    """
    Dependency that verifies the bearer token belongs to an Admin.
    """
    SECRET_KEY = os.getenv("SECRET_KEY", "your-default-secret")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        admin_id = payload.get("user_id")
        if admin_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication credentials")

        result = await db.execute(select(Admin).where(Admin.id == admin_id))
        admin = result.unique().scalar_one_or_none()
        if not admin or admin.role != "superadmin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")

        return admin
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")


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

async def verify_camera_token(
    camera_id: str,
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> str:
    """
    Ensure the bearer‐token in Authorization: Bearer <token>
    matches exactly the one stored for this camera_id.
    """
    token_row = await db.get(CameraToken, camera_id)
    if not token_row or token_row.token != creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or mismatched camera token"
        )
    return camera_id

async def verify_dosing_device_token(
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db=Depends(get_db),
) -> str:
    """
    Validates a dosing-unit’s bearer token and returns its device_id.
    """
    token = creds.credentials
    tok = await db.scalar(
        select(DosingDeviceToken).where(DosingDeviceToken.token == token)
    )
    if not tok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid dosing device token")
    return tok.device_id

async def verify_valve_device_token(
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db=Depends(get_db),
) -> str:
    """
    Validates a valve-controller’s bearer token and returns its device_id.
    """
    token = creds.credentials
    tok = await db.scalar(
        select(ValveDeviceToken).where(ValveDeviceToken.token == token)
    )
    if not tok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid valve device token")
    return tok.device_id


async def verify_switch_device_token(
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db=Depends(get_db),
) -> str:
    """
    Validates a smart-switch’s bearer token and returns its device_id.
    """
    token = creds.credentials
    tok = await db.scalar(
        select(SwitchDeviceToken).where(SwitchDeviceToken.token == token)
    )
    if not tok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid switch device token")
    return tok.device_id