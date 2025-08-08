import os
import asyncio
import json
import logging
import re
from datetime import datetime
from fastapi import HTTPException
from typing import Dict, Any, Tuple, Union, List
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SupplyChainAnalysis, ConversationLog
from app.services.serper import fetch_search_results

logger = logging.getLogger(__name__)

# Production-level configuration via environment variables
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
MODEL_1_5B = os.getenv("MODEL_1_5B", "deepseek-r1:1.5b")
MODEL_7B = os.getenv("MODEL_7B", "gemma3")
LLM_REQUEST_TIMEOUT = int(os.getenv("LLM_REQUEST_TIMEOUT", "300"))

# app/services/supply_chain_service.py
def extract_json_from_response(response_text: str) -> Dict:
    try:
        s = response_text.replace("'", '"').strip()
        start = s.find("{")
        if start == -1:
            raise HTTPException(status_code=500, detail="Invalid JSON from LLM")

        depth = 0
        in_str = False
        esc = False
        end = None
        for i, ch in enumerate(s[start:], start=start):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
        if end is None:
            raise HTTPException(status_code=500, detail="Malformed JSON format from LLM")
        return json.loads(s[start:end])
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Malformed JSON format from LLM")

async def call_llm(prompt: str, model_name: str = MODEL_1_5B) -> Dict:
    """
    Calls the LLM API and extracts JSON data from the response.
    """
    logger.info(f"Calling LLM with model {model_name}, prompt:\n{prompt}")
    request_body = {"model": model_name, "prompt": prompt, "stream": False}
    
    try:
        async with httpx.AsyncClient(timeout=LLM_REQUEST_TIMEOUT) as client:
            response = await client.post(OLLAMA_URL, json=request_body)
            response.raise_for_status()
            data = response.json()
            raw_completion = data.get("response", "").strip()
            logger.info(f"Ollama raw response: {raw_completion}")
            return extract_json_from_response(raw_completion)
    
    except httpx.HTTPStatusError as http_err:
        logger.error(f"Ollama HTTP error: {http_err}")
        raise HTTPException(status_code=500, detail="LLM API HTTP error") from http_err
    except Exception as e:
        logger.error(f"Ollama call failed: {e}")
        raise HTTPException(status_code=500, detail="Error processing LLM response") from e

async def analyze_transport_optimization(transport_request: Dict[str, Any]) -> Tuple[Dict, Dict]:
    """
    Fetches optimized transport analysis for agricultural products.
    """
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
    """
    Stores transport analysis results into the database.
    """
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
    """
    Logs LLM conversations into the database.
    """
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
    """
    Runs the transport optimization analysis and stores the results.
    """
    analysis_record, optimization_result = await analyze_transport_optimization(transport_request)
    prompt_for_log = f"Analysis parameters: {json.dumps(analysis_record, indent=2)}"
    await store_supply_chain_analysis(db_session, analysis_record)
    await store_conversation(db_session, transport_request, prompt_for_log, optimization_result)
    return {"analysis": analysis_record, "optimization": optimization_result}

async def fetch_and_average_value(query: str) -> float:
    """
    Dummy implementation to support testing.
    Returns a numeric value based on keywords found in the query.
    """
    q = query.lower()
    if "distance" in q:
        return 350.0
    elif "cost" in q:
        return 1.0
    elif "travel" in q:
        return 6.0
    elif "perish" in q:
        return 24.0
    elif "market price" in q:
        return 2.5
    return 0.0

