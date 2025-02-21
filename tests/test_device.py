# test_device.py
import paho.mqtt.client as mqtt
import json
import time
from datetime import datetime, UTC

# Device configuration
DEVICE_ID = "test_device_1"
BROKER_ADDRESS = "192.168.221.155"  # Your MQTT broker IP

def on_connect(client, userdata, flags, rc):
    print(f"Connected with result code {rc}")
    # Subscribe to discovery and command topics
    client.subscribe("krishiverse/discovery")
    client.subscribe(f"krishiverse/{DEVICE_ID}/command")

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        print(f"Received message on {msg.topic}: {payload}")
        
        if msg.topic == "krishiverse/discovery":
            # Respond to discovery
            response = {
                "device_id": DEVICE_ID,
                "type": "dosing_unit",
                "capabilities": ["ph", "tds", "dosing"],
                "status": "ready",
                "timestamp": datetime.now(UTC).isoformat()
            }
            client.publish("krishiverse/discovery/response", json.dumps(response))
            
    except Exception as e:
        print(f"Error processing message: {e}")

# Setup MQTT client
client = mqtt.Client()
client.on_connect = on_connect
client.on_message = on_message

# Connect to broker
print(f"Connecting to broker at {BROKER_ADDRESS}")
client.connect(BROKER_ADDRESS, 1883, 60)
client.loop_start()

# Send periodic status updates
try:
    while True:
        status = {
            "status": "online",
            "readings": {
                "ph": 7.0,
                "tds": 500
            },
            "timestamp": datetime.now(UTC).isoformat()
        }
        client.publish(f"krishiverse/{DEVICE_ID}/status", json.dumps(status))
        time.sleep(5)
except KeyboardInterrupt:
    print("Shutting down...")
    client.loop_stop()
    client.disconnect()