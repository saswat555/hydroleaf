# tests/services/test_farm_service.py

import pytest
from fastapi import HTTPException
from types import SimpleNamespace

from app.services.farm_service import (
    create_farm,
    list_farms_for_user,
    get_farm_by_id,
    share_farm_with_user,
)
from app.models import Farm

# -------------------------------------------------------------------
# Fake session & result helpers
# -------------------------------------------------------------------

class DummyResult:
    def __init__(self, items):
        self._items = items
    def scalars(self):
        return self
    def all(self):
        return self._items

class FakeFarmSession:
    def __init__(self, farms=None, single=None):
        # farms: what execute() will return for list_farms_for_user
        # single: what get(Farm, pk) will return
        self._farms = farms or []
        self._single = single
        self.last_added = None
        self.committed = False

    async def execute(self, stmt):
        return DummyResult(self._farms)

    async def get(self, model, pk):
        return self._single

    async def add(self, obj):
        # record the exact object you tried to add
        self.last_added = obj

    async def commit(self):
        # record that commit() was called
        self.committed = True

    async def refresh(self, obj):
        # no-op for tests
        pass

# -------------------------------------------------------------------
# Tests
# -------------------------------------------------------------------

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

    # it's a real ORM object
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
    # 404 when missing
    assert exc.value.status_code == 404
    assert "not found" in exc.value.detail.lower()

@pytest.mark.asyncio
async def test_list_farms_for_user():
    f1 = Farm(id=1, owner_id=5, name="A", address="AddrA", latitude=0, longitude=0)
    f2 = Farm(id=2, owner_id=5, name="B", address="AddrB", latitude=1, longitude=1)
    sess = FakeFarmSession(farms=[f1, f2])

    farms = await list_farms_for_user(user_id=5, db=sess)

    # we get back exactly what we seeded
    assert farms == [f1, f2]

@pytest.mark.asyncio
async def test_share_farm_not_found():
    sess = FakeFarmSession(single=None)
    with pytest.raises(HTTPException) as exc:
        await share_farm_with_user(farm_id=5, user_id=11, db=sess)
    # still a 404 for missing farm
    assert exc.value.status_code == 404

@pytest.mark.asyncio
async def test_share_farm_success_records_and_returns_association():
    # prepare a farm
    farm = Farm(id=7, owner_id=3, name="Shared Farm", address="X", latitude=0, longitude=0)
    sess = FakeFarmSession(single=farm)
    sub_user_id = 99

    assoc = await share_farm_with_user(farm_id=7, user_id=sub_user_id, db=sess)

    # 1) check you added exactly that object to the session
    assert sess.last_added is assoc

    # 2) check you committed
    assert sess.committed is True

    # 3) and that the return has the right attributes
    assert isinstance(assoc, SimpleNamespace)
    assert assoc.farm_id == 7
    assert assoc.user_id == sub_user_id


@pytest.mark.asyncio
async def test_get_farm_by_id_success():
    # retrieving a known farm yields the exact ORM object
    farm = Farm(id=8, owner_id=2, name="Farm8", address="Addr8", latitude=8.8, longitude=9.9)
    sess = FakeFarmSession(single=farm)
    result = await get_farm_by_id(8, db=sess)
    assert result is farm

@pytest.mark.asyncio
async def test_list_farms_for_user_empty():
    # user with no farms returns []
    sess = FakeFarmSession(farms=[])
    farms = await list_farms_for_user(user_id=99, db=sess)
    assert farms == []