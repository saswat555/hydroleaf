# app/routers/cloud.py
"""
Cloud-key management & device authentication.

• Every generated key is stored in the `cloud_keys` table (latest row is current).
• Keys survive process restarts and are shared across all workers.
• `/authenticate` simply checks the supplied key and returns a random token.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timezone, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import (
    CloudAuthenticationRequest,
    CloudAuthenticationResponse,
    DosingCancellationRequest,
)
from app.dependencies import get_current_admin
from app.core.database import get_db
from sqlalchemy import Column, Integer, String, DateTime
from app.models import CloudKey
logger = logging.getLogger(__name__)
router = APIRouter()
from app.models import (
    Device,
    DosingDeviceToken,
    ValveDeviceToken,
    SwitchDeviceToken,
    CameraToken,
    CloudKey,
    CloudKeyUsage,
    User
)
# ─────────────────────────────────────────────────────────────────────────────
# 2.  Helper – fetch the newest key
# ─────────────────────────────────────────────────────────────────────────────
async def _is_valid_key(db: AsyncSession, key: str) -> bool:
    """
    Return **True** if the key exists in the `cloud_keys` table.
    Uses COUNT(*) so the result is a plain boolean‑friendly integer.
    """
    count = await db.scalar(
        select(func.count()).select_from(CloudKey).where(CloudKey.key == key)
    )
    return bool(count)

# ─────────────────────────────────────────────────────────────────────────────
# 3.  Public endpoints
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/authenticate", response_model=CloudAuthenticationResponse)
async def authenticate_cloud(
    payload: CloudAuthenticationRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Device → cloud authentication (server-side only).

    1. Validate that *cloud_key* exists in `cloud_keys`.
    2. Detect what kind of device is logging in (camera, dosing-unit, valve-controller, smart-switch).
    3. **Upsert** a token in the corresponding *_device_tokens table.*
       – cameras ➜ `camera_tokens`  
       – dosing units ➜ `dosing_device_tokens`  
       – valve controllers ➜ `valve_device_tokens`  
       – smart switches ➜ `switch_device_tokens`
    4. Record the usage in `cloud_key_usages`.
    5. Return the bearer *token* to the caller.

    No schemas are altered; we only write rows into tables that already exist.
    """
    # 1) validate the cloud key ------------------------------------------------
    ck_row = await db.scalar(select(CloudKey).where(CloudKey.key == payload.cloud_key))
    if not ck_row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid cloud key")

    # 2) look up the device (may be NULL if it’s a new camera) -----------------
    dev = await db.get(Device, payload.device_id)
    token = secrets.token_hex(16)        # 32-char hex bearer token

    # 3) insert / update the right token table ---------------------------------
    if dev and dev.type:                       # ─── registered non-camera ───
        tbl_map = {
            "dosing_unit":     DosingDeviceToken,
            "valve_controller":ValveDeviceToken,
            "smart_switch":    SwitchDeviceToken,
        }
        tbl = tbl_map.get(dev.type.value)
        if tbl:                                # dosing / valve / switch
            existing = await db.get(tbl, payload.device_id)
            if existing:
                existing.token     = token
                existing.issued_at = func.now()
            else:
                db.add(tbl(device_id=payload.device_id, token=token))
    else:                                      # ─── camera or unregistered ──
        # wipe any previous token for this camera_id, then add a fresh one
        await db.execute(delete(CameraToken).where(CameraToken.camera_id == payload.device_id))
        db.add(CameraToken(camera_id=payload.device_id, token=token))

    # 4) bookkeeping -----------------------------------------------------------
    db.add(CloudKeyUsage(cloud_key_id=ck_row.id, resource_id=payload.device_id))
    await db.commit()

    logger.info("Auth OK • device=%s • token=%s", payload.device_id, token)
    return CloudAuthenticationResponse(token=token, message="Authentication successful")


@router.post("/verify_key")
async def verify_cloud_key(
    payload: CloudAuthenticationRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Quick check used by devices/portal: “is this key still valid?”.
    """
    if await _is_valid_key(db, payload.cloud_key):
        return {"status": "valid", "message": "Cloud key is valid"}
    raise HTTPException(status_code=401, detail="Invalid cloud key")


@router.post("/dosing_cancel")
async def dosing_cancel(request: DosingCancellationRequest):
    """
    Webhook target for a device reporting a cancelled dosing event.
    """
    if request.event != "dosing_cancelled":
        raise HTTPException(400, "Invalid event type")
    logger.info("Dosing cancelled – device=%s", request.device_id)
    return {"message": "Dosing cancellation received", "device_id": request.device_id}


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Admin endpoints
# ─────────────────────────────────────────────────────────────────────────────
@router.post(
    "/admin/generate_cloud_key",
    dependencies=[Depends(get_current_admin)],
)
@router.post("/admin/generate_cloud_key", dependencies=[Depends(get_current_admin)])
async def generate_cloud_key(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),   # ← get the actual admin user
    ):
    new_key = secrets.token_hex(16)
    cloud_key = CloudKey(key=new_key, created_by=admin.id)
    db.add(cloud_key)
    await db.commit()
    logger.info("New cloud key generated: %s", new_key)
    return {"cloud_key": new_key}

@router.get("/admin/cloud-keys", dependencies=[Depends(get_current_admin)])
async def list_cloud_keys(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(CloudKey))
    return [
        {"key": ck.key, "created_by": ck.created_by, "created_at": ck.created_at}
        for ck in result.scalars().all()
    ]
    
@router.get("/admin/cloud-key-usages", dependencies=[Depends(get_current_admin)])
async def list_cloud_key_usages(db: AsyncSession = Depends(get_db)):
    """
    Admin‐only: show every cloud_key → device/camera mapping ever recorded.
    """
    rows = await db.execute(select(CloudKeyUsage).order_by(CloudKeyUsage.used_at.desc()))
    out = []
    for u in rows.scalars().all():
        out.append({
            "cloud_key":       u.cloud_key.key,
            "resource_id":     u.resource_id,
            "used_at":         u.used_at,
        })
    return out
