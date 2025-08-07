# tests/test_build_dosing_prompt.py
import re
import pytest
from app.services.llm import build_dosing_prompt

class DummyDev:
    def __init__(self, pumps=None):
        self.id = "dev-x"
        self.pump_configurations = pumps or [
            {"pump_number": 1, "chemical_name": "A", "chemical_description": "Alpha"},
            {"pump_number": 2, "chemical_name": "B", "chemical_description": "Beta"},
        ]

@pytest.mark.asyncio
async def test_build_dosing_prompt_includes_schedule_and_sensors():
    dev = DummyDev()
    sensor = {"ph": 6.0, "tds": 250}
    profile = {
        "plant_name": "Herb",
        "plant_type": "Leafy",
        "growth_stage": "Flower",
        "seeding_date": "2021-01-01",
        "region": "Green",
        "location": "Zone A",
        "target_ph_min": 5.5, "target_ph_max": 6.5,
        "target_tds_min": 200, "target_tds_max": 300,
        "dosing_schedule": {"morning": "08:00", "evening": "20:00"},
    }
    prompt = await build_dosing_prompt(dev, sensor, profile)
    # pumps
    assert "Pump 1: A" in prompt and "Pump 2: B" in prompt
    # sensor readings summary (single line with colon)
    assert re.search(r"Current.+pH:\s*6\.0", prompt)
    assert re.search(r"Current.+TDS:\s*250", prompt)
    # schedule section
    assert "morning" in prompt and "08:00" in prompt
    assert "evening" in prompt and "20:00" in prompt
    # JSON-only instruction
    assert "Return ONLY a JSON object" in prompt

@pytest.mark.asyncio
async def test_prompt_formats_numbers_cleanly():
    dev = DummyDev()
    sensor = {"ph": 5.83, "tds": 300.0}
    profile = {
        "plant_name": "X", "plant_type": "Y", "growth_stage": "Z",
        "seeding_date": "2021-01-01", "region": "R", "location": "L",
        "target_ph_min": 5.5, "target_ph_max": 6.5,
        "target_tds_min": 200, "target_tds_max": 400,
        "dosing_schedule": {"noon": "12:00"},
    }
    prompt = await build_dosing_prompt(dev, sensor, profile)
    assert "pH: 5.8" in prompt  # single decimal
    assert re.search(r"TDS:\s*300\b", prompt)

@pytest.mark.asyncio
async def test_pumps_are_sorted_and_description_optional():
    dev = DummyDev(pumps=[
        {"pump_number": 3, "chemical_name": "Buffer", "chemical_description": None},
        {"pump_number": 1, "chemical_name": "Acid", "chemical_description": "pH Up"},
    ])
    sensor = {"ph": 6.0, "tds": 250}
    profile = {
        "plant_name": "A", "plant_type": "B", "growth_stage": "C",
        "seeding_date": "2020-01-01", "region": "R", "location": "L",
        "target_ph_min": 5.5, "target_ph_max": 6.5,
        "target_tds_min": 200, "target_tds_max": 300,
        "dosing_schedule": {},
    }
    prompt = await build_dosing_prompt(dev, sensor, profile)
    # Order check: Pump 1 appears before Pump 3
    assert prompt.index("Pump 1: Acid") < prompt.index("Pump 3: Buffer")
    # No double space/strange punctuation if description missing
    assert "Pump 3: Buffer —" not in prompt

@pytest.mark.asyncio
async def test_missing_schedule_is_handled_gracefully():
    dev = DummyDev()
    sensor = {"ph": 6.0, "tds": 250}
    profile = {
        "plant_name": "A", "plant_type": "B", "growth_stage": "C",
        "seeding_date": "2020-01-01", "region": "R", "location": "L",
        "target_ph_min": 5.5, "target_ph_max": 6.5,
        "target_tds_min": 200, "target_tds_max": 300,
        # no dosing_schedule
    }
    prompt = await build_dosing_prompt(dev, sensor, profile)
    assert "Dosing Schedule:" in prompt
    assert "(none)" in prompt

@pytest.mark.asyncio
async def test_targets_and_profile_block_present():
    dev = DummyDev()
    sensor = {"ph": 6.0, "tds": 250}
    profile = {
        "plant_name": "Herb", "plant_type": "Leafy", "growth_stage": "Flower",
        "seeding_date": "2021-01-01", "region": "Green", "location": "Zone A",
        "target_ph_min": 5.5, "target_ph_max": 6.5,
        "target_tds_min": 200, "target_tds_max": 300,
        "dosing_schedule": {"morning": "08:00"},
    }
    prompt = await build_dosing_prompt(dev, sensor, profile)
    assert "Plant Profile:" in prompt
    assert "- plant_name: Herb" in prompt
    # Make sure both bounds appear on the same line for pH/TDS targets
    assert re.search(r"- pH:\s*5\.5\s*-\s*6\.5", prompt)
    assert re.search(r"- TDS:\s*200\s*-\s*300\b", prompt)
