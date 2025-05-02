from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
import secrets

from app.models import ActivationKey, SubscriptionPlan, Device, User
from app.schemas import DeviceType, ActivationKeyResponse
from app.dependencies import get_current_admin
from app.core.database import get_db

router = APIRouter(prefix="/admin", tags=["admin"])

@router.post(
    "/generate_device_activation_key",
    response_model=ActivationKeyResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_admin)]
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
            detail=f"Plan does not support device type {device.type.value}"
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
