# tests/test_plants_dosing_flow.py
import os, importlib
import json
import datetime as dt
import pytest
from httpx import AsyncClient

# force real Ollama integration
os.environ["USE_OLLAMA"] = "true"
import app.services.llm as llm_mod
importlib.reload(llm_mod)


@pytest.mark.asyncio
async def test_complete_plants_dosing_flow(async_client: AsyncClient, signed_up_user):
    # reuse your signup fixture
    _, _, headers = signed_up_user

    # 1) Create a new farm
    farm_payload = {
        "name": "Integration Farm",
        "address": "123 Test Blvd",
        "latitude": 12.3456,
        "longitude": 65.4321
    }
    farm_resp = await async_client.post(
        "/api/v1/farms/",
        json=farm_payload,
        headers=headers,
    )
    assert farm_resp.status_code == 201
    farm = farm_resp.json()
    farm_id = farm["id"]

    # 2) Create a plant in that farm
    plant_payload = {
        "name": "Test Lettuce",
        "type": "leaf",
        "growth_stage": "veg",
        "seeding_date": "2025-07-01T00:00:00Z",
        "region": "Greenhouse",
        "location_description": "Rack 1",
        "target_ph_min": 5.5,
        "target_ph_max": 6.5,
        "target_tds_min": 300,
        "target_tds_max": 700
    }
    plant_resp = await async_client.post(
        f"/api/v1/farms/{farm_id}/plants/",
        json=plant_payload,
        headers=headers,
    )
    assert plant_resp.status_code == 201
    plant = plant_resp.json()
    plant_id = plant["id"]

    # 3) Register a dosing device
    device_payload = {
        "mac_id": "AA:BB:CC:DD",
        "name": "Integration Doser",
        "type": "dosing_unit",
        "http_endpoint": "http://doser.local",
        "pump_configurations": [{"pump_number": 1, "chemical_name": "N"}],
    }
    dev_resp = await async_client.post(
        "/api/v1/devices/dosing",
        json=device_payload,
        headers=headers,
    )
    assert dev_resp.status_code == 201
    device = dev_resp.json()
    device_id = device["id"]

    # 4) Link the dosing device to our farm
    link = await async_client.post(
        f"/api/v1/farms/{farm_id}/devices",
        json={"device_id": device_id},
        headers=headers,
    )
    assert link.status_code == 200
    # 5) First dosing run
    run1_resp = await async_client.post(
        "/api/v1/dosing/run",
        json={
            "farm_id": farm_id,
            "plant_id": plant_id,
            "device_id": device_id,
        },
        headers=headers,
    )
    assert run1_resp.status_code == 200
    run1 = run1_resp.json()
    assert "actions" in run1 and isinstance(run1["actions"], list)
    assert run1["actions"][0]["dose_ml"] == 5
    assert run1["actions"][0]["pump_number"] == 1

    # 5) Second dosing run (should see two total logs afterwards)
    run2_resp = await async_client.post(
        "/api/v1/dosing/run",
        json={
            "farm_id": farm_id,
            "plant_id": plant_id,
            "device_id": device_id,
        },
        headers=headers,
    )
    assert run2_resp.status_code == 200
    run2 = run2_resp.json()
    assert run2["actions"][0]["dose_ml"] == 5

    # 6) Fetch dosing logs for that plant
    logs_resp = await async_client.get(
        f"/api/v1/farms/{farm_id}/plants/{plant_id}/logs",
        headers=headers,
    )
    assert logs_resp.status_code == 200
    logs = logs_resp.json()
    assert isinstance(logs, list)
    # Expect exactly two entries
    assert len(logs) == 2

    # Verify chronological order
    t1 = dt.datetime.fromisoformat(logs[0]["timestamp"].rstrip("Z"))
    t2 = dt.datetime.fromisoformat(logs[1]["timestamp"].rstrip("Z"))
    assert t1 < t2


@pytest.mark.asyncio
async def test_dosing_run_fails_without_plant(async_client: AsyncClient, signed_up_user):
    # user + headers
    _, _, headers = signed_up_user

    # create a farm (but NO plant)
    farm = (await async_client.post(
        "/api/v1/farms/",
        json={"name":"NoPlant Farm","address":"A","latitude":0,"longitude":0},
        headers=headers,
    )).json()
    farm_id = farm["id"]

    # register a dosing device
    device = (await async_client.post(
        "/api/v1/devices/dosing",
        json={
            "mac_id": "NP:AA:BB",
            "name": "NoPlant Doser",
            "type": "dosing_unit",
            "http_endpoint": "http://doser.local",
            "pump_configurations": [{"pump_number": 1, "chemical_name": "N"}],
        },
        headers=headers,
    )).json()["id"]

    # try to run dosing with a bogus plant_id → must fail
    resp = await async_client.post(
        "/api/v1/dosing/run",
        json={"farm_id": farm_id, "plant_id": 999999, "device_id": device},
        headers=headers,
    )
    assert resp.status_code in (400, 404, 422)  # failure expected