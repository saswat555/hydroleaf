# tests/test_dose_manager.py
import uuid
import pytest

# Use the module directly (new API is function-based)
from app.services import dose_manager as dm


class DummyController:
    def __init__(self, *args, **kw):
        pass

    async def execute_dosing(self, pump, amount, combined=False):
        return {"ok": True, "pump": pump, "amount": amount, "combined": combined}

    async def cancel_dosing(self):
        return {"cancelled": True}


@pytest.fixture(autouse=True)
def patch_controller(monkeypatch):
    # Replace DeviceController inside dose_manager with our dummy
    monkeypatch.setattr(
        "app.services.dose_manager.DeviceController",
        lambda *a, **k: DummyController(),
    )


@pytest.mark.asyncio
async def test_execute_dosing_empty_actions():
    with pytest.raises(ValueError):
        await dm.execute_dosing_operation(str(uuid.uuid4()), "http://x", [], combined=False)

@pytest.mark.asyncio
async def test_execute_dosing_missing_fields():
    # action missing pump_number or dose_ml
    with pytest.raises(ValueError):
        await dm.execute_dosing_operation(str(uuid.uuid4()), "http://x", [{"foo": 1}], combined=True)


@pytest.mark.asyncio
async def test_execute_dosing_success_single_and_combined():
    actions = [{"pump_number": 2, "dose_ml": 25}]
    res = await dm.execute_dosing_operation(str(uuid.uuid4()), "http://x", actions, combined=False)
    assert res["status"] == "command_sent"
    assert res.get("device_id")
    assert res["actions"] == actions

    # combined = True
    res2 = await dm.execute_dosing_operation(str(uuid.uuid4()), "http://x", actions, combined=True)
    assert res2["status"] == "command_sent"
    assert res2["actions"] == actions


@pytest.mark.asyncio
async def test_cancel_dosing_idempotent():
    res = await dm.cancel_dosing_operation(str(uuid.uuid4()), "http://x")
    assert res["status"] == "dosing_cancelled"
    assert res.get("device_id")
    assert res.get("response", {}).get("cancelled") is True