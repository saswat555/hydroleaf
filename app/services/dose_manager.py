# app/services/dose_manager.py

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import httpx
from fastapi import HTTPException
from app.services.device_controller import DeviceController 
from app.core.database import AsyncSessionLocal
from app.models import DosingOperation as DosingOperationModel


def _validate_actions(actions: Sequence[Mapping[str, Any]]) -> None:
    """
    Validate dosing action payloads. Each action must contain:
      • pump_number (int in [1..4])
      • dose_ml (float > 0)
    """
    if not actions:
        raise ValueError("No actions supplied")

    for idx, a in enumerate(actions, start=1):
        if "pump_number" not in a or "dose_ml" not in a:
            raise ValueError(
                f"Action #{idx} missing required fields (pump_number, dose_ml)"
            )
        try:
            pump = int(a["pump_number"])
        except Exception:
            raise ValueError(f"Action #{idx} has non-integer pump_number")
        try:
            dose = float(a["dose_ml"])
        except Exception:
            raise ValueError(f"Action #{idx} has non-numeric dose_ml")
        if not (1 <= pump <= 4):
            raise ValueError(f"Action #{idx} pump_number must be 1–4")
        if dose <= 0:
            raise ValueError(f"Action #{idx} dose_ml must be > 0")


def _default_actions_from_config(pump_configurations: Sequence[Mapping[str, Any]]):
    """
    Build a minimal dosing plan from stored pump configurations.
    Defaults to a small 5ml dose for each configured pump.
    """
    actions = []
    for i, cfg in enumerate(pump_configurations or [], start=1):
        pump_number = int(cfg.get("pump_number", i))
        chem_name = (
            cfg.get("chemical_name")
            or cfg.get("name")
            or f"pump_{pump_number}"
        )
        actions.append(
            {
                "pump_number": pump_number,
                "chemical_name": chem_name,
                "dose_ml": 5.0,
                "reasoning": "Automatic scheduled dose",
            }
        )
    return actions


async def execute_dosing_operation(
    device_id: str,
    device_endpoint: str,
    pump_configurations: Sequence[Mapping[str, Any]] | None,
):
    """
    Create & record a dosing operation for a device.
    Tries to notify the device's local endpoint, but succeeds even if the
    device is offline (operation is still recorded).
    """
    actions = _default_actions_from_config(pump_configurations or [])
    _validate_actions(actions)

    op_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)

    # Try to notify device (best-effort)
    try:
        url = f"{device_endpoint.rstrip('/')}/dose"
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json={"operation_id": op_id, "actions": actions})
        status = "sent"
    except Exception:
        status = "queued"

    # Persist operation
    async with AsyncSessionLocal() as session:
        op = DosingOperationModel(
            device_id=device_id,
            operation_id=op_id,
            actions=actions,
            status=status,
            timestamp=now,
        )
        session.add(op)
        await session.commit()
        await session.refresh(op)
        return op


async def cancel_dosing_operation(device_id: str, device_endpoint: str):
    """
    Ask the device to cancel an active dosing operation.
    Returns a simple acknowledgement even if the device is offline.
    """
    try:
        url = f"{device_endpoint.rstrip('/')}/cancel"
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(url, json={"device_id": device_id})
            r.raise_for_status()
            try:
                return r.json()
            except Exception:
                pass
    except Exception:
        pass

    return {"message": "Dosing cancellation requested", "device_id": device_id}


# -----------------------------------------------------------------------------
# Back-compat shim for modules importing a class API (e.g., app.services.llm)
# -----------------------------------------------------------------------------
class DoseManager:
    """Stateless wrapper so legacy code can `from ... import DoseManager`."""

    # Some code may instantiate; allow both patterns
    def __init__(self) -> None:
        pass

    # Common method name (short)
    async def execute(
        self,
        device_id: str,
        device_endpoint: str,
        pump_configurations: Sequence[Mapping[str, Any]] | None,
    ):
        return await execute_dosing_operation(device_id, device_endpoint, pump_configurations)

    # Verbose method name (used by some callers)
    async def execute_dosing_operation(
        self,
        device_id: str,
        device_endpoint: str,
        pump_configurations: Sequence[Mapping[str, Any]] | None,
    ):
        return await execute_dosing_operation(device_id, device_endpoint, pump_configurations)

    async def cancel(self, device_id: str, device_endpoint: str):
        return await cancel_dosing_operation(device_id, device_endpoint)

    async def cancel_dosing_operation(self, device_id: str, device_endpoint: str):
        return await cancel_dosing_operation(device_id, device_endpoint)


__all__ = ["execute_dosing", "cancel_dosing", "DeviceController"]