# app/routers/admin_subscription_plans.py
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.dependencies import get_current_admin
from app.core.database import get_db
from app.models import SubscriptionPlan, User
from app.schemas import SubscriptionPlanCreate, SubscriptionPlanResponse

router = APIRouter(prefix="/admin/plans", tags=["admin-plans"], dependencies=[Depends(get_current_admin)])

@router.post("/", response_model=SubscriptionPlanResponse, status_code=status.HTTP_201_CREATED)
async def create_plan(payload: SubscriptionPlanCreate, db: AsyncSession = Depends(get_db), admin: User = Depends(get_current_admin)):
    plan = SubscriptionPlan(**payload.model_dump(), created_by=admin.id)
    db.add(plan); await db.commit(); await db.refresh(plan)
    return plan

@router.get("/", response_model=list[SubscriptionPlanResponse])
async def list_plans(db: AsyncSession = Depends(get_db)):
    return (await db.execute(select(SubscriptionPlan))).scalars().all()

@router.delete("/{plan_id}")
async def delete_plan(plan_id: int, db: AsyncSession = Depends(get_db)):
    plan = await db.get(SubscriptionPlan, plan_id)
    if not plan: raise HTTPException(404, "Plan not found")
    await db.delete(plan); await db.commit()
    return {"detail": "deleted"}
