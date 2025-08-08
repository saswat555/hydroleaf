# tests/test_plant_endpoints.py
import pytest
from httpx import AsyncClient
import uuid
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
        "location_latitude": 12.3456,
        "location_longitude": 65.4321,
    }
    base.update(overrides)
    return base

@pytest.fixture
async def farm_and_headers(async_client: AsyncClient, signed_up_user):
    _, _, hdrs = signed_up_user
    farm = (await async_client.post(
        "/api/v1/farms/",
        json={"name": "F", "address": "A", "latitude": 0, "longitude": 0},
        headers=hdrs
    )).json()
    return farm["id"], hdrs

@pytest.fixture
async def another_user_headers(async_client: AsyncClient):
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

# ─────────────────────────────────────────────────────
# Happy path & isolation
# ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_plants_empty_for_new_farm(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers
    uuid.UUID(str(farm_id))  # ensure farm_id is UUID
    resp = await async_client.get(f"/api/v1/farms/{farm_id}/plants/", headers=hdrs)
    assert resp.status_code == 200
    assert resp.json() == []

@pytest.mark.asyncio
async def test_create_get_list_delete_roundtrip(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers
    create = await async_client.post(
        f"/api/v1/farms/{farm_id}/plants/",
        json=_plant_payload(),
        headers=hdrs,
    )
    assert create.status_code == 201
    plant = create.json()
    pid = plant["id"]
    uuid.UUID(str(pid)) 
    # list contains it
    lst = await async_client.get(f"/api/v1/farms/{farm_id}/plants/", headers=hdrs)
    assert any(p["id"] == pid for p in lst.json())

    # get detail
    one = await async_client.get(f"/api/v1/farms/{farm_id}/plants/{pid}", headers=hdrs)
    assert one.status_code == 200
    detail = one.json()
    assert detail["id"] == pid
    # NEW: persisted geo matches input (approx)
    assert detail.get("location_latitude") == pytest.approx(12.3456)
    assert detail.get("location_longitude") == pytest.approx(65.4321)

    # delete
    dele = await async_client.delete(f"/api/v1/farms/{farm_id}/plants/{pid}", headers=hdrs)
    assert dele.status_code == 204

    # now 404 on get
    missing = await async_client.get(f"/api/v1/farms/{farm_id}/plants/{pid}", headers=hdrs)
    assert missing.status_code == 404

@pytest.mark.asyncio
async def test_multiple_plants_and_isolation_per_farm(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers
    farm2 = (await async_client.post(
        "/api/v1/farms/",
        json={"name": "F2", "address": "B", "latitude": 1, "longitude": 1},
        headers=hdrs,
    )).json()["id"]

    p1 = (await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=_plant_payload(name="A"), headers=hdrs)).json()
    p2 = (await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=_plant_payload(name="B"), headers=hdrs)).json()
    p3 = (await async_client.post(f"/api/v1/farms/{farm2}/plants/", json=_plant_payload(name="C"), headers=hdrs)).json()

    ids1 = {p["id"] for p in (await async_client.get(f"/api/v1/farms/{farm_id}/plants/", headers=hdrs)).json()}
    ids2 = {p["id"] for p in (await async_client.get(f"/api/v1/farms/{farm2}/plants/", headers=hdrs)).json()}
    assert ids1 == {p1["id"], p2["id"]}
    assert ids2 == {p3["id"]}

# ─────────────────────────────────────────────────────
# Not-found / wrong farm (only if you keep nested routes)
# ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_plant_in_nonexistent_farm_returns_404(async_client: AsyncClient, farm_and_headers):
    _, hdrs = farm_and_headers
    resp = await async_client.post(f"/api/v1/farms/{uuid.uuid4()}/plants/", json=_plant_payload(), headers=hdrs)
    assert resp.status_code == 404

@pytest.mark.asyncio
async def test_get_plant_from_wrong_farm_404(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers
    farm2 = (await async_client.post("/api/v1/farms/", json={"name": "F2", "address": "B", "latitude": 1, "longitude": 1}, headers=hdrs)).json()["id"]
    plant = (await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=_plant_payload(), headers=hdrs)).json()
    r = await async_client.get(f"/api/v1/farms/{farm2}/plants/{plant['id']}", headers=hdrs)
    assert r.status_code == 404

@pytest.mark.asyncio
async def test_delete_plant_from_wrong_farm_404(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers
    farm2 = (await async_client.post("/api/v1/farms/", json={"name": "F2", "address": "B", "latitude": 1, "longitude": 1}, headers=hdrs)).json()["id"]
    plant = (await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=_plant_payload(), headers=hdrs)).json()
    r = await async_client.delete(f"/api/v1/farms/{farm2}/plants/{plant['id']}", headers=hdrs)
    assert r.status_code == 404

@pytest.mark.asyncio
async def test_get_unknown_plant_404(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers
    r = await async_client.get(f"/api/v1/farms/{farm_id}/plants/{uuid.uuid4()}", headers=hdrs)
    assert r.status_code == 404

# ─────────────────────────────────────────────────────
# Auth guardrails
# ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unauthenticated_requests_are_rejected(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers
    # create requires auth
    r1 = await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=_plant_payload())
    assert r1.status_code == 401
    # list requires auth
    r2 = await async_client.get(f"/api/v1/farms/{farm_id}/plants/")
    assert r2.status_code == 401
    # make a plant (with auth) to test detail/delete unauthenticated
    plant_id = (await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=_plant_payload(name="Auth OK"), headers=hdrs)).json()["id"]
    r3 = await async_client.get(f"/api/v1/farms/{farm_id}/plants/{plant_id}")
    r4 = await async_client.delete(f"/api/v1/farms/{farm_id}/plants/{plant_id}")
    assert r3.status_code == 401
    assert r4.status_code == 401

@pytest.mark.asyncio
async def test_other_user_cannot_access_my_farm_plants(async_client: AsyncClient, farm_and_headers, another_user_headers):
    farm_id, hdrs_owner = farm_and_headers
    plant_id = (await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=_plant_payload(name="Private"), headers=hdrs_owner)).json()["id"]

    # No-leak policy: pretend it does not exist for others -> 404 everywhere
    r_list = await async_client.get(f"/api/v1/farms/{farm_id}/plants/", headers=another_user_headers)
    r_get  = await async_client.get(f"/api/v1/farms/{farm_id}/plants/{plant_id}", headers=another_user_headers)
    r_del  = await async_client.delete(f"/api/v1/farms/{farm_id}/plants/{plant_id}", headers=another_user_headers)
    assert r_list.status_code == 404
    assert r_get.status_code  == 404
    assert r_del.status_code  == 404

# ─────────────────────────────────────────────────────
# Validation (deterministic; no “201 or 422”)
# ─────────────────────────────────────────────────────

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
async def test_create_plant_invalid_ph_range_422(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers
    # pH must be within 0..14
    bad_low  = _plant_payload(target_ph_min=-0.1)
    bad_high = _plant_payload(target_ph_max=14.5)
    r1 = await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=bad_low, headers=hdrs)
    r2 = await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=bad_high, headers=hdrs)
    assert r1.status_code == 422
    assert r2.status_code == 422

@pytest.mark.asyncio
async def test_create_plant_min_gt_max_422(async_client: AsyncClient, farm_and_headers):
    farm_id, hdrs = farm_and_headers
    # pH min > max
    bad_ph = _plant_payload(target_ph_min=7.0, target_ph_max=6.0)
    # TDS min > max
    bad_tds = _plant_payload(target_tds_min=800, target_tds_max=700)
    r1 = await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=bad_ph, headers=hdrs)
    r2 = await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=bad_tds, headers=hdrs)
    assert r1.status_code == 422
    assert r2.status_code == 422

@pytest.mark.asyncio
@pytest.mark.parametrize("lat,lon", [
    (-91, 0), (91, 0), (0, -181), (0, 181),
])
async def test_create_plant_invalid_latlon_range_422(async_client: AsyncClient, farm_and_headers, lat, lon):
    farm_id, hdrs = farm_and_headers
    bad = _plant_payload(location_latitude=lat, location_longitude=lon)
    r = await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=bad, headers=hdrs)
    assert r.status_code == 422

@pytest.mark.asyncio
@pytest.mark.parametrize("field,bad_value", [
    # existing numeric type checks…
    ("target_ph_min", "low"),
    ("target_ph_max", "high"),
    ("target_tds_min", "a"),
    ("target_tds_max", "b"),
    ("target_ph_min", None),
    ("target_tds_min", None),
    # NEW: lat/lon must be numeric if provided
    ("location_latitude", "north"),
    ("location_longitude", "east"),
])
async def test_create_plant_numeric_fields_wrong_type_422(async_client: AsyncClient, farm_and_headers, field, bad_value):
    farm_id, hdrs = farm_and_headers
    payload = _plant_payload(**{field: bad_value})
    r = await async_client.post(f"/api/v1/farms/{farm_id}/plants/", json=payload, headers=hdrs)
    assert r.status_code == 422