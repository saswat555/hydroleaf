import json
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from app.models import Device, DosingProfile
from app.services.ph_tds import get_ph_tds_readings
from app.services.llm import call_llm_async, build_dosing_prompt
from app.services.device_discovery import discover_devices

logger = logging.getLogger(__name__)

async def set_dosing_profile_service(profile_data: dict, db: AsyncSession) -> dict:
    """
    Set the dosing profile for a dosing device.
    
    If the dosing device specified by profile_data["device_id"] does not exist in the database,
    attempt to discover it via the updated HTTP discovery service and add it.
    
    Then, using a monitoring device to fetch sensor readings (PH/TDS), build an LLM prompt,
    call the LLM for dosing recommendations, and save a new dosing profile.
    """
    device_id = profile_data.get("device_id")
    if not device_id:
        raise HTTPException(status_code=400, detail="Device ID is required in profile data")
    
    # Try to retrieve the dosing device from the database.
    result = await db.execute(select(Device).where(Device.id == device_id))
    dosing_device = result.scalars().first()
    
    # If the dosing device is not found, attempt to discover devices via HTTP.
    if not dosing_device:
        discovered = await discover_devices()
        devices_list = discovered.get("devices", [])
        if devices_list:
            # For simplicity, pick the first discovered device.
            discovered_device = devices_list[0]
            new_device = Device(
                name=discovered_device.get("name", "Discovered Dosing Device"),
                type="dosing_unit",  # Use appropriate enum if available
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
                raise HTTPException(
                    status_code=500,
                    detail=f"Error adding discovered device: {exc}"
                ) from exc
        else:
            raise HTTPException(status_code=404, detail="Dosing device not found and could not be discovered")
    
    # Retrieve the monitoring device (assumed to be of type "monitoring") to fetch sensor readings.
    result = await db.execute(select(Device).where(Device.type == "monitoring"))
    monitoring_device = result.scalars().first()
    if not monitoring_device:
        raise HTTPException(status_code=500, detail="No monitoring device configured")
    
    # Use the monitoring device's location as the sensor IP.
    sensor_ip = monitoring_device.location

    try:
        readings = get_ph_tds_readings(sensor_ip)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching PH/TDS readings: {exc}"
        ) from exc

    ph = readings.get("ph")
    tds = readings.get("tds")

    # Build a comprehensive prompt using sensor data and profile data.
    prompt = build_dosing_prompt({"ph": ph, "tds": tds}, profile_data)
    try:
        llm_response = await call_llm_async(prompt)
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

    # Create a new dosing profile using the discovered or existing dosing device.
    new_profile = DosingProfile(
        device_id=dosing_device.id,
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
        raise HTTPException(
            status_code=500,
            detail=f"Error saving dosing profile: {exc}"
        ) from exc

    return {"recommended_dose": recommended_dose, "profile": new_profile}
