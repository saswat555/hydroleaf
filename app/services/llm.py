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

from app.models import Device
from app.services.dose_manager import DoseManager
from .serper import fetch_search_results

logger = logging.getLogger(__name__)

# Example local Ollama endpoint
OLLAMA_URL = "http://localhost:11434/api/generate"

# Example models
MODEL_1_5B = "deepseek-r1:1.5b"
MODEL_7B = "deepseek-r1:7b"

dosing_manager = DoseManager()

def enhance_query(user_query: str, plant_profile: dict) -> str:
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
    if location.lower() not in user_query.lower():
        return f"{user_query}. {additional_context}"
    return user_query

def parse_json_response(json_str: str) -> Union[List[str], dict]:
    """
    Parse the provided string as JSON. If parsing fails, return a list of cleaned text lines.
    """
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
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

def parse_ollama_response(raw_response: str) -> str:
    """
    Remove any <think> block from the raw response.
    Returns the cleaned text for automated processing.
    """
    cleaned = re.sub(r"<think>.*?</think>", "", raw_response, flags=re.DOTALL).strip()
    return cleaned

async def build_dosing_prompt(device: Device, sensor_data: dict, plant_profile: dict) -> str:
    """
    Creates a text prompt that asks Ollama for a JSON-based dosing plan.
    """
    if not device.pump_configurations:
        raise ValueError(f"Device {device.id} has no pump configurations available")

    pump_info = "\n".join([
        f"Pump {pump['pump_number']}: {pump['chemical_name']} - {pump.get('chemical_description', 'No description')}"
        for pump in device.pump_configurations
    ])
    plant_info = (
    f"Plant: {plant_profile.get('plant_name', 'Unknown')}\n"
    f"Type: {plant_profile.get('plant_type', 'Unknown')}\n"
    f"Growth Stage: {plant_profile.get('growth_stage', 'N/A')} days\n"
    f"Seeding Date: {plant_profile.get('seeding_date', 'N/A')}\n"
    f"Region: {plant_profile.get('region', 'Unknown')}\n"
    f"Location: {plant_profile.get('location', 'Unknown')}\n"
    f"Target pH Range: {plant_profile.get('target_ph_min', 'N/A')}-{plant_profile.get('target_ph_max', 'N/A')}\n"
    f"Target TDS Range: {plant_profile.get('target_tds_min', 'N/A')}-{plant_profile.get('target_tds_max', 'N/A')}"
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
    prompt += "\n\nPlease provide a JSON response with exactly two keys: 'actions' (a list) and 'next_check_hours' (a number). Do not include any additional text or formatting. Limit your answer to 300 tokens."
    return prompt

async def build_plan_prompt(sensor_data: dict, plant_profile: dict, query: str) -> str:
    """
    Creates a text prompt for a more detailed "plan."
    Optionally uses a quick Serper-based search for additional info.
    """
    plant_info = (
        f"Plant: {plant_profile['plant_name']}\n"
        f"Plant Type: {plant_profile['plant_type']}\n"
        f"Growth Stage: {plant_profile['growth_stage']} days from seeding (seeded at {plant_profile['seeding_date']})\n"
        f"Region: {plant_profile.get('region', 'Unknown')}\n"
        f"Location: {plant_profile.get('location', 'Unknown')}"
    )
    promptPlan = f"""
You are an expert hydroponic system manager. Based on the following information, determine optimal nutrient dosing amounts.

Plant Information:
{plant_info}

Current Sensor Readings:
- pH: {sensor_data.get('P','Unknown')}
- TDS (PPM): {sensor_data.get('TDS','Unknown')}

Provide an efficient and optimized solution according to the plant's location, local weather conditions, and soil conditions.

Consider:
1. Place of planting
2. Plant growth stage
3. Chemical interactions
4. Maximum safe dosing limits

Provide a detailed growing plan for {plant_profile['plant_name']} based on the {plant_profile['location']}. Include the best months for planting and the total growing duration. Specify pH and TDS requirements based on the local soil and water conditions. If the query mentions 'seeding' or 'growing,' tailor the plan accordingly. Break down the process into clear steps, covering:

1. Ideal Planting Time
2. Growth Duration
3. Soil and Water Conditions
4. Seeding Stage
5. Growing Stage
6. Harvesting Time
7. Additional Tips
""".strip()

    # Enhance the query with additional location context.
    enhanced_query = f"{query}. Focus on best practices in {plant_profile.get('region', 'Unknown')} for {plant_profile.get('plant_type', 'Unknown')} cultivation."

    # Optionally gather additional data from a web search.
    search_results = await fetch_search_results(enhanced_query)
    raw_info_list = [
        f"{entry['title']}: {entry['snippet']}"
        for entry in search_results.get("organic", [])
        if "title" in entry and "snippet" in entry
    ]
    raw_info = " ".join(raw_info_list) if raw_info_list else "No additional information available."

    # Clean the snippet using BeautifulSoup.
    soup = BeautifulSoup(raw_info, "html.parser")
    cleaned_snippet = soup.get_text(separator=" ")

    final_prompt = f"{promptPlan}\n\nAdditional Information:\n{cleaned_snippet}"
    return final_prompt.strip()

async def direct_ollama_call(prompt: str, model_name: str) -> str:
    """
    Calls your local Ollama server directly (via HTTP) to run the prompt on `model_name`.
    Returns the raw completion (which may include the <think> block) for UI display.
    """
    logger.info(f"Making direct Ollama call to model {model_name} with prompt:\n{prompt}")
    try:
        request_body = {
            "model": model_name,
            "prompt": prompt,
            "stream": False
        }
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(OLLAMA_URL, json=request_body)
            resp.raise_for_status()
            data = resp.json()  # Expected format: { "response": "...text..." }
            raw_completion = data.get("response", "").strip()
            logger.info(f"Ollama raw completion: {raw_completion}")
            print("LLM raw response:", raw_completion)
            # For processing, we later clean the response—but here we return the full text.
            if not raw_completion:
                logger.error("No response received from Ollama.")
                raise HTTPException(status_code=500, detail="Empty response from Ollama")
            return raw_completion
    except Exception as e:
        logger.error(f"Ollama call failed: {e}")
        raise HTTPException(status_code=500, detail="Error calling local Ollama") from e

def validate_llm_response(response: Dict) -> None:
    """
    Validates that the parsed JSON response has a top-level "actions" list with required keys.
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

async def call_llm_async(prompt: str, model_name: str = MODEL_1_5B) -> (Dict, str):
    """
    Calls the local Ollama API and returns a tuple:
      - Parsed JSON response (after cleaning the <think> block) for automated processing.
      - The full raw completion (with the <think> block) for UI display.
    """
    logger.info(f"Sending prompt to local Ollama:\n{prompt}")
    raw_completion = await direct_ollama_call(prompt, model_name)
    # Clean the response for JSON processing
    cleaned = parse_ollama_response(raw_completion).replace("'", '"').strip()
    print("Cleaned LLM response for parsing:", cleaned)  # Print cleaned response
    # Extract the first JSON object from the cleaned text
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start == -1 or end == -1 or end <= start:
        logger.error("No valid JSON block found in cleaned response.")
        raise HTTPException(status_code=500, detail="Invalid JSON from Ollama")
    cleaned = cleaned[start:end+1]
    print("Extracted JSON block:", cleaned)
    try:
        parsed_response = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON response from Ollama after extraction: {cleaned}")
        raise HTTPException(status_code=500, detail="Invalid JSON from Ollama") from e

    # *** NEW: Return the parsed JSON and the full raw completion ***
    return parsed_response, raw_completion



async def call_llm_plan(prompt: str, model_name: str = MODEL_1_5B) -> str:
    """
    Calls the local Ollama API for a freeform plan.
    Returns the raw text (which may include the <think> block) so it can be displayed directly.
    """
    logger.info(f"Sending plan prompt to local Ollama:\n{prompt}")
    raw_completion = await direct_ollama_call(prompt, model_name)
    logger.info(f"Ollama plan raw text: {raw_completion}")
    return raw_completion

# ------------------------------------------------------------------
# Remainder of your logic that executes dosing, etc.
# ------------------------------------------------------------------

async def execute_dosing_plan(device: Device, dosing_plan: Dict) -> Dict:
    """
    Executes the dosing plan by posting to the device’s /pump endpoint for each action.
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
    Retrieves sensor data from the device’s /monitor endpoint.
    """
    if not device.http_endpoint:
        raise ValueError(f"Device {device.id} has no HTTP endpoint configured")
    logger.info(f"Fetching sensor readings for device {device.id}")
    http_endpoint = device.http_endpoint
    if not http_endpoint.startswith("http"):
        http_endpoint = f"http://{http_endpoint}"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{http_endpoint}/monitor", timeout=10)
            response_data = response.json()
            if response.status_code == 200:
                logger.info(f"pH and TDS readings fetched successfully: {response_data}")
            else:
                logger.error(f"❌ Failed to fetch readings: {response_data}")
                raise HTTPException(status_code=500, detail="PH/TDS reading request failed")
        except httpx.RequestError as e:
            logger.error(f"❌ HTTP request to PH/TDS sensor failed: {e}")
            raise HTTPException(status_code=500, detail="PH/TDS reading request failed")
    return response_data

async def process_dosing_request(
    device_id: int,
    sensor_data: dict,
    plant_profile: dict,
    db: AsyncSession
) -> (Dict, str):
    """
    Triggered by /api/v1/dosing/llm-request.
    Builds a prompt, calls Ollama, parses JSON for automated dosing,
    and executes the dosing plan.
    Returns both the processed result and the raw AI response for UI.
    """
    try:
        device = await dosing_manager.get_device(device_id, db)
        if not device.pump_configurations:
            raise ValueError(f"Device {device.id} has no pump configurations available")
        if not device.http_endpoint:
            raise ValueError(f"Device {device.id} has no HTTP endpoint configured")
        prompt = await build_dosing_prompt(device, sensor_data, plant_profile)
        dosing_plan, ai_response = await call_llm_async(prompt=prompt, model_name=MODEL_1_5B)
        result = await execute_dosing_plan(device, dosing_plan)
        return result, ai_response
    except ValueError as ve:
        logger.error(f"ValueError in dosing request: {ve}")
        raise HTTPException(status_code=400, detail=str(ve))
    except json.JSONDecodeError as je:
        logger.error(f"JSON Parsing Error: {je}")
        raise HTTPException(status_code=500, detail="Invalid JSON format from Ollama")
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred")

async def process_sensor_plan(
    device_id: int,
    sensor_data: dict,
    plant_profile: dict,
    query: str,
    db: AsyncSession
):
    """
    Triggered by /api/v1/dosing/llm-plan.
    Builds a freeform plan prompt, calls Ollama, and returns the parsed text.
    """
    try:
        device = await dosing_manager.get_device(device_id, db)
        if not device.http_endpoint:
            raise ValueError(f"Device {device.id} has no HTTP endpoint configured")
        prompt = await build_plan_prompt(sensor_data, plant_profile, query)
        sensor_plan = await call_llm_plan(prompt, MODEL_1_5B)
        beautify_response = parse_json_response(sensor_plan)
        if isinstance(beautify_response, list):
            beautify_response = {"plan": "\n".join(beautify_response)}
        return beautify_response
    except ValueError as ve:
        logger.error(f"ValueError in sensor plan request: {ve}")
        raise HTTPException(status_code=400, detail=str(ve))
    except json.JSONDecodeError as je:
        logger.error(f"JSON Parsing Error: {je}")
        raise HTTPException(status_code=500, detail="Invalid format from Ollama")
    except Exception as e:
        logger.exception(f"Unexpected error in /llm-plan: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred")

async def call_llm(prompt: str, model_name: str) -> Dict:
    """
    A utility function that calls the local Ollama API and returns the parsed JSON response.
    (For supply-chain style logic where only the parsed data is needed.)
    """
    logger.info(f"Calling local Ollama with model {model_name}, prompt:\n{prompt}")
    raw_completion = await direct_ollama_call(prompt, model_name)
    cleaned = parse_ollama_response(raw_completion).replace("'", '"').strip()
    try:
        parsed_response = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.error(f"Invalid JSON response from local Ollama: {raw_completion}")
        raise HTTPException(status_code=500, detail="Invalid JSON from local Ollama")
    return parsed_response

async def analyze_transport_options(origin: str, destination: str, weight_kg: float) -> Dict:
    prompt = f"""
    You are a logistics expert. Analyze the best railway and trucking options for transporting goods.
    - Origin: {origin}
    - Destination: {destination}
    - Weight: {weight_kg} kg

    Provide a JSON output with estimated cost, time, and best transport mode.
    """
    return await call_llm(prompt, MODEL_1_5B)

async def analyze_market_price(produce_type: str) -> Dict:
    prompt = f"""
    You are a market analyst. Provide the latest price per kg of {produce_type} in major cities.
    - Provide an approximate or typical value if uncertain.
    - Output must be valid JSON.
    """
    return await call_llm(prompt, MODEL_1_5B)

async def generate_final_decision(transport_analysis: Dict, market_price: Dict) -> Dict:
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
