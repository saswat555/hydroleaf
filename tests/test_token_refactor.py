# tests/test_token_refactor.py

import secrets
import pytest
from app.models import Device, DeviceToken, DeviceType
from app.dependencies import verify_device_token
from httpx import AsyncClient
from app.main import app
from app.core.database import AsyncSessionLocal

@pytest.mark.asyncio
async def test_issue_and_verify_device_token():
    # use one transaction for both inserts
    async with AsyncSessionLocal() as db:
        async with db.begin():
            dev = Device(
                id="dev-1",
                mac_id="mac-1",
                name="Demo",
                type=DeviceType.DOSING_UNIT,
                http_endpoint="http://example",
                is_active=True,
            )
            db.add(dev)

            token = secrets.token_urlsafe(16)
            db.add(DeviceToken(
                device_id=dev.id,
                token=token,
                device_type=dev.type,
            ))
    # both rows are flushed & committed at this point

    # now verify against the token
    class DummyCred:
        credentials = token

    async with AsyncSessionLocal() as db_verify:
        device_id = await verify_device_token(
            DummyCred,
            db_verify,
            expected_type=DeviceType.DOSING_UNIT
        )
    assert device_id == "dev-1"


@pytest.mark.asyncio
async def test_admin_issue_token_endpoint(monkeypatch):
    # stub admin auth dependency to always succeed
    monkeypatch.setattr(
        "app.routers.admin_subscriptions.get_current_admin",
        lambda: True
    )

    # insert a device in one transaction
    async with AsyncSessionLocal() as db:
        async with db.begin():
            db.add(Device(
                id="dev-2",
                mac_id="mac-2",
                name="Demo2",
                type=DeviceType.VALVE_CONTROLLER,
                http_endpoint="http://x",
                is_active=True,
            ))

    # now call the endpoint
    async with AsyncClient(app=app, base_url="http://test") as client:
        resp = await client.post("/admin/device/dev-2/issue-token")
    assert resp.status_code == 201
    data = resp.json()
    assert data["device_id"] == "dev-2"
    assert "token" in data
