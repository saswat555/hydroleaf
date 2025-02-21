# app/services/mqtt.py

import json
import logging
import os
import time
import subprocess
import platform
from typing import Optional, Callable
from paho.mqtt import client as mqtt_client
from datetime import datetime

logger = logging.getLogger(__name__)

class MQTTBrokerManager:
    def __init__(self):
        self.system = platform.system()
        self.broker_process = None

    def start_broker(self):
        """Start the Mosquitto broker based on the operating system"""
        try:
            if self.system == "Darwin":  # macOS
                # Check if mosquitto is installed
                if subprocess.run(['which', 'mosquitto'], capture_output=True).returncode != 0:
                    logger.error("Mosquitto not installed. Please install using 'brew install mosquitto'")
                    return False
                
                # Start mosquitto broker
                self.broker_process = subprocess.Popen(
                    ['mosquitto', '-c', '/usr/local/etc/mosquitto/mosquitto.conf'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
            
            elif self.system == "Linux":  # Raspberry Pi
                # Check if mosquitto is installed
                if subprocess.run(['which', 'mosquitto'], capture_output=True).returncode != 0:
                    logger.error("Mosquitto not installed. Please install using 'sudo apt-get install mosquitto'")
                    return False
                
                # Start mosquitto broker
                self.broker_process = subprocess.Popen(
                    ['mosquitto', '-c', '/etc/mosquitto/mosquitto.conf'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
            
            logger.info("MQTT Broker started successfully")
            return True
        
        except Exception as e:
            logger.error(f"Failed to start MQTT broker: {e}")
            return False

    def stop_broker(self):
        """Stop the Mosquitto broker"""
        if self.broker_process:
            self.broker_process.terminate()
            self.broker_process = None
            logger.info("MQTT Broker stopped")

# app/services/mqtt.py

class MQTTPublisher:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MQTTPublisher, cls).__new__(cls)
            cls._instance.initialized = False
        return cls._instance

    def __init__(self):
        if self.initialized:
            return

        self.broker = os.getenv("MQTT_BROKER", "localhost")
        self.port = int(os.getenv("MQTT_PORT", 1883))
        self.client_id = f"krishiverse_publisher_{int(time.time())}"
        self.username = os.getenv("MQTT_USERNAME")
        self.password = os.getenv("MQTT_PASSWORD")
        self.topics = {}
        self.subscribed_topics = {}  # Add this line
        self.connected = False
        self.published_messages = []
        
        # Initialize in test mode or normal mode
        if os.getenv("TESTING", "0") == "1":
            self.connected = True
            self.client = None
        else:
            self.broker_manager = MQTTBrokerManager()
            self.client = mqtt_client.Client(
                client_id=self.client_id,
                protocol=mqtt_client.MQTTv5
            )
            
            if self.username and self.password:
                self.client.username_pw_set(self.username, self.password)

            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
            
            self.initialize_connection()
        
        self.initialized = True
    def subscribe(self, topic: str, callback: Optional[Callable] = None):
        """Subscribe to a topic with optional callback"""
        self.subscribed_topics[topic] = callback
        
        if os.getenv("TESTING", "0") == "1":
            logger.info(f"Test mode: Subscribed to topic: {topic}")
            return
            
        if self.client:
            if callback:
                self.client.message_callback_add(topic, callback)
            self.client.subscribe(topic)
            logger.info(f"Subscribed to topic: {topic}")



    def publish(self, topic: str, payload: dict, qos: int = 1):
        """Publish a message to a topic"""
        if isinstance(payload, dict) and 'timestamp' not in payload:
            payload['timestamp'] = datetime.now(UTC).isoformat()

        # In test mode, just store the message
        if os.getenv("TESTING", "0") == "1":
            self.published_messages.append({
                "topic": topic,
                "payload": payload,
                "qos": qos
            })
            logger.info(f"Test mode: Published to {topic}: {payload}")
            return [0, 1]  # Return success code

        if not self.connected:
            self._connect()

        message = json.dumps(payload)
        result = self.client.publish(topic, message, qos=qos)
        
        if result[0] == 0:
            logger.info(f"Published to {topic}: {message}")
        else:
            logger.error(f"Failed to publish to {topic}")
            raise Exception(f"MQTT publish failed with code {result[0]}")
        
        return result

    def initialize_connection(self):
        """Initialize MQTT connection and broker if needed"""
        if os.getenv("TESTING", "0") == "1" or os.getenv("SKIP_MQTT", "0") == "1":
            self.connected = True
            logger.info("Skipping MQTT connection in TESTING/SKIP_MQTT mode")
            return

        # Start broker if it's not running
        if not self._check_broker_running():
            self.broker_manager.start_broker()
            time.sleep(2)  # Wait for broker to start

        self._connect()

    def _check_broker_running(self) -> bool:
        """Check if MQTT broker is running"""
        try:
            result = subprocess.run(
                ['pgrep', 'mosquitto'],
                capture_output=True,
                text=True
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Error checking broker status: {e}")
            return False

    def _connect(self):
        """Establish connection to MQTT broker"""
        try:
            self.client.connect(self.broker, self.port)
            self.client.loop_start()
            retry = 0
            while not self.connected and retry < 5:
                time.sleep(1)
                retry += 1
            if not self.connected:
                logger.warning("MQTT connection not established after retries")
        except Exception as e:
            logger.error(f"MQTT connection failed: {e}")

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """Callback for when the client connects to the broker"""
        if rc == 0:
            self.connected = True
            logger.info("Connected to MQTT Broker!")
            # Resubscribe to all topics
            for topic, callback in self.topics.items():
                self.subscribe(topic, callback)
        else:
            logger.error(f"Failed to connect to MQTT Broker, return code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        """Callback for when the client disconnects from the broker"""
        self.connected = False
        logger.warning(f"Disconnected from MQTT Broker with code: {rc}")

    def _on_message(self, client, userdata, msg):
        """Default callback for message reception"""
        logger.debug(f"Received message on topic {msg.topic}: {msg.payload.decode()}")

    def cleanup(self):
        """Cleanup MQTT connection and broker"""
        if os.getenv("TESTING", "0") == "1":
            return
            
        if self.client:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception as e:
                logger.warning(f"Error during MQTT cleanup: {e}")
            
        if hasattr(self, 'broker_manager'):
            self.broker_manager.stop_broker()