# app/services/llm.py

import asyncio
import json
import logging
import re
import ollama
import time
from typing import Dict, List
from datetime import datetime

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

    def get_device(self, device_id: str) -> DosingDevice:
        """Get a registered dosing device"""
        if device_id not in self.devices:
            raise ValueError(f"Dosing device {device_id} not registered")
        return self.devices[device_id]

# Create singleton instance
dosing_manager = DosingManager()

def build_dosing_prompt(device_id: str, sensor_data: dict, plant_profile: dict) -> str:
    """
    Build a comprehensive prompt for the LLM based on device configuration and sensor data
    """
    device = dosing_manager.get_device(device_id)
    
    # Build pump configuration string
    pump_info = "\n".join([
        f"Pump {idx + 1}: {config['chemical_name']} - {config['chemical_description']}"
        for idx, (_, config) in enumerate(device.pumps_config.items())
    ])

    # Build plant profile string
    plant_info = (
        f"Plant: {plant_profile['plant_name']}\n"
        f"Growth Stage: {plant_profile['current_age']} days from seeding "
        f"(seeded at {plant_profile['seeding_age']} days)\n"
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
            "dose_ml": 50.0,
            "reasoning": "Brief explanation"
        }},
        ...
    ],
    "next_check_hours": 24
}}

Consider:
1. Current pH and TDS levels
2. Plant growth stage
3. Chemical interactions
4. Maximum safe dosing limits
"""
    return prompt.strip()

async def call_llm_async(prompt: str) -> Dict:
    """
    Asynchronously call the local Ollama model and process the response.
    It now strips any <think>...</think> blocks and attempts to extract the first valid JSON object.
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
        
        # Remove any <think>...</think> blocks
        content = re.sub(r'<think>.*?</think>', '', raw_content, flags=re.DOTALL).strip()
        
        # Try to load JSON directly
        try:
            parsed_response = json.loads(content)
        except json.JSONDecodeError as e:
            # If there's extra data, try to extract the first JSON object.
            match = re.search(r'({.*})', content, re.DOTALL)
            if match:
                content = match.group(1)
                parsed_response = json.loads(content)
            else:
                logger.error(f"LLM raw response could not be parsed: {raw_content}")
                raise ValueError(f"Invalid JSON response from LLM: {e}")
        
        validate_llm_response(parsed_response)
        return parsed_response

    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        raise


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

async def execute_dosing_plan(device_id: str, dosing_plan: Dict):
    
    # Prepare dosing message
    message = {
        "timestamp": datetime.utcnow().isoformat(),
        "device_id": device_id,
        "actions": dosing_plan["actions"],
        "next_check_hours": dosing_plan.get("next_check_hours", 24)
    }
    
    
    logger.info(f"Dosing plan sent to device {device_id}: {message}")
    return message

async def process_dosing_request(
    device_id: str,
    sensor_data: Dict,
    plant_profile: Dict
) -> Dict:
    """
    Process a complete dosing request from sensor data to execution
    """
    # Build and send prompt to LLM
    prompt = build_dosing_prompt(device_id, sensor_data, plant_profile)
    dosing_plan = await call_llm_async(prompt)
    
    # Execute the dosing plan
    result = await execute_dosing_plan(device_id, dosing_plan)
    
    return result