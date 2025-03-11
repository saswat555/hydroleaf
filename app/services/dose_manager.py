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

        async def cancel_dosing(self, device_id: str, http_endpoint: str) -> dict:
        # Create a controller instance for the device.
            controller = DeviceController(device_ip=http_endpoint)
            response = await controller.cancel_dosing()
            logger.info(f"Cancellation response for device {device_id}: {response}")
            return {"status": "dosing_cancelled", "device_id": device_id, "response": response}


# Create singleton instance
dose_manager = DoseManager()

async def execute_dosing_operation(device_id: str, http_endpoint: str, dosing_actions: list, combined: bool = False) -> dict:
    return await dose_manager.execute_dosing(device_id, http_endpoint, dosing_actions, combined)

async def cancel_dosing_operation(device_id: int, http_endpoint: str) -> dict:
    return await dose_manager.cancel_dosing(str(device_id), http_endpoint)

