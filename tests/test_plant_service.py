# tests/services/test_plant_service.py

import pytest
from fastapi import HTTPException
from types import SimpleNamespace

from app.services.plant_service import (
    create_plant,
    list_plants_by_farm,
    get_plant_by_id,
    delete_plant,
)
from app.models import Plant, Farm

# --- fake result/session helpers ---

class DummyResult:
    def __init__(self, items):
        self._items = items
    def scalars(self):
        return self
    def all(self):
        return self._items

class FakePlantSession:
    def __init__(self, plants=None, single=None):
        # plants: what execute(select...) returns
        # single: what get(Farm, id) or get(Plant, id) returns
        self._plants = plants or []
        self._single = single
    async def execute(self, stmt):
        return DummyResult(self._plants)
    async def get(self, model, pk):
        # if asking for Farm, return self._single only if it's a Farm
        if model is Farm and isinstance(self._single, Farm):
            return self._single
        # if asking for Plant, return self._single only if it's a Plant
        if model is Plant and isinstance(self._single, Plant):
            return self._single
        return None
    async def add(self, obj):
        self.last_added = obj
    async def commit(self):
        pass
    async def refresh(self, obj):
        # simulate DB assigning an ID
        if getattr(obj, "id", None) is None:
            obj.id = 123
    async def delete(self, obj):
        pass

# --- tests ---

@pytest.mark.asyncio
async def test_create_plant_requires_existing_farm():
    sess = FakePlantSession(single=None)
    payload = {
        "name": "Lettuce",
        "type": "leaf",
        "growth_stage": "veg",
        "seeding_date": "2025-07-01T00:00:00Z",
        "region": "Bangalore",
        "location_description": "Greenhouse",
        "target_ph_min": 5.5,
        "target_ph_max": 6.5,
        "target_tds_min": 300,
        "target_tds_max": 700,
    }
    with pytest.raises(HTTPException) as exc:
        await create_plant(farm_id=42, payload=payload, db=sess)
    assert exc.value.status_code == 404

@pytest.mark.asyncio
async def test_create_and_list_plants_success():
    farm = Farm(id=5, owner_id=1, name="FarmX", address="Addr", latitude=0, longitude=0)
    plant = Plant(
        id=None,
        farm_id=5,
        name="Tomato",
        type="fruit",
        growth_stage="flower",
        seeding_date="2025-06-15T00:00:00Z",
        region="Bangalore",
        location_description="Greenhouse",
        target_ph_min=5.8,
        target_ph_max=6.2,
        target_tds_min=400,
        target_tds_max=800
    )
    sess = FakePlantSession(plants=[plant], single=farm)
    # create
    payload = {
        "name": plant.name,
        "type": plant.type,
        "growth_stage": plant.growth_stage,
        "seeding_date": plant.seeding_date,
        "region": plant.region,
        "location_description": plant.location_description,
        "target_ph_min": plant.target_ph_min,
        "target_ph_max": plant.target_ph_max,
        "target_tds_min": plant.target_tds_min,
        "target_tds_max": plant.target_tds_max,
    }
    new = await create_plant(farm_id=5, payload=payload, db=sess)
    assert isinstance(new, Plant)
    assert new.farm_id == 5

    # list
    plants = await list_plants_by_farm(farm_id=5, db=sess)
    assert plants == [plant]

@pytest.mark.asyncio
async def test_get_plant_by_id_not_found():
    sess = FakePlantSession(single=None)
    with pytest.raises(HTTPException) as exc:
        await get_plant_by_id(99, db=sess)
    assert exc.value.status_code == 404

@pytest.mark.asyncio
async def test_delete_plant_success_and_missing():
    # delete when missing
    sess1 = FakePlantSession(single=None)
    with pytest.raises(HTTPException) as exc:
        await delete_plant(1, db=sess1)
    assert exc.value.status_code == 404

    # delete when exists
    p = Plant(id=13, farm_id=5, name="Herb", type="herb", growth_stage="seedling",
              seeding_date="2025-07-10T00:00:00Z", region="R", location_description="Home",
              target_ph_min=6.0, target_ph_max=7.0, target_tds_min=200, target_tds_max=600)
    sess2 = FakePlantSession(single=p)
    out = await delete_plant(13, db=sess2)
    assert out == {"message": "Plant deleted successfully"}



@pytest.mark.asyncio
async def test_list_plants_empty():
    # when there are no plants in the farm, you get back an empty list
    sess = FakePlantSession(plants=[], single=SimpleNamespace(id=5))
    plants = await list_plants_by_farm(farm_id=5, db=sess)
    assert plants == []

@pytest.mark.asyncio
async def test_get_plant_by_id_success():
    # retrieving an existing plant returns it directly
    p = Plant(
        id=42, farm_id=7, name="Basil", type="herb", growth_stage="seed",
        seeding_date="2025-08-01T00:00:00Z", region="Kitchen", location_description="Window",
        target_ph_min=6.0, target_ph_max=7.0, target_tds_min=250, target_tds_max=450
    )
    sess = FakePlantSession(single=p)
    result = await get_plant_by_id(42, db=sess)
    assert result is p