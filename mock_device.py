# mock_devices.py
import threading
import uvicorn
from fastapi import FastAPI
from typing import List, Dict

# -------- Dosing Device --------
dosing_app = FastAPI()

@dosing_app.get("/discovery")
async def discovery():
    return {
        "device_id": "mock-dosing-001",
        "name": "Mock Dosing Device",
        "type": "dosing_unit",
        "status": "online",
        "version": "1.0.0"
    }

@dosing_app.get("/version")
async def discovery():
    return {
        "status": "online",
        "version": "1.0.0"
    }


# -------- Sensor Device --------
sensor_app = FastAPI()

@sensor_app.get("/monitor")
async def monitor():
    return {
        "ph": 6.1,
        "tds": 720,
        "temperature": 23.5,
        "timestamp": "2025-07-10T15:30:00Z"
    }

@sensor_app.get("/version")
async def discovery():
    return {
        "status": "online",
        "version": "1.0.0"
    }

# -------- Valve Device --------
valve_app = FastAPI()


@valve_app.get("/state")
async def get_valve_state() -> Dict:
    return {
        "device_id": "valve_1234ABCD",
        "type": "valve_controller",
        "valves": [
            {"id": 1, "state": "closed"},
            {"id": 2, "state": "open"},
            {"id": 3, "state": "closed"},
            {"id": 4, "state": "closed"}
        ]
    }


# -------- Runner Function --------
def run_app(app, port):
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

if __name__ == "__main__":
    # Start both devices on different ports
    threading.Thread(target=run_app, args=(dosing_app, 8001), daemon=True).start()
    threading.Thread(target=run_app, args=(sensor_app, 8002), daemon=True).start()
    threading.Thread(target=run_app, args=(valve_app, 8003), daemon=True).start()

    print("🚀 Mock devices running:")
    print("   Dosing device: http://localhost:8001/discovery")
    print("   Sensor device: http://localhost:8002/monitor")
    print("   Valve device: http://localhost:8002/state")
    
    # Keep main thread alive
    import time
    while True:
        time.sleep(1)
