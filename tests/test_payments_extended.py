# tests/test_payments_extended.py

import datetime as dt
import pytest
from httpx import AsyncClient

from app.main import app
from app.models import PaymentStatus, SubscriptionPlan

# ─────────────────────────────────────────────────────────────────────────────
# Helpers / overrides
# ─────────────────────────────────────────────────────────────────────────────
class _DummyAdmin:
    id = 1
    role = "superadmin"
    email = "admin@example.com"
    hashed_password = "x"

async def _always_admin() -> _DummyAdmin:
    return _DummyAdmin

def _override_admin_dep():
    """Stub out admin auth for all @admin routes."""
    from app.dependencies import get_current_admin
    app.dependency_overrides[get_current_admin] = _always_admin


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
async def basic_plan(async_client: AsyncClient, signed_up_user):
    """
    Admin creates a plan with device_limit=1 for testing.
    """
    _override_admin_dep()
    resp = await async_client.post(
        "/admin/plans/",
        json={
            "name": "30-day-basic",
            "device_types": ["dosing_unit"],
            "device_limit": 1,
            "duration_days": 30,
            "price_cents": 10000,
        },
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 201
    return resp.json()["id"]

@pytest.fixture
async def dosing_device(async_client: AsyncClient, signed_up_user):
    """
    User registers a dosing device via the public endpoint.
    """
    _, _, hdrs = signed_up_user
    resp = await async_client.post(
        "/api/v1/devices/dosing",
        json={
            "mac_id": "FF:EE:AA",
            "name": "Test Doser",
            "type": "dosing_unit",
            "http_endpoint": "http://doser.local",
            "pump_configurations": [{"pump_number": 1, "chemical_name": "N"}],
        },
        headers=hdrs,
    )
    assert resp.status_code == 201
    return resp.json()["id"]





# ─────────────────────────────────────────────────────────────────────────────
# 2) Payment → Subscription creation
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_payment_happy_path_creates_subscription(async_client: AsyncClient, signed_up_user, basic_plan, dosing_device):
    _override_admin_dep()
    _, hdrs = signed_up_user

    # 1) create order
    order = (await async_client.post(
        "/api/v1/payments/create",
        json={"device_id": dosing_device, "plan_id": basic_plan},
        headers=hdrs,
    )).json()
    assert PaymentStatus(order["status"]) is PaymentStatus.PENDING
    assert order["qr_code_url"].endswith(".png")
    exp = dt.datetime.fromisoformat(order["expires_at"].rstrip("Z"))
    assert exp > dt.datetime.utcnow()

    # 2) upload proof
    up = await async_client.post(
        f"/api/v1/payments/upload/{order['id']}",
        headers=hdrs,
        files={"file": ("proof.jpg", b"\xFF\xD8\xFF", "image/jpeg")},
    )
    assert up.status_code == 200
    assert up.json()["screenshot_path"].endswith(".jpg")

    # 3) confirm → PROCESSING
    conf = (await async_client.post(
        f"/api/v1/payments/confirm/{order['id']}",
        json={"upi_transaction_id": "TXN-100"},
        headers=hdrs,
    )).json()
    assert PaymentStatus(conf["status"]) is PaymentStatus.PROCESSING
    assert conf["upi_transaction_id"] == "TXN-100"

    # 4) admin approve → COMPLETED
    done = (await async_client.post(
        f"/admin/payments/approve/{order['id']}",
        headers={"Authorization": "Bearer admin-token"},
    )).json()
    assert PaymentStatus(done["status"]) is PaymentStatus.COMPLETED

    # 5) subscription is created and active
    subs = (await async_client.get("/api/v1/subscriptions/", headers=hdrs)).json()
    assert len(subs) == 1
    sub = subs[0]
    assert sub["device_id"] == dosing_device
    assert sub["active"] is True
    start = dt.datetime.fromisoformat(sub["start_date"].rstrip("Z"))
    end = dt.datetime.fromisoformat(sub["end_date"].rstrip("Z"))
    # exactly 30 days
    assert (end - start).days == 30
    assert sub["device_limit"] == 1


@pytest.mark.asyncio
async def test_double_confirm_errors(async_client: AsyncClient, signed_up_user, basic_plan, dosing_device):
    _override_admin_dep()
    _, hdrs = signed_up_user

    order = (await async_client.post(
        "/api/v1/payments/create",
        json={"device_id": dosing_device, "plan_id": basic_plan},
        headers=hdrs,
    )).json()
    # upload...
    await async_client.post(
        f"/api/v1/payments/upload/{order['id']}",
        headers=hdrs,
        files={"file": ("a.jpg", b"x", "image/jpeg")},
    )
    # first confirm
    await async_client.post(
        f"/api/v1/payments/confirm/{order['id']}",
        json={"upi_transaction_id": "TXN-A"},
        headers=hdrs,
    )
    # second confirm
    resp2 = await async_client.post(
        f"/api/v1/payments/confirm/{order['id']}",
        json={"upi_transaction_id": "TXN-B"},
        headers=hdrs,
    )
    assert resp2.status_code == 400
    assert "current status" in resp2.json()["detail"].lower()

@pytest.mark.asyncio
async def test_admin_auth_required_for_approve(async_client: AsyncClient, signed_up_user, basic_plan, dosing_device):
    # no override
    _, hdrs = signed_up_user
    order = (await async_client.post(
        "/api/v1/payments/create",
        json={"device_id": dosing_device, "plan_id": basic_plan},
        headers=hdrs,
    )).json()
    # upload+confirm
    await async_client.post(f"/api/v1/payments/upload/{order['id']}", headers=hdrs,
                            files={"file": ("p.jpg", b"x", "image/jpeg")})
    await async_client.post(f"/api/v1/payments/confirm/{order['id']}",
                            json={"upi_transaction_id": "TXN-Z"}, headers=hdrs)
    # unauthorized approve
    r = await async_client.post(f"/admin/payments/approve/{order['id']}")
    assert r.status_code in (401, 403)

@pytest.mark.asyncio
async def test_reject_pending(async_client: AsyncClient, signed_up_user, basic_plan, dosing_device):
    _override_admin_dep()
    _, hdrs = signed_up_user
    order = (await async_client.post(
        "/api/v1/payments/create",
        json={"device_id": dosing_device, "plan_id": basic_plan},
        headers=hdrs,
    )).json()
    # upload only
    await async_client.post(f"/api/v1/payments/upload/{order['id']}", headers=hdrs,
                            files={"file": ("r.jpg", b"x", "image/jpeg")})
    # admin reject
    rej = await async_client.post(
        f"/admin/payments/reject/{order['id']}",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert rej.status_code == 200
    assert rej.json()["status"] == PaymentStatus.FAILED


# ─────────────────────────────────────────────────────────────────────────────
# 4) Expiry blocks confirm
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_expiry_blocks_confirm(async_client: AsyncClient, monkeypatch, signed_up_user, basic_plan, dosing_device):
    _override_admin_dep()
    _, hdrs = signed_up_user

    import app.routers.payments as pay_mod
    orig = pay_mod.datetime.utcnow
    # make now = 31m before real now, so order.expires_at < real now
    monkeypatch.setattr(pay_mod.datetime, "utcnow", staticmethod(lambda: orig() - dt.timedelta(minutes=31)))

    order = (await async_client.post(
        "/api/v1/payments/create",
        json={"device_id": dosing_device, "plan_id": basic_plan},
        headers=hdrs,
    )).json()

    # restore real
    monkeypatch.setattr(pay_mod.datetime, "utcnow", staticmethod(orig))

    # upload proof
    await async_client.post(
        f"/api/v1/payments/upload/{order['id']}",
        headers=hdrs,
        files={"file": ("e.jpg", b"x", "image/jpeg")},
    )
    # confirm should 400 with "expired"
    resp = await async_client.post(
        f"/api/v1/payments/confirm/{order['id']}",
        json={"upi_transaction_id": "LATE"},
        headers=hdrs,
    )
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# 6) Pro-rated Extension & Device-Linking
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_extension_and_device_linking(async_client: AsyncClient, signed_up_user, basic_plan, dosing_device):
    _override_admin_dep()
    _, hdrs = signed_up_user

    # Purchase & activate initial subscription
    ord1 = (await async_client.post(
        "/api/v1/payments/create",
        json={"device_id": dosing_device, "plan_id": basic_plan},
        headers=hdrs,
    )).json()
    await async_client.post(f"/api/v1/payments/upload/{ord1['id']}", headers=hdrs,
                            files={"file": ("1.jpg", b"x", "image/jpeg")})
    await async_client.post(f"/api/v1/payments/confirm/{ord1['id']}",
                            json={"upi_transaction_id": "TXN1"}, headers=hdrs)
    await async_client.post(f"/admin/payments/approve/{ord1['id']}",
                            headers={"Authorization": "Bearer admin-token"})

    # get subscription
    subs = (await async_client.get("/api/v1/subscriptions/", headers=hdrs)).json()
    sub = subs[0]
    sid = sub["id"]
    start = dt.datetime.fromisoformat(sub["start_date"].rstrip("Z"))
    end = dt.datetime.fromisoformat(sub["end_date"].rstrip("Z"))
    assert (end - start).days == 30

    # Half-way through period: monkeypatch now = start + 15d
    import app.routers.payments as pay_mod
    monkeypatch_time = start + dt.timedelta(days=15)
    monkeypatch.setattr(pay_mod.datetime, "utcnow", staticmethod(lambda: monkeypatch_time))

    # Create extension order — price_cents should be roughly 5000 (half of 10000)
    ext = (await async_client.post(
        "/api/v1/payments/create",
        json={"subscription_id": sid, "plan_id": basic_plan},
        headers=hdrs,
    )).json()
    assert ext["price_cents"] == 5000

    # Finish extension
    await async_client.post(f"/api/v1/payments/upload/{ext['id']}", headers=hdrs,
                            files={"file": ("2.jpg", b"x", "image/jpeg")})
    await async_client.post(f"/api/v1/payments/confirm/{ext['id']}",
                            json={"upi_transaction_id": "TXN2"}, headers=hdrs)
    await async_client.post(f"/admin/payments/approve/{ext['id']}",
                            headers={"Authorization": "Bearer admin-token"})

    # After approve, subscription end = old_end + 30d
    subs2 = (await async_client.get("/api/v1/subscriptions/", headers=hdrs)).json()[0]
    new_end = dt.datetime.fromisoformat(subs2["end_date"].rstrip("Z"))
    assert (new_end - end).days == 30

    # Device-linking up to device_limit=1 still only allows 1
    r1 = await async_client.post(
        f"/api/v1/subscriptions/{sid}/devices",
        json={"device_id": dosing_device},
        headers=hdrs,
    )
    # should succeed for the original device
    assert r1.status_code == 200

    # Register a second device
    second = (await async_client.post(
        "/api/v1/devices/dosing",
        json={
            "mac_id": "GG:HH:II",
            "name": "Extra",
            "type": "dosing_unit",
            "http_endpoint": "http://extra",
            "pump_configurations": [{"pump_number": 1, "chemical_name": "X"}],
        },
        headers=hdrs,
    )).json()["id"]

    # Now limit = 1 + (extended?) → we expect limit still 1, so this fails
    r2 = await async_client.post(
        f"/api/v1/subscriptions/{sid}/devices",
        json={"device_id": second},
        headers=hdrs,
    )
    assert r2.status_code == 400
    assert "limit" in r2.json()["detail"].lower()
