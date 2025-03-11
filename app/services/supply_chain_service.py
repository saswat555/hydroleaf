import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Dict, Any, Tuple

from fastapi import HTTPException
import httpx

from langchain_ollama import ChatOllama

from app.models import SupplyChainAnalysis  # ConversationLog is imported inside functions
from app.services.serper import fetch_search_results

logger = logging.getLogger(__name__)

# Model names for Ollama
MODEL_1_5B = "deepseek-r1:1.5b"
MODEL_7B = "deepseek-r1:7b"

# Initialize separate ChatOllama clients for each model
ollama_client_1_5b = ChatOllama(base_url="http://localhost:11434", model=MODEL_1_5B)
ollama_client_7b = ChatOllama(base_url="http://localhost:11434", model=MODEL_7B)

async def call_llm(prompt: str, model_name: str = MODEL_1_5B) -> Dict[str, Any]:
    """
    Generic function to call the LLM using the appropriate ChatOllama client.
    Ensures the output is valid JSON.
    """
    logger.info(f"Calling LLM {model_name} with prompt:\n{prompt}")
    client = ollama_client_1_5b if model_name == MODEL_1_5B else ollama_client_7b
    try:
        response = await asyncio.to_thread(
            client.chat,
            messages=[{"role": "user", "content": prompt}]
        )
        raw_content = response.get("message", {}).get("content", "").strip()
        logger.info(f"Raw LLM response: {raw_content}")
        # Remove any <think> blocks and enforce double quotes
        content = re.sub(r'<think>.*?</think>', '', raw_content, flags=re.DOTALL).strip()
        content = content.replace("'", '"')
        try:
            parsed_response = json.loads(content)
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON from LLM: {raw_content}")
            raise HTTPException(status_code=500, detail="Invalid LLM response format")
        return parsed_response
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        raise HTTPException(status_code=500, detail="Error processing LLM response")

async def fetch_and_average_value(query: str) -> float:
    """
    Uses the Serper API to search for the given query.
    Extracts numerical values from the top three organic results and returns their average.
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
    else:
        logger.error(f"No numerical values found for query: {query}")
        raise HTTPException(status_code=500, detail=f"Unable to determine value for query: {query}")

async def analyze_transport_optimization(transport_request: Dict[str, Any], db_session) -> Tuple[Dict, Dict]:
    """
    Analyze the transport optimization for a supply chain request.
    Uses Serper searches to fill unknown parameters and calls the LLM to optimize the transport plan.
    The conversation (user request, prompt, LLM response) is stored in the database.
    """
    origin = transport_request.get("origin")
    destination = transport_request.get("destination")
    produce_type = transport_request.get("produce_type")
    weight_kg = transport_request.get("weight_kg")
    transport_mode = transport_request.get("transport_mode", "railway")

    # Use Serper to fetch unknown numerical values:
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

    # Build an LLM prompt to optimize the transport plan
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
"""
    optimization_result = await call_llm(prompt, MODEL_7B)

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
         "final_recommendation": optimization_result.get("final_recommendation", "No recommendation provided")
    }

    # Store the analysis record in the database
    await store_supply_chain_analysis(db_session, analysis_record)

    # Store the conversation log
    conversation = {
         "user_request": transport_request,
         "llm_prompt": prompt,
         "llm_response": optimization_result
    }
    await store_conversation(db_session, conversation)

    return analysis_record, optimization_result

async def store_supply_chain_analysis(db_session, analysis_record: Dict[str, Any]):
    """
    Store the supply chain analysis result in the database.
    """
    from app.models import SupplyChainAnalysis  # Import here to avoid circular dependencies
    record = SupplyChainAnalysis(
         origin=analysis_record["origin"],
         destination=analysis_record["destination"],
         produce_type=analysis_record["produce_type"],
         weight_kg=analysis_record["weight_kg"],
         transport_mode=analysis_record["transport_mode"],
         distance_km=analysis_record["distance_km"],
         cost_per_kg=analysis_record["cost_per_kg"],
         total_cost=analysis_record["total_cost"],
         estimated_time_hours=analysis_record["estimated_time_hours"],
         market_price_per_kg=analysis_record["market_price_per_kg"],
         net_profit_per_kg=analysis_record["net_profit_per_kg"],
         final_recommendation=analysis_record["final_recommendation"]
    )
    db_session.add(record)
    await db_session.commit()
    await db_session.refresh(record)
    logger.info(f"Supply chain analysis record stored with ID: {record.id}")

async def store_conversation(db_session, conversation: Dict[str, Any]):
    """
    Store the conversation log in the database.
    """
    from app.models import ConversationLog  # Import the new conversation log model
    log = ConversationLog(
         conversation=conversation
    )
    db_session.add(log)
    await db_session.commit()
    await db_session.refresh(log)
    logger.info(f"Conversation log stored with ID: {log.id}")

# Optional trigger function to run the transport analysis.
async def trigger_transport_analysis(transport_request: Dict[str, Any], db_session) -> Dict[str, Any]:
    """
    Trigger the transport optimization analysis and return the results.
    """
    analysis_record, optimization_result = await analyze_transport_optimization(transport_request, db_session)
    return {
         "analysis": analysis_record,
         "optimization": optimization_result
    }
