# tests/test_payments_extended.py
import datetime
import pytest
from httpx import AsyncClient
from app.main import app
from app.models import PaymentStatus


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
class _DummyAdmin:
    id = 1
    role = "superadmin"
    email = "admin@example.com"
    hashed_password = "x"


async def _always_admin():
    return _DummyAdmin


def _override_admin_dep():
    from app.dependencies import get_current_admin
    app.dependency_overrides[get_current_admin] = _always_admin


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def signed_up_user(async_client):
    """
    Quickly sign up a user and return (token, headers).
    """
    resp = await async_client.post(
        "/api/v1/auth/signup",
        json={"email": "pay@example.com", "password": "p", "name": "n", "location": "blr"},
    )
    token = resp.json()["access_token"]
    return token, {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def basic_plan(async_client):
    _override_admin_dep()
    resp = await async_client.post(
        "/admin/plans/",
        json={
            "name": "30-day",
            "device_types": ["dosing_unit"],
            "duration_days": 30,
            "price_cents": 9999,
        },
        headers={"Authorization": "Bearer x"},
    )
    return resp.json()["id"]


@pytest.fixture
async def dosing_device(async_client, signed_up_user):
    _, hdrs = signed_up_user
    resp = await async_client.post(
        "/api/v1/devices/dosing",
        json={
            "mac_id": "FF:EE",
            "name": "Test Doser",
            "type": "dosing_unit",
            "http_endpoint": "http://dosing",  # picked up by MockController
            "pump_configurations": [{"pump_number": 1, "chemical_name": "N"}],
        },
        headers=hdrs,
    )
    return resp.json()["id"]


# --------------------------------------------------------------------------- #
# 1) Happy-path still works (sanity check)                                     #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_payment_happy_path(async_client, signed_up_user, basic_plan, dosing_device):
    _override_admin_dep()
    token, hdrs = signed_up_user

    # create → upload proof → confirm
    order = (
        await async_client.post(
            "/api/v1/payments/create", json={"device_id": dosing_device, "plan_id": basic_plan}, headers=hdrs
        )
    ).json()
    assert order["status"] == PaymentStatus.PENDING

    await async_client.post(
        f'/api/v1/payments/upload/{order["id"]}',
        headers=hdrs,
        files={"file": ("proof.jpg", b"BIN", "image/jpeg")},
    )

    order = (
        await async_client.post(
            f'/api/v1/payments/confirm/{order["id"]}',
            json={"upi_transaction_id": "TXN-1"},
            headers=hdrs,
        )
    ).json()
    assert order["status"] == PaymentStatus.PROCESSING

    # admin approves
    order = (
        await async_client.post(
            f'/admin/payments/approve/{order["id"]}',
            headers={"Authorization": "Bearer x"},
        )
    ).json()
    assert order["status"] == PaymentStatus.COMPLETED


# --------------------------------------------------------------------------- #
# 2) Reject flow                                                               #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_payment_reject_flow(async_client, signed_up_user, basic_plan, dosing_device):
    _override_admin_dep()
    _, hdrs = signed_up_user

    order = (
        await async_client.post(
            "/api/v1/payments/create", json={"device_id": dosing_device, "plan_id": basic_plan}, headers=hdrs
        )
    ).json()

    # upload proof but *do not* confirm yet
    await async_client.post(
        f'/api/v1/payments/upload/{order["id"]}',
        headers=hdrs,
        files={"file": ("proof.jpg", b"BIN", "image/jpeg")},
    )

    # admin rejects directly from PENDING
    r = await async_client.post(
        f'/admin/payments/reject/{order["id"]}',
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == PaymentStatus.FAILED


# --------------------------------------------------------------------------- #
# 3) Confirm without proof                                                     #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_confirm_without_screenshot_fails(async_client, signed_up_user, basic_plan, dosing_device):
    _, hdrs = signed_up_user
    order = (
        await async_client.post(
            "/api/v1/payments/create", json={"device_id": dosing_device, "plan_id": basic_plan}, headers=hdrs
        )
    ).json()

    r = await async_client.post(
        f'/api/v1/payments/confirm/{order["id"]}',
        json={"upi_transaction_id": "TXN-1"},
        headers=hdrs,
    )
    assert r.status_code == 400
    assert "upload" in r.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# 4) Double confirm idempotency                                                #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_double_confirm_is_noop(async_client, signed_up_user, basic_plan, dosing_device):
    _override_admin_dep()
    _, hdrs = signed_up_user
    order = (
        await async_client.post(
            "/api/v1/payments/create", json={"device_id": dosing_device, "plan_id": basic_plan}, headers=hdrs
        )
    ).json()

    await async_client.post(
        f'/api/v1/payments/upload/{order["id"]}',
        headers=hdrs,
        files={"file": ("proof.jpg", b"BIN", "image/jpeg")},
    )

    # first confirm
    await async_client.post(
        f'/api/v1/payments/confirm/{order["id"]}',
        json={"upi_transaction_id": "TXN-X"},
        headers=hdrs,
    )

    # second confirm should error 400
    r = await async_client.post(
        f'/api/v1/payments/confirm/{order["id"]}',
        json={"upi_transaction_id": "TXN-Y"},
        headers=hdrs,
    )
    assert r.status_code == 400
    assert "current status" in r.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# 5) Unauthorized approve must fail                                            #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_admin_auth_required_for_approve(async_client, signed_up_user, basic_plan, dosing_device):
    token, hdrs = signed_up_user
    order = (
        await async_client.post(
            "/api/v1/payments/create", json={"device_id": dosing_device, "plan_id": basic_plan}, headers=hdrs
        )
    ).json()

    await async_client.post(
        f'/api/v1/payments/upload/{order["id"]}',
        headers=hdrs,
        files={"file": ("proof.jpg", b"BIN", "image/jpeg")},
    )
    await async_client.post(
        f'/api/v1/payments/confirm/{order["id"]}',
        json={"upi_transaction_id": "TXN-Z"},
        headers=hdrs,
    )

    # no dependency override → real admin auth kicks in → expect 401/403
    r = await async_client.post(f'/admin/payments/approve/{order["id"]}')
    assert r.status_code in (401, 403)
