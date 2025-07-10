# app/services/device_controller.py
import logging
from datetime import datetime
from typing import Dict, Optional

import httpx
from fastapi import HTTPException

from app.schemas import DeviceType

logger = logging.getLogger(__name__)

class DeviceController:
    """
    Unified controller for IoT devices. Supports:
      - Discovery (/discovery, /state)
      - Version check (/version)
      - Sensor readings (/monitor)
      - Dosing (/pump, /dose_monitor)
      - Cancellation (/pump_calibration)
      - Valve state (/state, /toggle)
    """
    def __init__(self, device_ip: str, request_timeout: float = 10.0):
        # remember exactly what user passed in, for tests
        self._raw_ip = device_ip
        # ensure base_url is always a proper URL for AsyncClient
        if not device_ip.startswith(("http://", "https://")):
            device_ip = f"http://{device_ip}"
        self.base_url = device_ip.rstrip("/")
        self.request_timeout = request_timeout

    async def discover(self) -> Optional[Dict]:
        """
        1) GET  /discovery
        2) GET  /state  (valve-controller fallback)
        """
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.request_timeout) as client:
            # primary discovery
            try:
                res = await client.get("/discovery")
                if res.status_code == 200:
                    data = res.json()
                    data.setdefault("type", data.get("type", DeviceType.DOSING_UNIT.value))
                    data["ip"] = self._raw_ip
                    return data
            except Exception:
                logger.debug(f"/discovery failed for {self.base_url}")

            # fallback for valve controllers (check status_code, not raise_for_status)
            try:
                res = await client.get("/state")
                if res.status_code == 200:
                    state = res.json()
                    return {
                        "device_id": state.get("device_id"),
                        "type":       DeviceType.VALVE_CONTROLLER.value,
                        "valves":     state.get("valves", []),
                        "ip":         self._raw_ip,
                    }
                else:
                    logger.debug(f"/state returned {res.status_code} for {self.base_url}")
            except Exception:
                logger.debug(f"/state discovery failed for {self.base_url}")

        return None

    async def get_version(self) -> Optional[str]:
        """
        GET /version, fallback to discovery.version
        """
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.request_timeout) as client:
            try:
                res = await client.get("/version")
                if res.status_code == 200:
                    return res.json().get("version")
            except Exception:
                logger.debug(f"/version failed for {self.base_url}")

        # fallback to discovery payload
        disc = await self.discover()
        return disc.get("version") if disc else None

    async def get_sensor_readings(self) -> Dict:
        """
        GET /monitor → { ph: float, tds: float }
        Raises httpx.HTTPStatusError on non-200.
        """
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.request_timeout) as client:
            res = await client.get("/monitor")
            if res.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Sensor read failed: {res.status_code}", request=None, response=res
                )
            data = res.json()
            logger.info(f"Sensor readings from {self.base_url}: {data}")
            return data

    async def execute_dosing(self, pump: int, amount: int, combined: bool = False) -> Dict:
        """
        POST /pump  or  /dose_monitor  { pump, amount, timestamp }
        """
        endpoint = "/dose_monitor" if combined else "/pump"
        payload = {"pump": pump, "amount": amount, "timestamp": datetime.utcnow().isoformat()}
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.request_timeout) as client:
            res = await client.post(endpoint, json=payload)
            if res.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Dosing command failed: {res.status_code}", request=None, response=res
                )
            return res.json()

    async def cancel_dosing(self) -> Dict:
        """
        POST /pump_calibration  { command: stop }
        """
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.request_timeout) as client:
            res = await client.post("/pump_calibration", json={"command": "stop"})
            if res.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Cancel dosing failed: {res.status_code}", request=None, response=res
                )
            return res.json()
    async def get_state(self) -> Dict:
        """
        GET /state  →  generic state (valves or switches)
        """
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.request_timeout) as client:
            res = await client.get("/state")
            if res.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Get state failed: {res.status_code}", request=None, response=res
                )
            return res.json()

    async def toggle_valve(self, valve_id: int) -> Dict:
        """
        POST /toggle  { valve_id }  --  raises ValueError if valve_id not in 1–4
        """
        if not (1 <= valve_id <= 4):
            raise ValueError("Invalid valve_id (must be 1–4)")

        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.request_timeout) as client:
            res = await client.post("/toggle", json={"valve_id": valve_id})
            if res.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Toggle valve failed: {res.status_code}", request=None, response=res
                )
            return res.json()

# Factory for override/mocking in tests
def get_device_controller(device_ip: str) -> DeviceController:
    return DeviceController(device_ip)
