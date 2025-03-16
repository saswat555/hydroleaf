import time

# In‑memory registry: key=device_id, value=dict(ip=<ip>, last_seen=<timestamp>)
_connected_devices = {}

def update_device(device_id: str, ip: str) -> None:
    _connected_devices[device_id] = {"ip": ip, "last_seen": time.time()}

def get_connected_devices() -> dict:
    now = time.time()
    # Only return devices seen in the last 60 seconds (adjust as needed)
    return {device_id: info for device_id, info in _connected_devices.items() if now - info["last_seen"] < 60}
