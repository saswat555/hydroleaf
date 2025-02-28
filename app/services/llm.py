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
from app.services.dose_manager import DosingManager
from .serper import fetch_search_results
from bs4 import BeautifulSoup



logger = logging.getLogger(__name__)

MODEL_NAME = "deepseek-r1:1.5b"
SESSION_MAX_DURATION = 1800  # 30 minutes in seconds

# Create singleton instance
dosing_manager = DosingManager()

def enhance_query(user_query: str, plant_profile: dict) -> str:

    """
    Ensure the query includes relevant plant details such as name, type, growth stage, 
    seeding date, and location.
    """

    location = plant_profile.get("location", "Unknown")
    plant_name = plant_profile.get("plant_name", "Unknown Plant")
    plant_type = plant_profile.get("plant_type", "Unknown Type")
    growth_stage = plant_profile.get("growth_stage", "Unknown Stage")
    seeding_date = plant_profile.get("seeding_date", "Unknown Date")

    additional_context = (
        f"What are the best practices in {location} for growing {plant_name} ({plant_type}) ? "
        f"Include information about optimal soil type, moisture levels, temperature range, "
        f"weather conditions, and safety concerns. Also, consider its growth stage ({growth_stage} days from seeding, seeded on {seeding_date})."
    )

    if not isinstance(location, str):
        location = str(location)

    
    # Check if the query already contains location info, otherwise append details
    if location.lower() not in user_query.lower():
        enhanced_query = f"{user_query}. {additional_context}"
    else:
        enhanced_query = user_query  # If location is already included, don't repeat
    
    return enhanced_query

def parse_json_response(json_str):

    data = json.loads(json_str)

    paragraphs = data.split("\n")

    result = {}

    for para in paragraphs:
        if "**" in para:
            bullets = para.split("**")
            for bullet in bullets:
                if bullet.strip():
                    result.append(f"-{bullet.strip()}")  
        else:
            result.append(para)
    return result

async def build_dosing_prompt(device: Device, sensor_data: dict, plant_profile: dict) -> str:
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
        f"Plant Type: {plant_profile['plant_type']} "
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

async def build_plan_prompt(sensor_data: dict , plant_profile: dict, query: str) -> str:
    """
    Generate the dosing prompt based on the device instance fetched from the database.
    """

    plant_info = (
        f"Plant: {plant_profile['plant_name']}\n"
        f"Plant Type: {plant_profile['plant_type']} "
        f"Growth Stage: {plant_profile['growth_stage']} days from seeding "
        f"(seeded at {plant_profile['seeding_date']} days)\n"
        f"Region: {plant_profile.get('region', 'Unknown')}"
        f"Location: {plant_profile.get('location', 'Unknown')}"
    )

    promptPlan = f"""
You are an expert hydroponic system manager. Based on the following information, determine optimal nutrient dosing amounts.

Plant Information:
{plant_info}

Current Sensor Readings:
- pH: {sensor_data.get('pH', 'Unknown')}
- TDS (PPM): {sensor_data.get('TDS', 'Unknown')}


Provide efficient and optimised solution according to plants Place, its Weather Conditions in the given region or location if present and soil conditions.

Consider:
1. Place of planting
2. Plant growth stage
3. Chemical interactions
4. Maximum safe dosing limits

Provide a detailed growing plan for {plant_profile['plant_name']} based on the {plant_profile['location']}. Include the best months for planting and the total growing duration. Specify pH and TDS requirements based on the local soil and water conditions. If the query mentions 'seeding' or 'growing,' tailor the plan accordingly. Break down the process into clear steps, covering:

1. Ideal Planting Time - Best months for planting in the given location.
2. Growth Duration - Total time needed from planting to harvest.
3. Soil and Water Conditions - Required pH and TDS levels based on local conditions.
4. Seeding Stage (if applicable) - Step-by-step guide for seed germination, soil preparation, and watering needs.
5. Growing Stage - Proper care, sunlight, nutrients, pruning, and maintenance.
6. Harvesting Time - Signs of maturity and best practices for harvesting.
7. Additional Tips - Common challenges, pest control, and climate-specific recommendations.

""".strip()
    
    enhanced_query = enhance_query(user_query=query , plant_profile=plant_profile)
    
    # Await the API call using our fetch_search_results function
    search_results = await fetch_search_results(enhanced_query)
    
    raw_info_list = [
        f"{entry['title']}: {entry['snippet']}"
        for entry in search_results.get("organic", [])
        if "title" in entry and "snippet" in entry
    ]

    # Join all snippets together for better context
    raw_info = " ".join(raw_info_list) if raw_info_list else "No additional information available."

    # Process with BeautifulSoup (if necessary)
    soup = BeautifulSoup(raw_info, "html.parser")
    cleaned_snippet = soup.get_text(separator=" ")

    # Combine the original prompt with the additional information
    final_prompt = f"{promptPlan}\n\nAdditional Information:\n{cleaned_snippet}"
    
    
    return final_prompt.strip()

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

async def call_llm_plan(promptPlan: str) -> Dict:
    """
    Asynchronously call the local Ollama model and process the response.
    It now ensures the output is valid JSON.
    """
    logger.info(f"Sending Search prompt to LLM:\n{promptPlan}")
    
    try:
        response = await asyncio.to_thread(
            ollama.chat,
            model=MODEL_NAME,
            messages=[{"role": "user", "content": promptPlan}]
        )
        
        raw_content = response.get("message", {}).get("content", "").strip()
        logger.info(f"Raw LLM response: {raw_content}")

        
        return raw_content

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


async def getSensorData(device: Device):
    """
    Execute the dosing plan by sending real HTTP requests to the pump controller.
    """
    if not device.http_endpoint:
        raise ValueError(f"Device {device.id} has no HTTP endpoint configured")

    logger.info(f"Soil Ingredients plan generated for device {device.id}")

    # Send requests to activate pumps
    async with httpx.AsyncClient() as client:

            http_endpoint = device.http_endpoint
            if not http_endpoint.startswith("http"):
                http_endpoint = f"http://{http_endpoint}" 
            try:
                response = await client.get(
                    f"{http_endpoint}/monitor",
                    timeout=10  # Set timeout to 10 seconds
                )

                response_data = response.json()

                if response.status_code == 200 :
                    logger.info(f"PH and Tds readings fetched successfully: {response_data}")
                else:
                    logger.error(f"❌ Failed to Fetch Readings : {response_data}")

            except httpx.RequestError as e:
                logger.error(f"❌ HTTP request to PH/TDS sensor failed: {e}")
                raise HTTPException(status_code=500, detail=f"PH/TDS reading request failed")

    return response_data  # Return the Reading summary

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
        prompt = await build_dosing_prompt(device, sensor_data, plant_profile)

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

async def process_sensor_plan(
        device_id: int,
        sensor_data: dict,
        plant_profile: dict,
        query: dict,
        db: AsyncSession
):
    """
    Process a complete dosing request from sensor data to execution.
    """
    try:
        # Fetch the device from the database
        device = await dosing_manager.get_device(device_id, db)

        if not device:
            raise ValueError(f"Device {device.id}  available")

        if not device.http_endpoint:
            raise ValueError(f"Device {device.id} has no HTTP endpoint configured for sensor control")
        

        # Generate the dosing prompt
        prompt = await build_plan_prompt(sensor_data, plant_profile ,query )

        # Call LLM with the generated prompt
        sensor_plan = await call_llm_plan(promptPlan = prompt)

        # beautify_response = parse_json_response(sensor_plan)

        return sensor_plan

    except ValueError as ve:
        logger.error(f"ValueError in dosing request: {ve}")
        raise HTTPException(status_code=400, detail=str(ve))  # Convert ValueError to 400 error

    except json.JSONDecodeError as je:
        logger.error(f"JSON Parsing Error: {je}")
        raise HTTPException(status_code=500, detail="Invalid response format from LLM")

    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred")