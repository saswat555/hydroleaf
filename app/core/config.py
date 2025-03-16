# app/core/config.py

import os
from dotenv import load_dotenv

load_dotenv()

# Environment
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
TESTING = os.getenv("TESTING", "0") == "1"
DEPLOYMENT_MODE = os.getenv("DEPLOYMENT_MODE", "LAN").upper()  # Valid values: "LAN" or "CLOUD"

# Database
if TESTING:
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
else:
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./krishiverse.db")


# API Configuration
API_V1_STR = "/api/v1"
PROJECT_NAME = "Krishiverse"