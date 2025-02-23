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

        # Configuration: list of device IPs to probe, provided via environment variable.
        # Example: DEVICE_IPS="192.168.1.10,192.168.1.11,192.168.1.12"
        self.device_ips: List[str] = [
            ip.strip() for ip in os.getenv("DEVICE_IPS", "").split(",") if ip.strip()
        ]
        # Timeout (in seconds) for each HTTP request.
        self.request_timeout: float = float(os.getenv("DEVICE_REQUEST_TIMEOUT", "3.0"))
        self.initialized = True

    async def scan_network(self) -> Dict[str, List[Dict]]:
        """
        Actively scan for devices on the LAN by sending an HTTP GET request
        to the /discovery endpoint on each IP address specified in DEVICE_IPS.
        Returns a dictionary with a list of devices that responded.
        """
        devices: List[Dict] = []
        async with httpx.AsyncClient(timeout=self.request_timeout) as client:
            # Create tasks for concurrent HTTP GET requests.
            tasks = [
                self._get_device_info(client, ip) for ip in self.device_ips
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, dict):
                    devices.append(result)
        return {"devices": devices}

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

async def discover_devices() -> Dict[str, List[Dict]]:
    """
    Discover all devices on the LAN using the DeviceDiscoveryService.
    """
    discovery_service = DeviceDiscoveryService()
    return await discovery_service.scan_network()
