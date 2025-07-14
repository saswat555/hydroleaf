# tests/test_subscriptions_flow.py
"""
End-to-end tests that exercise:
1.  User sign-up  →  plan creation  →  device registration
2.  Payment order life-cycle (PENDING → PROCESSING → COMPLETED)
3.  Automatic expiry logic (order EXPIRED  → confirm must fail)

We rely on the `async_client` fixture defined in tests/conftest.py and the
MockController that makes device discovery deterministic.
"""

import datetime
import json
import pytest

from httpx import AsyncClient
from app.main import app
from app.models import PaymentStatus


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
class _DummyAdmin:  # minimal object with the attrs used in deps
    id   = 1
    role = "superadmin"
    email = "root@example.com"
    hashed_password = "x"


def _override_admin_dep():
    """
    FastAPI dependency override that always returns a _DummyAdmin.
    (We don’t care about password verification in these tests.)
    """
    from app.dependencies import get_current_admin

    async def _dummy():
        return _DummyAdmin

    app.dependency_overrides[get_current_admin] = _dummy


# --------------------------------------------------------------------------- #
# 1) Happy-path                                                               #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_full_payment_lifecycle(async_client: AsyncClient, monkeypatch):
    _override_admin_dep()  # allow all /admin/* calls

    # 1) ─── user sign-up ────────────────────────────────────────────────────
    r = await async_client.post(
        "/api/v1/auth/signup",
        json={
            "email": "u1@example.com",
            "password": "p",
            "name": "farm-owner",
            "location": "blr",
        },
    )
    assert r.status_code == 200
    user_token = r.json()["access_token"]
    user_hdrs = {"Authorization": f"Bearer {user_token}"}

    # 2) ─── admin creates a plan ───────────────────────────────────────────
    admin_hdrs = {"Authorization": "Bearer whatever"}  # token value irrelevant now
    r = await async_client.post(
        "/admin/plans/",
        json={
            "name": "Basic-30",
            "device_types": ["dosing_unit"],
            "duration_days": 30,
            "price_cents": 12_345,
        },
        headers=admin_hdrs,
    )
    assert r.status_code == 201
    plan_id = r.json()["id"]

    # 3) ─── user registers a *mock* dosing device ──────────────────────────
    r = await async_client.post(
        "/api/v1/devices/dosing",
        json={
            "mac_id": "AA:BB",
            "name": "Mock Doser",
            "type": "dosing_unit",
            "http_endpoint": "http://dosing",  # triggers MockController discovery
            "pump_configurations": [{"pump_number": 1, "chemical_name": "N"}],
        },
        headers=user_hdrs,
    )
    assert r.status_code == 200
    device_id = r.json()["id"]

    # 4) ─── create payment order ───────────────────────────────────────────
    r = await async_client.post(
        "/api/v1/payments/create",
        json={"device_id": device_id, "plan_id": plan_id},
        headers=user_hdrs,
    )
    assert r.status_code == 201
    order = r.json()
    assert order["status"] == PaymentStatus.PENDING

    # 5) ─── upload proof screenshot ────────────────────────────────────────
    proof_resp = await async_client.post(
        f'/api/v1/payments/upload/{order["id"]}',
        headers=user_hdrs,
        files={"file": ("proof.jpg", b"JPEG-DATA", "image/jpeg")},
    )
    assert proof_resp.status_code == 200
    assert proof_resp.json()["screenshot_path"]

    # 6) ─── confirm payment (user) ─────────────────────────────────────────
    r = await async_client.post(
        f'/api/v1/payments/confirm/{order["id"]}',
        json={"upi_transaction_id": "TXN-123"},
        headers=user_hdrs,
    )
    assert r.status_code == 200
    assert r.json()["status"] == PaymentStatus.PROCESSING

    # 7) ─── approve payment (admin) ────────────────────────────────────────
    r = await async_client.post(
        f'/admin/payments/approve/{order["id"]}', headers=admin_hdrs
    )
    assert r.status_code == 200
    assert r.json()["status"] == PaymentStatus.COMPLETED

    # 8) ─── user should now have an active subscription ────────────────────
    subs = (
        await async_client.get("/api/v1/subscriptions/", headers=user_hdrs)
    ).json()
    assert len(subs) == 1
    assert subs[0]["active"] is True
    assert subs[0]["device_id"] == device_id


# --------------------------------------------------------------------------- #
# 2) Expired orders cannot be confirmed                                       #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_order_expiry(async_client: AsyncClient, monkeypatch):
    _override_admin_dep()

    # --- sign-up user --------------------------------------------------------
    r = await async_client.post(
        "/api/v1/auth/signup",
        json={
            "email": "u2@example.com",
            "password": "p",
            "name": "foo",
            "location": "blr",
        },
    )
    user_token = r.json()["access_token"]
    hdrs = {"Authorization": f"Bearer {user_token}"}

    # --- create plan ---------------------------------------------------------
    plan_id = (
        await async_client.post(
            "/admin/plans/",
            json={
                "name": "Exp-Plan",
                "device_types": ["dosing_unit"],
                "duration_days": 30,
                "price_cents": 5000,
            },
            headers={"Authorization": "Bearer x"},
        )
    ).json()["id"]

    # --- register device -----------------------------------------------------
    device_id = (
        await async_client.post(
            "/api/v1/devices/dosing",
            json={
                "mac_id": "CC:DD",
                "name": "Mock Doser 2",
                "type": "dosing_unit",
                "http_endpoint": "http://dosing2",
                "pump_configurations": [{"pump_number": 1, "chemical_name": "P"}],
            },
            headers=hdrs,
        )
    ).json()["id"]

    # ------------------------------------------------------------------------
    # monkey-patch *datetime.utcnow* inside the payments router **before**
    # we hit /payments/create so that the order is already expired.
    # ------------------------------------------------------------------------
    import app.routers.payments as pay_mod

    original_utcnow = pay_mod.datetime.utcnow

    def _past():
        return original_utcnow() - datetime.timedelta(minutes=20)

    monkeypatch.setattr(pay_mod.datetime, "utcnow", _past)

    # --- create order (will carry past expiry) -------------------------------
    order = (
        await async_client.post(
            "/api/v1/payments/create",
            json={"device_id": device_id, "plan_id": plan_id},
            headers=hdrs,
        )
    ).json()
    assert order["status"] == PaymentStatus.PENDING

    # revert utcnow patch so *confirm* uses real current time
    monkeypatch.setattr(pay_mod.datetime, "utcnow", original_utcnow)

    # --- upload screenshot so failure is purely because of expiry ------------
    await async_client.post(
        f'/api/v1/payments/upload/{order["id"]}',
        headers=hdrs,
        files={"file": ("proof.jpg", b"IMG", "image/jpeg")},
    )

    # --- confirm should now fail with 400 ------------------------------------
    r = await async_client.post(
        f'/api/v1/payments/confirm/{order["id"]}',
        json={"upi_transaction_id": "LATE-TXN"},
        headers=hdrs,
    )
    assert r.status_code == 400
    assert "expired" in r.json()["detail"].lower()
