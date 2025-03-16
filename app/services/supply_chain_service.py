import asyncio
import json
import logging
import re
from datetime import datetime
from fastapi import HTTPException
from typing import Dict, List, Union, Any, Tuple
import httpx
from bs4 import BeautifulSoup
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SupplyChainAnalysis, ConversationLog
from .serper import fetch_search_results

logger = logging.getLogger(__name__)

# Ollama endpoint and model names
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_1_5B = "deepseek-r1:1.5b"
MODEL_7B = "deepseek-r1:7b"


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
    Attempts to parse the provided string as JSON.
    On failure, splits the string into cleaned lines.
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
            elif para.strip():
                result.append(para.strip())
        return result
    return data


def parse_ollama_response(raw_response: str) -> str:
    """
    Removes any <think> block from the raw response.
    Returns the cleaned text that is expected to be valid JSON.
    """
    cleaned = re.sub(r"<think>.*?</think>", "", raw_response, flags=re.DOTALL).strip()
    return cleaned


async def fetch_and_average_value(query: str) -> float:
    """
    Fetches numerical values from a search query and returns their average.
    """
    logger.info(f"Fetching search results for query: {query}")
    results = await fetch_search_results(query)
    values = []
    if "organic" in results:
        for entry in results["organic"][:3]:
            snippet = entry.get("snippet", "")
            numbers = re.findall(r'\d+\.?\d*', snippet)
            if numbers:
                try:
                    values.append(float(numbers[0]))
                except ValueError:
                    continue
    if values:
        avg_value = sum(values) / len(values)
        logger.info(f"Average value for query '{query}': {avg_value}")
        return avg_value
    logger.warning(f"No numerical values found for query: {query}. Returning 0.0")
    return 0.0


async def call_llm(prompt: str, model_name: str = MODEL_1_5B) -> Dict:
    """
    Calls the local Ollama API via HTTP.
    Strips out any <think> block from the response and parses the JSON.
    """
    logger.info(f"Calling Ollama with model {model_name}, prompt:\n{prompt}")
    request_body = {"model": model_name, "prompt": prompt, "stream": False}
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(OLLAMA_URL, json=request_body)
            resp.raise_for_status()
            data = resp.json()
            raw_completion = data.get("response", "").strip()
            logger.info(f"Ollama raw response: {raw_completion}")
            print("LLM raw response:", raw_completion)  # Print raw response
            # Clean the response by removing any <think> block and normalizing quotes
            cleaned_content = parse_ollama_response(raw_completion).replace("'", '"').strip()
            print("Cleaned LLM response for supply chain:", cleaned_content)  # Print cleaned response
            # Extract the first JSON object using regex
            import re  # if not already imported
            match = re.search(r"(\{.*\})", cleaned_content, flags=re.DOTALL)
            if match:
                cleaned_content = match.group(1)
                print("Extracted JSON block for supply chain:", cleaned_content)  # Print extracted JSON block
            else:
                logger.error("No JSON block found in cleaned supply chain response.")
                raise HTTPException(status_code=500, detail="Invalid JSON from local Ollama")
            try:
                parsed_response = json.loads(cleaned_content)
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON after processing (supply chain): {cleaned_content}")
                raise HTTPException(status_code=500, detail="Invalid JSON format from local Ollama") from e
            return parsed_response

    except httpx.HTTPStatusError as http_err:
        logger.error(f"Ollama HTTP error: {http_err}")
        raise HTTPException(status_code=500, detail="Ollama API HTTP error") from http_err
    except Exception as e:
        logger.error(f"Ollama call failed: {e}")
        raise HTTPException(status_code=500, detail="Error processing Ollama response") from e


async def analyze_transport_optimization(transport_request: Dict[str, Any]) -> Tuple[Dict, Dict]:
    """
    Analyzes supply chain transportation options using the LLM.
    Gathers key numeric estimates from a search then builds a prompt.
    Returns a tuple (analysis_record, optimization_result) where the latter is the parsed LLM output.
    """
    origin = transport_request.get("origin", "Unknown")
    destination = transport_request.get("destination", "Unknown")
    produce_type = transport_request.get("produce_type", "Unknown Product")
    weight_kg = transport_request.get("weight_kg", 0)
    transport_mode = transport_request.get("transport_mode", "railway")

    # Build queries for estimation.
    distance_query = f"average distance in km from {origin} to {destination} by {transport_mode}"
    cost_query = f"average cost per kg to transport {produce_type} from {origin} to {destination} by {transport_mode}"
    time_query = f"average travel time in hours from {origin} to {destination} by {transport_mode}"
    perish_query = f"average time in hours before {produce_type} perishes during transport"
    market_price_query = f"average market price per kg for {produce_type} in {destination}"

    distance_km = await fetch_and_average_value(distance_query)
    cost_per_kg = await fetch_and_average_value(cost_query)
    estimated_time_hours = await fetch_and_average_value(time_query)
    perish_time_hours = await fetch_and_average_value(perish_query)
    market_price_per_kg = await fetch_and_average_value(market_price_query)

    total_cost = cost_per_kg * weight_kg
    net_profit_per_kg = market_price_per_kg - cost_per_kg

    prompt = f"""
You are a supply chain optimization expert. Evaluate the following transport parameters for {produce_type}:
- Origin: {origin}
- Destination: {destination}
- Transport Mode: {transport_mode}
- Distance: {distance_km:.2f} km
- Cost per kg: {cost_per_kg:.2f} USD
- Total Weight: {weight_kg} kg
- Estimated Travel Time: {estimated_time_hours:.2f} hours
- Time before perish: {perish_time_hours:.2f} hours
- Market Price per kg: {market_price_per_kg:.2f} USD

Considering possible train delays and perishability, provide a final recommendation to optimize transportation.
Output in JSON format:
{{
  "final_recommendation": "<optimized transport plan>",
  "reasoning": "<detailed explanation>"
}}
""".strip()

    optimization_result = await call_llm(prompt, model_name=MODEL_7B)

    analysis_record = {
        "origin": origin,
        "destination": destination,
        "produce_type": produce_type,
        "weight_kg": weight_kg,
        "transport_mode": transport_mode,
        "distance_km": distance_km,
        "cost_per_kg": cost_per_kg,
        "total_cost": total_cost,
        "estimated_time_hours": estimated_time_hours,
        "market_price_per_kg": market_price_per_kg,
        "net_profit_per_kg": net_profit_per_kg,
        "final_recommendation": json.dumps(optimization_result.get("final_recommendation", "No recommendation provided"))

    }
    return analysis_record, optimization_result


async def store_supply_chain_analysis(db_session: AsyncSession, analysis_record: Dict[str, Any]):
    """
    Stores the supply chain analysis record.
    """
    record = SupplyChainAnalysis(**analysis_record)
    db_session.add(record)
    await db_session.commit()
    await db_session.refresh(record)
    logger.info(f"Supply chain analysis record stored with ID: {record.id}")


async def store_conversation(db_session: AsyncSession, user_request: Dict[str, Any],
                             prompt: str, llm_response: Dict[str, Any]):
    """
    Stores the LLM conversation for debugging or future improvements.
    """
    log = ConversationLog(conversation={
        "user_request": user_request,
        "llm_prompt": prompt,
        "llm_response": llm_response
    })
    db_session.add(log)
    await db_session.commit()
    await db_session.refresh(log)
    logger.info(f"Conversation log stored with ID: {log.id}")


async def trigger_transport_analysis(transport_request: Dict[str, Any], db_session: AsyncSession) -> Dict[str, Any]:
    """
    Entry point to trigger supply chain analysis.
    It calls the analysis function, stores the results and conversation, and returns a summary.
    """
    analysis_record, optimization_result = await analyze_transport_optimization(transport_request)
    await store_supply_chain_analysis(db_session, analysis_record)
    await store_conversation(db_session, transport_request, 
                             f"Prompt: {analysis_record}", optimization_result)
    return {"analysis": analysis_record, "optimization": optimization_result}
