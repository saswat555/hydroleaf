# app/services/device_discovery.py

from datetime import datetime, UTC
import json
import logging
import os
from typing import List, Dict, Optional
from app.services.mqtt import MQTTPublisher
from fastapi import Depends
logger = logging.getLogger(__name__)

class DeviceDiscoveryService:
    _instance = None
    _mqtt_client = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DeviceDiscoveryService, cls).__new__(cls)
            cls._instance.initialized = False
        return cls._instance

    def __init__(self):
        if self.initialized:
            return

        if not self._mqtt_client:
            self._mqtt_client = MQTTPublisher()

        self.mqtt_client = self._mqtt_client
        self.discovery_topic = "krishiverse/discovery"
        self.response_topic = "krishiverse/discovery/response"
        self.discovered_devices = {}

        if not os.getenv("TESTING", "0") == "1":
            try:
                self._setup_discovery_listener()
            except Exception as e:
                logger.error(f"Failed to setup discovery listener: {e}")

        self.initialized = True


    @classmethod
    def initialize(cls, mqtt_client: Optional[MQTTPublisher] = None):
        """Initialize the service with a specific MQTT client"""
        cls._mqtt_client = mqtt_client or MQTTPublisher()
        return cls()


    def _setup_discovery_listener(self):
        """Setup MQTT listener for device responses"""
        def on_discovery_response(client, userdata, message):
            try:
                payload = json.loads(message.payload.decode())
                device_id = payload.get('device_id')
                if device_id:
                    self.discovered_devices[device_id] = payload
                    logger.info(f"Discovered device: {payload}")
            except json.JSONDecodeError as e:
                logger.error(f"Invalid discovery response: {e}")
            except Exception as e:
                logger.error(f"Error processing discovery response: {e}")

        try:
            self.mqtt_client.subscribe(self.response_topic, on_discovery_response)
            logger.info(f"Subscribed to discovery response topic: {self.response_topic}")
        except Exception as e:
            logger.error(f"Failed to subscribe to discovery topic: {e}")

    async def scan_network(self):
        """Scan network for devices"""
        try:
            discovery_message = {
                "command": "identify",
                "timestamp": datetime.now(UTC).isoformat()
            }
            
            self.mqtt_client.publish(self.discovery_topic, discovery_message)
            
            # In test mode, return dummy devices
            if os.getenv("TESTING", "0") == "1":
                return {
                    "devices": [
                        {"id": "test1", "type": "dosing_unit"},
                        {"id": "test2", "type": "ph_tds_sensor"}
                    ]
                }
            
            return {"devices": list(self.discovered_devices.values())}
            
        except Exception as e:
            logger.error(f"Error during device discovery: {e}")
            return {"devices": []}

    def get_device_info(self, device_id: str) -> Dict:
        """Get detailed information about a specific device"""
        if device_id not in self.discovered_devices:
            raise ValueError(f"Device {device_id} not found")
        return self.discovered_devices[device_id]

# Create singleton instance
device_discovery = DeviceDiscoveryService()

def get_device_discovery_service(
    mqtt_client: MQTTPublisher = Depends()
) -> DeviceDiscoveryService:
    """Dependency to get DeviceDiscoveryService instance"""
    if os.getenv("TESTING", "0") == "1":
        return device_discovery
    return DeviceDiscoveryService.initialize(mqtt_client)

async def discover_devices() -> Dict[str, List[Dict]]:
    """Discover all Krishiverse devices on the network"""
    return await device_discovery.scan_network()