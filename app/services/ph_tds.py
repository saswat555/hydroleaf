import logging
import httpx
from typing import Dict

logger = logging.getLogger(__name__)

class PHTDSReader:
    def __init__(self):
        # Under development: this device will eventually provide live readings via HTTP.
        # For now, we use HTTP to fetch dummy sensor data.
        pass

    async def get_readings(self, device_ip: str) -> Dict:
        """
        Get the latest sensor readings from the PH/TDS device via HTTP.
        Expected response JSON from the device:
            {
                "ph": <float>,
                "tds": <float>,
                "volume": <float>  # water volume in litres
            }
        If the HTTP call fails, return default dummy values.
        """
        url = f"http://{device_ip}/readings"
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"Received sensor readings from {device_ip}: {data}")
                    return data
                else:
                    logger.error(f"Error: Received status code {response.status_code} from {url}")
        except Exception as e:
            logger.error(f"Error fetching readings from {device_ip}: {e}")
        
        # Return default dummy values as fallback.
        return {"ph": 7.0, "tds": 150, "volume": 1000.0}

    async def request_reading(self, device_ip: str) -> None:
        """
        Request an immediate sensor reading from the device via HTTP.
        Sends an HTTP POST request to the device's /command endpoint with a 'read' command.
        """
        url = f"http://{device_ip}/command"
        payload = {"command": "read"}
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.post(url, json=payload)
                if response.status_code == 200:
                    logger.info(f"Successfully requested reading from {device_ip}")
                else:
                    logger.error(f"Failed to request reading from {device_ip}: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"Error requesting reading from {device_ip}: {e}")

# Create a singleton instance
ph_tds_reader = PHTDSReader()

async def get_ph_tds_readings(device_ip: str) -> Dict:
    """
    Function to retrieve sensor readings from a PH/TDS device given its IP address.
    """
    return await ph_tds_reader.get_readings(device_ip)
