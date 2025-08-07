# tests/test_plant_endpoints.py
import pytest
from httpx import AsyncClient


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _plant_payload(**overrides):
    base = {
        "name": "Lettuce",
        "type": "leaf",
        "growth_stage": "veg",
        "seeding_date": "2025-07-01T00:00:00Z",
        "region": "Greenhouse",
        "location_description": "Rack 1",
        "target_ph_min": 5.5,
        "target_ph_max": 6.5,
        "target_tds_min": 300,
        "target_tds_max": 700,
    }
    base.update(overrides)
    return base


@pytest.fixture
async def farm_and_headers(async_client: AsyncClient, signed_up_user):
    # signed_up_user returns (payload, token, headers)
    _, _, hdrs = signed_up_user
    farm = (await async_client.post(
        "/api/v1/farms/",
        json={"name": "F", "address": "A", "latitude": 0, "longitude": 0},
        headers=hdrs
    )).json()
    return farm["id"], hdrs


@pytest.fixture
async def another_user_headers(async_client: AsyncClient):
    """
    A second, separate user to test ownership/authorization.
    """
    payload = {
        "email": "otheruser@example.com",
        "password": "Pass!234",
        "first_name": "Other",
        "last_name": "User",
        "phone": "1234567890",
        "address": "1 St",
        "city": "C",
        "state": "S",
        "country": "IN",
        "postal_code": "000000",
    }
    r = await async_client.post("/api/v1/auth/signup", json=payload)
    assert r.status_code == 201
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────────────────────────────────────────────────────
# Happy path & basic CRUD
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_plants_empty_for_new_farm(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers
    resp = await async_client.get(f"/api/v1/farms/{farm_id}/plants/", headers=hdrs)
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_create_get_list_delete_roundtrip(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers

    # Create
    create = await async_client.post(
        f"/api/v1/farms/{farm_id}/plants/",
        json=_plant_payload(),
        headers=hdrs,
    )
    assert create.status_code == 201
    plant = create.json()
    pid = plant["id"]

    # Round-trip fields sanity
    assert plant["name"] == "Lettuce"
    assert plant["type"] == "leaf"
    assert plant["growth_stage"] == "veg"
    assert plant["target_ph_min"] == pytest.approx(5.5)
    assert plant["target_tds_max"] == pytest.approx(700)

    # List contains it
    lst = await async_client.get(f"/api/v1/farms/{farm_id}/plants/", headers=hdrs)
    assert lst.status_code == 200
    assert any(p["id"] == pid for p in lst.json())

    # Get detail
    one = await async_client.get(
        f"/api/v1/farms/{farm_id}/plants/{pid}",
        headers=hdrs,
    )
    assert one.status_code == 200
    assert one.json()["id"] == pid

    # Delete
    dele = await async_client.delete(
        f"/api/v1/farms/{farm_id}/plants/{pid}",
        headers=hdrs,
    )
    assert dele.status_code == 204

    # Now 404 on get
    missing = await async_client.get(
        f"/api/v1/farms/{farm_id}/plants/{pid}",
        headers=hdrs,
    )
    assert missing.status_code == 404

    # And list no longer contains it
    lst2 = await async_client.get(f"/api/v1/farms/{farm_id}/plants/", headers=hdrs)
    assert all(p["id"] != pid for p in lst2.json())


@pytest.mark.asyncio
async def test_multiple_plants_and_isolation_per_farm(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers
    # Second farm for isolation
    farm2 = (await async_client.post(
        "/api/v1/farms/",
        json={"name": "F2", "address": "B", "latitude": 1, "longitude": 1},
        headers=hdrs,
    )).json()
    farm2_id = farm2["id"]

    # Create 2 plants in farm 1
    p1 = (await async_client.post(
        f"/api/v1/farms/{farm_id}/plants/",
        json=_plant_payload(name="A"),
        headers=hdrs,
    )).json()
    p2 = (await async_client.post(
        f"/api/v1/farms/{farm_id}/plants/",
        json=_plant_payload(name="B"),
        headers=hdrs,
    )).json()

    # Create 1 plant in farm 2
    p3 = (await async_client.post(
        f"/api/v1/farms/{farm2_id}/plants/",
        json=_plant_payload(name="C"),
        headers=hdrs,
    )).json()

    # Lists are isolated
    lst1 = (await async_client.get(f"/api/v1/farms/{farm_id}/plants/", headers=hdrs)).json()
    lst2 = (await async_client.get(f"/api/v1/farms/{farm2_id}/plants/", headers=hdrs)).json()

    ids1 = {p["id"] for p in lst1}
    ids2 = {p["id"] for p in lst2}
    assert ids1 == {p1["id"], p2["id"]}
    assert ids2 == {p3["id"]}


# ─────────────────────────────────────────────────────────────────────────────
# Not-found and wrong-farm routing
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_plant_in_nonexistent_farm_returns_404(async_client: AsyncClient, farm_and_headers):
    _, hdrs = farm_and_headers
    resp = await async_client.post(
        "/api/v1/farms/999999/plants/",
        json=_plant_payload(),
        headers=hdrs,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_plant_from_wrong_farm_404(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers
    # Make a second farm
    farm2 = (await async_client.post(
        "/api/v1/farms/",
        json={"name": "F2", "address": "B", "latitude": 1, "longitude": 1},
        headers=hdrs,
    )).json()
    farm2_id = farm2["id"]

    # Create plant in farm 1
    plant = (await async_client.post(
        f"/api/v1/farms/{farm_id}/plants/",
        json=_plant_payload(),
        headers=hdrs,
    )).json()

    # Try to fetch via farm 2 path → should not leak, expect 404
    r = await async_client.get(
        f"/api/v1/farms/{farm2_id}/plants/{plant['id']}",
        headers=hdrs,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_plant_from_wrong_farm_404(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers
    farm2 = (await async_client.post(
        "/api/v1/farms/",
        json={"name": "F2", "address": "B", "latitude": 1, "longitude": 1},
        headers=hdrs,
    )).json()
    farm2_id = farm2["id"]

    plant = (await async_client.post(
        f"/api/v1/farms/{farm_id}/plants/",
        json=_plant_payload(),
        headers=hdrs,
    )).json()

    r = await async_client.delete(
        f"/api/v1/farms/{farm2_id}/plants/{plant['id']}",
        headers=hdrs,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_twice_second_404(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers
    plant = (await async_client.post(
        f"/api/v1/farms/{farm_id}/plants/",
        json=_plant_payload(),
        headers=hdrs,
    )).json()
    # First delete ok
    r1 = await async_client.delete(f"/api/v1/farms/{farm_id}/plants/{plant['id']}", headers=hdrs)
    assert r1.status_code == 204
    # Second delete 404
    r2 = await async_client.delete(f"/api/v1/farms/{farm_id}/plants/{plant['id']}", headers=hdrs)
    assert r2.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# AuthZ/AuthN guardrails
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unauthenticated_requests_are_rejected(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers
    # Create requires auth
    r1 = await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=_plant_payload())
    assert r1.status_code in (401, 403)
    # List requires auth
    r2 = await async_client.get(f"/api/v1/farms/{farm_id}/plants/")
    assert r2.status_code in (401, 403)
    # Detail requires auth
    # Make a plant with auth first
    plant = (await async_client.post(
        f"/api/v1/farms/{farm_id}/plants/",
        json=_plant_payload(name="Auth OK"),
        headers=hdrs,
    )).json()
    r3 = await async_client.get(f"/api/v1/farms/{farm_id}/plants/{plant['id']}")
    assert r3.status_code in (401, 403)
    # Delete requires auth
    r4 = await async_client.delete(f"/api/v1/farms/{farm_id}/plants/{plant['id']}")
    assert r4.status_code in (401, 403)


@pytest.mark.asyncio
async def test_other_user_cannot_access_my_farm_plants(async_client: AsyncClient, farm_and_headers, another_user_headers):
    farm_id, hdrs_owner = farm_and_headers
    # Owner creates a plant
    plant = (await async_client.post(
        f"/api/v1/farms/{farm_id}/plants/",
        json=_plant_payload(name="Private"),
        headers=hdrs_owner,
    )).json()

    # Other user tries to list/get/delete — should not be allowed (403 or 404)
    r_list = await async_client.get(f"/api/v1/farms/{farm_id}/plants/", headers=another_user_headers)
    assert r_list.status_code in (403, 404)

    r_get = await async_client.get(
        f"/api/v1/farms/{farm_id}/plants/{plant['id']}",
        headers=another_user_headers,
    )
    assert r_get.status_code in (403, 404)

    r_del = await async_client.delete(
        f"/api/v1/farms/{farm_id}/plants/{plant['id']}",
        headers=another_user_headers,
    )
    assert r_del.status_code in (403, 404)


# ─────────────────────────────────────────────────────────────────────────────
# Validation & payload edge cases
# (Rely on FastAPI/Pydantic to return 422 on validation errors)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("missing_key", [
    "name", "type", "growth_stage", "seeding_date",
    "region", "location_description",
    "target_ph_min", "target_ph_max",
    "target_tds_min", "target_tds_max",
])
async def test_create_plant_missing_required_field_422(async_client: AsyncClient, farm_and_headers, missing_key):
    farm_id, hdrs = farm_and_headers
    payload = _plant_payload()
    payload.pop(missing_key)
    r = await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=payload, headers=hdrs)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_plant_invalid_iso_datetime_422(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers
    bad = _plant_payload(seeding_date="07/01/2025 00:00")  # not ISO8601
    r = await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=bad, headers=hdrs)
    assert r.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("field,bad_value", [
    ("target_ph_min", "low"),
    ("target_ph_max", "high"),
    ("target_tds_min", "a"),
    ("target_tds_max", "b"),
    ("target_ph_min", None),
    ("target_tds_min", None),
])
async def test_create_plant_numeric_fields_wrong_type_422(async_client: AsyncClient, farm_and_headers, field, bad_value):
    farm_id, hdrs = farm_and_headers
    payload = _plant_payload(**{field: bad_value})
    r = await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=payload, headers=hdrs)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_plant_extreme_numeric_values_ok_or_422(async_client: AsyncClient, farm_and_headers):
    """
    Some backends allow wide numeric ranges; others validate.
    We assert: either it accepts (201) or rejects (422). No 5xx allowed.
    """
    farm_id, hdrs = farm_and_headers
    payload = _plant_payload(target_ph_min=0.0, target_ph_max=14.0, target_tds_min=0, target_tds_max=50000)
    r = await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=payload, headers=hdrs)
    assert r.status_code in (201, 422)


@pytest.mark.asyncio
async def test_get_unknown_plant_404(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers
    r = await async_client.get(f"/api/v1/farms/{farm_id}/plants/999999", headers=hdrs)
    assert r.status_code == 404
