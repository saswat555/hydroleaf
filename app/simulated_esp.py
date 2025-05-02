# simulated_esp_all.py
"""
Hydroleaf Simulated Device Server
Simulates an ESP32-CAM, Dosing Unit, and Valve Controller on a single HTTP endpoint for testing.
Listens on port 8080 and provides all device-specific API routes.
"""
import logging
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime

# Configure logging
tlogging = logging.getLogger("simulator")
logging.basicConfig(level=logging.INFO)

# Instantiate FastAPI app
app = FastAPI(
    title="Hydroleaf Simulated ESP32 Devices",
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None,
)

# Allow all CORS (for testing)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

## Discovery Endpoint
@app.get("/discovery", summary="Simulated device discovery")
async def discovery():
    """
    Simulates the /discovery endpoint for all devices.
    Returns device metadata for registration/discovery.
    """
    logging.info("/discovery called")
    return {
        "device_id": "dummy_device",
        "name": "Simulated ESP Device",
        # For dosing registrations, tests expect "dosing_unit"
        "type": "dosing_unit",
        "version": "3.0.0",
        "status": "online",
        "ip": "127.0.0.1"
    }

## Pump Endpoint (Dosing Unit)
@app.post("/pump", summary="Activate a pump")
async def pump(request: Request):
    data = await request.json()
    pump = data.get("pump")
    amount = data.get("amount")
    if pump is None or amount is None:
        logging.warning("/pump missing pump or amount")
        raise HTTPException(status_code=400, detail="Missing pump or amount")
    logging.info(f"/pump called: pump={pump}, amount={amount}")
    return {
        "message": "Pump started",
        "pump": pump,
        "dose_ml": amount,
        "timestamp": datetime.utcnow().isoformat()
    }

## Combined Dose + Monitor Endpoint
@app.post("/dose_monitor", summary="Activate pump and monitor")
async def dose_monitor(request: Request):
    data = await request.json()
    pump = data.get("pump")
    amount = data.get("amount")
    if pump is None or amount is None:
        logging.warning("/dose_monitor missing pump or amount")
        raise HTTPException(status_code=400, detail="Missing pump or amount")
    logging.info(f"/dose_monitor called: pump={pump}, amount={amount}")
    return {
        "message": "Combined started",
        "pump": pump,
        "dose_ml": amount,
        "timestamp": datetime.utcnow().isoformat()
    }

## Pump Calibration Endpoint
@app.post("/pump_calibration", summary="Pump calibration command")
async def pump_calibration(request: Request):
    data = await request.json()
    command = data.get("command")
    logging.info(f"/pump_calibration called: command={command}")
    if command == "start":
        return {"message": "All pumps on"}
    elif command == "stop":
        return {"message": "All pumps off"}
    else:
        raise HTTPException(status_code=400, detail="Invalid command")

## Sensor Monitor Endpoint
@app.get("/monitor", summary="Return sensor readings")
async def monitor():
    logging.info("/monitor called")
    return {
        "device_id": "dummy_device",
        "type": "dosing_unit",
        "version": "3.0.0",
        "wifi_connected": True,
        # Provide fixed pH and TDS
        "ph": 6.8,
        "tds": 750
    }

## Valve State Endpoint (Valve Controller)
@app.get("/state", summary="Return valve states")
async def state():
    logging.info("/state called")
    return {
        "device_id": "dummy_valve",
        "valves": [
            {"id": i, "state": "off"} for i in range(1, 5)
        ]
    }

## Toggle Valve Endpoint
@app.post("/toggle", summary="Toggle a valve")
async def toggle(request: Request):
    data = await request.json()
    valve = data.get("valve_id")
    logging.info(f"/toggle called: valve_id={valve}")
    if valve not in [1, 2, 3, 4]:
        raise HTTPException(status_code=400, detail="Invalid valve_id")
    # Echo back toggled state
    return {
        "device_id": "dummy_valve",
        "valve_id": valve,
        "new_state": "toggled"
    }

## Main Entrypoint
def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)

if __name__ == "__main__":
    main()
