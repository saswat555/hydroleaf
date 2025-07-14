# app/routers/cloud.py
"""
Cloud-key management & device authentication (refactored).

Key points
----------
• A *single* `device_tokens` table (see models.py) stores bearer-tokens for
  **all** IoT device types – dosing-unit, valve-controller, smart-switch, etc.
  Cameras keep their own `camera_tokens` table because they have no entry in
  `devices`.
• `/authenticate` validates the cloud-key, (upserts a token → device_tokens or
  camera_tokens), records usage, and returns the token.
• Admin helpers allow key generation and simple audit listings.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.dependencies import get_current_admin
from app.schemas import (
    CloudAuthenticationRequest,
    CloudAuthenticationResponse,
    DosingCancellationRequest,
)
from app.models import (
    CameraToken,
    CloudKey,
    CloudKeyUsage,
    Device,
    DeviceToken,
    User,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
async def _assert_valid_cloud_key(db: AsyncSession, key: str) -> CloudKey:
    """Return the CloudKey row or raise 401 if it doesn’t exist."""
    row = await db.scalar(select(CloudKey).where(CloudKey.key == key))
    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid cloud key"
        )
    return row


# ─────────────────────────────────────────────────────────────────────────────
# Public endpoints
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/authenticate", response_model=CloudAuthenticationResponse)
async def authenticate_cloud(
    payload: CloudAuthenticationRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Devices call this once after boot (or whenever Wi-Fi changes):

    1. Check that `cloud_key` is still valid.
    2. If the device is registered in `devices` → upsert into **device_tokens**.
       Otherwise treat it as a camera and upsert into **camera_tokens**.
    3. Record the usage in `cloud_key_usages`.
    4. Return the freshly-minted bearer token.
    """
    ck_row = await _assert_valid_cloud_key(db, payload.cloud_key)

    # ------------------------------------------------------------------ #
    # Decide whether it’s a registered IoT device or a stand-alone camera
    # ------------------------------------------------------------------ #
    dev: Device | None = await db.get(Device, payload.device_id)
    new_token = secrets.token_hex(16)  # 32-char hex

    if dev:
        # --- IoT device: upsert into device_tokens ---------------------
        rec = await db.get(DeviceToken, payload.device_id)
        if rec:
            rec.token = new_token
            rec.issued_at = func.now()
        else:
            db.add(
                DeviceToken(
                    device_id=payload.device_id,
                    token=new_token,
                    device_type=dev.type,
                )
            )
    else:
        # --- Camera: wipe any previous token then insert ---------------
        await db.execute(
            delete(CameraToken).where(CameraToken.camera_id == payload.device_id)
        )
        db.add(CameraToken(camera_id=payload.device_id, token=new_token))

    # ------------------------------------------------------------------ #
    # Book-keeping
    # ------------------------------------------------------------------ #
    db.add(CloudKeyUsage(cloud_key_id=ck_row.id, resource_id=payload.device_id))
    await db.commit()

    logger.info("Auth OK • device=%s • token=%s", payload.device_id, new_token)
    return CloudAuthenticationResponse(
        token=new_token, message="Authentication successful"
    )


@router.post("/verify_key")
async def verify_cloud_key(
    payload: CloudAuthenticationRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Lightweight endpoint for devices/portal to check “is this cloud-key valid?”
    (no token returned, just a yes/no).
    """
    await _assert_valid_cloud_key(db, payload.cloud_key)
    return {"status": "valid", "message": "Cloud key is valid"}


@router.post("/dosing_cancel")
async def dosing_cancel(request: DosingCancellationRequest):
    """
    Webhook target for a device reporting that it aborted a dosing cycle.
    """
    if request.event != "dosing_cancelled":
        raise HTTPException(status_code=400, detail="Invalid event type")
    logger.info("Dosing cancelled – device=%s", request.device_id)
    return {
        "message": "Dosing cancellation received",
        "device_id": request.device_id,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Admin endpoints
# ─────────────────────────────────────────────────────────────────────────────
@router.post(
    "/admin/generate_cloud_key",
    dependencies=[Depends(get_current_admin)],
)
async def generate_cloud_key(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """
    Mint a **new** cloud-key.  
    The latest one is considered “current” – keep/distribute only the newest
    in production; older keys remain valid until rotated manually.
    """
    new_key = secrets.token_hex(16)
    db.add(CloudKey(key=new_key, created_by=admin.id))
    await db.commit()
    logger.info("New cloud key generated: %s", new_key)
    return {"cloud_key": new_key}


@router.get("/admin/cloud-keys", dependencies=[Depends(get_current_admin)])
async def list_cloud_keys(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(CloudKey))).scalars().all()
    return [
        {
            "key": row.key,
            "created_by": row.created_by,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get("/admin/cloud-key-usages", dependencies=[Depends(get_current_admin)])
async def list_cloud_key_usages(db: AsyncSession = Depends(get_db)):
    """
    Show every {cloud_key → device/camera} access ever recorded.
    Sorted by most recent first.
    """
    rows = (
        await db.execute(select(CloudKeyUsage).order_by(CloudKeyUsage.used_at.desc()))
    ).scalars().all()
    return [
        {
            "cloud_key": usage.cloud_key.key,
            "resource_id": usage.resource_id,
            "used_at": usage.used_at,
        }
        for usage in rows
    ]
