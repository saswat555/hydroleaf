import logging
from typing import Dict
from fastapi import HTTPException
import httpx

logger = logging.getLogger(__name__)

async def get_ph_tds_readings(device_ip: str) -> Dict[str, float]:
    """
    Fetch pH and TDS readings from the device's /monitor endpoint directly,
    without using DeviceController.
    """
    url = f"{device_ip}/monitor"
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            logger.info(f"[{device_ip}] Raw /monitor response: {data}")

            if "pH" in data and "TDS" in data:
                return {
                    "ph": float(data["pH"]),
                    "tds": float(data["TDS"])
                }
            else:
                raise HTTPException(status_code=500, detail=f"Invalid /monitor response: {data}")

    except Exception as e:
        logger.error(f"Failed to fetch pH/TDS from {device_ip}: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching from /monitor: {e}")
