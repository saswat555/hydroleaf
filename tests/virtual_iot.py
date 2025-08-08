import multiprocessing
import os
import time
import threading
from typing import Optional, List, Dict, Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ============================================================
# In-memory device states
# ============================================================

# Dosing Unit State
_dosing_state = {
    "device_id": "doser-virtual",
    "type": "dosing_unit",
    "version": "1.0.0",
    "ph": 7.2,
    "tds": 450.0,
    "pumps": {},  # pump_number -> count
}

# Valve Controller State
_valve_state = {
    "device_id": "valve-virtual",
    "type": "valve_controller",
    "version": "1.0.0",
    # 4 valves, False=off, True=on
    "valves": [False, False, False, False],
}

# Smart Switch State
_switch_state = {
    "device_id": "switch-virtual",
    "type": "smart_switch",
    "version": "1.0.0",
    # 8 relays, False=off, True=on
    "channels": [False] * 8,
}

# CCTV State
_cctv_state = {
    "device_id": "camera-virtual",
    "type": "cctv",
    "version": "1.0.0",
    "status": "operational",
}

# ============================================================
# Optional “device pulls tasks from cloud” scaffolding
# (off by default; enabled if CLOUD_TASK_BASE is set)
# ============================================================

CLOUD_TASK_BASE = os.getenv("CLOUD_TASK_BASE", "").rstrip("/")
CLOUD_POLL_SEC = float(os.getenv("CLOUD_POLL_SEC", "0"))  # 0 = disabled

# local task queues (simulate what a cloud queue would give us)
_local_queues: Dict[str, List[Dict[str, Any]]] = {
    _valve_state["device_id"]: [],
    _switch_state["device_id"]: [],
    _dosing_state["device_id"]: [],
    _cctv_state["device_id"]: [],
}

def _enqueue_local(device_id: str, task: Dict[str, Any]) -> None:
    _local_queues.setdefault(device_id, []).append(task)

def _dequeue_local(device_id: str) -> Optional[Dict[str, Any]]:
    q = _local_queues.setdefault(device_id, [])
    return q.pop(0) if q else None


def _apply_task(device_id: str, task: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a queued task against local state.
    Supported:
      - {"type":"valve","valve_id":1,"action":"toggle"}
      - {"type":"switch","channel":2,"action":"toggle"}
      - {"type":"pump","pump_number":1,"amount":10}
    """
    t = task.get("type")
    if t == "valve":
        vid = int(task["valve_id"])
        if not (1 <= vid <= 4):
            return {"error": "invalid valve_id"}
        idx = vid - 1
        _valve_state["valves"][idx] = not _valve_state["valves"][idx]
        return {"valve_id": vid, "new_state": "on" if _valve_state["valves"][idx] else "off"}

    if t == "switch":
        ch = int(task["channel"])
        if not (1 <= ch <= 8):
            return {"error": "invalid channel"}
        idx = ch - 1
        _switch_state["channels"][idx] = not _switch_state["channels"][idx]
        return {"channel": ch, "new_state": "on" if _switch_state["channels"][idx] else "off"}

    if t == "pump":
        pn = int(task.get("pump_number") or task.get("pump") or 0)
        if pn <= 0:
            return {"error": "invalid pump_number"}
        _dosing_state["pumps"][pn] = _dosing_state["pumps"].get(pn, 0) + 1
        return {"message": "pump executed", "pump_number": pn}

    return {"error": "unknown task type"}


def _device_poller(device_id: str):
    """
    Background poller to demonstrate device-pull flow.
    Disabled unless CLOUD_TASK_BASE and CLOUD_POLL_SEC > 0 are set.
    In demo mode we only consume from the local queue.
    """
    if not CLOUD_TASK_BASE or CLOUD_POLL_SEC <= 0:
        return
    while True:
        try:
            # In a real device, this would be:
            #   httpx.get(f"{CLOUD_TASK_BASE}/api/v1/device_comm/pending_tasks?device_id={device_id}", ...)
            task = _dequeue_local(device_id)
            if task:
                _apply_task(device_id, task)
        except Exception:
            pass
        time.sleep(CLOUD_POLL_SEC)


# ============================================================
# Request models
# ============================================================

class PumpCommand(BaseModel):
    pump_number: Optional[int] = None
    pump: Optional[int] = None
    amount: Optional[int] = None
    timestamp: Optional[str] = None

    def resolved_number(self) -> int:
        n = self.pump_number if self.pump_number is not None else self.pump
        if n is None:
            raise ValueError("pump_number or pump is required")
        return int(n)

class ToggleCommand(BaseModel):
    valve_id: Optional[int] = None  # for valve controller
    channel: Optional[int] = None   # for smart switch

class QueueTask(BaseModel):
    type: str
    valve_id: Optional[int] = None
    channel: Optional[int] = None
    pump_number: Optional[int] = None
    pump: Optional[int] = None
    amount: Optional[int] = None


# ============================================================
# App factories
# ============================================================

def create_dosing_app() -> FastAPI:
    app = FastAPI(title="virtual-doser")

    @app.get("/discovery")
    async def discovery():
        return {
            "device_id": _dosing_state["device_id"],
            "type": _dosing_state["type"],
            "version": _dosing_state["version"],
        }

    @app.get("/version")
    async def version():
        return {"version": _dosing_state["version"]}

    @app.get("/monitor")
    async def monitor():
        return {"ph": _dosing_state["ph"], "tds": _dosing_state["tds"]}

    # (Alias often used by callers)
    @app.get("/sensor")
    async def sensor():
        return {"ph": _dosing_state["ph"], "tds": _dosing_state["tds"]}

    @app.post("/pump")
    async def pump(cmd: PumpCommand):
        try:
            num = cmd.resolved_number()
        except ValueError:
            raise HTTPException(status_code=422, detail="pump_number or pump required")
        _dosing_state["pumps"][num] = _dosing_state["pumps"].get(num, 0) + 1
        return {"message": "pump executed", "pump_number": num}

    @app.post("/dose_monitor")
    async def dose_monitor(cmd: PumpCommand):
        try:
            num = cmd.resolved_number()
        except ValueError:
            raise HTTPException(status_code=422, detail="pump_number or pump required")
        _dosing_state["pumps"][num] = _dosing_state["pumps"].get(num, 0) + 1
        return {"message": "combined pump executed", "pump_number": num}

    # Local “cloud queue” test hook
    @app.post("/tasks/push")
    async def push_task(task: QueueTask):
        _enqueue_local(_dosing_state["device_id"], task.dict())
        return {"queued": True}

    @app.post("/pump_calibration")
    async def pump_calibration():
        # emulate a stop/cancel action
        return {"message": "dosing cancelled"}

    # start background poller if enabled
    threading.Thread(target=_device_poller, args=(_dosing_state["device_id"],), daemon=True).start()
    return app


def create_valve_app() -> FastAPI:
    app = FastAPI(title="virtual-valve")

    @app.get("/discovery")
    async def discovery():
        valves = [{"id": i + 1, "state": "on" if st else "off"} for i, st in enumerate(_valve_state["valves"])]
        return {
            "device_id": _valve_state["device_id"],
            "type": _valve_state["type"],
            "version": _valve_state["version"],
            "valves": valves,
        }

    @app.get("/version")
    async def version():
        return {"version": _valve_state["version"]}

    @app.get("/state")
    async def state():
        valves = [{"id": i + 1, "state": "on" if st else "off"} for i, st in enumerate(_valve_state["valves"])]
        return {"device_id": _valve_state["device_id"], "valves": valves}

    @app.post("/toggle")
    async def toggle(cmd: ToggleCommand):
        vid = cmd.valve_id
        if vid is None or not (1 <= vid <= len(_valve_state["valves"])):
            raise HTTPException(status_code=400, detail="Invalid valve_id")
        idx = vid - 1
        _valve_state["valves"][idx] = not _valve_state["valves"][idx]
        return {"valve_id": vid, "new_state": "on" if _valve_state["valves"][idx] else "off"}

    @app.post("/tasks/push")
    async def push_task(task: QueueTask):
        _enqueue_local(_valve_state["device_id"], task.dict())
        return {"queued": True}

    threading.Thread(target=_device_poller, args=(_valve_state["device_id"],), daemon=True).start()
    return app


def create_switch_app() -> FastAPI:
    app = FastAPI(title="virtual-switch")

    @app.get("/discovery")
    async def discovery():
        return {
            "device_id": _switch_state["device_id"],
            "type": _switch_state["type"],
            "version": _switch_state["version"],
        }

    @app.get("/version")
    async def version():
        return {"version": _switch_state["version"]}

    @app.get("/state")
    async def state():
        channels = [{"channel": i + 1, "state": "on" if st else "off"}
                    for i, st in enumerate(_switch_state["channels"])]
        # tests expect "channels"; include "switches" for backwards-compat
        return {
            "device_id": _switch_state["device_id"],
            "channels": channels,
            "switches": channels,  # optional alias
        }

    @app.post("/toggle")
    async def toggle(cmd: ToggleCommand):
        ch = cmd.channel
        if ch is None or not (1 <= ch <= len(_switch_state["channels"])):
            raise HTTPException(status_code=400, detail="Invalid channel")
        idx = ch - 1
        _switch_state["channels"][idx] = not _switch_state["channels"][idx]
        return {"channel": ch, "new_state": "on" if _switch_state["channels"][idx] else "off"}

    @app.post("/tasks/push")
    async def push_task(task: QueueTask):
        _enqueue_local(_switch_state["device_id"], task.dict())
        return {"queued": True}

    threading.Thread(target=_device_poller, args=(_switch_state["device_id"],), daemon=True).start()
    return app


def create_cctv_app() -> FastAPI:
    app = FastAPI(title="virtual-cctv")

    @app.get("/discovery")
    async def discovery():
        return {
            "device_id": _cctv_state["device_id"],
            "type": _cctv_state["type"],
            "version": _cctv_state["version"],
        }

    @app.get("/version")
    async def version():
        return {"version": _cctv_state["version"]}

    @app.get("/status")
    async def status():
        return {
            "camera_id": _cctv_state["device_id"],
            "status": _cctv_state["status"],
        }

    @app.post("/tasks/push")
    async def push_task(task: QueueTask):
        _enqueue_local(_cctv_state["device_id"], task.dict())
        return {"queued": True}

    threading.Thread(target=_device_poller, args=(_cctv_state["device_id"],), daemon=True).start()
    return app


# ============================================================
# Process management
# ============================================================

_EMULATORS = [
    (create_dosing_app, 8001),
    (create_valve_app,  8002),
    (create_switch_app, 8003),
    (create_cctv_app,   8004),
]

_processes: List[multiprocessing.Process] = []


def _run_app(factory, port):
    app = factory()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def start_virtual_iot():
    global _processes
    for factory, port in _EMULATORS:
        p = multiprocessing.Process(target=_run_app, args=(factory, port), daemon=True)
        p.start()
        _processes.append(p)
    # let servers boot
    time.sleep(1.0)


def stop_virtual_iot():
    global _processes
    for p in _processes:
        p.terminate()
        p.join(timeout=1.0)
    _processes.clear()
