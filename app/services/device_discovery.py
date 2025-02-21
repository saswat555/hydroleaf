# app/services/device_discovery.py

from datetime import datetime, UTC
import json
import logging
import os
from typing import List, Dict, Optional
from app.services.mqtt import MQTTPublisher
from fastapi import Depends
logger = logging.getLogger(__name__)
import time
from datetime import datetime, UTC
import json
import logging
import os
import asyncio
import uuid
from typing import List, Dict, Optional
from app.services.mqtt import MQTTPublisher
from fastapi import Depends

logger = logging.getLogger(__name__)
import time

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

        self.mqtt_client = MQTTPublisher()
        self.discovered_devices = {}
        self.device_status = {}
        self.pending_requests = {}

        # Define standard topics for device communication
        self.topics = {
            'discovery_request': 'krishiverse/discovery/request',
            'discovery_response': 'krishiverse/discovery/response',
            'status': 'krishiverse/+/status',
            'heartbeat': 'krishiverse/+/heartbeat',
            'command': 'krishiverse/+/command',
            'controller_status': 'krishiverse/controller/status'
        }

        # Configuration
        self.discovery_interval = int(os.getenv('MQTT_DISCOVERY_INTERVAL', 30))
        self.device_timeout = int(os.getenv('MQTT_DEVICE_TIMEOUT', 120))

        if not os.getenv("TESTING", "0") == "1":
            self._setup_listeners()
            self._start_periodic_discovery()
            self._start_device_cleanup()

        self.initialized = True

    @classmethod
    def initialize(cls, mqtt_client: Optional[MQTTPublisher] = None):
        """Initialize the service with a specific MQTT client"""
        cls._mqtt_client = mqtt_client or MQTTPublisher()
        return cls()

    def _setup_listeners(self):
        """Setup MQTT listeners for all device communications"""
        try:
            # Listen for device announcements
            self.mqtt_client.subscribe(self.topics['discovery_response'], self._handle_discovery_response)
            
            # Listen for device status updates
            self.mqtt_client.subscribe(self.topics['status'], self._handle_status_update)
            
            # Listen for device heartbeats
            self.mqtt_client.subscribe(self.topics['heartbeat'], self._handle_heartbeat)
            
            # Announce controller presence
            self._announce_controller()
            
            logger.info("Device discovery service initialized successfully")
        except Exception as e:
            logger.error(f"Failed to setup device discovery: {e}")

    def _announce_controller(self):
        """Announce controller presence"""
        message = {
            "status": "online",
            "timestamp": datetime.now(UTC).isoformat(),
            "controller_id": self.mqtt_client.client_id
        }
        self.mqtt_client.publish(
            self.topics['controller_status'],
            message,
            retain=True,
            qos=1
        )

    def _handle_discovery_response(self, client, userdata, message):
        """Handle device discovery responses"""
        try:
            payload = json.loads(message.payload.decode())
            device_id = payload.get('device_id')
            
            if not device_id:
                logger.warning("Received discovery response without device_id")
                return
                
            current_time = datetime.now(UTC)
            
            self.discovered_devices[device_id] = {
                **payload,
                'last_seen': current_time.isoformat(),
                'last_heartbeat': current_time.isoformat(),
                'status': 'online'
            }
            
            # Check if this was a response to a pending request
            request_id = payload.get('request_id')
            if request_id in self.pending_requests:
                self.pending_requests[request_id]['responded'].add(device_id)
                
            logger.info(f"Device discovered: {device_id}")
            
        except Exception as e:
            logger.error(f"Error handling discovery response: {e}")

    def _handle_status_update(self, client, userdata, message):
        """Handle device status updates"""
        try:
            topic_parts = message.topic.split('/')
            device_id = topic_parts[1]
            payload = json.loads(message.payload.decode())
            
            if device_id in self.discovered_devices:
                self.discovered_devices[device_id].update({
                    'status': payload.get('status', 'unknown'),
                    'last_seen': datetime.now(UTC).isoformat()
                })
                logger.info(f"Device status updated: {device_id} - {payload}")
        except Exception as e:
            logger.error(f"Error handling status update: {e}")

    def _handle_heartbeat(self, client, userdata, message):
        """Handle device heartbeat messages"""
        try:
            topic_parts = message.topic.split('/')
            device_id = topic_parts[1]
            payload = json.loads(message.payload.decode())
            
            current_time = datetime.now(UTC)
            
            if device_id in self.discovered_devices:
                self.discovered_devices[device_id].update({
                    'last_heartbeat': current_time.isoformat(),
                    'status': 'online'
                })
            else:
                # If device sends heartbeat but isn't discovered, request identification
                self.request_device_identification(device_id)
                
        except Exception as e:
            logger.error(f"Error handling heartbeat: {e}")

    def _start_periodic_discovery(self):
        """Start periodic device discovery"""
        def discovery_loop():
            while True:
                try:
                    self.broadcast_discovery()
                    time.sleep(self.discovery_interval)
                except Exception as e:
                    logger.error(f"Error in discovery loop: {e}")
                    time.sleep(5)

        import threading
        thread = threading.Thread(target=discovery_loop, daemon=True)
        thread.start()

    def _start_device_cleanup(self):
        """Start periodic cleanup of stale devices"""
        def cleanup_loop():
            while True:
                try:
                    self._cleanup_stale_devices()
                    time.sleep(30)  # Check every 30 seconds
                except Exception as e:
                    logger.error(f"Error in cleanup loop: {e}")
                    time.sleep(5)

        import threading
        thread = threading.Thread(target=cleanup_loop, daemon=True)
        thread.start()

    def _cleanup_stale_devices(self):
        """Remove stale devices"""
        current_time = datetime.now(UTC)
        stale_devices = []
        
        for device_id, device in self.discovered_devices.items():
            last_heartbeat = datetime.fromisoformat(device.get('last_heartbeat', '2000-01-01T00:00:00+00:00'))
            if (current_time - last_heartbeat).seconds > self.device_timeout:
                stale_devices.append(device_id)
                
        for device_id in stale_devices:
            self.discovered_devices.pop(device_id, None)
            logger.info(f"Removed stale device: {device_id}")

    def broadcast_discovery(self):
        """Broadcast discovery message"""
        request_id = str(uuid.uuid4())
        message = {
            "command": "identify",
            "source": "krishiverse_controller",
            "timestamp": datetime.now(UTC).isoformat(),
            "request_id": request_id
        }
        
        # Track this request
        self.pending_requests[request_id] = {
            'timestamp': datetime.now(UTC),
            'responded': set()
        }
        
        self.mqtt_client.publish(
            self.topics['discovery_request'],
            message,
            retain=True,
            qos=1
        )
        logger.debug(f"Discovery broadcast sent with request_id: {request_id}")

    def request_device_identification(self, device_id: str):
        """Request specific device to identify itself"""
        message = {
            "command": "identify",
            "timestamp": datetime.now(UTC).isoformat(),
            "request_id": str(uuid.uuid4())
        }
        self.mqtt_client.publish(f"krishiverse/{device_id}/identify", message, qos=1)

    async def scan_network(self):
        """Actively scan for devices"""
        try:
            # Clean up old pending requests
            current_time = datetime.now(UTC)
            self.pending_requests = {
                req_id: req for req_id, req in self.pending_requests.items()
                if (current_time - req['timestamp']).seconds < 60
            }
            
            # Broadcast new discovery request
            self.broadcast_discovery()
            
            # Wait briefly for responses
            await asyncio.sleep(2)
            
            # Return currently known devices with their status
            devices = [
                {
                    **device,
                    'online': (current_time - datetime.fromisoformat(device.get('last_heartbeat', '2000-01-01T00:00:00+00:00'))).seconds < self.device_timeout
                }
                for device in self.discovered_devices.values()
            ]
            
            return {"devices": devices}
        except Exception as e:
            logger.error(f"Error during device discovery: {e}")
            return {"devices": []}

    def get_device_info(self, device_id: str) -> Dict:
        """Get detailed information about a specific device"""
        if device_id not in self.discovered_devices:
            raise ValueError(f"Device {device_id} not found")
        return self.discovered_devices[device_id]

    def send_command(self, device_id: str, command: dict):
        """Send command to a specific device"""
        if device_id not in self.discovered_devices:
            raise ValueError(f"Device {device_id} not found")
            
        topic = f"krishiverse/{device_id}/command"
        self.mqtt_client.publish(topic, command, qos=1)
        logger.info(f"Command sent to device {device_id}: {command}")

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