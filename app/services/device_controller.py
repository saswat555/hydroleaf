# app/services/device_controller.py
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


def _normalize_base(url: str) -> str:
    """Ensure scheme present and no trailing slash."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url.rstrip("/")


class DeviceController:
    def __init__(self, base_url: str, *, timeout: float = 2.0):
        self.base_url = base_url if base_url.startswith(("http://", "https://")) else f"http://{base_url}"
        self._client = httpx.AsyncClient(timeout=timeout)

    # ---------- Common helpers ----------
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"
    async def _get_json(self, path: str, *, timeout: float = 5.0) -> Dict[str, Any]:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{self.base_url}{path}", timeout=timeout)
            if r.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Unexpected {r.status_code} from {path}", request=r.request, response=r
                )
            return r.json()

    async def _post_json(
        self,
        path: str,
        payload: Dict[str, Any],
        *,
        timeout: float = 5.0,
        raise_on_error: bool = True,
    ) -> Dict[str, Any]:
        async with httpx.AsyncClient() as client:
            r = await client.post(f"{self.base_url}{path}", json=payload, timeout=timeout)
            if r.status_code != 200 and raise_on_error:
                raise httpx.HTTPStatusError(
                    f"Unexpected {r.status_code} from {path}", request=r.request, response=r
                )
            # if device returned non-200 and raise_on_error=False, try to parse JSON, else stub
            try:
                return r.json()
            except Exception:
                return {"status": r.status_code, "message": "ok" if r.status_code == 200 else "sent"}

    # ---------- Discovery / version ----------

    async def discover(self) -> Optional[Dict[str, Any]]:
        try:
            data = await self._get_json("/discovery", timeout=3)
            host = self.base_url.split("://", 1)[1]
            data.setdefault("ip", host)
            return data
        except Exception as e:
            logger.info("Discovery failed for %s: %s", self.base_url, e)
            return None

    async def get_version(self) -> Optional[str]:
        # Prefer /version
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{self.base_url}/version", timeout=3)
                if r.status_code == 200:
                    # JSON object with "version"
                    try:
                        j = r.json()
                        if isinstance(j, dict) and "version" in j:
                            return str(j["version"])
                    except Exception:
                        pass
                    # or plain text body
                    return (getattr(r, "text", "") or "").strip() or None
        except Exception:
            pass

        # Fallback to /discovery
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{self.base_url}/discovery", timeout=3)
                if r.status_code == 200:
                    try:
                        j = r.json()
                        if isinstance(j, dict):
                            v = j.get("version")
                            return str(v) if v else None
                    except Exception:
                        return None
        except Exception:
            pass

        return None

    # ---------- Sensors / dosing ----------

    async def get_sensor_readings(self) -> Dict[str, Any]:
        return await self._get_json("/sensor", timeout=5)

    async def execute_dosing(self, pump: int, amount: float, *, combined: bool = False) -> Dict[str, Any]:
        endpoint = "/dose_monitor" if combined else "/pump"
        return await self._post_json(endpoint, {"pump": pump, "amount": amount}, timeout=5)

    async def cancel_dosing(self):
        for path in ("/pump_calibration", "/pump/calibration", "/dosing/stop"):
            try:
                r = await self._client.post(self._url(path), json={"command": "stop"})
                if r.status_code < 500:
                    return r.json()
            except httpx.RequestError:
                continue
        raise httpx.RequestError("All stop endpoints failed")

    # ---------- Generic state (valves & switches) ----------

    async def get_state(self):
        r = await self._client.get(self._url("/state"))
        r.raise_for_status()
        return r.json()
    # ---------- Valves & switches actions ----------

    async def toggle_valve(self, valve_id: str) -> Dict[str, Any]:
        if not isinstance(valve_id, int) or not (1 <= valve_id <= 4):
            raise ValueError("valve_id must be in 1–4")
        return await self._post_json("/toggle", {"valve_id": valve_id}, timeout=5)

    async def toggle_switch(self, channel: int) -> Dict[str, Any]:
        if not isinstance(channel, int) or not (1 <= channel <= 8):
            raise ValueError("channel must be in 1–8")
        return await self._post_json("/toggle", {"channel": channel}, timeout=5)

    # ---------- CCTV ----------

    async def get_status(self) -> Dict[str, Any]:
        """Fetch `/status` from CCTV device."""
        return await self._get_json("/status", timeout=5)
    
    async def aclose(self):
        await self._client.aclose()

# --- tiny factory kept for tests ------------------------------------------------
def get_device_controller(device_ip: str) -> DeviceController:
    return DeviceController(device_ip)
