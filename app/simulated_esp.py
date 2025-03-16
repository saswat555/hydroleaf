# simulated_esp.py
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from datetime import datetime

simulated_esp_app = FastAPI(title="Simulated ESP32 Device")

@simulated_esp_app.get("/discovery")
async def discovery():
    return {
        "device_id": "dummy_device",
        "name": "Simulated ESP Device",
        "type": "DOSING_MONITOR_UNIT",
        "version": "2.1.0",
        "status": "online",
        "ip": "127.0.0.1"  # or the local host as needed
    }

@simulated_esp_app.post("/pump")
async def pump(request: Request):
    data = await request.json()
    pump = data.get("pump")
    amount = data.get("amount")
    if pump is None or amount is None:
        raise HTTPException(status_code=400, detail="Missing pump or amount")
    return {
        "msg": f"Pump {pump} activated with dose {amount}",
        "timestamp": datetime.utcnow().isoformat()
    }

@simulated_esp_app.get("/monitor")
async def monitor():
    # Simulated sensor readings
    return {
        "device_id": "dummy_device",
        "type": "DOSING_MONITOR_UNIT",
        "version": "2.1.0",
        "wifi_connected": True,
        "pH": 6.8,
        "TDS": 750
    }

# You can add other endpoints (/dose_monitor, /pump_calibration, etc.) as needed.
