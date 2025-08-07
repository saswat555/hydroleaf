# app/dependencies.py

import os
from datetime import datetime, timezone
from typing import Optional, Any
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from jwt import InvalidTokenError
from app.core.config import SECRET_KEY as CONFIG_SECRET
from app.core.database import get_db
from app.models import (
    User,
    Admin,
    Device,
    ActivationKey,
    CameraToken,
    Subscription,
    SubscriptionPlan,
    DeviceToken,
)
# --- auth schemes & settings ----------------------------------------
from app.schemas import DeviceType
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")
bearer_scheme = HTTPBearer()

ALGORITHM = os.getenv("ALGORITHM", "HS256")
SECRET_KEY = os.getenv("SECRET_KEY", CONFIG_SECRET)


# --- user & admin JWT -----------------------------------------------

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("user_id")
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication credentials")
    except InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def get_current_admin(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> Admin:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        admin_id: str = payload.get("user_id")
        if not admin_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication credentials")
    except InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")

    result = await db.execute(select(Admin).where(Admin.id == admin_id))
    admin = result.scalar_one_or_none()
    if not admin or getattr(admin, "role", None) != "superadmin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return admin


# --- device activation-key & subscription check ---------------------

async def get_current_device(
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Device:
    # 1) look up the activation key
    result = await db.execute(select(ActivationKey).where(ActivationKey.key == creds.credentials))
    ak: ActivationKey = result.scalar_one_or_none()
    if not ak or not ak.redeemed:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or un-redeemed device key")

    # 2) load the device
    device = await db.get(Device, ak.redeemed_device_id)
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    # 3) ensure there’s a currently-active subscription
    now = datetime.now(timezone.utc)
    sub_q = (
        select(Subscription)
        .where(
            Subscription.device_id == device.id,
            Subscription.active.is_(True),
            Subscription.start_date <= now,
            Subscription.end_date >= now,
        )
    )
    sub = (await db.execute(sub_q)).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No active subscription")

    plan = await db.get(SubscriptionPlan, sub.plan_id)
    if device.type.value not in plan.device_types:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Plan does not cover this device type")

    return device


# --- camera token ---------------------------------------------------

async def verify_camera_token(
    camera_id: str,
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> str:
    record: CameraToken = await db.get(CameraToken, camera_id)
    if not record or record.token != creds.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or mismatched camera token")
    return camera_id


# --- device token ---------------------------------------------------

async def verify_device_token(
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
    *,
    expected_type: Optional[DeviceType] = None,
) -> str:
    # 1) find the token row (no JOIN)
    result = await db.execute(
        select(DeviceToken).where(DeviceToken.token == creds.credentials)
    )
    tok_row: DeviceToken = result.scalar_one_or_none()

    if not tok_row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid device token")

    # 2) optional type-check
    if expected_type and tok_row.device_type != expected_type:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token/device type mismatch")

    # 3) optional expiration-check
    if getattr(tok_row, "expires_at", None) and tok_row.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device token expired")

    # 4) device must exist and be active
    device = await db.get(Device, tok_row.device_id)
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    if getattr(device, "is_active", True) is False:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Device is inactive")

    # 5) success!
    return device.id
