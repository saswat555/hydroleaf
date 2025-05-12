# app/routers/admin_subscriptions.py

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
import secrets

from app.models import (
    ActivationKey,
    DosingDeviceToken,
    ValveDeviceToken,
    SwitchDeviceToken,
    SubscriptionPlan,
    Device,
    User,
)
from app.schemas import DeviceType, ActivationKeyResponse
from app.dependencies import get_current_admin
from app.core.database import get_db

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post(
    "/generate_device_activation_key",
    response_model=ActivationKeyResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_admin)],
)
async def generate_device_activation_key(
    device_id: str,
    plan_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    # 1) Validate device exists
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    # 2) Validate plan exists & covers this type
    plan = await db.get(SubscriptionPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if device.type.value not in plan.device_types:
        raise HTTPException(
            status_code=400,
            detail=f"Plan does not support device type {device.type.value}",
        )

    # 3) Mint & store key
    key = secrets.token_urlsafe(32)
    ak = ActivationKey(
        key=key,
        device_type=device.type,
        plan_id=plan.id,
        created_by=admin.id,
        allowed_device_id=device.id,
    )
    db.add(ak)
    await db.commit()

    return {"activation_key": key}


@router.post(
    "/device/{device_id}/issue-dosing-token",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_admin)],
)
async def issue_dosing_token(
    device_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Generate or rotate a DosingDeviceToken for the given device_id.
    """
    existing = await db.get(DosingDeviceToken, device_id)
    token = secrets.token_urlsafe(32)

    if existing:
        existing.token = token
        existing.issued_at = func.now()
    else:
        db.add(DosingDeviceToken(device_id=device_id, token=token))

    await db.commit()
    return {"token": token}


@router.post(
    "/device/{device_id}/issue-valve-token",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_admin)],
)
async def issue_valve_token(
    device_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Generate or rotate a ValveDeviceToken for the given device_id.
    """
    existing = await db.get(ValveDeviceToken, device_id)
    token = secrets.token_urlsafe(32)

    if existing:
        existing.token = token
        existing.issued_at = func.now()
    else:
        db.add(ValveDeviceToken(device_id=device_id, token=token))

    await db.commit()
    return {"token": token}


@router.post(
    "/device/{device_id}/issue-switch-token",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_admin)],
)
async def issue_switch_token(
    device_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Generate or rotate a SwitchDeviceToken for the given device_id.
    """
    existing = await db.get(SwitchDeviceToken, device_id)
    token = secrets.token_urlsafe(32)

    if existing:
        existing.token = token
        existing.issued_at = func.now()
    else:
        db.add(SwitchDeviceToken(device_id=device_id, token=token))

    await db.commit()
    return {"token": token}
