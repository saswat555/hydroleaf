# dosing_profile_service.py
import json
import logging
from datetime import datetime
from typing import Dict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from app.models import Device, DosingProfile
from app.services.device_controller import DeviceController
from app.services.ph_tds import get_ph_tds_readings  # Ensure this returns averaged values
from app.services.llm import call_llm_async, build_dosing_prompt

logger = logging.getLogger(__name__)

async def set_dosing_profile_service(profile_data: dict, db: AsyncSession) -> dict:
    """
    Set the dosing profile for a unified dosing/monitoring device.
    This function uses the unified device for both sensor reading and dosing.
    """
    device_id = profile_data.get("device_id")
    if not device_id:
        raise HTTPException(status_code=400, detail="Device ID is required in profile data")
    
    # Retrieve the unified device from the database.
    result = await db.execute(select(Device).where(Device.id == device_id))
    dosing_device = result.scalars().first()
    
    if not dosing_device:
        # If the device is not found, attempt discovery via the unified controller.
        device_ip = profile_data.get("device_ip")
        if not device_ip:
            raise HTTPException(status_code=404, detail="Unified device not found and device_ip not provided")
        controller = DeviceController(device_ip=device_ip)
        discovered_device = await controller.discover()
        if discovered_device:
            new_device = Device(
                name=discovered_device.get("name", "Discovered Unified Device"),
                type="dosing_unit",  # Using the same type for unified devices
                http_endpoint=discovered_device.get("http_endpoint"),
                location_description=discovered_device.get("location_description", ""),
                pump_configurations=[],  # Can be updated later if needed
                is_active=True
            )
            db.add(new_device)
            try:
                await db.commit()
                await db.refresh(new_device)
                dosing_device = new_device
            except Exception as exc:
                await db.rollback()
                raise HTTPException(status_code=500, detail=f"Error adding discovered device: {exc}") from exc
        else:
            raise HTTPException(status_code=404, detail="Unified dosing device not found and could not be discovered")
    
    # For the unified device, use its HTTP endpoint to get sensor readings.
    sensor_ip = dosing_device.http_endpoint
    try:
        readings = await get_ph_tds_readings(sensor_ip)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching PH/TDS readings: {exc}"
        ) from exc

    ph = readings.get("ph")
    tds = readings.get("tds")

    # Build a comprehensive dosing prompt using the unified device details.
    # (Now using the unified device instance, averaged sensor values, and profile_data.)
    prompt = await build_dosing_prompt(dosing_device, {"ph": ph, "tds": tds}, profile_data)
    try:
        llm_response, raw_llm = await call_llm_async(prompt)
        logger.info(f"LLM response: {llm_response}")
        if isinstance(llm_response, str):
            result_json = json.loads(llm_response)
        elif isinstance(llm_response, list):
            result_json = {"actions": llm_response}
        elif isinstance(llm_response, dict):
            result_json = llm_response
        else:
            raise ValueError("Unexpected response format from LLM.")
        
        recommended_dose = result_json.get("actions", [])
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Error calling LLM: {exc}"
        ) from exc

    # Create a new dosing profile using the unified device.
    new_profile = DosingProfile(
        device_id=dosing_device.id,
        plant_name=profile_data.get("plant_name"),
        plant_type=profile_data.get("plant_type"),
        growth_stage=profile_data.get("growth_stage"),
        seeding_date=profile_data.get("seeding_date"),
        target_ph_min=profile_data.get("target_ph_min"),
        target_ph_max=profile_data.get("target_ph_max"),
        target_tds_min=profile_data.get("target_tds_min"),
        target_tds_max=profile_data.get("target_tds_max"),
        dosing_schedule=profile_data.get("dosing_schedule")
    )
    db.add(new_profile)
    try:
        await db.commit()
        await db.refresh(new_profile)
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Error saving dosing profile: {exc}"
        ) from exc

    return {"recommended_dose": recommended_dose, "profile": new_profile}
