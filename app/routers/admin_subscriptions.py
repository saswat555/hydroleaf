# app/routers/admin_subscriptions.py

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func
import secrets

from app.core.database import get_db
from app.dependencies import get_current_admin
from app.models import ActivationKey, DeviceToken, SubscriptionPlan, Device
from app.schemas import ActivationKeyResponse

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(get_current_admin)],
)


@router.post(
    "/generate_device_activation_key",
    response_model=ActivationKeyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a new activation key for a device",
)
async def generate_device_activation_key(
    device_id: str,
    plan_id: int,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin),
):
    # 1) Validate device exists
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    # 2) Validate plan exists & covers this device type
    plan = await db.get(SubscriptionPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    if device.type.value not in plan.device_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
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
    # Flush so that tests in the same transaction can see the key
    await db.flush()

    return ActivationKeyResponse(activation_key=key)


@router.post(
    "/device/{device_id}/issue-token",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
    summary="Generate or rotate a device token",
)
async def issue_device_token(
    device_id: str,
    db: AsyncSession = Depends(get_db),
):
    # 1) Verify device exists
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    # 2) Create or update the DeviceToken
    token = secrets.token_urlsafe(32)
    record = await db.get(DeviceToken, device_id)
    if record:
        record.token = token
        record.issued_at = func.now()
    else:
        record = DeviceToken(
            device_id=device_id,
            token=token,
            device_type=device.type,
        )
        db.add(record)

    # 3) Flush so changes are immediately visible in this transaction
    await db.flush()

    return {"device_id": device_id, "token": token}
