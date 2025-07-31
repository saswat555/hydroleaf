# app/services/device_controller.py

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from fastapi import HTTPException

try:
    from app.schemas import DeviceType
    _TYPE_DOSING = DeviceType.DOSING_UNIT.value        # "dosing_unit"
    _TYPE_VALVE = DeviceType.VALVE_CONTROLLER.value    # "valve_controller"
except ImportError:
    _TYPE_DOSING = "dosing_unit"
    _TYPE_VALVE = "valve_controller"

logger = logging.getLogger(__name__)


class DeviceController:
    """
    Thin async wrapper around an edge device’s HTTP API.
    """

    def __init__(self, device_ip: str, request_timeout: float = 10.0):
        # preserve exactly what was passed in
        self._raw_ip = device_ip
        # ensure we have a full URL for httpx
        if not device_ip.startswith(("http://", "https://")):
            device_ip = f"http://{device_ip}"
        self.base_url = device_ip.rstrip("/")
        self.request_timeout = request_timeout

    async def discover(self) -> Optional[Dict[str, Any]]:
        """
        1) Try GET /discovery
        2) If that fails or non-200, try GET /state (for valve controllers)
        Returns None if both fail.
        """
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.request_timeout) as client:
            # Primary discovery endpoint
            try:
                res = await client.get("/discovery")
                if res.status_code == 200:
                    data = res.json()
                    data.setdefault("type", _TYPE_DOSING)
                    data["ip"] = self._raw_ip
                    return data
            except Exception as exc:
                logger.debug("Discovery failed on %s: %s", self.base_url, exc)

            # Fallback for valve controllers
            try:
                res = await client.get("/state")
                if res.status_code == 200:
                    state = res.json()
                    return {
                        "device_id": state.get("device_id"),
                        "type": _TYPE_VALVE,
                        "valves": state.get("valves", []),
                        "ip": self._raw_ip,
                    }
            except Exception as exc:
                logger.debug("State fallback failed on %s: %s", self.base_url, exc)

        return None

    async def get_version(self) -> Optional[str]:
        """
        Prefer GET /version; if that fails, fall back to discovery().version
        """
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.request_timeout) as client:
            try:
                res = await client.get("/version")
                if res.status_code == 200:
                    return res.json().get("version")
            except Exception:
                logger.debug("Version endpoint failed on %s", self.base_url)

        disc = await self.discover()
        return disc.get("version") if disc else None

    async def get_sensor_readings(self) -> Dict[str, Any]:
        """
        GET /monitor → { ph: float, tds: float }
        Raises HTTPStatusError on non-200
        """
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.request_timeout) as client:
            res = await client.get("/monitor")
            if res.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Sensor read failed: {res.status_code}",
                    request=None,
                    response=res,
                )
            return res.json()

    async def execute_dosing(
        self, pump: int, amount: int, *, combined: bool = False
    ) -> Dict[str, Any]:
        """
        POST /pump         (single)
        POST /dose_monitor (combined)
        Raises HTTPStatusError on non-200
        """
        endpoint = "/dose_monitor" if combined else "/pump"
        payload = {
            "pump": pump,
            "amount": amount,
            "timestamp": datetime.utcnow().isoformat(),
        }
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.request_timeout) as client:
            res = await client.post(endpoint, json=payload)
            if res.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Dosing command failed: {res.status_code}",
                    request=None,
                    response=res,
                )
            return res.json()

    async def cancel_dosing(self) -> Dict[str, Any]:
        """
        POST /pump_calibration { command: "stop" }
        Raises HTTPStatusError on non-200
        """
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.request_timeout) as client:
            res = await client.post("/pump_calibration", json={"command": "stop"})
            if res.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Cancel dosing failed: {res.status_code}",
                    request=None,
                    response=res,
                )
            return res.json()

    async def get_state(self) -> Dict[str, Any]:
        """
        GET /state → generic valve/switch state JSON
        Raises HTTPStatusError on non-200
        """
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.request_timeout) as client:
            res = await client.get("/state")
            if res.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Get state failed: {res.status_code}",
                    request=None,
                    response=res,
                )
            return res.json()

    async def toggle_valve(self, valve_id: int) -> Dict[str, Any]:
        """
        POST /toggle { valve_id }
        • Raises ValueError if valve_id not in 1–4
        • Raises HTTPStatusError on non-200
        """
        if not 1 <= valve_id <= 4:
            raise ValueError("Invalid valve_id (must be 1–4)")

        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.request_timeout) as client:
            res = await client.post("/toggle", json={"valve_id": valve_id})
            if res.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Toggle valve failed: {res.status_code}",
                    request=None,
                    response=res,
                )
            return res.json()


def get_device_controller(device_ip: str) -> DeviceController:
    """
    Factory for obtaining a real controller (tests can monkey‐patch this).
    """
    return DeviceController(device_ip)
