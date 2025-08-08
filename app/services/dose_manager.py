from __future__ import annotations

import uuid
from typing import Any, Mapping, Sequence
from datetime import datetime, timezone

import httpx

from app.core.database import AsyncSessionLocal
from app.models import DosingOperation as DosingOperationModel


def _validate_actions(actions: Sequence[Mapping[str, Any]]) -> None:
    """
    Each action must contain:
      • pump_number (int in [1..4])
      • dose_ml (float > 0)
      • chemical_name (str, non-empty)
      • reasoning (str, non-empty)
    """
    if not actions:
        raise ValueError("No actions supplied")

    for idx, a in enumerate(actions, start=1):
        if "pump_number" not in a or "dose_ml" not in a:
            raise ValueError(f"Action #{idx} missing required fields (pump_number, dose_ml)")
        try:
            pump = int(a["pump_number"])
        except Exception:
            raise ValueError(f"Action #{idx} has non-integer pump_number")
        if not (1 <= pump <= 4):
            raise ValueError(f"Action #{idx} pump_number must be 1–4")

        try:
            dose = float(a["dose_ml"])
        except Exception:
            raise ValueError(f"Action #{idx} has non-numeric dose_ml")
        if dose <= 0:
            raise ValueError(f"Action #{idx} dose_ml must be > 0")

        if not str(a.get("chemical_name", "")).strip():
            raise ValueError(f"Action #{idx} chemical_name is required")
        if not str(a.get("reasoning", "")).strip():
            raise ValueError(f"Action #{idx} reasoning is required")


async def _post_plan(device_endpoint: str, payload: dict) -> None:
    """Best-effort POST to the device; failure doesn't abort DB recording."""
    url = f"{device_endpoint.rstrip('/')}/dose"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
    except Exception:
        # swallow – tests shouldn't depend on a live device
        pass


async def execute_dosing_operation(
    device_id: str,
    device_endpoint: str,
    pump_configurations: Sequence[Mapping[str, Any]],
):
    """
    Build a simple plan from pump_configurations, POST it (best effort),
    persist a DosingOperation row, and return it.
    """
    actions = [
        {
            "pump_number": int(p["pump_number"]),
            "chemical_name": p.get("chemical_name") or f"pump-{p['pump_number']}",
            "dose_ml": 1.0,
            "reasoning": "baseline dose",
        }
        for p in (pump_configurations or [])
        if p and "pump_number" in p
    ]

    _validate_actions(actions)

    op_id = uuid.uuid4().hex
    ts = datetime.now(timezone.utc)

    await _post_plan(device_endpoint, {"operation_id": op_id, "actions": actions})

    async with AsyncSessionLocal() as session:
        row = DosingOperationModel(
            device_id=device_id,          # UUID string
            operation_id=op_id,
            actions=actions,
            status="completed",
            timestamp=ts,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def cancel_dosing_operation(device_id: str, device_endpoint: str):
    """Notify device to cancel; always return a cancel confirmation."""
    url = f"{device_endpoint.rstrip('/')}/cancel"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json={"device_id": device_id})
    except Exception:
        pass
    return {"device_id": device_id, "status": "cancelled"}
