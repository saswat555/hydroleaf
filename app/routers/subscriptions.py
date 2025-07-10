# app/routers/subscriptions.py

from typing import List
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.dependencies import get_current_user
from app.models import (
    ActivationKey,
    Subscription,
    SubscriptionPlan,
    Device,
    DosingDeviceToken,
    ValveDeviceToken,
    SwitchDeviceToken,
)
from app.schemas import SubscriptionPlanResponse, SubscriptionResponse

router = APIRouter(prefix="/api/v1/subscriptions", tags=["subscriptions"])


@router.post(
    "/redeem",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def redeem_key(
    activation_key: str,
    device_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    # 1) Fetch & validate the activation key
    ak = await db.scalar(
        select(ActivationKey)
        .where(
            ActivationKey.key == activation_key,
            ActivationKey.redeemed == False,
        )
    )
    if not ak:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or already-used activation key",
        )

    # 2) Fetch & validate the device
    device = await db.get(Device, device_id)
    if not device or device.type != ak.device_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Key does not match this device",
        )
    if ak.allowed_device_id and ak.allowed_device_id != device_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This key is not valid for that device",
        )

    # 3) Mark key redeemed
    ak.redeemed           = True
    ak.redeemed_at        = datetime.utcnow()
    ak.redeemed_user_id   = current_user.id
    ak.redeemed_device_id = device_id

    # 4) Create the subscription
    plan  = await db.get(SubscriptionPlan, ak.plan_id)
    start = datetime.utcnow()
    end   = start + timedelta(days=plan.duration_days)

    # Attach device to user
    device.user_id   = current_user.id
    device.is_active = True

    sub = Subscription(
        user_id    = current_user.id,
        device_id  = device_id,
        plan_id    = plan.id,
        start_date = start,
        end_date   = end,
        active     = True,
    )

    # 5) Issue the appropriate device token
    token = secrets.token_urlsafe(32)
    if device.type.value == "dosing_unit":
        db.add(DosingDeviceToken(device_id=device_id, token=token))
    elif device.type.value == "valve_controller":
        db.add(ValveDeviceToken(device_id=device_id, token=token))
    elif device.type.value == "smart_switch":
        db.add(SwitchDeviceToken(device_id=device_id, token=token))

    # 6) Persist everything
    db.add_all([ak, device, sub])
    await db.commit()
    await db.refresh(sub)

    return sub


@router.get(
    "/plans",
    response_model=List[SubscriptionPlanResponse],
    summary="List all subscription plans",
)
async def list_plans(
    db: AsyncSession = Depends(get_db),
    _ = Depends(get_current_user),
):
    result = await db.execute(select(SubscriptionPlan))
    return result.scalars().all()


@router.get(
    "/",
    response_model=List[SubscriptionResponse],
    summary="List my subscriptions",
)
async def list_my_subscriptions(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == current_user.id)
    )
    return result.scalars().all()
