# tests/test_subscriptions_flow.py
"""
End-to-end verification of the subscription life-cycle.

Scenarios
---------
1.  Happy-path:  SIGN-UP → PLAN → DEVICE → ORDER (PENDING → PROCESSING → COMPLETED)
2.  Guard-rails:
      a. confirm *without* screenshot  → 400
      b. double confirm                → 400
      c. unauthenticated admin approve → 401 / 403
3.  Reject flow: PENDING  → FAILED        (admin rejects)
4.  Expiry logic: already-expired order cannot be confirmed.
"""

from __future__ import annotations

import datetime as _dt
from typing import Tuple

import pytest
from httpx import AsyncClient

from app.main import app
from app.models import PaymentStatus


# ─────────────────────────────────────────────────────────────────────────────
# Helpers / overrides
# ─────────────────────────────────────────────────────────────────────────────
class _DummyAdmin:
    id = 1
    role = "superadmin"
    email = "root@example.com"
    hashed_password = "x"


async def _always_admin() -> _DummyAdmin:  # pragma: no cover
    return _DummyAdmin


def _override_admin_dep() -> None:
    """Force all admin-protected routes to succeed."""
    from app.dependencies import get_current_admin

    app.dependency_overrides[get_current_admin] = _always_admin


# ─────────────────────────────────────────────────────────────────────────────
# Fixture factories
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
async def new_user(async_client: AsyncClient) -> Tuple[str, dict]:
    """Sign-up a user and return ``(token, headers)``."""
    resp = await async_client.post(
        "/api/v1/auth/signup",
        json={
            "email": "sub@test.io",
            "password": "pwd",
            "name": "grower",
            "location": "blr",
        },
    )
    token = resp.json()["access_token"]
    return token, {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def plan(async_client: AsyncClient) -> str:
    """Create a 30-day dosing plan (admin)."""
    _override_admin_dep()
    resp = await async_client.post(
        "/admin/plans/",
        json={
            "name": "30-Day-Basic",
            "device_types": ["dosing_unit"],
            "duration_days": 30,
            "price_cents": 123_45,
        },
        headers={"Authorization": "Bearer any"},
    )
    return resp.json()["id"]


@pytest.fixture
async def device(async_client: AsyncClient, new_user: Tuple[str, dict]) -> str:
    """Register a mock dosing unit (discovered via MockController)."""
    _, hdrs = new_user
    resp = await async_client.post(
        "/api/v1/devices/dosing",
        json={
            "mac_id": "AA:BB",
            "name": "Mock Doser",
            "type": "dosing_unit",
            "http_endpoint": "http://dosing",  # triggers tests.conftest.MockController
            "pump_configurations": [{"pump_number": 1, "chemical_name": "N"}],
        },
        headers=hdrs,
    )
    return resp.json()["id"]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Happy-path
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_subscription_happy_path(async_client: AsyncClient, new_user, plan, device):
    _override_admin_dep()
    _, hdrs = new_user

    # ── 1) create payment order ────────────────────────────────────────────
    order = (
        await async_client.post(
            "/api/v1/payments/create", json={"device_id": device, "plan_id": plan}, headers=hdrs
        )
    ).json()
    assert PaymentStatus(order["status"]) is PaymentStatus.PENDING
    assert order["qr_code_url"].endswith(".png")
    expires_at = _dt.datetime.fromisoformat(order["expires_at"].rstrip("Z"))
    assert expires_at > _dt.datetime.utcnow()  # in the future

    # ── 2) upload screenshot ───────────────────────────────────────────────
    up = await async_client.post(
        f'/api/v1/payments/upload/{order["id"]}',
        headers=hdrs,
        files={"file": ("proof.jpg", b"\xFF\xD8\xFF", "image/jpeg")},
    )
    assert up.status_code == 200
    assert up.json()["screenshot_path"].endswith(".jpg")

    # ── 3) confirm (→ PROCESSING) ───────────────────────────────────────────
    conf = await async_client.post(
        f'/api/v1/payments/confirm/{order["id"]}',
        json={"upi_transaction_id": "TXN-001"},
        headers=hdrs,
    )
    assert PaymentStatus(conf.json()["status"]) is PaymentStatus.PROCESSING
    assert conf.json()["upi_transaction_id"] == "TXN-001"

    # ── 4) admin approve (→ COMPLETED) ──────────────────────────────────────
    done = await async_client.post(
        f'/admin/payments/approve/{order["id"]}', headers={"Authorization": "Bearer any"}
    )
    assert PaymentStatus(done.json()["status"]) is PaymentStatus.COMPLETED

    # ── 5) subscription visible & active ───────────────────────────────────
    subs = (await async_client.get("/api/v1/subscriptions/", headers=hdrs)).json()
    assert len(subs) == 1
    sub = subs[0]
    assert sub["device_id"] == device
    assert sub["active"] is True
    # dates sanity
    start = _dt.datetime.fromisoformat(sub["start_date"].rstrip("Z"))
    end = _dt.datetime.fromisoformat(sub["end_date"].rstrip("Z"))
    assert (end - start).days == 30


# ─────────────────────────────────────────────────────────────────────────────
# 2. Guard-rails
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_confirm_without_screenshot(async_client: AsyncClient, new_user, plan, device):
    _, hdrs = new_user
    order = (
        await async_client.post(
            "/api/v1/payments/create", json={"device_id": device, "plan_id": plan}, headers=hdrs
        )
    ).json()

    r = await async_client.post(
        f'/api/v1/payments/confirm/{order["id"]}',
        json={"upi_transaction_id": "NO-PIC"},
        headers=hdrs,
    )
    assert r.status_code == 400
    assert "upload" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_double_confirm(async_client: AsyncClient, new_user, plan, device):
    _override_admin_dep()
    _, hdrs = new_user
    order = (
        await async_client.post(
            "/api/v1/payments/create", json={"device_id": device, "plan_id": plan}, headers=hdrs
        )
    ).json()
    await async_client.post(
        f'/api/v1/payments/upload/{order["id"]}',
        headers=hdrs,
        files={"file": ("proof.jpg", b"IMG", "image/jpeg")},
    )
    await async_client.post(
        f'/api/v1/payments/confirm/{order["id"]}',
        json={"upi_transaction_id": "FIRST"},
        headers=hdrs,
    )
    second = await async_client.post(
        f'/api/v1/payments/confirm/{order["id"]}',
        json={"upi_transaction_id": "SECOND"},
        headers=hdrs,
    )
    assert second.status_code == 400
    assert "current status" in second.json()["detail"].lower()


@pytest.mark.asyncio
async def test_admin_auth_required(async_client: AsyncClient, new_user, plan, device):
    _, hdrs = new_user
    order = (
        await async_client.post(
            "/api/v1/payments/create", json={"device_id": device, "plan_id": plan}, headers=hdrs
        )
    ).json()
    await async_client.post(
        f'/api/v1/payments/upload/{order["id"]}',
        headers=hdrs,
        files={"file": ("proof.jpg", b"BIN", "image/jpeg")},
    )
    await async_client.post(
        f'/api/v1/payments/confirm/{order["id"]}',
        json={"upi_transaction_id": "TXN"},
        headers=hdrs,
    )

    # no admin override here → expect 401 / 403
    unauth = await async_client.post(f'/admin/payments/approve/{order["id"]}')
    assert unauth.status_code in (401, 403)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Reject flow (PENDING → FAILED)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_reject_flow(async_client: AsyncClient, new_user, plan, device):
    _override_admin_dep()
    _, hdrs = new_user
    order = (
        await async_client.post(
            "/api/v1/payments/create", json={"device_id": device, "plan_id": plan}, headers=hdrs
        )
    ).json()
    await async_client.post(
        f'/api/v1/payments/upload/{order["id"]}',
        headers=hdrs,
        files={"file": ("proof.jpg", b"PIC", "image/jpeg")},
    )
    rej = await async_client.post(
        f'/admin/payments/reject/{order["id"]}', headers={"Authorization": "Bearer any"}
    )
    assert PaymentStatus(rej.json()["status"]) is PaymentStatus.FAILED


# ─────────────────────────────────────────────────────────────────────────────
# 4. Expiry logic
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_order_expired_cannot_confirm(async_client: AsyncClient, monkeypatch, new_user, plan, device):
    _override_admin_dep()
    _, hdrs = new_user

    # Patch datetime.utcnow **before** hitting /create so order gets past timestamp
    import app.routers.payments as pay_mod

    orig_utcnow = pay_mod.datetime.utcnow

    def _utc_past() -> _dt.datetime:
        return orig_utcnow() - _dt.timedelta(minutes=20)

    # monkeypatch the *method* on the datetime class
    monkeypatch.setattr(pay_mod.datetime, "utcnow", staticmethod(_utc_past))

    order = (
        await async_client.post(
            "/api/v1/payments/create", json={"device_id": device, "plan_id": plan}, headers=hdrs
        )
    ).json()

    # restore clock for the remainder of the flow
    monkeypatch.setattr(pay_mod.datetime, "utcnow", staticmethod(orig_utcnow))

    # proof uploaded so the only reason to fail is expiry
    await async_client.post(
        f'/api/v1/payments/upload/{order["id"]}',
        headers=hdrs,
        files={"file": ("proof.jpg", b"IMG", "image/jpeg")},
    )

    r = await async_client.post(
        f'/api/v1/payments/confirm/{order["id"]}',
        json={"upi_transaction_id": "LATE"},
        headers=hdrs,
    )
    assert r.status_code == 400
    assert "expired" in r.json()["detail"].lower()
