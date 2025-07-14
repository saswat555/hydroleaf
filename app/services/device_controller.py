"""
app/services/device_controller.py
─────────────────────────────────
Unified helper for talking to every kind of edge-device in the system.
All public method names stay unchanged, so nothing else needs to change.

Test-suite expectations handled:
• IP in the returned JSON is always exactly the string that was passed in
  (without an added “http://” unless the caller provided it).
• Fallback from /discovery → /state for valve controllers.
• Robust error-handling that raises httpx.HTTPStatusError on non-200.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Optional

import httpx
from fastapi import HTTPException

# The enum is handy, but in unit-tests we only compare the *string* value.
try:
    from app.schemas import DeviceType

    _TYPE_DOSING  = DeviceType.DOSING_UNIT.value          # "dosing_unit"
    _TYPE_VALVE   = DeviceType.VALVE_CONTROLLER.value     # "valve_controller"
except Exception:  # pragma: no cover – only if enum absent
    _TYPE_DOSING  = "dosing_unit"
    _TYPE_VALVE   = "valve_controller"

logger = logging.getLogger(__name__)


class DeviceController:
    """
    Thin async wrapper around the device’s HTTP API.

        /discovery         general info (preferred)
        /version           firmware version
        /monitor           sensor readings  (pH / TDS)
        /pump              single-pump dosing
        /dose_monitor      combined dosing
        /pump_calibration  stop / cancel
        /state             valve state      (for valve controllers)
        /toggle            valve toggle
    """

    # --------------------------------------------------------------------- #
    # Init / helpers                                                        #
    # --------------------------------------------------------------------- #
    def __init__(self, device_ip: str, request_timeout: float = 10.0):
        # keep *exactly* what caller passed – tests inspect this
        self._raw_ip = device_ip

        # httpx.AsyncClient always needs a full URL
        if not device_ip.startswith(("http://", "https://")):
            device_ip = f"http://{device_ip}"
        self.base_url = device_ip.rstrip("/")
        self.request_timeout = request_timeout

    # --------------------------------------------------------------------- #
    # Discovery & version                                                   #
    # --------------------------------------------------------------------- #
    async def discover(self) -> Optional[Dict]:
        """
        1) GET /discovery – preferred
        2) GET /state     – fallback for valve controllers
        """
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.request_timeout
        ) as client:
            # ---------- primary path (/discovery) ----------
            try:
                res = await client.get("/discovery")
                if res.status_code == 200:
                    data = res.json()
                    # Some firmwares omit "type" – assume dosing_unit
                    data.setdefault("type", _TYPE_DOSING)
                    data["ip"] = self._raw_ip
                    return data
            except Exception as exc:  # pragma: no cover
                logger.debug("/discovery failed for %s – %s", self.base_url, exc)

            # ---------- fallback path (/state) -------------
            try:
                res = await client.get("/state")
                if res.status_code == 200:
                    state = res.json()
                    return {
                        "device_id": state.get("device_id"),
                        "type":      _TYPE_VALVE,
                        "valves":    state.get("valves", []),
                        "ip":        self._raw_ip,
                    }
            except Exception as exc:  # pragma: no cover
                logger.debug("/state failed for %s – %s", self.base_url, exc)

        return None

    async def get_version(self) -> Optional[str]:
        """
        Returns the firmware version or *None* when unavailable.
        """
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.request_timeout
        ) as client:
            try:
                res = await client.get("/version")
                if res.status_code == 200:
                    return res.json().get("version")
            except Exception:  # pragma: no cover
                logger.debug("/version failed for %s", self.base_url)

        disc = await self.discover()
        return disc.get("version") if disc else None

    # --------------------------------------------------------------------- #
    # Sensor readings                                                       #
    # --------------------------------------------------------------------- #
    async def get_sensor_readings(self) -> Dict:
        """
        GET /monitor  →  { ph: float, tds: float }
        Raises httpx.HTTPStatusError on non-200.
        """
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.request_timeout
        ) as client:
            res = await client.get("/monitor")
            if res.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Sensor read failed: {res.status_code}", request=None, response=res
                )
            return res.json()

    # --------------------------------------------------------------------- #
    # Dosing operations                                                     #
    # --------------------------------------------------------------------- #
    async def execute_dosing(
        self, pump: int, amount: int, *, combined: bool = False
    ) -> Dict:
        """
        POST /pump          (single)
        POST /dose_monitor  (combined)

        Raises httpx.HTTPStatusError on failure.
        """
        endpoint = "/dose_monitor" if combined else "/pump"
        payload = {
            "pump":      pump,
            "amount":    amount,
            "timestamp": datetime.utcnow().isoformat(),
        }
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.request_timeout
        ) as client:
            res = await client.post(endpoint, json=payload)
            if res.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Dosing command failed: {res.status_code}",
                    request=None,
                    response=res,
                )
            return res.json()

    async def cancel_dosing(self) -> Dict:
        """
        POST /pump_calibration {command:"stop"}
        """
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.request_timeout
        ) as client:
            res = await client.post("/pump_calibration", json={"command": "stop"})
            if res.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Cancel dosing failed: {res.status_code}",
                    request=None,
                    response=res,
                )
            return res.json()

    # --------------------------------------------------------------------- #
    # Valve helpers                                                         #
    # --------------------------------------------------------------------- #
    async def get_state(self) -> Dict:
        """
        GET /state  – generic valve / switch state.
        """
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.request_timeout
        ) as client:
            res = await client.get("/state")
            if res.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Get state failed: {res.status_code}", request=None, response=res
                )
            return res.json()

    async def toggle_valve(self, valve_id: int) -> Dict:
        """
        POST /toggle  { valve_id }
        Raises:
            • ValueError when valve_id ∉ [1,4]
            • httpx.HTTPStatusError on non-200
        """
        if not 1 <= valve_id <= 4:
            raise ValueError("Invalid valve_id (must be 1–4)")

        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.request_timeout
        ) as client:
            res = await client.post("/toggle", json={"valve_id": valve_id})
            if res.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Toggle valve failed: {res.status_code}",
                    request=None,
                    response=res,
                )
            return res.json()


# --------------------------------------------------------------------------- #
# Factory – makes monkey-patching easy in the tests                           #
# --------------------------------------------------------------------------- #
def get_device_controller(device_ip: str) -> DeviceController:  # noqa: D401
    """Return a *real* DeviceController (tests monkey-patch this factory)."""
    return DeviceController(device_ip)
