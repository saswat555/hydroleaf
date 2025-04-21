# device_controller.py
import logging
import asyncio
from datetime import datetime
from typing import Dict, Optional
import httpx
from fastapi import HTTPException
import re
logger = logging.getLogger(__name__)
from app.schemas import DeviceType

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
        Try /discovery first; if that fails, assume it's a valve controller and call /state.
        """
        async with httpx.AsyncClient(timeout=self.request_timeout) as client:
            # 1) Try standard discovery
            try:
                url = await self._build_url("discovery")
                res = await client.get(url)
                if res.status_code == 200:
                    data = res.json()
                    data["ip"] = self.device_ip
                    return data
            except Exception:
                logger.debug(f"/discovery failed for {self.device_ip}, trying /state")

            # 2) Fallback to valve controller /state
            try:
                url = await self._build_url("state")
                res = await client.get(url)
                res.raise_for_status()
                state = res.json()
                # expect { device_id, valves: [ {id, state}, … ] }
                return {
                    "device_id": state.get("device_id"),
                    "type": DeviceType.VALVE_CONTROLLER.value,
                    "valves": state.get("valves", []),
                    "ip": self.device_ip
                }
            except Exception as e:
                logger.debug(f"/state discovery failed for {self.device_ip}: {e}")

        return None


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
    
    async def _build_url(self, path: str) -> str:
        base = self.device_ip
        if not base.startswith(("http://","https://")):
            base = f"http://{base}"
        return f"{base.rstrip('/')}/{path.lstrip('/')}"
    
    async def execute_dosing(self, pump: int, amount: int, combined: bool = False) -> Dict:
        endpoint = "dose_monitor" if combined else "pump"
        url = await self._build_url(endpoint)
        payload = {"pump": pump, "amount": amount, "timestamp": datetime.utcnow().isoformat()}
        async with httpx.AsyncClient(timeout=self.request_timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
    async def get_state(self) -> Dict:
        """
        Fetch the current valves state from /state.
        """
        url = await self._build_url("state")
        async with httpx.AsyncClient(timeout=self.request_timeout) as client:
            res = await client.get(url)
            res.raise_for_status()
            return res.json()

    async def toggle_valve(self, valve_id: int) -> Dict:
        """
        Toggle a single valve via /toggle.
        """
        url = await self._build_url("toggle")
        payload = {"valve_id": valve_id}
        async with httpx.AsyncClient(timeout=self.request_timeout) as client:
            res = await client.post(url, json=payload)
            res.raise_for_status()
            return res.json()