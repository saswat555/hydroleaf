# app/services/ph_tds.py

import json
import logging
from typing import Dict
from app.services.mqtt import MQTTPublisher

logger = logging.getLogger(__name__)

class PHTDSReader:
    def __init__(self):
        self.mqtt_client = MQTTPublisher()
        self.latest_readings = {}

    async def setup_subscription(self, device_id: str):
        """Subscribe to the device's MQTT topic"""
        topic = f"krishiverse/devices/{device_id}/readings"
        
        def on_message(client, userdata, message):
            try:
                payload = json.loads(message.payload.decode())
                self.latest_readings[device_id] = {
                    "ph": payload.get("ph"),
                    "tds": payload.get("tds"),
                    "timestamp": payload.get("timestamp")
                }
                logger.info(f"Received readings from device {device_id}: {payload}")
            except json.JSONDecodeError as e:
                logger.error(f"Error decoding MQTT message: {e}")
            except Exception as e:
                logger.error(f"Error processing MQTT message: {e}")

        self.mqtt_client.client.subscribe(topic)
        self.mqtt_client.client.message_callback_add(topic, on_message)

    async def get_readings(self, device_id: str) -> Dict:
        """
        Get the latest readings for a specific device
        Returns a dictionary with ph and tds values
        """
        if device_id not in self.latest_readings:
            await self.setup_subscription(device_id)
            return {"ph": 7.0, "tds": 150}  # Default values while waiting for first reading
        
        return {
            "ph": self.latest_readings[device_id]["ph"],
            "tds": self.latest_readings[device_id]["tds"]
        }

    async def request_reading(self, device_id: str):
        """
        Request an immediate reading from the device
        """
        topic = f"krishiverse/devices/{device_id}/command"
        message = {
            "command": "read",
            "timestamp": None  # Will be added by the MQTT publisher
        }
        self.mqtt_client.publish(topic, message)

# Create a singleton instance
ph_tds_reader = PHTDSReader()

# Function to be used by other services
async def get_ph_tds_readings(device_id: str) -> Dict:
    return await ph_tds_reader.get_readings(device_id)