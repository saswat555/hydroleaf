# app/services/llm.py

import asyncio
import json
import logging
import re
from datetime import datetime
from fastapi import HTTPException
from typing import Dict, List, Union
import httpx
from bs4 import BeautifulSoup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

import ollama
from langchain_ollama import ChatOllama

from app.models import Device
from app.services.dose_manager import DoseManager
from .serper import fetch_search_results

logger = logging.getLogger(__name__)

# Model names and session duration
MODEL_1_5B = "deepseek-r1:1.5b"
MODEL_7B = "deepseek-r1:7b"
SESSION_MAX_DURATION = 1800  # 30 minutes in seconds

# Initialize separate ChatOllama clients for each model
ollama_client_1_5b = ChatOllama(base_url="http://localhost:11434", model=MODEL_1_5B)
ollama_client_7b = ChatOllama(base_url="http://localhost:11434", model=MODEL_7B)

# Create singleton instance for dose management
dosing_manager = DoseManager()


def enhance_query(user_query: str, plant_profile: dict) -> str:
    """
    Ensure the query includes relevant plant details such as name, type, growth stage, 
    seeding date, and location.
    """
    location = str(plant_profile.get("location", "Unknown"))
    plant_name = plant_profile.get("plant_name", "Unknown Plant")
    plant_type = plant_profile.get("plant_type", "Unknown Type")
    growth_stage = plant_profile.get("growth_stage", "Unknown Stage")
    seeding_date = plant_profile.get("seeding_date", "Unknown Date")

    additional_context = (
        f"What are the best practices in {location} for growing {plant_name} ({plant_type})? "
        f"Include information about optimal soil type, moisture levels, temperature range, "
        f"weather conditions, and safety concerns. Also, consider its growth stage "
        f"({growth_stage} days from seeding, seeded on {seeding_date})."
    )

    # Append details only if location is not already mentioned in the query
    if location.lower() not in user_query.lower():
        enhanced_query = f"{user_query}. {additional_context}"
    else:
        enhanced_query = user_query

    return enhanced_query


def parse_json_response(json_str: str) -> Union[List[str], dict]:
    """
    Parse a JSON response that may contain markdown bullet formatting.
    Returns a list of strings (each a cleaned line) if the parsed JSON is a string,
    or returns the parsed JSON object if it is already structured.
    """
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        # If parsing fails, fallback to the raw string
        data = json_str

    if isinstance(data, str):
        paragraphs = data.split("\n")
        result = []
        for para in paragraphs:
            if "**" in para:
                bullets = para.split("**")
                for bullet in bullets:
                    if bullet.strip():
                        result.append(f"- {bullet.strip()}")
            else:
                if para.strip():
                    result.append(para.strip())
        return result
    return data


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
        f"Plant Type: {plant_profile['plant_type']}\n"
        f"Growth Stage: {plant_profile['growth_stage']} days from seeding "
        f"(seeded at {plant_profile['seeding_date']})\n"
        f"Location: {plant_profile.get('location', 'Unknown')}"
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

    return prompt


async def build_plan_prompt(sensor_data: dict, plant_profile: dict, query: str) -> str:
    """
    Generate the growing plan prompt based on sensor data, plant profile, and user query.
    """
    plant_info = (
        f"Plant: {plant_profile['plant_name']}\n"
        f"Plant Type: {plant_profile['plant_type']}\n"
        f"Growth Stage: {plant_profile['growth_stage']} days from seeding "
        f"(seeded at {plant_profile['seeding_date']})\n"
        f"Region: {plant_profile.get('region', 'Unknown')}\n"
        f"Location: {plant_profile.get('location', 'Unknown')}"
    )

    promptPlan = f"""
You are an expert hydroponic system manager. Based on the following information, determine optimal nutrient dosing amounts.

Plant Information:
{plant_info}

Current Sensor Readings:
- pH: {sensor_data.get('pH', 'Unknown')}
- TDS (PPM): {sensor_data.get('TDS', 'Unknown')}

Provide an efficient and optimized solution according to the plant's location, local weather conditions, and soil conditions.

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

    enhanced_query = enhance_query(user_query=query, plant_profile=plant_profile)
    search_results = await fetch_search_results(enhanced_query)

    raw_info_list = [
        f"{entry['title']}: {entry['snippet']}"
        for entry in search_results.get("organic", [])
        if "title" in entry and "snippet" in entry
    ]

    raw_info = " ".join(raw_info_list) if raw_info_list else "No additional information available."
    soup = BeautifulSoup(raw_info, "html.parser")
    cleaned_snippet = soup.get_text(separator=" ")

    final_prompt = f"{promptPlan}\n\nAdditional Information:\n{cleaned_snippet}"

    return final_prompt.strip()


async def call_llm_async(prompt: str) -> (Dict, str):
    """
    Asynchronously call the local Ollama model (1.5B) and process the response.
    Ensures the output is valid JSON.
    """
    logger.info(f"Sending prompt to LLM:\n{prompt}")

    try:
        response = await asyncio.to_thread(
            ollama_client_1_5b.chat,
            messages=[{"role": "user", "content": prompt}]
        )

        raw_content = response.get("message", {}).get("content", "").strip()
        logger.info(f"Raw LLM response: {raw_content}")

        # Remove any <think>...</think> blocks
        content = re.sub(r'<think>.*?</think>', '', raw_content, flags=re.DOTALL).strip()
        content = content.replace("'", '"')  # Ensure JSON double quotes

        try:
            parsed_response = json.loads(content)
        except json.JSONDecodeError as e:
            # Extract the first valid JSON object if possible
            match = re.search(r'({.*})', content, re.DOTALL)
            if match:
                content = match.group(1)
                parsed_response = json.loads(content)
            else:
                logger.error(f"LLM raw response could not be parsed: {raw_content}")
                raise ValueError(f"Invalid JSON response from LLM: {e}")

        validate_llm_response(parsed_response)
        return parsed_response, raw_content

    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        raise HTTPException(status_code=500, detail="Error processing LLM response")


async def call_llm_plan(promptPlan: str) -> str:
    """
    Asynchronously call the local Ollama model (1.5B) to get the growing plan.
    """
    logger.info(f"Sending Search prompt to LLM:\n{promptPlan}")

    try:
        response = await asyncio.to_thread(
            ollama_client_1_5b.chat,
            messages=[{"role": "user", "content": promptPlan}]
        )

        raw_content = response.get("message", {}).get("content", "").strip()
        logger.info(f"Raw LLM response: {raw_content}")
        return raw_content

    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        raise HTTPException(status_code=500, detail="Error processing LLM response")


def validate_llm_response(response: Dict) -> None:
    """
    Validate the LLM response format and values.
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


async def execute_dosing_plan(device: Device, dosing_plan: Dict) -> Dict:
    """
    Execute the dosing plan by sending HTTP requests to the pump controller.
    """
    if not device.http_endpoint:
        raise ValueError(f"Device {device.id} has no HTTP endpoint configured")

    message = {
        "timestamp": datetime.utcnow().isoformat(),
        "device_id": device.id,
        "actions": dosing_plan["actions"],
        "next_check_hours": dosing_plan.get("next_check_hours", 24)
    }

    logger.info(f"Dosing plan generated for device {device.id}: {message}")

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
                    timeout=10
                )

                response_data = response.json()

                if response.status_code == 200 and response_data.get("message") == "Pump started":
                    logger.info(f"✅ Pump {pump_number} activated successfully: {response_data}")
                else:
                    logger.error(f"❌ Failed to activate pump {pump_number}: {response_data}")

            except httpx.RequestError as e:
                logger.error(f"❌ HTTP request to pump {pump_number} failed: {e}")
                raise HTTPException(status_code=500, detail=f"Pump {pump_number} activation failed")

    return message


async def getSensorData(device: Device) -> dict:
    """
    Retrieve sensor data by sending an HTTP GET request to the sensor controller.
    """
    if not device.http_endpoint:
        raise ValueError(f"Device {device.id} has no HTTP endpoint configured")

    logger.info(f"Fetching sensor readings for device {device.id}")

    async with httpx.AsyncClient() as client:
        http_endpoint = device.http_endpoint
        if not http_endpoint.startswith("http"):
            http_endpoint = f"http://{http_endpoint}"
        try:
            response = await client.get(
                f"{http_endpoint}/monitor",
                timeout=10
            )

            response_data = response.json()

            if response.status_code == 200:
                logger.info(f"pH and TDS readings fetched successfully: {response_data}")
            else:
                logger.error(f"❌ Failed to fetch readings: {response_data}")

        except httpx.RequestError as e:
            logger.error(f"❌ HTTP request to PH/TDS sensor failed: {e}")
            raise HTTPException(status_code=500, detail="PH/TDS reading request failed")

    return response_data


async def process_dosing_request(
    device_id: int,
    sensor_data: dict,
    plant_profile: dict,
    db: AsyncSession
) -> (dict, str):
    """
    Process a complete dosing request from sensor data to pump activation.
    """
    try:
        device = await dosing_manager.get_device(device_id, db)

        if not device.pump_configurations:
            raise ValueError(f"Device {device.id} has no pump configurations available")

        if not device.http_endpoint:
            raise ValueError(f"Device {device.id} has no HTTP endpoint configured for pump control")

        prompt = await build_dosing_prompt(device, sensor_data, plant_profile)
        dosing_plan, ai_response = await call_llm_async(prompt)
        result = await execute_dosing_plan(device, dosing_plan)

        return result, ai_response

    except ValueError as ve:
        logger.error(f"ValueError in dosing request: {ve}")
        raise HTTPException(status_code=400, detail=str(ve))
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
    query: str,  # now a string instead of dict for consistency
    db: AsyncSession
):
    """
    Process a complete sensor plan request and return a beautified plan.
    """
    try:
        device = await dosing_manager.get_device(device_id, db)
        if not device:
            raise ValueError("Device not available")

        if not device.http_endpoint:
            raise ValueError(f"Device {device.id} has no HTTP endpoint configured for sensor control")

        prompt = await build_plan_prompt(sensor_data, plant_profile, query)
        sensor_plan = await call_llm_plan(prompt)
        beautify_response = parse_json_response(sensor_plan)

        return beautify_response

    except ValueError as ve:
        logger.error(f"ValueError in sensor plan request: {ve}")
        raise HTTPException(status_code=400, detail=str(ve))
    except json.JSONDecodeError as je:
        logger.error(f"JSON Parsing Error: {je}")
        raise HTTPException(status_code=500, detail="Invalid response format from LLM")
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred")


async def call_llm(prompt: str, model_name: str) -> Dict:
    """
    Call a specific DeepSeek model via the appropriate Ollama client.
    """
    logger.info(f"Calling LLM Model {model_name} with prompt: {prompt}")

    # Choose the correct client based on the model name
    if model_name == MODEL_1_5B:
        client = ollama_client_1_5b
    elif model_name == MODEL_7B:
        client = ollama_client_7b
    else:
        client = ollama_client_1_5b  # Fallback to 1.5B if unspecified

    try:
        response = await asyncio.to_thread(
            client.chat,
            messages=[{"role": "user", "content": prompt}]
        )

        raw_content = response.get("message", {}).get("content", "").strip()
        logger.info(f"Raw LLM response: {raw_content}")

        # Ensure JSON format
        content = raw_content.replace("'", '"')
        try:
            parsed_response = json.loads(content)
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON response: {raw_content}")
            raise HTTPException(status_code=500, detail="Invalid LLM response format")

        return parsed_response

    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        raise HTTPException(status_code=500, detail="Error processing LLM response")


async def analyze_transport_options(origin: str, destination: str, weight_kg: float) -> Dict:
    """
    Use DeepSeek 1.5B to analyze transport options based on cost, distance, and feasibility.
    """
    prompt = f"""
    You are a logistics expert. Analyze the best railway and trucking options for transporting goods.
    - Origin: {origin}
    - Destination: {destination}
    - Weight: {weight_kg} kg

    Provide a JSON output with estimated cost, time, and best transport mode.
    """
    return await call_llm(prompt, MODEL_1_5B)


async def analyze_market_price(produce_type: str) -> Dict:
    """
    Use DeepSeek 1.5B to analyze the latest market price data.
    """
    prompt = f"""
    You are a market analyst. Provide the latest price per kg of {produce_type} in major cities.
    - Fetch the best available data.
    - Ensure JSON output with the most reliable price.
    """
    return await call_llm(prompt, MODEL_1_5B)


async def generate_final_decision(transport_analysis: Dict, market_price: Dict) -> Dict:
    """
    Use DeepSeek 7B to make a final recommendation on supply chain profitability.
    """
    prompt = f"""
    You are an AI supply chain consultant. Based on the transport analysis and market price insights, 
    determine if this transportation plan is profitable.

    Transport Analysis:
    {json.dumps(transport_analysis, indent=2)}

    Market Price Data:
    {json.dumps(market_price, indent=2)}

    Provide a JSON output with the final decision and reasoning.
    """
    return await call_llm(prompt, MODEL_7B)
