import os
import asyncio
import openai
import json
import logging
import re
from datetime import datetime
from fastapi import HTTPException
from typing import Dict, List, Union, Tuple
import httpx
from bs4 import BeautifulSoup
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device
from app.services.dose_manager import DoseManager
from app.services.serper import fetch_search_results


logger = logging.getLogger(__name__)

# Production-level configuration via environment variables
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
MODEL_1_5B = os.getenv("MODEL_1_5B", "deepseek-r1:1.5b")
GPT_MODEL = os.getenv("GPT_MODEL")
MODEL_7B = os.getenv("MODEL_7B", "deepseek-r1:7b")
LLM_REQUEST_TIMEOUT = int(os.getenv("LLM_REQUEST_TIMEOUT", "300"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
USE_OLLAMA = os.getenv("USE_OLLAMA", "false").lower() == "true"


dosing_manager = DoseManager()

def enhance_query(user_query: str, plant_profile: dict) -> str:
    location = str(plant_profile.get("location", "Unknown"))
    plant_name = plant_profile.get("plant_name", "Unknown Plant")
    plant_type = plant_profile.get("plant_type", "Unknown Type")
    growth_stage = plant_profile.get("growth_stage", "Unknown Stage")
    seeding_date = plant_profile.get("seeding_date", "Unknown Date")
    additional_context = (
        f"Please consider that the plant '{plant_name}' of type '{plant_type}' is in the '{growth_stage}' stage, "
        f"seeded on {seeding_date}, and located in {location}. Provide precise nutrient dosing recommendations based on current sensor data."
    )
    if location.lower() not in user_query.lower():
        return f"{user_query}. {additional_context}"
    return user_query

def parse_json_response(json_str: str) -> Union[List[str], dict]:
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        # Split into lines if JSON parsing fails
        paragraphs = json_str.split("\n")
        result = [para.strip() for para in paragraphs if para.strip()]
        return result
    return data

def parse_ollama_response(raw_response: str) -> str:
    # Remove any <think> block and extra whitespace
    cleaned = re.sub(r"<think>.*?</think>", "", raw_response, flags=re.DOTALL).strip()
    return cleaned

def parse_openai_response(raw_response: str) -> str:
    """Extracts and cleans OpenAI's response to match Ollama's JSON format."""
    
    # Remove any <think> blocks (if present)
    cleaned = re.sub(r"<think>.*?</think>", "", raw_response, flags=re.DOTALL).strip()
    start = cleaned.find('{')
    end = cleaned.rfind('}')

    if start == -1 or end == -1 or end <= start:
        logger.error(f"No valid JSON block found in OpenAI response: {cleaned}")
        raise ValueError("Invalid JSON response from OpenAI")

    cleaned_json = cleaned[start:end+1]

    # Attempt to parse and reformat to ensure valid JSON
    try:
        parsed_response = json.loads(cleaned_json)
        return json.dumps(parsed_response)  # Ensure JSON consistency
    except json.JSONDecodeError as e:
        logger.error(f"Malformed JSON from OpenAI: {cleaned_json}")
        raise ValueError("Malformed JSON from OpenAI") from e


async def build_dosing_prompt(device: Device, sensor_data: dict, plant_profile: dict) -> str:
    """
    Creates a text prompt that asks the LLM for a JSON-based dosing plan.
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
        f"Region: {plant_profile.get('region', 'Bangalore')}\n"
        f"Location: {plant_profile.get('location', 'Bangalore')}\n"
        f"Target pH Range: {plant_profile.get('target_ph_min', '3')} - {plant_profile.get('target_ph_max', '4')}\n"
        f"Target TDS Range: {plant_profile.get('target_tds_min', '150')} - {plant_profile.get('target_tds_max', '1000')}\n"
    )
    prompt = (
        "You are an expert hydroponic system manager. Based on the following information, determine optimal nutrient dosing amounts.\n\n"
        "Current Sensor Readings:\n"
        f"- pH: {sensor_data.get('ph', 'Unknown')}\n"
        f"- TDS (PPM): {sensor_data.get('tds', 'Unknown')}\n\n"
        "Plant Information:\n"
        f"{plant_info}\n\n"
        "Available Dosing Pumps:\n"
        f"{pump_info}\n\n"
        "Provide dosing recommendations in the following JSON format:\n"
        '{\n'
        '  "actions": [\n'
        '    {\n'
        '      "pump_number": 1,\n'
        '      "chemical_name": "Nutrient A",\n'
        '      "dose_ml": 50,\n'
        '      "reasoning": "Brief explanation"\n'
        '    }\n'
        '  ],\n'
        '  "next_check_hours": 24\n'
        '}\n\n'
        "Consider:\n"
        "1. Current pH and TDS levels\n"
        "2. Plant growth stage\n"
        "3. Chemical interactions\n"
        "4. Maximum safe dosing limits\n\n"
        "5. You **must NOT** create additional pumps beyond those listed above.\n"
        "Please provide a JSON response with exactly two keys: 'actions' (a list) and 'next_check_hours' (a number). "
        "Do not include any additional text or formatting. Limit your answer to 300 tokens."
    )
    return prompt

async def build_plan_prompt(sensor_data: dict, plant_profile: dict, query: str) -> str:
    """
    Creates a detailed text prompt for a growing plan.
    Optionally uses a Serper-based web search to gather additional detailed context.
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

    # Gather additional data from a web search using Serper.
    search_results = await fetch_search_results(enhanced_query)
    organic_results = search_results.get("organic", [])
    if organic_results:
        # Process the top 5 results for a richer context.
        raw_info_list = []
        for entry in organic_results[:5]:
            title = entry.get("title", "No Title")
            snippet = entry.get("snippet", "No snippet available.")
            link = entry.get("link", None)
            info_str = f"• Title: {title}\n  Snippet: {snippet}"
            if link:
                info_str += f"\n  Link: {link}"
            raw_info_list.append(info_str)
        raw_info = "\n\n".join(raw_info_list)
    else:
        raw_info = "No additional information available."

    # Append a header to the additional information.
    final_prompt = f"{promptPlan}\n\nDetailed Search Insights:\n{raw_info}"
    return final_prompt.strip()


async def direct_ollama_call(prompt: str, model_name: str) -> str:
    """
    Calls the local Ollama API to run the prompt on the specified model.
    Returns the raw completion for further processing.
    """
    logger.info(f"Making direct Ollama call to model {model_name} with prompt:\n{prompt}")
    try:
        request_body = {
            "model": model_name,
            "prompt": prompt,
            "stream": False
        }
        async with httpx.AsyncClient(timeout=LLM_REQUEST_TIMEOUT) as client:
            response = await client.post(OLLAMA_URL, json=request_body)
            response.raise_for_status()
            data = response.json()
            raw_completion = data.get("response", "").strip()
            logger.info(f"Ollama raw completion: {raw_completion}")
            if not raw_completion:
                logger.error("No response received from Ollama.")
                raise HTTPException(status_code=500, detail="Empty response from LLM service")
            return raw_completion
    except Exception as e:
        logger.error(f"Ollama call failed: {e}")
        raise HTTPException(status_code=500, detail="Error calling LLM service") from e


async def direct_openai_text_call(prompt: str, model_name: str) -> str:
    client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
    response = await client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=800,
        temperature=0.5
    )
    return response.choices[0].message.content.strip()


async def direct_openai_call(prompt: str, model_name: str) -> str:
    """
    Calls OpenAI's API to generate a response and formats it like Ollama's.
    """
    if not OPENAI_API_KEY:
        raise ValueError("OpenAI API Key is missing. Set it as an environment variable.")

    logger.info(f"Making OpenAI call to model {model_name} with prompt:\n{prompt}")

    try:
        client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        response = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600
        )
        logger.info(f"OpenAI response: {response}")
        
        raw_completion = response.choices[0].message.content.strip()
        cleaned_response = parse_openai_response(raw_completion)
        
        logger.info(f"OpenAI cleaned response: {cleaned_response}")
        return cleaned_response
    except Exception as e:
        logger.error(f"OpenAI call failed: {e}")
        raise HTTPException(status_code=500, detail="Error calling OpenAI LLM") from e
    
def validate_llm_response(response: Dict) -> None:
    """
    Validates that the parsed JSON response has a top-level 'actions' list with the required keys.
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

async def call_llm_async(prompt: str, model_name: str = MODEL_1_5B) -> Tuple[Dict, str]:
    """
    Calls either Ollama or OpenAI and ensures the response format is consistent.
    Returns:
      - The parsed JSON response.
      - The full raw completion for UI display.
    """
    logger.info(f"Sending prompt to LLM:\n{prompt}")

    if USE_OLLAMA:
        raw_completion = await direct_ollama_call(prompt, model_name)
        cleaned = parse_ollama_response(raw_completion).replace("'", '"').strip()
        start = cleaned.find('{')
        end = cleaned.rfind('}')
        if start == -1 or end == -1 or end <= start:
          logger.error("No valid JSON block found in cleaned response.")
          raise HTTPException(status_code=500, detail="Invalid JSON from LLM")
        cleaned_json = cleaned[start:end+1]   
    else:
        raw_completion = await direct_openai_call(prompt, GPT_MODEL)  
        cleaned_json = parse_openai_response(raw_completion)  

    logger.info(f"Raw response from LLM:\n{raw_completion}")

    # Validate JSON structure
    try:
        parsed_response = json.loads(cleaned_json)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON from LLM after extraction: {cleaned_json}")
        raise HTTPException(status_code=500, detail="Invalid JSON from LLM") from e
    return parsed_response, raw_completion

async def call_llm_plan(prompt: str, model_name: str = MODEL_1_5B) -> str:
    """
    Calls the local Ollama API for a freeform plan.
    Returns the raw text (which may include a <think> block) for display.
    """
    logger.info(f"Sending plan prompt to LLM:\n{prompt}")
    if USE_OLLAMA:
       raw_completion = await direct_ollama_call(prompt, model_name)
    else:
       logger.info(f" plan raw text: {GPT_MODEL}")
       raw_completion = await direct_openai_text_call(prompt, GPT_MODEL)   
    logger.info(f"Ollama plan raw text: {raw_completion}")
    return raw_completion

async def execute_dosing_plan(device: Device, dosing_plan: Dict) -> Dict:
    """
    Executes the dosing plan by calling the device’s /pump endpoint for each dosing action.
    """
    if not device.http_endpoint:
        raise ValueError(f"Device {device.id} has no HTTP endpoint configured")
    message = {
        "timestamp": datetime.utcnow().isoformat(),
        "device_id": device.id,
        "actions": dosing_plan.get("actions", []),
        "next_check_hours": dosing_plan.get("next_check_hours", 24)
    }
    
    logger.info(f"Dosing plan for device {device.id}: {message}")
    async with httpx.AsyncClient() as client:
        for action in dosing_plan.get("actions", []):
            pump_number = action.get("pump_number")
            dose_ml = action.get("dose_ml")
            endpoint = device.http_endpoint if device.http_endpoint.startswith("http") else f"http://{device.http_endpoint}"
            try:
                logger.info(f"Pump activation started")
                response = await client.post(
                    f"{endpoint}/pump",
                    json={"pump": pump_number, "amount": int(dose_ml)},
                    timeout=10
                )
                response_data = response.json()
                success_message = response_data.get("message") or response_data.get("msg")
                if response.status_code == 200 and success_message == "Pump started":
                    logger.info(f"Pump {pump_number} activated successfully: {response_data}")
                else:
                      logger.error(f"Failed to activate pump {pump_number}: {response_data}")

            except httpx.RequestError as e:
                logger.error(f"HTTP request to pump {pump_number} failed: {e}")
                raise HTTPException(status_code=500, detail=f"Pump {pump_number} activation failed") from e
    return message

async def getSensorData(device: Device) -> dict:
    """
    Retrieves sensor data from the device’s /monitor endpoint.
    """
    if not device.http_endpoint:
        raise ValueError(f"Device {device.id} has no HTTP endpoint configured")
    logger.info(f"Fetching sensor data for device {device.id}")
    endpoint = device.http_endpoint if device.http_endpoint.startswith("http") else f"http://{device.http_endpoint}"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{endpoint}/monitor", timeout=10)
            response.raise_for_status()
            data = response.json()
            logger.info(f"Sensor data for device {device.id}: {data}")
            return data
        except Exception as e:
            logger.error(f"Error fetching sensor data: {e}")
            raise HTTPException(status_code=500, detail="Failed to fetch sensor data") from e

async def process_dosing_request(
    device_id: str,
    sensor_data: dict,
    plant_profile: dict,
    db: AsyncSession
) -> Tuple[Dict, str]:
    """
    Triggered by the dosing endpoint; builds a prompt, calls the LLM,
    parses the JSON dosing plan, and executes it.
    Returns the execution result and the raw LLM response.
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
        raise HTTPException(status_code=500, detail="Invalid JSON format from LLM")
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred") from e

async def process_sensor_plan(
    device_id: str,
    sensor_data: dict,
    plant_profile: dict,
    query: str,
    db: AsyncSession
):
    """
    Triggered by the plan endpoint; builds a prompt, calls the LLM,
    and returns a structured growing plan.
    """
    try:
        device = await dosing_manager.get_device(device_id, db)
        if not device.http_endpoint:
            raise ValueError(f"Device {device.id} has no HTTP endpoint configured")
        prompt = await build_plan_prompt(sensor_data, plant_profile, query)
        sensor_plan_raw = await call_llm_plan(prompt, MODEL_1_5B)
        beautify_response = parse_json_response(sensor_plan_raw)
        if isinstance(beautify_response, list):
            beautify_response = {"plan": "\n".join(beautify_response)}
        return beautify_response
    except ValueError as ve:
        logger.error(f"ValueError in sensor plan request: {ve}")
        raise HTTPException(status_code=400, detail=str(ve))
    except json.JSONDecodeError as je:
        logger.error(f"JSON Parsing Error: {je}")
        raise HTTPException(status_code=500, detail="Invalid format from LLM")
    except Exception as e:
        logger.exception(f"Unexpected error in sensor plan: {e}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred") from e

async def call_llm(prompt: str, model_name: str) -> Dict:
    """
    Utility function that calls the LLM and returns the parsed JSON response.
    """
    logger.info(f"Calling LLM with model {model_name}, prompt:\n{prompt}")
    if USE_OLLAMA:
       raw_completion = await direct_ollama_call(prompt, model_name)
       cleaned = parse_ollama_response(raw_completion).replace("'", '"').strip()
    else:
       raw_completion = await direct_openai_call(prompt, GPT_MODEL)  
       cleaned =  parse_openai_response(raw_completion).replace("'", '"').strip()  
    try:
        parsed_response = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.error(f"Invalid JSON from LLM: {raw_completion}")
        raise HTTPException(status_code=500, detail="Invalid JSON from LLM")
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
