# app/services/dose_manager.py

import logging
from datetime import datetime
from app.services.mqtt import MQTTPublisher

logger = logging.getLogger(__name__)

class DoseManager:
    def __init__(self):
        self.mqtt_client = MQTTPublisher()

    async def execute_dosing(self, device_id: str, dosing_actions: list) -> dict:
        """
        Execute a dosing command for a device.
        
        For compatibility with the previous API, we accept a device_id and a list
        of dosing_actions. However, since your ESP32 firmware is fixed, we expect
        a single dosing action. We extract the pump number and amount from the first
        element of dosing_actions and ignore any extra actions.
        
        The simplified payload format is:
            {
                "pump": <pump number>,       // from action["pump_number"] or action["pump"]
                "amount": <dose_ml>,         // from action["dose_ml"] or action["amount"]
                "timestamp": "<ISO timestamp>"
            }
        This message is published on the fixed topic "krishiverse/pump".
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
        topic = "krishiverse/pump"
        published = self.mqtt_client.publish(topic, payload)
        if not published:
            raise Exception("Failed to publish dosing command")
        logger.info(f"Published dosing command to {topic}: {payload}")
        # Return a response similar to the original structure
        return {
            "status": "command_sent",
            "device_id": device_id,
            "actions": dosing_actions
        }

    async def cancel_dosing(self, device_id: str) -> dict:
        """
        Cancel active dosing operation for a device.
        Since the ESP32 firmware does not support cancellation,
        this function simply logs the attempt.
        """
        logger.info(f"Cancellation requested for device {device_id}, but cancellation is not supported by the device firmware.")
        return {"status": "cancel_not_supported", "device_id": device_id}

# Create singleton instance
dose_manager = DoseManager()

async def execute_dosing_operation(device_id: str, dosing_actions: list) -> dict:
    return await dose_manager.execute_dosing(device_id, dosing_actions)

async def cancel_dosing_operation(device_id: str) -> dict:
    return await dose_manager.cancel_dosing(device_id)
