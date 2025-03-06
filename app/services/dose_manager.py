# dose_manager.py
import logging
from datetime import datetime
from fastapi import HTTPException
from app.services.device_controller import DeviceController

logger = logging.getLogger(__name__)

class DoseManager:
    def __init__(self):
        pass

    async def execute_dosing(self, device_id: str, http_endpoint: str, dosing_actions: list, combined: bool = False) -> dict:
        """
        Execute a dosing command using the unified device controller.
        If combined=True, the controller will use the /dose_monitor endpoint.
        """
        if not dosing_actions:
            raise ValueError("No dosing action provided")
        action = dosing_actions[0]
        pump = action.get("pump_number") or action.get("pump")
        amount = action.get("dose_ml") or action.get("amount")
        if pump is None or amount is None:
            raise ValueError("Dosing action must include pump number and dose amount")
        
        # Create a new controller instance pointing to the device's HTTP endpoint
        controller = DeviceController(device_ip=http_endpoint)
        try:
            response = await controller.execute_dosing(pump, amount, combined=combined)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        
        logger.info(f"Sent dosing command to device {device_id}: {response}")
        return {
            "status": "command_sent",
            "device_id": device_id,
            "actions": dosing_actions
        }

    async def cancel_dosing(self, device_id: str) -> dict:
        # Cancellation is not supported; log and return a fixed response.
        logger.info(f"Cancellation requested for device {device_id}, but cancellation is not supported.")
        return {"status": "cancel_not_supported", "device_id": device_id}

# Create singleton instance
dose_manager = DoseManager()

async def execute_dosing_operation(device_id: str, http_endpoint: str, dosing_actions: list, combined: bool = False) -> dict:
    return await dose_manager.execute_dosing(device_id, http_endpoint, dosing_actions, combined)

async def cancel_dosing_operation(device_id: str) -> dict:
    return await dose_manager.cancel_dosing(device_id)
