# tests/test_dose_manager.py
import pytest
from fastapi import HTTPException
from app.services.dose_manager import DoseManager

class DummyController:
    def __init__(self, *args, **kw): pass
    async def execute_dosing(self, pump, amount, combined=False):
        return {"ok": True, "pump": pump, "amount": amount, "combined": combined}
    async def cancel_dosing(self):
        return {"cancelled": True}

@pytest.fixture(autouse=True)
def patch_controller(monkeypatch):
    # replace DeviceController inside DoseManager with our dummy
    monkeypatch.setattr("app.services.dose_manager.DeviceController", lambda *a, **k: DummyController())

dm = DoseManager()

@pytest.mark.asyncio
async def test_execute_dosing_empty_actions():
    with pytest.raises(ValueError):
        await dm.execute_dosing("dev1", "http://x", [], combined=False)

@pytest.mark.asyncio
async def test_execute_dosing_missing_fields():
    # action missing pump or dose
    with pytest.raises(ValueError):
        await dm.execute_dosing("dev1", "http://x", [{"foo":1}], combined=True)

@pytest.mark.asyncio
async def test_execute_dosing_success_single_and_combined():
    actions = [{"pump_number": 2, "dose_ml": 25}]
    res = await dm.execute_dosing("dev1", "http://x", actions, combined=False)
    assert res["status"] == "command_sent"
    assert res["device_id"] == "dev1"
    assert res["actions"] == actions
    # combined
    res2 = await dm.execute_dosing("dev1", "http://x", actions, combined=True)
    assert res2["status"] == "command_sent"
    assert res2["actions"] == actions

@pytest.mark.asyncio
async def test_cancel_dosing_idempotent():
    res = await dm.cancel_dosing("devX","http://x")
    assert res == {"status":"dosing_cancelled","device_id":"devX","response":{"cancelled":True}}