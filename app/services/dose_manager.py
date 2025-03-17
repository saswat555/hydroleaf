# dose_manager.py
import logging
from datetime import datetime
from app.models import Device
from fastapi import HTTPException
from app.services.device_controller import DeviceController
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

class DoseManager:
    def __init__(self):
        pass

    async def execute_dosing(self, device_id: int, http_endpoint: str, dosing_actions: list, combined: bool = False) -> dict:
        """
        Execute a dosing command using the unified device controller.
        If combined=True, the controller will use the /dose_monitor endpoint.
        """
        if not dosing_actions:
            raise ValueError("No dosing action provided")
        controller = DeviceController(device_ip=http_endpoint)
        responses = []
        for action in dosing_actions:
            pump = action.get("pump_number") or action.get("pump")
            amount = action.get("dose_ml") or action.get("amount")
            if pump is None or amount is None:
                raise ValueError("Dosing action must include pump number and dose amount")
            try:
                resp = await controller.execute_dosing(pump, amount, combined=combined)
                responses.append(resp)
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))
        
        logger.info(f"Sent dosing commands to device {device_id}: {responses}")
        return {
            "status": "command_sent",
            "device_id": device_id,
            "actions": dosing_actions,
            "responses": responses
        }

    async def cancel_dosing(self, device_id: str, http_endpoint: str) -> dict:
    # Create a controller instance for the device.
        controller = DeviceController(device_ip=http_endpoint)
        response = await controller.cancel_dosing()
        logger.info(f"Cancellation response for device {device_id}: {response}")
        return {"status": "dosing_cancelled", "device_id": device_id, "response": response}
    async def get_device(self, device_id: int, db: AsyncSession):
        device = await db.get(Device, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        return device


# Create singleton instance
dose_manager = DoseManager()

async def execute_dosing_operation(device_id: str, http_endpoint: str, dosing_actions: list, combined: bool = False) -> dict:
    return await dose_manager.execute_dosing(device_id, http_endpoint, dosing_actions, combined)

async def cancel_dosing_operation(device_id: int, http_endpoint: str) -> dict:
    return await dose_manager.cancel_dosing(device_id, http_endpoint)
