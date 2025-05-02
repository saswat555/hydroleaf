# app/routers/subscriptions.py
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta

from app.models import ActivationKey, Subscription, SubscriptionPlan, Device
from app.schemas import SubscriptionPlanResponse, SubscriptionResponse  # you’ll need to define this
from app.dependencies import get_current_user
from app.core.database import get_db

router = APIRouter(prefix="/api/v1/subscriptions", tags=["subscriptions"])

@router.post("/redeem", response_model=SubscriptionResponse)
async def redeem_key(
    activation_key: str,
    device_id: int,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    # 1) fetch & validate key
    ak = await db.scalar(
        select(ActivationKey).where(
            ActivationKey.key == activation_key,
            ActivationKey.redeemed == False
        )
    )
    if not ak:
        raise HTTPException(400, "Invalid or already‐used activation key")

    # 2) fetch & validate device
    device = await db.get(Device, device_id)
    if ak.allowed_device_id and ak.allowed_device_id != device_id:
        raise HTTPException(400, "This key is not valid for that device")
    device = await db.get(Device, device_id)

    if not device or device.type != ak.device_type:
        raise HTTPException(400, "Key does not match this device type")

    # 3) mark the key redeemed
    ak.redeemed       = True
    ak.redeemed_at    = datetime.utcnow()  # ← now we record when
    ak.redeemed_user_id   = current_user.id
    ak.redeemed_device_id = device_id

    # 4) create a Subscription
    plan = await db.get(SubscriptionPlan, ak.plan_id)
    start = datetime.utcnow()
    end = start + timedelta(days=plan.duration_days)
    device.user_id = current_user.id
    device.is_active = True
    sub = Subscription(
        user_id=current_user.id,
        device_id=device_id,
        plan_id=plan.id,
        start_date=start,
        end_date=end,
        active=True
    )
    db.add_all([ak, sub, device])
    await db.commit()
    await db.refresh(sub)
    return sub

@router.get("/plans", response_model=List[SubscriptionPlanResponse])
async def list_plans(
    db: AsyncSession = Depends(get_db),
    _ = Depends(get_current_user)   # any logged-in user may browse plans
):
    result = await db.execute(select(SubscriptionPlan))
    return result.scalars().all()

@router.get("/", response_model=List[SubscriptionResponse])
async def list_my_subscriptions(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == current_user.id)
    )
    return result.scalars().all()