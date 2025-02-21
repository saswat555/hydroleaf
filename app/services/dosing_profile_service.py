import json
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from app.models import Device, DosingProfile
from app.services.ph_tds import get_ph_tds_readings
from app.services.llm import call_llm_async, build_dosing_prompt

async def set_dosing_profile_service(profile_data: dict, db: AsyncSession) -> dict:
    # Retrieve the monitoring device from the database (assumes type "monitoring")
    result = await db.execute(select(Device).where(Device.type == "monitoring"))
    monitoring_device = result.scalars().first()
    if not monitoring_device:
        raise HTTPException(status_code=500, detail="No monitoring device configured")
    
    # Use the stored device IP instead of hardcoding
    sensor_ip = monitoring_device.location  

    try:
        readings = get_ph_tds_readings(sensor_ip)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error fetching PH/TDS readings: {exc}") from exc

    ph = readings.get("ph")
    tds = readings.get("tds")

    # Build a comprehensive prompt with full context
    prompt = build_dosing_prompt({"ph": ph, "tds": tds}, profile_data)
    try:
        llm_response = await call_llm_async(prompt)
        logging.info(f"LLM response: {llm_response}")

        if isinstance(llm_response, str):
            result_json = json.loads(llm_response)
        elif isinstance(llm_response, list):
            result_json = {"actions": llm_response}  # Wrap response in a dict
        else:
            raise ValueError("Unexpected response format from LLM.")

        dose_amount = result_json.get("actions", [])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error calling LLM: {exc}") from exc

    try:
        result_json = {"actions": llm_response} 
        dose_amount = result_json.get("dose")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error parsing LLM response: {e}") from e

    new_profile = DosingProfile(
        device_id=profile_data.get("device_id"),
        chemical_name=profile_data.get("chemical_name"),
        chemical_description=profile_data.get("chemical_description"),
        plant_name=profile_data.get("plant_name"),
        weather_locale=profile_data.get("weather_locale"),
        seeding_age=profile_data.get("seeding_age"),
        current_age=profile_data.get("current_age")
    )
    db.add(new_profile)
    try:
        await db.commit()
        await db.refresh(new_profile)
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Error saving dosing profile: {exc}") from exc

    return {"recommended_dose": dose_amount, "profile": new_profile}
