# device_controller.py
import logging
import asyncio
from datetime import datetime
from typing import Dict, Optional
import httpx
from fastapi import HTTPException
import re
logger = logging.getLogger(__name__)

class DeviceController:
    """
    Unified controller for the dosing and monitoring device.
    Provides methods for:
      - Discovering the device via its /discovery endpoint.
      - Executing dosing commands via /pump or the combined /dose_monitor endpoint.
      - Fetching sensor readings via the /monitor endpoint.
    """
    def __init__(self, device_ip: str, request_timeout: float = 10.0):
        self.device_ip = device_ip
        self.request_timeout = request_timeout

    async def discover(self) -> Optional[Dict]:
        """
        Discover device info via the /discovery endpoint.
        """
        if not re.match(r'^https?://', self.device_ip):
            url = f"http://{self.device_ip}/discovery"
        else:
            url = f"{self.device_ip}/discovery"
            

        try:
            async with httpx.AsyncClient(timeout=self.request_timeout) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    data = response.json()
                    data["ip"] = self.device_ip  # Include the IP in the device info
                    logger.info(f"Discovered device at {self.device_ip}: {data}")
                    return data
                else:
                    logger.debug(f"Discovery failed for {self.device_ip} with status {response.status_code}")
        except Exception as e:
            logger.debug(f"Discovery error for {self.device_ip}: {e}")
        return None

    async def execute_dosing(self, pump: int, amount: int, combined: bool = False) -> Dict:
        """
        Execute a dosing command.
        If 'combined' is True, uses the /dose_monitor endpoint for a combined dosing/monitoring sequence.
        Otherwise, uses the standard /pump endpoint.
        """
        endpoint = "/dose_monitor" if combined else "/pump"
        payload = {
            "pump": pump,
            "amount": amount,
            "timestamp": datetime.utcnow().isoformat()
        }
        if not re.match(r'^https?://', self.device_ip):
            url = f"http://{self.device_ip}/discovery"
        else:
            url = f"{self.device_ip}/discovery"
        try:
            async with httpx.AsyncClient(timeout=self.request_timeout) as client:
                response = await client.post(url, json=payload)
                if response.status_code == 200:
                    logger.info(f"Dosing command sent to {url}: {payload}")
                    return response.json()
                else:
                    raise HTTPException(status_code=response.status_code, detail=f"Dosing failed: {response.text}")
        except Exception as e:
            logger.error(f"Error executing dosing command: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    async def get_sensor_readings(self) -> Dict:
        """
        Retrieve averaged sensor readings from the device via the /monitor endpoint.
        (The device now returns averaged pH and TDS values.)
        """
        url = f"http://{self.device_ip}/monitor"
        try:
            async with httpx.AsyncClient(timeout=self.request_timeout) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"Sensor readings from {self.device_ip}: {data}")
                    return data
                else:
                    raise HTTPException(status_code=response.status_code, detail=f"Sensor reading failed: {response.text}")
        except Exception as e:
            logger.error(f"Error fetching sensor readings: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    async def cancel_dosing(self) -> Dict:
        """
        Cancel dosing by sending a stop command to the device.
        Uses the /pump_calibration endpoint with {"command": "stop"}.
        """
        url = f"http://{self.device_ip}/pump_calibration"
        payload = {"command": "stop"}
        try:
            async with httpx.AsyncClient(timeout=self.request_timeout) as client:
                response = await client.post(url, json=payload)
                if response.status_code == 200:
                    logger.info(f"Cancellation command sent to {url}: {payload}")
                    return response.json()
                else:
                    raise HTTPException(status_code=response.status_code, detail=f"Cancellation failed: {response.text}")
        except Exception as e:
            logger.error(f"Error sending cancellation command: {e}")
            raise HTTPException(status_code=500, detail=str(e))
