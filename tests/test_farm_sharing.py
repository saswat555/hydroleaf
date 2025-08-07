# tests/test_farm_sharing.py

import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_share_farm_endpoint(async_client, new_user, plan_id, device):
    token, hdrs = new_user
    # first create farm
    farm = (await async_client.post("/api/v1/farms/", json={
      "name":"S","address":"A","latitude":0,"longitude":0
    }, headers=hdrs)).json()
    # share with another user id=some_subuser
    target_user_id = 9999  # you'd create it or assume exists
    r1 = await async_client.post(
        f"/api/v1/farms/{farm['id']}/share",
        json={"user_id": target_user_id},
        headers=hdrs
    )
    assert r1.status_code == 200
    assert r1.json()["user_id"] == target_user_id

    # 404 if farm not found
    r2 = await async_client.post(
        "/api/v1/farms/0/share",
        json={"user_id": target_user_id},
        headers=hdrs
    )
    assert r2.status_code == 404
