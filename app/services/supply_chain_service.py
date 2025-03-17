import os
import asyncio
import json
import logging
import re
from datetime import datetime
from fastapi import HTTPException
from typing import Dict, Any, Tuple, Union, List
import httpx
from bs4 import BeautifulSoup
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SupplyChainAnalysis, ConversationLog
from app.services.serper import fetch_search_results

logger = logging.getLogger(__name__)

# Production-level configuration via environment variables
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
MODEL_1_5B = os.getenv("MODEL_1_5B", "deepseek-r1:1.5b")
MODEL_7B = os.getenv("MODEL_7B", "deepseek-r1:7b")
LLM_REQUEST_TIMEOUT = int(os.getenv("LLM_REQUEST_TIMEOUT", "300"))

def enhance_query(user_query: str, plant_profile: dict) -> str:
    location = str(plant_profile.get("location", "Unknown"))
    plant_name = plant_profile.get("plant_name", "Unknown Plant")
    plant_type = plant_profile.get("plant_type", "Unknown Type")
    growth_stage = plant_profile.get("growth_stage", "Unknown Stage")
    seeding_date = plant_profile.get("seeding_date", "Unknown Date")
    additional_context = (
        f"Ensure your analysis accounts for conditions in {location}. The plant '{plant_name}' "
        f"({plant_type}), at {growth_stage} stage (seeded on {seeding_date}), may require tailored transport strategies. "
        "Provide detailed calculations and a final recommendation."
    )
    return f"{user_query}. {additional_context}"

def parse_json_response(json_str: str) -> Union[List[str], dict]:
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        paragraphs = json_str.split("\n")
        return [para.strip() for para in paragraphs if para.strip()]

def parse_ollama_response(raw_response: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>", "", raw_response, flags=re.DOTALL).strip()
    return cleaned

async def fetch_and_average_value(query: str) -> float:
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
    logger.info(f"Calling LLM with model {model_name}, prompt:\n{prompt}")
    request_body = {"model": model_name, "prompt": prompt, "stream": False}
    try:
        async with httpx.AsyncClient(timeout=LLM_REQUEST_TIMEOUT) as client:
            response = await client.post(OLLAMA_URL, json=request_body)
            response.raise_for_status()
            data = response.json()
            raw_completion = data.get("response", "").strip()
            logger.info(f"Ollama raw response: {raw_completion}")
            cleaned_content = parse_ollama_response(raw_completion).replace("'", '"').strip()
            match = re.search(r"(\{.*\})", cleaned_content, flags=re.DOTALL)
            if match:
                cleaned_content = match.group(1)
            else:
                logger.error("No JSON block found in cleaned supply chain response.")
                raise HTTPException(status_code=500, detail="Invalid JSON from LLM")
            try:
                parsed_response = json.loads(cleaned_content)
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON after processing: {cleaned_content}")
                raise HTTPException(status_code=500, detail="Invalid JSON format from LLM") from e
            return parsed_response
    except httpx.HTTPStatusError as http_err:
        logger.error(f"Ollama HTTP error: {http_err}")
        raise HTTPException(status_code=500, detail="LLM API HTTP error") from http_err
    except Exception as e:
        logger.error(f"Ollama call failed: {e}")
        raise HTTPException(status_code=500, detail="Error processing LLM response") from e

async def analyze_transport_optimization(transport_request: Dict[str, Any]) -> Tuple[Dict, Dict]:
    origin = transport_request.get("origin", "Unknown")
    destination = transport_request.get("destination", "Unknown")
    produce_type = transport_request.get("produce_type", "Unknown Product")
    weight_kg = transport_request.get("weight_kg", 0)
    transport_mode = transport_request.get("transport_mode", "railway")

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

Considering possible delays and perishability constraints, provide a final recommendation to optimize transportation.
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
    record = SupplyChainAnalysis(**analysis_record)
    db_session.add(record)
    try:
        await db_session.commit()
        await db_session.refresh(record)
        logger.info(f"Supply chain analysis record stored with ID: {record.id}")
    except Exception as exc:
        await db_session.rollback()
        logger.error(f"Error storing supply chain analysis record: {exc}")
        raise HTTPException(status_code=500, detail="Failed to store supply chain analysis record") from exc

async def store_conversation(db_session: AsyncSession, user_request: Dict[str, Any],
                             prompt: str, llm_response: Dict[str, Any]):
    log = ConversationLog(conversation={
        "user_request": user_request,
        "llm_prompt": prompt,
        "llm_response": llm_response
    })
    db_session.add(log)
    try:
        await db_session.commit()
        await db_session.refresh(log)
        logger.info(f"Conversation log stored with ID: {log.id}")
    except Exception as exc:
        await db_session.rollback()
        logger.error(f"Error storing conversation log: {exc}")
        raise HTTPException(status_code=500, detail="Failed to store conversation log") from exc

async def trigger_transport_analysis(transport_request: Dict[str, Any], db_session: AsyncSession) -> Dict[str, Any]:
    analysis_record, optimization_result = await analyze_transport_optimization(transport_request)
    prompt_for_log = f"Analysis parameters: {json.dumps(analysis_record, indent=2)}"
    await store_supply_chain_analysis(db_session, analysis_record)
    await store_conversation(db_session, transport_request, prompt_for_log, optimization_result)
    return {"analysis": analysis_record, "optimization": optimization_result}
