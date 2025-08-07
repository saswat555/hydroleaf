# tests/test_user_profile.py

import pytest

@pytest.mark.asyncio
async def test_get_and_update_profile(async_client, signed_up_user):
    _, token, hdrs = signed_up_user
    # get
    me = (await async_client.get("/api/v1/users/me", headers=hdrs)).json()
    assert "email" in me
    # update
    r = await async_client.put("/api/v1/users/me",
        json={"first_name":"New","last_name":"Name"}, headers=hdrs
    )
    assert r.status_code==200
    assert r.json()["first_name"]=="New"
