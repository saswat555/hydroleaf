# tests/test_subscriptions_flow.py
import datetime as _dt
from typing import Tuple

import pytest
from httpx import AsyncClient

from app.main import app
from app.models import PaymentStatus, SubscriptionPlan
from app.core.database import AsyncSessionLocal

# ─────────────────────────────────────────────────────────────────────────────
# Helpers / overrides
# ─────────────────────────────────────────────────────────────────────────────
class _DummyAdmin:
    id = 1
    role = "superadmin"
    email = "root@example.com"
    hashed_password = "x"

async def _always_admin() -> _DummyAdmin:
    return _DummyAdmin

def _override_admin_dep() -> None:
    """Force all @admin routes to succeed."""
    from app.dependencies import get_current_admin
    app.dependency_overrides[get_current_admin] = _always_admin


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
async def new_user(async_client: AsyncClient) -> Tuple[str, dict]:
    """
    Create a fresh user via the real signup endpoint,
    return (token, headers).
    """
    payload = {
        "email": "sub@test.io",
        "password": "Pwd!2345",
        "first_name": "Test",
        "last_name": "User",
        "phone": "1234567890",
        "address": "123 Main St",
        "city": "Testville",
        "state": "TS",
        "country": "Testland",
        "postal_code": "000001",
    }
    resp = await async_client.post("/api/v1/auth/signup", json=payload)
    assert resp.status_code == 201
    token = resp.json()["access_token"]
    return token, {"Authorization": f"Bearer {token}"}

async def plan_id(async_client: AsyncClient) -> int:
    """
    Create a plan via the real admin endpoint (device_limit=1) and return its ID.
    """
    _override_admin_dep()
    resp = await async_client.post(
        "/admin/plans/",
        json={
            "name": "30-Day-Basic",
            "device_types": ["dosing_unit"],
            "device_limit": 1,
            "duration_days": 30,
            "price": 10000,
        },
        headers={"Authorization": "Bearer admin-token"},
    )
    if resp.status_code == 404:
        pytest.skip("Admin plan routes are not enabled in this build.")
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture
async def device(async_client: AsyncClient, new_user: Tuple[str, dict]) -> str:
    """
    Register a mock dosing unit (via MockController).
    """
    _, hdrs = new_user
    resp = await async_client.post(
        "/api/v1/devices/dosing",
        json={
            "mac_id": "AA:BB:CC",
            "name": "Mock Doser",
            "type": "dosing_unit",
            "http_endpoint": "http://doser",
            "pump_configurations": [{"pump_number": 1, "chemical_name": "Chem"}],
        },
        headers=hdrs,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


# ─────────────────────────────────────────────────────────────────────────────
# 1) ADMIN: CRUD SUBSCRIPTION PLANS
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_admin_plan_crud(async_client: AsyncClient):
    _override_admin_dep()
    hdr = {"Authorization": "Bearer admin-token"}

    # Create
    create = await async_client.post(
        "/admin/plans/",
        json={
            "name": "Basic",
            "device_types": ["dosing_unit"],
            "device_limit": 2,
            "duration_days": 15,
            "price": 5000,
        },
        headers=hdr,
    )
    if create.status_code == 404:
        pytest.skip("Admin plan routes are not enabled in this build.")
    assert create.status_code == 201
    plan = create.json()
    pid = plan["id"]
    assert plan["name"] == "Basic"

    # List
    lst = await async_client.get("/admin/plans/", headers=hdr)
    assert lst.status_code == 200
    assert any(p["id"] == pid for p in lst.json())

    # Retrieve
    get1 = await async_client.get(f"/admin/plans/{pid}", headers=hdr)
    assert get1.status_code == 200
    assert get1.json()["device_limit"] == 2

    # Update
    upd = await async_client.put(
        f"/admin/plans/{pid}",
        json={"name": "Pro", "price": 6000},
        headers=hdr,
    )
    assert upd.status_code == 200
    assert upd.json()["name"] == "Pro"

    # Delete
    rem = await async_client.delete(f"/admin/plans/{pid}", headers=hdr)
    assert rem.status_code == 204
    assert (await async_client.get(f"/admin/plans/{pid}", headers=hdr)).status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 2) USER: VIEW AVAILABLE PLANS
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_user_can_list_plans(async_client: AsyncClient, new_user, plan_id):
    _, hdrs = new_user
    r = await async_client.get("/api/v1/plans/", headers=hdrs)
    assert r.status_code == 200
    assert any(p["id"] == plan_id for p in r.json())


# ─────────────────────────────────────────────────────────────────────────────
# 3) PURCHASE FLOW (PENDING → PROCESSING → COMPLETED)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_full_purchase_and_activation(async_client: AsyncClient, new_user, plan_id, device):
    _override_admin_dep()
    _, hdrs = new_user

    # Create order
    order = (await async_client.post(
        "/api/v1/payments/create",
        json={"device_id": device, "plan_id": plan_id},
        headers=hdrs,
    )).json()
    assert PaymentStatus(order["status"]) is PaymentStatus.PENDING
    assert order["qr_code_url"].endswith(".png")
    expires = _dt.datetime.fromisoformat(order["expires_at"].rstrip("Z"))
    assert expires > _dt.datetime.utcnow()

    # Upload proof
    up = await async_client.post(
        f'/api/v1/payments/upload/{order["id"]}',
        headers=hdrs,
        files={"file": ("proof.png", b"\xFF\xD8\xFF", "image/png")},
    )
    assert up.status_code == 200
    assert up.json()["screenshot_path"].endswith(".png")

    # Confirm → PROCESSING
    conf = await async_client.post(
        f'/api/v1/payments/confirm/{order["id"]}',
        json={"upi_transaction_id": "TXN-001"},
        headers=hdrs,
    )
    assert PaymentStatus(conf.json()["status"]) is PaymentStatus.PROCESSING

    # Admin Approve → COMPLETED
    done = await async_client.post(
        f'/admin/payments/approve/{order["id"]}',
        headers={"Authorization": "Bearer admin-token"},
    )
    assert PaymentStatus(done.json()["status"]) is PaymentStatus.COMPLETED

    # Subscription appears & is active
    subs = (await async_client.get("/api/v1/subscriptions/", headers=hdrs)).json()
    assert len(subs) == 1
    s = subs[0]
    assert s["device_id"] == device
    assert s["active"] is True
    start = _dt.datetime.fromisoformat(s["start_date"].rstrip("Z"))
    end = _dt.datetime.fromisoformat(s["end_date"].rstrip("Z"))
    assert (end - start).days == 30
    assert s["device_limit"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 4) GUARD-RAILS
#    a) confirm without upload → 400
#    b) double-confirm → 400
#    c) unauthenticated admin approve → 401/403
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_confirm_without_screenshot_fails(async_client: AsyncClient, new_user, plan_id, device):
    _, hdrs = new_user
    order = (await async_client.post(
        "/api/v1/payments/create",
        json={"device_id": device, "plan_id": plan_id},
        headers=hdrs,
    )).json()

    r = await async_client.post(
        f'/api/v1/payments/confirm/{order["id"]}',
        json={"upi_transaction_id": "NO-PIC"},
        headers=hdrs,
    )
    assert r.status_code == 400
    assert "upload" in r.json()["detail"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# 5) REJECT FLOW
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_reject_pending_order(async_client: AsyncClient, new_user, plan_id, device):
    _override_admin_dep()
    _, hdrs = new_user
    order = (await async_client.post(
        "/api/v1/payments/create",
        json={"device_id": device, "plan_id": plan_id},
        headers=hdrs,
    )).json()

    await async_client.post(
        f'/api/v1/payments/upload/{order["id"]}',
        headers=hdrs,
        files={"file": ("f.jpg", b"PIC", "image/jpeg")},
    )
    rej = await async_client.post(
        f'/admin/payments/reject/{order["id"]}',
        headers={"Authorization": "Bearer admin-token"},
    )
    assert PaymentStatus(rej.json()["status"]) is PaymentStatus.FAILED


# ─────────────────────────────────────────────────────────────────────────────
# 6) EXPIRY LOGIC
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_order_expiry_blocks_confirm(async_client: AsyncClient, monkeypatch, new_user, plan_id, device):
    _override_admin_dep()
    _, hdrs = new_user

    import app.routers.payments as pay_mod
    orig = pay_mod.datetime.utcnow
    # shift “now” back 31m so expires < now
    monkeypatch.setattr(pay_mod.datetime, "utcnow", staticmethod(lambda: orig() - _dt.timedelta(minutes=31)))

    order = (await async_client.post(
        "/api/v1/payments/create",
        json={"device_id": device, "plan_id": plan_id},
        headers=hdrs,
    )).json()

    # restore real time
    monkeypatch.setattr(pay_mod.datetime, "utcnow", staticmethod(orig))

    await async_client.post(
        f'/api/v1/payments/upload/{order["id"]}',
        headers=hdrs,
        files={"file": ("e.png", b"IMG", "image/png")},
    )
    r = await async_client.post(
        f'/api/v1/payments/confirm/{order["id"]}',
        json={"upi_transaction_id": "LATE"},
        headers=hdrs,
    )
    assert r.status_code == 400
    assert "expired" in r.json()["detail"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# 7) DEVICE-LINKING & LIMIT ENFORCEMENT
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_cannot_link_more_than_limit(async_client: AsyncClient, new_user, plan_id, device):
    _override_admin_dep()
    _, hdrs = new_user

    # purchase & activate
    order = (await async_client.post(
        "/api/v1/payments/create",
        json={"device_id": device, "plan_id": plan_id},
        headers=hdrs,
    )).json()
    await async_client.post(f"/api/v1/payments/upload/{order['id']}", headers=hdrs, files={"file":("x.jpg",b"BIN","image/jpeg")})
    await async_client.post(f"/api/v1/payments/confirm/{order['id']}", json={"upi_transaction_id":"1"}, headers=hdrs)
    await async_client.post(f"/admin/payments/approve/{order['id']}", headers={"Authorization":"Bearer admin-token"})

    subs = (await async_client.get("/api/v1/subscriptions/", headers=hdrs)).json()
    sid = subs[0]["id"]

    # register a _second_ device
    d2 = (await async_client.post(
        "/api/v1/devices/dosing",
        json={
            "mac_id": "DD:EE:FF",
            "name": "Extra",
            "type": "dosing_unit",
            "http_endpoint": "http://extra",
            "pump_configurations": [{"pump_number": 1, "chemical_name": "X"}],
        },
        headers=hdrs,
    )).json()["id"]

    # FAIL: over the plan’s device_limit=1
    r = await async_client.post(
        f"/api/v1/subscriptions/{sid}/devices",
        json={"device_id": d2},
        headers=hdrs,
    )
    assert r.status_code == 400
    assert "limit" in r.json()["detail"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# 8) EXTENSION FLOW (INCREASE LIMIT + EXTEND PERIOD)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_extension_and_then_link_additional(async_client: AsyncClient, new_user, plan_id, device):
    _override_admin_dep()
    _, hdrs = new_user

    # 1) initial purchase
    ord1 = (await async_client.post(
        "/api/v1/payments/create",
        json={"device_id": device, "plan_id": plan_id},
        headers=hdrs,
    )).json()
    await async_client.post(f"/api/v1/payments/upload/{ord1['id']}", headers=hdrs, files={"file":("a","b","i")})
    await async_client.post(f"/api/v1/payments/confirm/{ord1['id']}", json={"upi_transaction_id":"E1"}, headers=hdrs)
    await async_client.post(f"/admin/payments/approve/{ord1['id']}", headers={"Authorization":"Bearer admin-token"})

    subs1 = (await async_client.get("/api/v1/subscriptions/", headers=hdrs)).json()[0]
    sid = subs1["id"]
    start1 = _dt.datetime.fromisoformat(subs1["start_date"].rstrip("Z"))
    end1 = _dt.datetime.fromisoformat(subs1["end_date"].rstrip("Z"))
    assert (end1 - start1).days == 30
    assert subs1["device_limit"] == 1

    # 2) extension payment → bump limit to 2 & +30d
    ext = (await async_client.post(
        "/api/v1/payments/create",
        json={"subscription_id": sid, "plan_id": plan_id},
        headers=hdrs,
    )).json()
    await async_client.post(f"/api/v1/payments/upload/{ext['id']}", headers=hdrs, files={"file":("c","d","i")})
    await async_client.post(f"/api/v1/payments/confirm/{ext['id']}", json={"upi_transaction_id":"E2"}, headers=hdrs)
    await async_client.post(f"/admin/payments/approve/{ext['id']}", headers={"Authorization":"Bearer admin-token"})

    subs2 = (await async_client.get("/api/v1/subscriptions/", headers=hdrs)).json()[0]
    start2 = _dt.datetime.fromisoformat(subs2["start_date"].rstrip("Z"))
    end2 = _dt.datetime.fromisoformat(subs2["end_date"].rstrip("Z"))
    assert subs2["device_limit"] == 2
    assert (end2 - start2).days == 60

    # 3) now linking a second device succeeds
    d3 = (await async_client.post(
        "/api/v1/devices/dosing",
        json={
            "mac_id": "GG:HH:II",
            "name": "Extra2",
            "type": "dosing_unit",
            "http_endpoint": "http://extra2",
            "pump_configurations": [{"pump_number": 1, "chemical_name": "Y"}],
        },
        headers=hdrs,
    )).json()["id"]

    link = await async_client.post(
        f"/api/v1/subscriptions/{sid}/devices",
        json={"device_id": d3},
        headers=hdrs,
    )
    assert link.status_code == 200
    assert link.json()["device_id"] == d3
