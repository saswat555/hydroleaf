# app/services/dose_manager.py

import asyncio
import json
import logging
from typing import List, Dict
from datetime import datetime
from app.services.mqtt import MQTTPublisher

logger = logging.getLogger(__name__)

class DoseManager:
    def __init__(self):
        self.mqtt_client = MQTTPublisher()
        self.active_operations = {}
        self.MAX_CONCURRENT_OPERATIONS = 3

    async def execute_dosing(self, device_id: str, dosing_actions: List[Dict]) -> Dict:
        """
        Execute dosing operations for a device
        
        dosing_actions format:
        [
            {
                "pump_number": 1,
                "dose_ml": 50.0,
                "chemical_name": "Nutrient A"
            },
            ...
        ]
        """
        if device_id in self.active_operations:
            raise ValueError(f"Device {device_id} already has active dosing operation")

        operation_id = f"dose_{device_id}_{int(datetime.utcnow().timestamp())}"
        
        try:
            # Prepare dosing message
            message = {
                "operation_id": operation_id,
                "actions": dosing_actions,
                "timestamp": datetime.utcnow().isoformat()
            }

            # Send dosing command
            topic = f"krishiverse/devices/{device_id}/command/dose"
            self.mqtt_client.publish(topic, message)

            # Track operation
            self.active_operations[device_id] = {
                "operation_id": operation_id,
                "status": "in_progress",
                "start_time": datetime.utcnow().isoformat(),
                "actions": dosing_actions
            }

            # Wait for completion confirmation
            await self._wait_for_completion(device_id, operation_id)

            return {
                "status": "completed",
                "device_id": device_id,
                "operation_id": operation_id,
                "actions": dosing_actions
            }

        except Exception as e:
            logger.error(f"Dosing operation failed for device {device_id}: {e}")
            if device_id in self.active_operations:
                del self.active_operations[device_id]
            raise

    async def _wait_for_completion(self, device_id: str, operation_id: str):
        """Wait for dosing operation completion"""
        completion_topic = f"krishiverse/devices/{device_id}/status/dose"
        completion_future = asyncio.Future()

        def on_completion(client, userdata, message):
            try:
                payload = json.loads(message.payload.decode())
                if payload.get("operation_id") == operation_id:
                    if payload.get("status") == "completed":
                        completion_future.set_result(True)
                    elif payload.get("status") == "error":
                        completion_future.set_exception(
                            Exception(f"Dosing failed: {payload.get('error')}")
                        )
            except Exception as e:
                logger.error(f"Error processing completion message: {e}")

        self.mqtt_client.subscribe(completion_topic, on_completion)

        try:
            await asyncio.wait_for(completion_future, timeout=300)  # 5 minutes timeout
        finally:
            self.mqtt_client.client.message_callback_remove(completion_topic)
            if device_id in self.active_operations:
                del self.active_operations[device_id]

    async def cancel_dosing(self, device_id: str):
        """Cancel active dosing operation"""
        if device_id not in self.active_operations:
            raise ValueError(f"No active dosing operation for device {device_id}")

        cancel_topic = f"krishiverse/devices/{device_id}/command/cancel"
        self.mqtt_client.publish(cancel_topic, {
            "operation_id": self.active_operations[device_id]["operation_id"],
            "timestamp": datetime.utcnow().isoformat()
        })

        del self.active_operations[device_id]

# Create singleton instance
dose_manager = DoseManager()

# Functions to be used by other services
async def execute_dosing_operation(device_id: str, dosing_actions: List[Dict]) -> Dict:
    return await dose_manager.execute_dosing(device_id, dosing_actions)

async def cancel_dosing_operation(device_id: str):
    await dose_manager.cancel_dosing(device_id)