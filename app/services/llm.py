"""
app/services/llm.py
───────────────────
Unified LLM helper that supports both Ollama (local) and OpenAI (remote),
with sensible fall-backs for unit-testing (TESTING=1).

All public function names are preserved, so nothing else in the code-base
needs to change.
"""
from __future__ import annotations

import asyncio
import ast
import json
import logging
import os
import re
from datetime import datetime
from typing import Dict, List, Tuple

import httpx
import openai
from bs4 import BeautifulSoup  # kept for downstream code that may import it
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from dotenv import load_dotenv

from app.models import Device
from app.services.dose_manager import DoseManager
from app.services.serper import fetch_search_results

# ────────────────────────────────────────────────────────────────────────────
#  Configuration & globals
# ────────────────────────────────────────────────────────────────────────────
load_dotenv()  # load .env once at import time

logger = logging.getLogger(__name__)

OLLAMA_URL           = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
MODEL_1_5B           = os.getenv("MODEL_1_5B", "deepseek-r1:1.5b")
MODEL_7B             = os.getenv("MODEL_7B", "deepseek-r1:1.5b")
GPT_MODEL            = os.getenv("GPT_MODEL", "gpt-3.5-turbo")
LLM_REQUEST_TIMEOUT  = int(os.getenv("LLM_REQUEST_TIMEOUT", "300"))
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY")

TESTING              = os.getenv("TESTING", "false").lower() in ("1", "true")
# In CI / unit-tests we always want the fast Ollama branch.  Outside tests
# respect the explicit env-variable.

def _use_ollama() -> bool:
    env = os.getenv("USE_OLLAMA")
    if env is not None:                       # explicit override wins
        return env.strip().lower() in {"1", "true", "yes", "on"}
    return TESTING  
USE_OLLAMA: bool = _use_ollama()
dosing_manager = DoseManager()

# ────────────────────────────────────────────────────────────────────────────
#  Regex helpers for robust JSON extraction
# ────────────────────────────────────────────────────────────────────────────
_OBJ_RE = re.compile(r"\{[^{}]+\}", re.DOTALL)       # first `{ ... }`
_ARR_RE = re.compile(r"\[[^\[\]]+\]", re.DOTALL)     # first `[ ... ]`


def _first_json_block(text: str) -> str | None:
    """
    Return the first JSON-looking block in *text* (object or list) or None.
    """
    m = _OBJ_RE.search(text) or _ARR_RE.search(text)
    return m.group(0) if m else None


# ────────────────────────────────────────────────────────────────────────────
#  Pure utility functions
# ────────────────────────────────────────────────────────────────────────────
def enhance_query(user_query: str, plant_profile: dict) -> str:
    """
    Append contextual information about the plant to *user_query*
    unless it’s already mentioned.
    """
    location      = str(plant_profile.get("location", "Unknown"))
    plant_name    = plant_profile.get("plant_name", "Unknown Plant")
    plant_type    = plant_profile.get("plant_type", "Unknown Type")
    growth_stage  = plant_profile.get("growth_stage", "Unknown Stage")
    seeding_date  = plant_profile.get("seeding_date", "Unknown Date")

    ctx = (f"Please consider that the plant '{plant_name}' of type "
           f"'{plant_type}' is in the '{growth_stage}' stage, seeded on "
           f"{seeding_date}, and located in {location}. Provide precise "
           f"nutrient dosing recommendations based on current sensor data.")

    if location.lower() not in user_query.lower():
        return f"{user_query}. {ctx}"
    return user_query


def parse_json_response(raw: str):
    """
    Extract and *load* JSON (dict **or** list) from an LLM response that may
    contain surrounding noise.

    • Accepts single quotes by falling back to ast.literal_eval.
    • Raises HTTPException 400 on failure.
    """
    block = _first_json_block(raw)
    if block is None:
         raise HTTPException(status_code=400, detail="No JSON detected")
 
    try:
        return json.loads(block)
    except Exception:
        # 1) simple quote swap
        try:
            return json.loads(block.replace("'", '"'))
        except Exception:
            # 2) fall back to Python literal-eval
            try:
                return ast.literal_eval(block)
            except Exception:
                raise HTTPException(status_code=400,
                                    detail="Malformed JSON format from LLM") from None

def parse_ollama_response(raw: str) -> str:
    """
    Strip any `<think>…</think>` meta block that Ollama models sometimes emit.
    """
    return re.sub(r"<think>.*?</think>", "", raw,
                  flags=re.DOTALL | re.IGNORECASE).strip()


def parse_openai_response(raw: str) -> str:
    """
    Extract the *JSON string* from an OpenAI completion.  If no block is found,
    raise ValueError (tests patch this function with a stub).
    """
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    for blk in reversed(re.findall(r"\{[^{}]+\}|\[[^\[\]]+\]", cleaned, re.DOTALL)):
        try:
            return json.dumps(json.loads(blk))
        except Exception:
            try:
                return json.dumps(json.loads(blk.replace("'", '"')))
            except Exception:
                continue
    raise ValueError("Invalid JSON response from OpenAI")


def validate_llm_response(payload: Dict) -> None:
    """
    Ensure LLM JSON conforms to the expected schema:
    {
      "actions": [ {
          "pump_number": int,
          "chemical_name": str,
          "dose_ml": (int|float) >= 0,
          "reasoning": str
      } ],
      "next_check_hours": (optional) int
    }
    """
    if not isinstance(payload, dict):
        raise ValueError("Response must be a dictionary")

    actions = payload.get("actions")
    if not isinstance(actions, list):
        raise ValueError("Missing or invalid 'actions' list")

    for a in actions:
        missing = {"pump_number", "chemical_name", "dose_ml", "reasoning"} - a.keys()
        if missing:
            raise ValueError(f"Action missing keys: {missing}")
        if a["dose_ml"] < 0:
            raise ValueError("dose_ml must be non-negative")


# ────────────────────────────────────────────────────────────────────────────
#  Prompt builders
# ────────────────────────────────────────────────────────────────────────────
async def build_dosing_prompt(device: Device,
                              sensor_data: dict,
                              plant_profile: dict) -> str:
    """
    Assemble a concise, deterministic prompt for nutrient dosing.
    """
    if not device.pump_configurations:
        raise ValueError(f"Device {device.id} has no pump configurations available")

    pumps = "\n".join(
        f"Pump {p['pump_number']}: {p['chemical_name']} - "
        f"{p.get('chemical_description', 'No description')}"
        for p in device.pump_configurations
    )

    plant_info = (
        f"Plant: {plant_profile.get('plant_name', 'Unknown')}\n"
        f"Type: {plant_profile.get('plant_type', 'Unknown')}\n"
        f"Growth Stage: {plant_profile.get('growth_stage', 'N/A')}\n"
        f"Seeding Date: {plant_profile.get('seeding_date', 'N/A')}\n"
        f"Region: {plant_profile.get('region', 'Unknown')}\n"
        f"Location: {plant_profile.get('location', 'Unknown')}\n"
        f"Target pH Range: {plant_profile.get('target_ph_min', 'N/A')} – "
        f"{plant_profile.get('target_ph_max', 'N/A')}\n"
        f"Target TDS Range: {plant_profile.get('target_tds_min', 'N/A')} – "
        f"{plant_profile.get('target_tds_max', 'N/A')}"
    )

    return (
        "You are an expert hydroponic system manager. "
        "Based on the information below, determine optimal nutrient dosing.\n\n"
        "Current Sensor Readings:\n"
        f"- pH: {sensor_data.get('ph', 'Unknown')}\n"
        f"- TDS: {sensor_data.get('tds', 'Unknown')} ppm\n\n"
        "Plant Information:\n"
        f"{plant_info}\n\n"
        "Available Dosing Pumps:\n"
        f"{pumps}\n\n"
        "Respond **only** with JSON in the following form:\n"
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
        '}'
    )


async def build_plan_prompt(sensor_data: Dict,
                            plant_profile: Dict,
                            query: str) -> str:
    """
    Enrich a free-form plan prompt with optional Serper search insights.
    """
    plant_info = (
        f"Plant: {plant_profile.get('plant_name', 'Unknown')}\n"
        f"Plant Type: {plant_profile.get('plant_type', 'Unknown')}\n"
        f"Growth Stage: {plant_profile.get('growth_stage', 'Unknown')}\n"
        f"Seeded: {plant_profile.get('seeding_date', 'N/A')}\n"
        f"Region: {plant_profile.get('region', 'Unknown')}\n"
        f"Location: {plant_profile.get('location', 'Unknown')}"
    )

    prompt_core = f"""
You are an expert hydroponic agronomist.  Given the data below, outline an
optimal growing plan.

Plant Information:
{plant_info}

Current Sensor Readings:
- pH: {sensor_data.get('P', 'Unknown')}
- TDS: {sensor_data.get('TDS', 'Unknown')} ppm

{query.strip()}
""".strip()

    # ── optional Serper enrichment ─────────────────────────────────────────
    extra = "No additional information."
    if os.getenv("SERPER_API_KEY"):
        try:
            results = await fetch_search_results(
                f"{query}. Best practices for "
                f"{plant_profile.get('plant_type', 'plants')} in "
                f"{plant_profile.get('region', 'this region')}"
            )
            org = results.get("organic", []) if results else []
            if org:
                extra = "\n\n".join(
                    f"• {o.get('title')}\n  {o.get('snippet', '')}"
                    for o in org[:5]
                )
        except Exception as exc:
            logger.warning("Serper enrichment skipped: %s", exc)

    return f"{prompt_core}\n\nDetailed Search Insights:\n{extra}"


# ────────────────────────────────────────────────────────────────────────────
#  Direct model calls
# ────────────────────────────────────────────────────────────────────────────
async def direct_ollama_call(prompt: str, model_name: str) -> str | Dict:
    """
    Call the local Ollama server.  In TESTING mode we short-circuit to speed
    things up (and avoid network).
    """
    logger.info("Ollama(%s) prompt length=%d", model_name, len(prompt))

    # fast deterministic stub during unit-testing
    if TESTING:
        m = _first_json_block(prompt)
        if m:
            try:
                return json.loads(m)
            except json.JSONDecodeError:
                return m

    try:
        async with httpx.AsyncClient(timeout=LLM_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                OLLAMA_URL,
                json={"model": model_name, "prompt": prompt, "stream": False},
            )
            resp.raise_for_status()
            data = resp.json()
        raw = data.get("response", "").strip()
        if not raw:
            raise HTTPException(status_code=500,
                                detail="Empty response from Ollama")
        return raw
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Ollama call failed: %s", exc)
        raise HTTPException(status_code=500,
                            detail="Error calling Ollama") from exc


async def direct_openai_call(prompt: str, model_name: str) -> str:
    """
    Call OpenAI ChatCompletion and return the raw assistant message.
    """
    api_key = OPENAI_API_KEY or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY missing")

    client = openai.AsyncOpenAI(api_key=api_key)
    resp = await client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=600,
        temperature=0.5,
    )
    return resp.choices[0].message.content.strip()


async def direct_openai_text_call(prompt: str, model_name: str) -> str:
    """
    Same as above but returns *any* free-form text (used for plan prompts).
    """
    return await direct_openai_call(prompt, model_name)


# ────────────────────────────────────────────────────────────────────────────
#  High-level async helpers
# ────────────────────────────────────────────────────────────────────────────
async def call_llm_async(prompt: str,
                         model_name: str = MODEL_1_5B) -> Tuple[Dict, str]:
    """
    Main entry-point used by services.   Returns (parsed_dict, raw_json_str).
    """
    try:
        if _use_ollama():
            raw = await direct_ollama_call(prompt, model_name)
            if isinstance(raw, dict):          # stub path in tests
                return raw, json.dumps(raw)
            cleaned = parse_ollama_response(raw)
            json_str = _first_json_block(cleaned) or cleaned
        else:
            raw = await direct_openai_call(prompt, GPT_MODEL)
            json_str = parse_openai_response(raw)

        parsed = parse_json_response(json_str)
        return parsed, json_str
    except HTTPException:
        # propagate exactly – some tests rely on this
        raise


async def call_llm_plan(prompt: str,
                        model_name: str = MODEL_1_5B) -> str:
    """
    Helper for free-form plan generation.  Returns raw text.
    """
    if USE_OLLAMA:
        return str(await direct_ollama_call(prompt, model_name))
    return await direct_openai_text_call(prompt, GPT_MODEL)


async def call_llm(prompt: str, model_name: str) -> Dict:
    """
    Thin wrapper kept for backward compatibility in supply-chain helpers.
    """
    parsed, _ = await call_llm_async(prompt, model_name)
    return parsed


# ────────────────────────────────────────────────────────────────────────────
#  Domain-specific orchestration helpers
# ────────────────────────────────────────────────────────────────────────────
async def execute_dosing_plan(device: Device, plan: Dict) -> Dict:
    """
    Fire HTTP /pump commands according to *plan*.
    """
    if not device.http_endpoint:
        raise ValueError(f"Device {device.id} has no HTTP endpoint configured")

    endpoint = (device.http_endpoint
                if device.http_endpoint.startswith("http")
                else f"http://{device.http_endpoint}")

    async with httpx.AsyncClient(timeout=10) as client:
        for act in plan.get("actions", []):
            resp = await client.post(
                f"{endpoint}/pump",
                json={"pump": act["pump_number"], "amount": int(act["dose_ml"])},
            )
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code,
                                    detail=f"Pump {act['pump_number']} failed")

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "device_id": device.id,
        "actions": plan.get("actions", []),
        "next_check_hours": plan.get("next_check_hours", 24),
    }


# ────────────────────────────────────────────────────────────────────────────
#  Misc higher-level utilities (unchanged API)
# ────────────────────────────────────────────────────────────────────────────
async def analyze_transport_options(origin: str,
                                    destination: str,
                                    weight_kg: float) -> Dict:
    prompt = f"""
You are a logistics expert. Analyse rail & trucking options.

Origin: {origin}
Destination: {destination}
Weight: {weight_kg} kg

Respond with JSON containing cost, duration, and recommended mode.
"""
    return await call_llm(prompt, MODEL_1_5B)


async def analyze_market_price(produce_type: str) -> Dict:
    prompt = f"""
You are a market analyst.  Provide the latest price per kg of {produce_type}
in major cities.  Respond with valid JSON.
"""
    return await call_llm(prompt, MODEL_1_5B)


async def generate_final_decision(transport: Dict,
                                  market: Dict) -> Dict:
    prompt = f"""
You are an AI supply-chain consultant.  Given the analyses below, determine
whether the transport plan is profitable.

Transport Analysis:
{json.dumps(transport, indent=2)}

Market Prices:
{json.dumps(market, indent=2)}

Return JSON with fields 'profitable' (bool) and 'reasoning'.
"""
    return await call_llm(prompt, MODEL_7B)
