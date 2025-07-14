# dose_manager.py
import logging
from datetime import datetime
from app.models import Device
from fastapi import HTTPException
from app.services.device_controller import DeviceController
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.device_controller import DeviceController
logger = logging.getLogger(__name__)

class DoseManager:
    def __init__(self):
        pass

    async def execute_dosing(self, device_id: str, http_ep: str, actions: list, combined: bool = False) -> dict:
        """
        Execute a dosing command using the unified device controller.
        If combined=True, the controller will use the /dose_monitor endpoint.
        """
        if not actions:
            raise ValueError("No actions supplied")
        for a in actions:
            if "pump_number" not in a or "dose_ml" not in a:
                raise ValueError("Each action needs pump_number & dose_ml")

        ctrl = DeviceController(http_ep)
        # The tests only ever pass a single action, but let’s loop for safety
        for act in actions:
            await ctrl.execute_dosing(act["pump_number"],
                                      act["dose_ml"],
                                      combined=combined)

        return {
            "status": "command_sent",
            "device_id": device_id,
            "actions": actions,
        }

    async def cancel_dosing(self, device_id: str, http_endpoint: str) -> dict:
    # Create a controller instance for the device.
        controller = DeviceController(device_ip=http_endpoint)
        response = await controller.cancel_dosing()
        logger.info(f"Cancellation response for device {device_id}: {response}")
        return {"status": "dosing_cancelled", "device_id": device_id, "response": response}
    async def get_device(self, device_id: str, db: AsyncSession):
        device = await db.get(Device, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        return device


# Create singleton instance
dose_manager = DoseManager()

async def execute_dosing_operation(device_id: str, http_endpoint: str, dosing_actions: list, combined: bool = False) -> dict:
    return await dose_manager.execute_dosing(device_id, http_endpoint, dosing_actions, combined)

async def cancel_dosing_operation(device_id: str, http_endpoint: str) -> dict:
    return await dose_manager.cancel_dosing(device_id, http_endpoint)
