import logging
from datetime import datetime
from typing import Dict
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Device

logger = logging.getLogger(__name__)

class DoseManager:
    def __init__(self):
        pass

    async def execute_dosing(self, device_id: str, http_endpoint: str, dosing_actions: list) -> dict:
        """
        Execute a dosing command for a device via HTTP.
        
        For compatibility with the previous API, we accept a device_id and a list of dosing_actions.
        Since the device firmware supports a single dosing action per command, we extract the pump number and dose 
        from the first element of dosing_actions and ignore any extra actions.
        
        The simplified payload format is:
            {
                "pump": <pump number>,       // from action["pump_number"] or action["pump"]
                "amount": <dose_ml>,         // from action["dose_ml"] or action["amount"]
                "timestamp": "<ISO timestamp>"
            }
        
        This payload is sent as an HTTP POST request to the dosing device’s /pump endpoint.
        """
        if not dosing_actions:
            raise ValueError("No dosing action provided")
        action = dosing_actions[0]
        pump = action.get("pump_number") or action.get("pump")
        amount = action.get("dose_ml") or action.get("amount")
        if pump is None or amount is None:
            raise ValueError("Dosing action must include pump number and dose amount")
        
        payload = {
            "pump": pump,
            "amount": amount,
            "timestamp": datetime.utcnow().isoformat()
        }
        url = f"http://{http_endpoint}/pump"
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                raise Exception(f"Failed to send dosing command: {response.text}")
        
        logger.info(f"Sent dosing command to {url}: {payload}")
        return {
            "status": "command_sent",
            "device_id": device_id,
            "actions": dosing_actions
        }

    async def cancel_dosing(self, device_id: str) -> dict:
        """
        Cancel active dosing operation for a device.
        Since the device firmware does not support cancellation,
        this function simply logs the attempt.
        """
        logger.info(f"Cancellation requested for device {device_id}, but cancellation is not supported.")
        return {"status": "cancel_not_supported", "device_id": device_id}

# Create singleton instance
dose_manager = DoseManager()

async def execute_dosing_operation(device_id: str, http_endpoint: str, dosing_actions: list) -> dict:
    return await dose_manager.execute_dosing(device_id, http_endpoint, dosing_actions)

async def cancel_dosing_operation(device_id: str) -> dict:
    return await dose_manager.cancel_dosing(device_id)


class DosingDevice:
    def __init__(self, device_id: str, pumps_config: Dict):
        """
        Initialize a dosing device with its pump configuration
        
        pumps_config format:
        {
            "pump1": {
                "chemical_name": "Nutrient A",
                "chemical_description": "Primary nutrients NPK"
            },
            ...
        }
        """
        self.device_id = device_id
        self.pumps_config = pumps_config

class DosingManager:
    def __init__(self):
        self.devices: Dict[str, DosingDevice] = {}

    def register_device(self, device_id: str, pumps_config: Dict, http_endpoint: str):
        """Register a new dosing device with its pump configuration"""
        self.devices[device_id] = DosingDevice(device_id, pumps_config)
        logger.info(f"Registered dosing device {device_id} with config: {pumps_config}")

    async def get_device(self, device_id: int, db: AsyncSession):
        """Retrieve the device from the database or raise an error."""
        result = await db.execute(select(Device).where(Device.id == device_id))
        device = result.scalar_one_or_none()

        if not device:
            raise ValueError(f"Dosing device {device_id} not registered in the database")

        return device  # Return the actual device instance from DB
