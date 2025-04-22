from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
import secrets
from app.models import ActivationKey, SubscriptionPlan, Subscription
from app.schemas import DeviceType, SubscriptionPlanCreate, ActivationKeyResponse
from app.dependencies import get_current_admin
from app.core.database import get_db

router = APIRouter(prefix="/admin", tags=["admin"])

@router.post("/generate_activation_key", response_model=ActivationKeyResponse)
async def generate_activation_key(
    device_type: DeviceType,
    plan_id: int,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin)
):
    key = secrets.token_urlsafe(32)
    ak = ActivationKey(
        key=key,
        device_type=device_type,
        created_by=admin.id
    )
    plan = await db.get(SubscriptionPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Subscription plan not found")

    key = secrets.token_urlsafe(32)
    ak = ActivationKey(
        key=key,
        device_type=device_type,
        plan_id=plan_id,
        created_by=admin.id
     )
    db.add(ak)
    await db.commit()
    return {"activation_key": key}

@router.post("/subscription_plans", response_model=SubscriptionPlan)
async def create_plan(
    plan: SubscriptionPlanCreate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin)
):
    if not (28 <= plan.duration_days <= 730):
        raise HTTPException(400, "Duration must be between 28 and 730 days")
    sp = SubscriptionPlan(**plan.model_dump(), created_by=admin.id)
    db.add(sp)
    await db.commit()
    await db.refresh(sp)
    return sp
