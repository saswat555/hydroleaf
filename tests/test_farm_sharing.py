# tests/test_farm_sharing.py

import uuid
import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_share_farm_endpoint(async_client, new_user):
    token, hdrs = new_user

    # create farm
    farm = (await async_client.post("/api/v1/farms/", json={
        "name":"S","address":"A","latitude":0,"longitude":0
    }, headers=hdrs)).json()

    # create the target user
    payload = {
        "email": "shared_to@example.com",
        "password": "Pass!234",
        "first_name": "Shared",
        "last_name": "User",
        "phone": "1234567890",
        "address": "1 St",
        "city": "C",
        "state": "S",
        "country": "IN",
        "postal_code": "000000",
    }
    created = (await async_client.post("/api/v1/auth/signup", json=payload)).json()
    target_user_id = created["user"]["id"]

    r1 = await async_client.post(
        f"/api/v1/farms/{farm['id']}/share",
        json={"user_id": target_user_id},
        headers=hdrs
    )
    assert r1.status_code == 200
    assert r1.json()["user_id"] == target_user_id

    # 404 if farm not found
    r2 = await async_client.post(
        f"/api/v1/farms/{str(uuid.uuid4())}/share",
        json={"user_id": target_user_id},
        headers=hdrs
    )
    assert r2.status_code == 404
