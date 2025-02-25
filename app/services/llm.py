# app/services/llm.py

import asyncio
import json
import logging
import re
from fastapi import HTTPException
import ollama
import time
from typing import Dict, List
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import httpx  # Import httpx for async HTTP requests
from app.models import Device

logger = logging.getLogger(__name__)

MODEL_NAME = "deepseek-r1:1.5b"
SESSION_MAX_DURATION = 1800  # 30 minutes in seconds

class DosingDevice:
    def __init__(self, device_id: str, pumps_config: Dict):
        """
        Initialize a dosing device with its pump configuration
        
        pumps_config format:
        {
            "pump1": {
                "chemical_name": "Nutrient A",
                "chemical_description": "Primary nutrients NPK"
            },
            ...
        }
        """
        self.device_id = device_id
        self.pumps_config = pumps_config

class DosingManager:
    def __init__(self):
        self.devices: Dict[str, DosingDevice] = {}

    def register_device(self, device_id: str, pumps_config: Dict, http_endpoint: str):
        """Register a new dosing device with its pump configuration"""
        self.devices[device_id] = DosingDevice(device_id, pumps_config)
        logger.info(f"Registered dosing device {device_id} with config: {pumps_config}")

    async def get_device(self, device_id: int, db: AsyncSession):
        """Retrieve the device from the database or raise an error."""
        result = await db.execute(select(Device).where(Device.id == device_id))
        device = result.scalar_one_or_none()

        if not device:
            raise ValueError(f"Dosing device {device_id} not registered in the database")

        return device  # Return the actual device instance from DB

# Create singleton instance
dosing_manager = DosingManager()
def build_dosing_prompt(device: Device, sensor_data: dict, plant_profile: dict) -> str:
    """
    Generate the dosing prompt based on the device instance fetched from the database.
    """
    if not device.pump_configurations:
        raise ValueError(f"Device {device.id} has no pump configurations available")

    pump_info = "\n".join([
        f"Pump {pump['pump_number']}: {pump['chemical_name']} - {pump.get('chemical_description', 'No description')}"
        for pump in device.pump_configurations
    ])

    plant_info = (
        f"Plant: {plant_profile['plant_name']}\n"
        f"Growth Stage: {plant_profile['growth_stage']} days from seeding "
        f"(seeded at {plant_profile['seeding_date']} days)\n"
        f"Location: {plant_profile.get('weather_locale', 'Unknown')}"
    )

    prompt = f"""
You are an expert hydroponic system manager. Based on the following information, determine optimal nutrient dosing amounts.

Current Sensor Readings:
- pH: {sensor_data.get('ph', 'Unknown')}
- TDS (PPM): {sensor_data.get('tds', 'Unknown')}

Plant Information:
{plant_info}

Available Dosing Pumps:
{pump_info}

Provide dosing recommendations in the following JSON format:
{{
    "actions": [
        {{
            "pump_number": 1,
            "chemical_name": "Nutrient A",
            "dose_ml": 50,
            "reasoning": "Brief explanation"
        }}
    ],
    "next_check_hours": 24
}}

Consider:
1. Current pH and TDS levels
2. Plant growth stage
3. Chemical interactions
4. Maximum safe dosing limits
""".strip()

    return prompt.strip()

async def call_llm_async(prompt: str) -> Dict:
    """
    Asynchronously call the local Ollama model and process the response.
    It now ensures the output is valid JSON.
    """
    logger.info(f"Sending prompt to LLM:\n{prompt}")
    
    try:
        response = await asyncio.to_thread(
            ollama.chat,
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}]
        )
        
        raw_content = response.get("message", {}).get("content", "").strip()
        logger.info(f"Raw LLM response: {raw_content}")

        # Remove <think>...</think> blocks
        content = re.sub(r'<think>.*?</think>', '', raw_content, flags=re.DOTALL).strip()
        
        # Ensure double quotes for JSON format
        content = content.replace("'", '"')  # Replace single quotes with double quotes
        
        # Try to parse JSON directly
        try:
            parsed_response = json.loads(content)
        except json.JSONDecodeError as e:
            # Extract the first valid JSON object
            match = re.search(r'({.*})', content, re.DOTALL)
            if match:
                content = match.group(1)
                parsed_response = json.loads(content)
            else:
                logger.error(f"LLM raw response could not be parsed: {raw_content}")
                raise ValueError(f"Invalid JSON response from LLM: {e}")
        
        validate_llm_response(parsed_response)
        return parsed_response,raw_content

    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        raise HTTPException(status_code=500, detail="Error processing LLM response")


def validate_llm_response(response: Dict):
    """
    Validate the LLM response format and values
    """
    if not isinstance(response, dict):
        raise ValueError("Response must be a dictionary")
    
    if "actions" not in response:
        raise ValueError("Response must contain 'actions' key")
    
    if not isinstance(response["actions"], list):
        raise ValueError("'actions' must be a list")
    
    for action in response["actions"]:
        required_keys = {"pump_number", "chemical_name", "dose_ml", "reasoning"}
        if not all(key in action for key in required_keys):
            raise ValueError(f"Action missing required keys: {required_keys}")
        
        if not isinstance(action["dose_ml"], (int, float)) or action["dose_ml"] < 0:
            raise ValueError("dose_ml must be a positive number")


async def execute_dosing_plan(device: Device, dosing_plan: Dict):
    """
    Execute the dosing plan by sending real HTTP requests to the pump controller.
    """
    if not device.http_endpoint:
        raise ValueError(f"Device {device.id} has no HTTP endpoint configured")

    # Prepare dosing message
    message = {
        "timestamp": datetime.utcnow().isoformat(),
        "device_id": device.id,
        "actions": dosing_plan["actions"],
        "next_check_hours": dosing_plan.get("next_check_hours", 24)
    }

    logger.info(f"Dosing plan generated for device {device.id}: {message}")

    # Send requests to activate pumps
    async with httpx.AsyncClient() as client:
        for action in dosing_plan["actions"]:
            pump_number = action["pump_number"]
            dose_ml = action["dose_ml"]

            http_endpoint = device.http_endpoint
            if not http_endpoint.startswith("http"):
                http_endpoint = f"http://{http_endpoint}" 
            try:
                response = await client.post(
                    f"{http_endpoint}/pump",
                    json={"pump": pump_number, "amount": int(dose_ml)},
                    timeout=10  # Set timeout to 10 seconds
                )

                response_data = response.json()

                if response.status_code == 200 and response_data.get("message") == "Pump started":
                    logger.info(f"✅ Pump {pump_number} activated successfully: {response_data}")
                else:
                    logger.error(f"❌ Failed to activate pump {pump_number}: {response_data}")

            except httpx.RequestError as e:
                logger.error(f"❌ HTTP request to pump {pump_number} failed: {e}")
                raise HTTPException(status_code=500, detail=f"Pump {pump_number} activation failed")

    return message  # Return the dosing execution summary

async def process_dosing_request(
    device_id: int,
    sensor_data: dict,
    plant_profile: dict,
    db: AsyncSession
) -> dict:
    """
    Process a complete dosing request from sensor data to execution.
    """
    try:
        # Fetch the device from the database
        device = await dosing_manager.get_device(device_id, db)

        if not device.pump_configurations:
            raise ValueError(f"Device {device.id} has no pump configurations available")

        if not device.http_endpoint:
            raise ValueError(f"Device {device.id} has no HTTP endpoint configured for pump control")

        # Generate the dosing prompt
        prompt = build_dosing_prompt(device, sensor_data, plant_profile)

        # Call LLM with the generated prompt
        dosing_plan, aiResponse = await call_llm_async(prompt)

        # Execute dosing plan (activate real pumps)
        result = await execute_dosing_plan(device, dosing_plan)

        return result,aiResponse

    except ValueError as ve:
        logger.error(f"ValueError in dosing request: {ve}")
        raise HTTPException(status_code=400, detail=str(ve))  # Convert ValueError to 400 error

    except json.JSONDecodeError as je:
        logger.error(f"JSON Parsing Error: {je}")
        raise HTTPException(status_code=500, detail="Invalid response format from LLM")

    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred")
