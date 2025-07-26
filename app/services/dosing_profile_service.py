# app/services/dosing_profile_service.py

import logging
from typing import Any, Dict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from app.models import Device, DosingProfile
from app.services.ph_tds import get_ph_tds_readings
from app.services.llm import call_llm_async, build_dosing_prompt
from app.schemas import DosingProfileResponse

logger = logging.getLogger(__name__)

async def set_dosing_profile_service(
    profile_data: Dict[str, Any],
    db: AsyncSession
) -> Dict[str, Any]:
    """
    Create a dosing profile for an existing dosing device by:
      1. Fetching real-time pH/TDS readings from the device
      2. Building & sending an LLM prompt to generate dosing actions
      3. Saving the new profile and returning it with the recommended actions
    """
    # 1) Validate input
    device_id = profile_data.get("device_id")
    if not device_id:
        raise HTTPException(status_code=400, detail="`device_id` is required")

    # 2) Load the device
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalars().first()
    if not device:
        raise HTTPException(status_code=404, detail=f"Device `{device_id}` not found")

    # 3) Retrieve averaged pH/TDS readings
    try:
        readings = await get_ph_tds_readings(device.http_endpoint)
    except Exception as exc:
        logger.error("Failed to fetch PH/TDS readings: %s", exc)
        raise HTTPException(status_code=502, detail="Error fetching pH/TDS readings") from exc

    ph = readings.get("ph")
    tds = readings.get("tds")
    if ph is None or tds is None:
        raise HTTPException(status_code=502, detail="Incomplete pH/TDS readings from device")

    # 4) Build & send LLM prompt
    try:
        prompt = await build_dosing_prompt(device, {"ph": ph, "tds": tds}, profile_data)
        parsed, raw = await call_llm_async(prompt)
        logger.info("LLM raw response: %s", raw)
        if not isinstance(parsed, dict):
            raise ValueError("LLM response not a JSON object")
        actions = parsed.get("actions")
        if not isinstance(actions, list):
            raise ValueError("`actions` key missing or not a list")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("LLM dosing plan generation failed")
        raise HTTPException(status_code=502, detail="Error generating dosing plan") from exc

    # 5) Persist the new DosingProfile
    try:
        new_profile = DosingProfile(
            device_id       = device.id,
            plant_name      = profile_data["plant_name"],
            plant_type      = profile_data["plant_type"],
            growth_stage    = profile_data["growth_stage"],
            seeding_date    = profile_data["seeding_date"],
            target_ph_min   = profile_data["target_ph_min"],
            target_ph_max   = profile_data["target_ph_max"],
            target_tds_min  = profile_data["target_tds_min"],
            target_tds_max  = profile_data["target_tds_max"],
            dosing_schedule = profile_data["dosing_schedule"],
        )
        db.add(new_profile)
        await db.commit()
        await db.refresh(new_profile)
    except KeyError as ke:
        raise HTTPException(status_code=400, detail=f"Missing profile field: {ke}") from ke
    except Exception as exc:
        logger.exception("Saving dosing profile failed")
        await db.rollback()
        raise HTTPException(status_code=500, detail="Error saving dosing profile") from exc

    # 6) Return both the actions and the freshly created profile
    profile_out = DosingProfileResponse.from_orm(new_profile)
    return {"recommended_dose": actions, "profile": profile_out}
