# tests/test_subscriptions_edge_cases.py

import datetime as dt
import pytest
from httpx import AsyncClient
from app.main import app
from app.models import PaymentStatus
import uuid

# ─────────────────────────────────────────────────────────────────────────────
# Admin‐override helper (so we can test both protected & unprotected flows)
# ─────────────────────────────────────────────────────────────────────────────
class _DummyAdmin:
    id = 1
    role = "superadmin"
    email = "admin@example.com"
    hashed_password = "x"

async def _always_admin() -> _DummyAdmin:
    return _DummyAdmin

def _override_admin_dep() -> None:
    """
    Stub out get_current_admin to always return a valid admin.
    """
    from app.dependencies import get_current_admin
    app.dependency_overrides[get_current_admin] = _always_admin

NONEXISTENT_ID = "00000000-0000-0000-0000-000000000001"
# ─────────────────────────────────────────────────────────────────────────────
# 1) Every /admin/* route must reject non‐admins
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_admin_endpoints_require_admin(async_client: AsyncClient):
    # plan‐CRUD
    r1 = await async_client.post("/admin/plans/", json={})
    assert r1.status_code in (401, 403, 404) 
    r2 = await async_client.put(f"/admin/plans/{NONEXISTENT_ID}", json={})
    assert r2.status_code in (401, 403, 404)
    r3 = await async_client.delete(f"/admin/plans/{NONEXISTENT_ID}")
    assert r3.status_code in (401, 403, 404)

    # payments approve/reject
    r4 = await async_client.post(f"/admin/payments/approve/{NONEXISTENT_ID}")
    assert r4.status_code in (401, 403, 404)
    r5 = await async_client.post(f"/admin/payments/reject/{NONEXISTENT_ID}")
    assert r5.status_code in (401, 403, 404)


# ─────────────────────────────────────────────────────────────────────────────
# 2) Creating a payment with bogus IDs → 404
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_create_payment_invalid_ids(async_client: AsyncClient,
                                          new_user, plan_id, device):
    _, hdrs = new_user

    # nonexistent device
    bad_dev = await async_client.post(
        "/api/v1/payments/create",
        json={"device_id": str(uuid.uuid4()), "plan_id": plan_id},
        headers=hdrs,
    )
    assert bad_dev.status_code == 404

    # nonexistent plan
    bad_plan = await async_client.post(
        "/api/v1/payments/create",
        json={"device_id": device, "plan_id": str(uuid.uuid4())},
        headers=hdrs,
    )
    assert bad_plan.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 3) Upload/Confirm/Approve against non‐existent order → 404
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_upload_and_confirm_invalid_order(async_client: AsyncClient,
                                                new_user):
    _, hdrs = new_user

    # upload to invalid order
    up = await async_client.post(
        f"/api/v1/payments/upload/{NONEXISTENT_ID}",
        headers=hdrs,
        files={"file": ("x.jpg", b"X", "image/jpeg")},
    )
    assert up.status_code == 404

    # confirm invalid order
    cf = await async_client.post(
        f"/api/v1/payments/confirm/{NONEXISTENT_ID}",
        json={"upi_transaction_id": "TX"},
        headers=hdrs,
    )
    assert cf.status_code == 404

    # approve invalid order
    _override_admin_dep()
    ap = await async_client.post(
        f"/admin/payments/approve/{NONEXISTENT_ID}",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert ap.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 4) Expired subscription is marked inactive & blocks device‐linking
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_expired_subscription_marked_inactive(async_client: AsyncClient,
                                                   monkeypatch,
                                                   new_user, plan_id, device):
    # 1) stub admin, purchase & activate
    _override_admin_dep()
    _, hdrs = new_user

    order = (await async_client.post(
        "/api/v1/payments/create",
        json={"device_id": device, "plan_id": plan_id},
        headers=hdrs,
    )).json()
    await async_client.post(
        f"/api/v1/payments/upload/{order['id']}",
        headers=hdrs,
        files={"file": ("p.jpg", b"P", "image/jpeg")},
    )
    await async_client.post(
        f"/api/v1/payments/confirm/{order['id']}",
        json={"upi_transaction_id": "T1"},
        headers=hdrs,
    )
    await async_client.post(
        f"/admin/payments/approve/{order['id']}",
        headers={"Authorization": "Bearer admin-token"},
    )

    # 2) list subscription → should be active
    subs = (await async_client.get("/api/v1/subscriptions/", headers=hdrs)).json()
    assert subs and subs[0]["active"] is True
    sub = subs[0]

    # 3) monkeypatch time *past* end_date
    end = dt.datetime.fromisoformat(sub["end_date"].rstrip("Z"))
    import app.routers.subscriptions as s_mod
    monkeypatch.setattr(
        s_mod.datetime, "utcnow",
        staticmethod(lambda: end + dt.timedelta(seconds=1))
    )

    # 4) list again → now inactive
    subs2 = (await async_client.get("/api/v1/subscriptions/", headers=hdrs)).json()
    assert subs2 and subs2[0]["active"] is False

    # 5) linking a device on an expired sub → 400
    link = await async_client.post(
        f"/api/v1/subscriptions/{sub['id']}/devices",
        json={"device_id": device},
        headers=hdrs,
    )
    assert link.status_code == 400
    assert "expired" in link.json()["detail"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# 5) DELETE a subscription end‐to‐end
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_subscription_deletion(async_client: AsyncClient,
                                     new_user, plan_id, device):
    # 1) stub admin, purchase & activate
    _override_admin_dep()
    _, hdrs = new_user

    order = (await async_client.post(
        "/api/v1/payments/create",
        json={"device_id": device, "plan_id": plan_id},
        headers=hdrs,
    )).json()
    await async_client.post(
        f"/api/v1/payments/upload/{order['id']}",
        headers=hdrs,
        files={"file": ("d.jpg", b"D", "image/jpeg")},
    )
    await async_client.post(
        f"/api/v1/payments/confirm/{order['id']}",
        json={"upi_transaction_id": "DEL"},
        headers=hdrs,
    )
    await async_client.post(
        f"/admin/payments/approve/{order['id']}",
        headers={"Authorization": "Bearer admin-token"},
    )

    # 2) confirm subscription exists
    subs = (await async_client.get("/api/v1/subscriptions/", headers=hdrs)).json()
    sid = subs[0]["id"]

    # 3) delete it
    resp = await async_client.delete(
        f"/api/v1/subscriptions/{sid}",
        headers=hdrs,
    )
    assert resp.status_code == 204

    # 4) now listing returns empty
    subs2 = (await async_client.get("/api/v1/subscriptions/", headers=hdrs)).json()
    assert subs2 == []
