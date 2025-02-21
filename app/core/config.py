# app/core/config.py

import os
from dotenv import load_dotenv

load_dotenv()

# Environment
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
TESTING = os.getenv("TESTING", "0") == "1"

# Database
if TESTING:
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
else:
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./krishiverse.db")

# MQTT Configuration
MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_CLIENT_ID_PREFIX = "krishiverse"

# API Configuration
API_V1_STR = "/api/v1"
PROJECT_NAME = "Krishiverse"