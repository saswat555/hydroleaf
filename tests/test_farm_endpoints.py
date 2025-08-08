# tests/test_farm_endpoints.py

import pytest
import uuid
@pytest.mark.asyncio
async def test_farm_crud_http(async_client, signed_up_user):
    _, _, hdrs = signed_up_user

    # 1) Initial list should be empty
    r = await async_client.get("/api/v1/farms/", headers=hdrs)
    assert r.status_code == 200
    assert r.json() == []

    # 2) Create a farm
    payload = {
        "name": "Test Farm",
        "address": "123 Garden Lane",
        "latitude": 12.34,
        "longitude": 56.78
    }
    r = await async_client.post("/api/v1/farms/", json=payload, headers=hdrs)
    assert r.status_code == 201
    farm = r.json()
    farm_id = farm["id"]
    uuid.UUID(str(farm_id))
    assert farm["name"] == payload["name"]

    # 3) List now contains it
    r = await async_client.get("/api/v1/farms/", headers=hdrs)
    assert any(f["id"] == farm_id for f in r.json())

    # 4) Retrieve by ID
    r = await async_client.get(f"/api/v1/farms/{farm_id}", headers=hdrs)
    assert r.status_code == 200
    assert r.json()["id"] == farm_id

    # 5) Update
    r = await async_client.put(
        f"/api/v1/farms/{farm_id}",
        json={"name": "Renamed Farm"},
        headers=hdrs
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed Farm"

    # 6) Delete
    r = await async_client.delete(f"/api/v1/farms/{farm_id}", headers=hdrs)
    assert r.status_code == 204

    # 7) Now 404 on get
    r = await async_client.get(f"/api/v1/farms/{farm_id}", headers=hdrs)
    assert r.status_code == 404
