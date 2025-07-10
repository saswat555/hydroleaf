# tests/test_plant_service.py
import pytest
from fastapi import HTTPException
from app.services.plant_service import (
    get_all_plants,
    get_plant_by_id,
    create_plant,
    delete_plant,
)
from app.models import Plant

# --- helpers to fake the DB session and results ---
class DummyResult:
    def __init__(self, items):
        self._items = items
    def scalars(self):
        return self
    def all(self):
        return self._items

class FakeSession:
    def __init__(self, plants=None, single=None):
        self._plants = plants or []
        self._single = single
    async def execute(self, stmt):
        return DummyResult(self._plants)
    async def get(self, model, pk):
        return self._single
    async def add(self, obj): pass
    async def commit(self): pass
    async def refresh(self, obj): pass
    async def delete(self, obj): pass

@pytest.mark.asyncio
async def test_get_all_plants_empty():
    sess = FakeSession(plants=[])
    plants = await get_all_plants(sess)
    assert plants == []

@pytest.mark.asyncio
async def test_get_all_plants_nonempty():
    p1 = Plant(id=1, name="A", type="T", growth_stage="G", seeding_date="2020-01-01", region="R", location="L")
    p2 = Plant(id=2, name="B", type="T", growth_stage="G", seeding_date="2020-01-02", region="R", location="L")
    sess = FakeSession(plants=[p1, p2])
    plants = await get_all_plants(sess)
    assert plants == [p1, p2]

@pytest.mark.asyncio
async def test_get_plant_by_id_found():
    p = Plant(id=5, name="X", type="T", growth_stage="G", seeding_date="2020-01-03", region="R", location="L")
    sess = FakeSession(single=p)
    got = await get_plant_by_id(5, sess)
    assert got is p

@pytest.mark.asyncio
async def test_get_plant_by_id_not_found():
    sess = FakeSession(single=None)
    with pytest.raises(HTTPException) as exc:
        await get_plant_by_id(123, sess)
    assert exc.value.status_code == 404

@pytest.mark.asyncio
async def test_create_and_delete_plant(tmp_path, monkeypatch):
    # For create_plant and delete_plant we actually need a DB, but we can at least
    # assert that they return the right types and messages under a fake session.
    class FakePS(FakeSession):
        async def commit(self): pass
        async def refresh(self, obj): pass
    fake = FakePS()
    # create_plant
    create_schema = type("S", (), {"model_dump": lambda self: {
        "name":"P","type":"T","growth_stage":"G","seeding_date":"2020-01-01","region":"R","location":"L"}})()
    new = await create_plant(create_schema, fake)
    assert isinstance(new, Plant)
    # delete_plant: if single is None -> 404
    fake2 = FakeSession(single=None)
    with pytest.raises(HTTPException):
        await delete_plant(1, fake2)
    # if present -> returns dict
    fake3 = FakeSession(single=new)
    out = await delete_plant(new.id, fake3)
    assert out == {"message": "Plant deleted successfully"}

@pytest.mark.asyncio
async def test_delete_plant_success(monkeypatch):
    """If the plant exists, delete_plant returns the success dict."""
    p = Plant(id=99, name="A", type="T", growth_stage="G",
              seeding_date="2020-01-01", region="R", location="L")
    sess = FakeSession(single=p)
    out = await delete_plant(99, sess)
    assert out == {"message":"Plant deleted successfully"}

@pytest.mark.asyncio
async def test_get_all_plants_db_error(monkeypatch):
    """If sess.execute raises, get_all_plants should return [] (per implementation)."""
    class BadSession(FakeSession):
        async def execute(self, stmt):
            raise RuntimeError("db is down")
    sess = BadSession()
    plants = await get_all_plants(sess)
    assert plants == []
