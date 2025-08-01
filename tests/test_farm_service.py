# tests/services/test_farm_service.py

import pytest
from fastapi import HTTPException
from app.services.farm_service import (
    create_farm,
    list_farms_for_user,
    get_farm_by_id,
    share_farm_with_user,
)
from app.models import Farm

# --- fake result/session helpers ---

class DummyResult:
    def __init__(self, items):
        self._items = items
    def scalars(self):
        return self
    def all(self):
        return self._items

class FakeFarmSession:
    def __init__(self, farms=None, single=None):
        # farms: what execute() will return
        # single: what get(Farm, id) will return
        self._farms = farms or []
        self._single = single
    async def execute(self, stmt):
        return DummyResult(self._farms)
    async def get(self, model, pk):
        return self._single
    async def add(self, obj):
        # record the added object for later inspection if needed
        self.last_added = obj
    async def commit(self):
        pass
    async def refresh(self, obj):
        pass

# --- tests ---

@pytest.mark.asyncio
async def test_create_farm_success():
    sess = FakeFarmSession()
    owner_id = 42
    payload = {
        "name": "Test Farm",
        "address": "123 Garden Lane",
        "latitude": 12.9716,
        "longitude": 77.5946
    }
    farm = await create_farm(owner_id=owner_id, payload=payload, db=sess)
    # these should all be set by create_farm
    assert isinstance(farm, Farm)
    assert farm.owner_id == owner_id
    assert farm.name == payload["name"]
    assert farm.address == payload["address"]
    assert pytest.approx(farm.latitude) == payload["latitude"]
    assert pytest.approx(farm.longitude) == payload["longitude"]

@pytest.mark.asyncio
async def test_get_farm_by_id_not_found():
    sess = FakeFarmSession(single=None)
    with pytest.raises(HTTPException) as exc:
        await get_farm_by_id(99, db=sess)
    assert exc.value.status_code == 404

@pytest.mark.asyncio
async def test_list_farms_for_user():
    f1 = Farm(id=1, owner_id=5, name="A", address="AddrA", latitude=0, longitude=0)
    f2 = Farm(id=2, owner_id=5, name="B", address="AddrB", latitude=1, longitude=1)
    sess = FakeFarmSession(farms=[f1, f2])
    farms = await list_farms_for_user(user_id=5, db=sess)
    # Expect exactly the list we seeded
    assert farms == [f1, f2]

@pytest.mark.asyncio
async def test_share_farm_success():
    farm = Farm(id=7, owner_id=3, name="Shared Farm", address="X", latitude=0, longitude=0)
    sess = FakeFarmSession(single=farm)
    sub_user_id = 99
    association = await share_farm_with_user(farm_id=7, user_id=sub_user_id, db=sess)
    # We expect the service to return an object with farm_id & user_id
    assert hasattr(association, "farm_id") and association.farm_id == 7
    assert hasattr(association, "user_id") and association.user_id == sub_user_id

@pytest.mark.asyncio
async def test_share_farm_not_found():
    sess = FakeFarmSession(single=None)
    with pytest.raises(HTTPException) as exc:
        await share_farm_with_user(farm_id=5, user_id=11, db=sess)
    assert exc.value.status_code == 404
