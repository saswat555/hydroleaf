import os
import logging
import asyncio
import httpx
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

class DeviceDiscoveryService:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DeviceDiscoveryService, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        # Ensure initialization happens only once.
        if hasattr(self, "initialized") and self.initialized:
            return

        # Configuration: list of device IPs to probe (optional, if still needed).
        self.device_ips: List[str] = [
            ip.strip() for ip in os.getenv("DEVICE_IPS", "").split(",") if ip.strip()
        ]
        # Timeout (in seconds) for each HTTP request.
        self.request_timeout: float = float(os.getenv("DEVICE_REQUEST_TIMEOUT", "3.0"))
        self.initialized = True

    async def check_device(self, ip: str) -> Dict[str, Optional[Dict]]:
        """
        Check if a device is connected at the given IP by sending a GET request
        to its /discovery endpoint. Returns a dictionary with key "device" that
        holds the device info if found, or None if not connected.
        """
        async with httpx.AsyncClient(timeout=self.request_timeout) as client:
            result = await self._get_device_info(client, ip)
            if result:
                logger.info(f"Device at {ip} is connected: {result}")
            else:
                logger.info(f"No device found at {ip}")
            return {"device": result}

    async def _get_device_info(self, client: httpx.AsyncClient, ip: str) -> Optional[Dict]:
        url = f"http://{ip}/discovery"
        try:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                data["ip"] = ip  # Add the IP address to the device info
                logger.info(f"Discovered device at {ip}: {data}")
                return data
            else:
                logger.debug(f"Device at {ip} returned status code {response.status_code}")
        except Exception as e:
            logger.debug(f"Device at {ip} did not respond: {e}")
        return None

def get_device_discovery_service() -> DeviceDiscoveryService:
    """
    Dependency to get the DeviceDiscoveryService instance.
    """
    return DeviceDiscoveryService()

# (Optional) Retain the scan_network method if needed for multi-IP scanning.
async def discover_devices() -> Dict[str, List[Dict]]:
    """
    Discover all devices on the LAN using the DeviceDiscoveryService.
    (This method may be deprecated if you only want per-IP validation.)
    """
    discovery_service = DeviceDiscoveryService()
    return await discovery_service.scan_network()
