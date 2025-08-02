# tests/test_token_admin.py

import secrets
import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from app.main import app
from app.models import Device, DeviceToken, DeviceType
from app.dependencies import verify_device_token, get_current_admin
from app.core.database import AsyncSessionLocal

# -------------------------------------------------------------------
# 1) verify_device_token unit tests
# -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_issue_and_verify_device_token_success():
    """
    Manually insert a Device + DeviceToken row, then verify_device_token returns its ID.
    """
    # 1. create device and token in the same transaction
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
    # 2. verify it
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
async def test_verify_device_token_invalid_token_raises_401():
    """Nonexistent token should raise a 401."""
    class DummyCred:
        credentials = "totally-invalid-token"

    async with AsyncSessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await verify_device_token(
                DummyCred,
                db,
                expected_type=DeviceType.DOSING_UNIT
            )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_device_token_wrong_type_raises_403():
    """
    If the token exists but the device_type doesn’t match expected_type,
    verify_device_token should raise a 403.
    """
    # Insert a valve_controller token
    async with AsyncSessionLocal() as db:
        async with db.begin():
            dev = Device(
                id="dev-2",
                mac_id="mac-2",
                name="Demo2",
                type=DeviceType.VALVE_CONTROLLER,
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
    class DummyCred:
        credentials = token

    # Now expect DOSING_UNIT, but token’s device_type is VALVE_CONTROLLER
    async with AsyncSessionLocal() as db_verify:
        with pytest.raises(HTTPException) as exc:
            await verify_device_token(
                DummyCred,
                db_verify,
                expected_type=DeviceType.DOSING_UNIT
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_verify_device_token_inactive_device_raises_403():
    """Tokens for inactive devices should be rejected."""
    async with AsyncSessionLocal() as db:
        async with db.begin():
            dev = Device(
                id="dev-3",
                mac_id="mac-3",
                name="Demo3",
                type=DeviceType.DOSING_UNIT,
                http_endpoint="http://example",
                is_active=False,
            )
            db.add(dev)

            token = secrets.token_urlsafe(16)
            db.add(DeviceToken(
                device_id=dev.id,
                token=token,
                device_type=dev.type,
            ))
    class DummyCred:
        credentials = token

    async with AsyncSessionLocal() as db_verify:
        with pytest.raises(HTTPException) as exc:
            await verify_device_token(
                DummyCred,
                db_verify,
                expected_type=DeviceType.DOSING_UNIT
            )
    assert exc.value.status_code == 403

# -------------------------------------------------------------------
# 2) Admin-only "issue-token" endpoint tests
# -------------------------------------------------------------------

@pytest.fixture
async def create_device(async_client: AsyncClient, signed_up_user):
    """
    Create a new dosing_unit via the public API so we can issue a token for it.
    """
    _, _, headers = signed_up_user
    payload = {
        "mac_id": "AA:BB:CC",
        "name": "Test Doser",
        "type": "dosing_unit",
        "http_endpoint": "http://device.local",
        "pump_configurations": [{"pump_number": 1, "chemical_name": "Water"}],
    }
    resp = await async_client.post(
        "/api/v1/devices/dosing",
        json=payload,
        headers=headers,
    )
    assert resp.status_code == 201, "Failed to create device"
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_admin_issue_token_endpoint_success(monkeypatch, async_client, create_device):
    """
    Admin can POST /admin/device/{device_id}/issue-token → 201 + JSON{device_id,token}.
    Returned token must authenticate that device.
    """
    # stub out admin auth
    dummy_admin = type("A", (), {"id": 1, "role": "superadmin"})()
    monkeypatch.setattr(get_current_admin, "__call__", lambda _: dummy_admin)

    resp = await async_client.post(f"/admin/device/{create_device}/issue-token")
    assert resp.status_code == 201, resp.text

    data = resp.json()
    assert data["device_id"] == create_device
    assert isinstance(data["token"], str) and len(data["token"]) > 10

    # newly issued token should pass verify_device_token
    new_token = data["token"]
    class Cred:
        credentials = new_token
    async with AsyncSessionLocal() as db_verify:
        v_id = await verify_device_token(Cred, db_verify, expected_type=DeviceType.DOSING_UNIT)
    assert v_id == create_device


@pytest.mark.asyncio
async def test_admin_issue_token_endpoint_unauthorized(async_client, create_device):
    """
    Without admin auth, issuing a token must be rejected.
    """
    resp = await async_client.post(f"/admin/device/{create_device}/issue-token")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_admin_issue_token_endpoint_404_for_unknown_device(monkeypatch, async_client):
    """
    POST /admin/device/{nonexistent}/issue-token should return 404.
    """
    # stub admin
    dummy_admin = type("A", (), {"id": 1, "role": "superadmin"})()
    monkeypatch.setattr(get_current_admin, "__call__", lambda _: dummy_admin)

    resp = await async_client.post("/admin/device/nonexistent/issue-token")
    assert resp.status_code == 404
    assert "not found" in resp.json().get("detail", "").lower()
