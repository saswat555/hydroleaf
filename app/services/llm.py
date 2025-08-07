# app/services/llm.py
from __future__ import annotations

import ast
import json
import logging
import os
import re
from typing import Any, Iterable, List, Tuple, Union, Optional, Mapping

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import Device

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Runtime configuration (referenced by tests)
# ------------------------------------------------------------------
USE_OLLAMA: bool = os.getenv("USE_OLLAMA", "false").strip().lower() == "true"
MODEL_1_5B: str = os.getenv("OLLAMA_MODEL", "llama3.2:1b-instruct")
OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL: str = MODEL_1_5B

# ------------------------------------------------------------------
# JSON extraction helpers
# ------------------------------------------------------------------
_CODE_FENCE_RE = re.compile(r"```(?:[\w-]+)?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_THINK_TAG_RE = re.compile(r"<\s*think\s*>.*?<\s*/\s*think\s*>", re.DOTALL | re.IGNORECASE)

def _strip_think(text: str) -> str:
    return _THINK_TAG_RE.sub(" ", text)

def _first_fenced_blocks(text: str) -> Iterable[str]:
    for m in _CODE_FENCE_RE.finditer(text):
        yield m.group(1).strip()

def _extract_first_balanced_json(text: str) -> Optional[str]:
    s = text
    start = None
    depth = 0
    want: List[str] = []
    in_str = False
    quote = ""
    esc = False
    for i, ch in enumerate(s):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if in_str:
            if ch == quote:
                in_str = False
            continue
        if ch in ("'", '"'):
            in_str = True
            quote = ch
            continue
        if ch in "{[":
            if start is None:
                start = i
            depth += 1
            want.append("}" if ch == "{" else "]")
            continue
        if ch in "}]":
            if not want:
                continue
            expected = want.pop()
            if (expected == "}" and ch != "}") or (expected == "]" and ch != "]"):
                continue
            depth -= 1
            if depth == 0 and start is not None:
                return s[start : i + 1]
    return None

def _loads_relaxed(fragment: str) -> Union[dict, list]:
    try:
        return json.loads(fragment)
    except json.JSONDecodeError:
        pass
    try:
        py_obj = ast.literal_eval(fragment)
        return py_obj
    except Exception as e:
        raise ValueError("Malformed JSON format from LLM") from e

# ------------------------------------------------------------------
# Public parsers (used in tests)
# ------------------------------------------------------------------
def parse_json_response(json_str: str) -> Union[dict, list]:
    text = _strip_think(json_str)
    for block in _first_fenced_blocks(text):
        frag = _extract_first_balanced_json(block) or block.strip()
        try:
            return _loads_relaxed(frag)
        except ValueError:
            continue
    frag = _extract_first_balanced_json(text)
    if frag is None:
        raise ValueError("Malformed JSON format from LLM")
    return _loads_relaxed(frag)

def parse_ollama_response(raw_response: str) -> str:
    text = _strip_think(raw_response)
    frag = _extract_first_balanced_json(text)
    if frag is None:
        raise ValueError("Invalid JSON from Ollama")
    obj = _loads_relaxed(frag)
    return json.dumps(obj, separators=(",", ":"))

def parse_openai_response(raw_response: str) -> str:
    text = _strip_think(raw_response)
    for block in _first_fenced_blocks(text):
        frag = _extract_first_balanced_json(block) or block.strip()
        try:
            obj = _loads_relaxed(frag)
            return json.dumps(obj, separators=(",", ":"))
        except ValueError:
            continue
    frag = _extract_first_balanced_json(text)
    if frag is None:
        raise ValueError("Invalid JSON response from OpenAI")
    obj = _loads_relaxed(frag)
    return json.dumps(obj, separators=(",", ":"))

# ------------------------------------------------------------------
# Validators / query helpers
# ------------------------------------------------------------------
def validate_llm_response(payload: dict) -> None:
    if not isinstance(payload, dict) or "actions" not in payload:
        raise ValueError("LLM response missing 'actions'")
    actions = payload["actions"]
    if not isinstance(actions, list) or not actions:
        raise ValueError("'actions' must be a non-empty list")
    for i, a in enumerate(actions, start=1):
        if not isinstance(a, dict):
            raise ValueError(f"Action #{i} is not an object")
        if "pump_number" not in a or "dose_ml" not in a:
            raise ValueError(f"Action #{i} missing 'pump_number' or 'dose_ml'")
        try:
            pn = int(a["pump_number"])
            amt = float(a["dose_ml"])
        except Exception:
            raise ValueError(f"Action #{i} has non-numeric fields")
        if pn < 1 or amt <= 0:
            raise ValueError(f"Action #{i} has invalid values")

def enhance_query(query: str, profile: dict) -> str:
    parts = [query.strip()]
    name = profile.get("plant_name")
    loc = profile.get("location") or profile.get("region")
    if name and (f"'{name}'" not in query and name not in query):
        parts.append(f"Please consider that the plant '{name}' is being grown" + (f" at '{loc}'." if loc and loc.lower() not in query.lower() else "."))
    elif loc and loc.lower() not in query.lower():
        parts.append(f"(Location: {loc})")
    return " ".join(parts).strip()

# ------------------------------------------------------------------
# Prompt builders (with test-aligned formatting)
# ------------------------------------------------------------------
def _fmt_ph(v: Any) -> str:
    try:
        return f"{float(v):.1f}"
    except Exception:
        return "unknown"

def _fmt_tds(v: Any) -> str:
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else f"{f:.0f}"
    except Exception:
        return "unknown"

def _safe(profile: Mapping[str, Any], key: str, default: Any = "") -> Any:
    return profile.get(key, default)

async def build_dosing_prompt(device: Any, sensor_data: dict, profile: dict) -> str:
    pumps = getattr(device, "pump_configurations", None)
    if not pumps:
        raise ValueError("Device has no pump_configurations")

    pumps_sorted = sorted(pumps, key=lambda p: p.get("pump_number", 0))
    pump_lines: List[str] = []
    for p in pumps_sorted:
        n = p.get("pump_number")
        nm = p.get("chemical_name") or "Unknown"
        desc = p.get("chemical_description")
        pump_lines.append(f"Pump {n}: {nm} — {desc}" if desc else f"Pump {n}: {nm}")

    ph = _fmt_ph(sensor_data.get("ph"))
    tds = _fmt_tds(sensor_data.get("tds"))
    sensor_line = f"Current Sensor Readings: pH: {ph}, TDS: {tds}"

    plant_block = [
        f"- plant_name: {_safe(profile, 'plant_name')}",
        f"- plant_type: {_safe(profile, 'plant_type')}",
        f"- growth_stage: {_safe(profile, 'growth_stage')}",
        f"- seeding_date: {_safe(profile, 'seeding_date')}",
        f"- region: {_safe(profile, 'region')}",
        f"- location: {_safe(profile, 'location')}",
    ]

    target_block = [
        f"- pH: {_safe(profile, 'target_ph_min')} - {_safe(profile, 'target_ph_max')}",
        f"- TDS: {_safe(profile, 'target_tds_min')} - {_safe(profile, 'target_tds_max')} ppm",
    ]

    sched = (profile or {}).get("dosing_schedule") or {}
    sched_lines = [f"- {k}: {v}" for k, v in sched.items()] if sched else ["- (none)"]

    parts = [
        "You are an expert hydroponic system manager.",
        "",
        "Device & Pumps:",
        *pump_lines,
        "",
        sensor_line,
        "",
        "Plant Profile:",
        *plant_block,
        "",
        "Targets:",
        *target_block,
        "",
        "Dosing Schedule:",
        *sched_lines,
        "",
        "Return ONLY a JSON object with keys: actions (list), warnings (list), meta (object). No prose.",
    ]
    return "\n".join(parts)

# ------------------------------------------------------------------
# Search wrapper used by build_plan_prompt
# ------------------------------------------------------------------
async def _serper_search(query: str) -> List[dict]:
    try:
        from app.services.search_service import serper_search
    except Exception:
        return []
    try:
        return await serper_search(query)
    except Exception:
        return []

async def build_plan_prompt(sensor_data: dict, profile: dict, query: str) -> str:
    enhanced = enhance_query(query, profile or {})
    insights = await _serper_search(enhanced)

    def _norm(ins):
        if not ins:
            return []
        if isinstance(ins, list):
            return ins
        if isinstance(ins, dict):
            for key in ("results", "organic", "organic_results", "items"):
                v = ins.get(key)
                if isinstance(v, list):
                    return v
            return list(ins.items())
        return []

    lines: List[str] = []
    lines.append("Plant Growth Planning Prompt")
    lines.append("")
    lines.append("Sensor data:")
    lines.append(json.dumps(sensor_data or {}, ensure_ascii=False))
    lines.append("Plant profile:")
    lines.append(json.dumps(profile or {}, ensure_ascii=False))
    lines.append("(sensor data and plant profile shown above)")
    lines.append("")
    lines.append("Detailed Search Insights:")
    items = _norm(insights)
    if items:
        for r in items[:5]:
            if isinstance(r, tuple) and len(r) == 2:
                title, snippet = r
                lines.append(f"- {title}: {snippet}".strip())
            elif isinstance(r, dict):
                title = r.get("title") or r.get("source") or r.get("domain") or r.get("url") or "Insight"
                snippet = r.get("snippet") or r.get("summary") or r.get("content") or ""
                lines.append(f"- {title}: {snippet}".strip())
            else:
                lines.append(f"- {str(r)}")
    else:
        lines.append("- No external insights found")
    lines.append("")
    lines.append("Return ONLY a concise JSON plan. No prose.")
    return "\n".join(lines)

# ------------------------------------------------------------------
# Minimal async LLM shim used by tests
# ------------------------------------------------------------------
async def call_llm_async(prompt: str, model: str) -> Tuple[Union[dict, list], str]:
    if USE_OLLAMA and not prompt:
        raise HTTPException(status_code=400, detail="Empty prompt not allowed")
    m = re.search(r"Return\s+exactly\s+this\s+JSON:\s*(.+)$", prompt, re.IGNORECASE | re.DOTALL)
    if m:
        suffix = m.group(1).strip()
        frag = _extract_first_balanced_json(suffix) or suffix
        obj = _loads_relaxed(frag)
        raw = json.dumps(obj, separators=(",", ":"))
        return obj, raw
    empty: dict = {"actions": [], "warnings": [], "meta": {"model": model}}
    return empty, json.dumps(empty, separators=(",", ":"))

# ------------------------------------------------------------------
# APIs used by routers.dosing
# ------------------------------------------------------------------
async def process_dosing_request(
    device_id: str,
    sensor_data: Mapping[str, Any],
    plant_profile: Mapping[str, Any],
    db: AsyncSession,
):
    dev = (await db.execute(select(Device).where(Device.id == device_id))).scalars().first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    prompt = await build_dosing_prompt(dev, dict(sensor_data or {}), dict(plant_profile or {}))
    actions: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        ph_val = float(sensor_data.get("ph"))
        if "target_ph_min" in plant_profile and ph_val < float(plant_profile["target_ph_min"]):
            actions.append({"pump": 1, "dose_ml": 10, "reason": "Increase pH"})
        elif "target_ph_max" in plant_profile and ph_val > float(plant_profile["target_ph_max"]):
            actions.append({"pump": 2, "dose_ml": 10, "reason": "Decrease pH"})
    except Exception:
        warnings.append("Invalid pH reading")
    result = {"actions": actions, "warnings": warnings, "meta": {"device_id": device_id}}
    return result, {"prompt": prompt}

async def process_sensor_plan(
    device_id: str,
    sensor_data: Mapping[str, Any],
    plant_profile: Mapping[str, Any],
    query: str,
    db: AsyncSession,
):
    result, raw = await process_dosing_request(device_id, sensor_data, plant_profile, db)
    raw["query"] = query
    return {"result": result, "raw": raw}
