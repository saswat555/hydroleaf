import json
import logging
import os
import time
import subprocess
import platform
import uuid
from typing import Optional, Callable, Dict, Any
from paho.mqtt import client as mqtt_client
from datetime import datetime, UTC

logger = logging.getLogger(__name__)

class MQTTBrokerManager:
    def __init__(self):
        self.system = platform.system()
        self.broker_process = None
        self.config_path = self._get_config_path()

    def _get_config_path(self) -> str:
        """Get the appropriate Mosquitto config path for the current OS"""
        if self.system == "Darwin":  # macOS
            return "/usr/local/etc/mosquitto/mosquitto.conf"
        elif self.system == "Linux":
            return "/etc/mosquitto/mosquitto.conf"
        else:
            raise NotImplementedError(f"Unsupported operating system: {self.system}")

    def _create_default_config(self):
        """Create a default Mosquitto configuration if none exists"""
        default_config = """
# Default mosquitto configuration for Krishiverse
listener 1883
allow_anonymous true
persistence true
persistence_location /var/lib/mosquitto/
log_dest stdout
log_dest file /var/log/mosquitto/mosquitto.log
connection_messages true
"""
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, 'w') as f:
                f.write(default_config)
            logger.info(f"Created default Mosquitto configuration at {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to create Mosquitto configuration: {e}")

    def start_broker(self) -> bool:
        """Start the Mosquitto broker with proper configuration"""
        try:
            # Check if mosquitto is installed
            mosquitto_check = subprocess.run(['which', 'mosquitto'], capture_output=True)
            if mosquitto_check.returncode != 0:
                if self.system == "Darwin":
                    logger.error("Mosquitto not installed. Please install using 'brew install mosquitto'")
                else:
                    logger.error("Mosquitto not installed. Please install using 'sudo apt-get install mosquitto'")
                return False

            # Ensure config exists
            if not os.path.exists(self.config_path):
                self._create_default_config()

            # Start mosquitto broker
            self.broker_process = subprocess.Popen(
                ['mosquitto', '-c', self.config_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            # Check if broker started successfully
            time.sleep(1)
            if self.broker_process.poll() is None:
                logger.info("MQTT Broker started successfully")
                return True
            else:
                stderr = self.broker_process.stderr.read().decode()
                logger.error(f"Failed to start MQTT broker: {stderr}")
                return False

        except Exception as e:
            logger.error(f"Failed to start MQTT broker: {e}")
            return False

    def stop_broker(self):
        """Stop the Mosquitto broker gracefully"""
        if self.broker_process:
            try:
                self.broker_process.terminate()
                self.broker_process.wait(timeout=5)
                logger.info("MQTT Broker stopped gracefully")
            except subprocess.TimeoutExpired:
                self.broker_process.kill()
                logger.warning("MQTT Broker forcefully terminated")
            finally:
                self.broker_process = None

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

        # Configuration
        self.broker = os.getenv("MQTT_BROKER", "0.0.0.0")  # Listen on all interfaces
        self.port = int(os.getenv("MQTT_PORT", 1883))
        self.client_id = f"krishiverse_publisher_{uuid.uuid4()}"
        self.username = os.getenv("MQTT_USERNAME")
        self.password = os.getenv("MQTT_PASSWORD")
        
        # State tracking
        self.topics = {}
        self.subscribed_topics: Dict[str, Callable] = {}
        self.connected = False
        self.published_messages = []
        self.connection_retries = 0
        self.max_retries = 5
        self.retry_delay = 2  # seconds
        
        # Initialize broker and client
        if os.getenv("TESTING", "0") == "1":
            self.connected = True
            self.client = None
        else:
            self.broker_manager = MQTTBrokerManager()
            self.client = self._create_mqtt_client()
            self.initialize_connection()
        
        self.initialized = True

    def _create_mqtt_client(self) -> mqtt_client.Client:
        """Create and configure MQTT client"""
        client = mqtt_client.Client(
            client_id=self.client_id,
            protocol=mqtt_client.MQTTv5
        )
        
        if self.username and self.password:
            client.username_pw_set(self.username, self.password)
    
        # Set callbacks
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.on_publish = self._on_publish
    
        # Configure MQTT v5.0 specific properties
        client.clean_start = True  # This replaces clean_session
        
        # Set last will and testament
        will_message = {
            "status": "offline",
            "timestamp": datetime.now(UTC).isoformat()
        }
        client.will_set(
            "krishiverse/controller/status",
            payload=json.dumps(will_message),
            qos=1,
            retain=True
        )
    
        return client

    def subscribe(self, topic: str, callback: Optional[Callable] = None) -> bool:
        """Subscribe to a topic with optional callback"""
        if os.getenv("TESTING", "0") == "1":
            self.subscribed_topics[topic] = callback
            logger.info(f"Test mode: Subscribed to topic: {topic}")
            return True

        try:
            if not self.connected:
                self._connect()

            if callback:
                self.client.message_callback_add(topic, callback)
            result = self.client.subscribe(topic, qos=1)
            
            if result[0] == 0:
                self.subscribed_topics[topic] = callback
                logger.info(f"Subscribed to topic: {topic}")
                return True
            else:
                logger.error(f"Failed to subscribe to topic {topic}: {result}")
                return False
        except Exception as e:
            logger.error(f"Error subscribing to topic {topic}: {e}")
            return False

    def publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 1) -> bool:
        """Publish a message to a topic"""
        if isinstance(payload, dict) and 'timestamp' not in payload:
            payload['timestamp'] = datetime.now(UTC).isoformat()

        if os.getenv("TESTING", "0") == "1":
            self.published_messages.append({
                "topic": topic,
                "payload": payload,
                "qos": qos,
                "retain": retain
            })
            logger.info(f"Test mode: Published to {topic}: {payload}")
            return True

        try:
            if not self.connected:
                self._connect()

            message = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)
            result = self.client.publish(topic, message, qos=qos, retain=retain)
            
            if result[0] == 0:
                logger.debug(f"Published to {topic}: {message}")
                return True
            else:
                logger.error(f"Failed to publish to {topic}: {result}")
                return False
        except Exception as e:
            logger.error(f"Error publishing to {topic}: {e}")
            return False

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

    def _connect(self) -> bool:
        """Establish connection to MQTT broker with retry logic"""
        while self.connection_retries < self.max_retries and not self.connected:
            try:
                self.client.connect(self.broker, self.port)
                self.client.loop_start()
                
                # Wait for connection
                retry = 0
                while not self.connected and retry < 5:
                    time.sleep(1)
                    retry += 1

                if self.connected:
                    self.connection_retries = 0
                    return True
                    
            except Exception as e:
                self.connection_retries += 1
                logger.error(f"MQTT connection attempt {self.connection_retries} failed: {e}")
                time.sleep(self.retry_delay)

        if not self.connected:
            logger.error("Failed to establish MQTT connection after maximum retries")
            return False
        
        return True

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """Callback for when the client connects to the broker"""
        if rc == 0:
            self.connected = True
            logger.info("Connected to MQTT Broker!")
            
            # Resubscribe to all topics
            for topic, callback in self.subscribed_topics.items():
                self.client.subscribe(topic, qos=1)
                if callback:
                    self.client.message_callback_add(topic, callback)
        else:
            logger.error(f"Failed to connect to MQTT Broker, return code {rc}")

    def _on_disconnect(self, client, userdata, rc, properties=None):
        """Callback for when the client disconnects from the broker"""
        self.connected = False
        logger.warning(f"Disconnected from MQTT Broker with code: {rc}")
        
        # Attempt reconnection if not shutting down
        if rc != 0:
            self._connect()

    def _on_message(self, client, userdata, msg):
        """Default callback for message reception"""
        logger.debug(f"Received message on topic {msg.topic}: {msg.payload.decode()}")

    def _on_publish(self, client, userdata, mid):
        """Callback for successful message publication"""
        logger.debug(f"Message {mid} published successfully")

    def cleanup(self):
        """Cleanup MQTT connection and broker"""
        if os.getenv("TESTING", "0") == "1":
            return
            
        if self.client:
            try:
                # Publish offline status
                offline_message = {
                    "status": "offline",
                    "timestamp": datetime.now(UTC).isoformat()
                }
                self.publish(
                    "krishiverse/controller/status",
                    offline_message,
                    retain=True,
                    qos=1
                )
                
                self.client.loop_stop()
                self.client.disconnect()
                logger.info("MQTT client disconnected")
            except Exception as e:
                logger.warning(f"Error during MQTT cleanup: {e}")
            
        if hasattr(self, 'broker_manager'):
            self.broker_manager.stop_broker()

    def __del__(self):
        """Destructor to ensure cleanup"""
        self.cleanup()