# tests/test_supply_chain_endpoint.py

import pytest

@pytest.mark.asyncio
async def test_supply_chain_extract_endpoint(async_client, signed_up_user):
    _, _, hdrs = signed_up_user
    body = "Here’s some text {\"foo\":1}"
    r = await async_client.post(
      "/api/v1/supply-chain/extract", json={"text":body}, headers=hdrs
    )
    assert r.status_code==200
    assert r.json()=={"foo":1}
