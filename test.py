#!/usr/bin/env python3
"""
Hydroleaf Full Route Test Suite
Covers every publicly exposed endpoint in one file.

Usage:
    python test_all_routes.py \
      --base-url http://localhost:8000 \
      --admin-email admin@gmail.com \
      --admin-password 123456 \
      --device-url http://127.0.0.1:8080
"""
import sys
import time
import uuid
import logging
import argparse
import base64
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------
# Argument parsing & URL shortcuts
# ---------------------------------
parser = argparse.ArgumentParser(description="Hydroleaf Full Route Test Suite")
parser.add_argument("--base-url",       default="http://localhost:8000", help="Base URL of the API server")
parser.add_argument("--admin-email",    required=True, help="Superadmin email")
parser.add_argument("--admin-password", required=True, help="Superadmin password")
parser.add_argument("--device-url",     default="http://127.0.0.1:8080", help="Simulated device endpoint")
args = parser.parse_args()

API    = f"{args.base_url}/api/v1"
ADMIN  = f"{args.base_url}/admin"
DEVICE = args.device_url

# ------------
# Sample image
# ------------
# A minimal 1×1 PNG base64; you may replace with any valid image.
BASE64_IMAGE = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGP4"
    "/w8AAn8B9kJAKgAAAABJRU5ErkJggg=="
)
IMG_BYTES = base64.b64decode(BASE64_IMAGE)

# ----------------
# Logging & Session
# ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("test_all_routes")

# session with retries
session = requests.Session()
retries = Retry(total=3, backoff_factor=0.3, status_forcelist=(500,502,503,504))
adapter = HTTPAdapter(max_retries=retries)
session.mount("http://", adapter)
session.mount("https://", adapter)

# --------------
# Assertion helper
# --------------
tested = []
def rr(resp, expect=None, allow=None):
    code = resp.status_code
    desc = f"[{resp.request.method} {resp.request.url}] → {code}"
    if expect is not None and code != expect:
        log.error(f"FAIL {desc}, expected {expect}\n  {resp.text}")
        sys.exit(1)
    if allow is not None and code not in allow:
        log.error(f"FAIL {desc}, expected one of {allow}\n  {resp.text}")
        sys.exit(1)
    log.info(f"OK   {desc}")
    tested.append((resp.request.method, resp.request.url, code))
    return resp

# --------------------
# Individual test steps
# --------------------
def test_health():
    log.info("=== HEALTH CHECKS ===")
    rr(session.get(f"{API}/health"), expect=200)
    rr(session.get(f"{API}/health/database"), expect=200)
    rr(session.get(f"{API}/health/system"), expect=200)

def test_auth_and_users():
    log.info("=== AUTH & USER FLOW ===")
    # superadmin login
    resp = rr(session.post(f"{API}/auth/login",
                           data={"username": args.admin_email, "password": args.admin_password}),
              expect=200)
    admin_token = resp.json()["access_token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # signup + login QA user
    email = f"qa_{uuid.uuid4().hex[:8]}@example.com"
    pw = "Secret123!"
    signup_data = {
        "email": email, "password": pw,
        "first_name":"QA","last_name":"Tester",
        "phone":"123","address":"123 Test St",
        "city":"Testville","state":"TS","country":"Testland",
        "postal_code":"00000","name":"My Farm","location":"Field 1"
    }
    signup = rr(session.post(f"{API}/auth/signup", json=signup_data), expect=200).json()
    user_id = signup["id"]

    login = rr(session.post(f"{API}/auth/login",
                            data={"username": email, "password": pw}),
               expect=200).json()
    user_token = login["access_token"]
    user_headers = {"Authorization": f"Bearer {user_token}"}

    # user profile
    rr(session.get(f"{API}/users/me", headers=user_headers), expect=200)
    rr(session.put(f"{API}/users/me", headers=user_headers,
                   json={"first_name":"QATwo",
                         "email":f"qa2_{uuid.uuid4().hex[:4]}@example.com"}),
       expect=200)

    # admin → user management
    rr(session.get(f"{ADMIN}/users", headers=admin_headers), expect=200)
    rr(session.get(f"{ADMIN}/users/{user_id}", headers=admin_headers), expect=200)
    rr(session.put(f"{ADMIN}/users/{user_id}", headers=admin_headers,
                   json={"role":"user"}), expect=200)
    imp = rr(session.post(f"{ADMIN}/users/impersonate/{user_id}", headers=admin_headers),
             expect=200).json()
    log.info(f"    impersonated as: {imp.get('impersonated_user')}")

    return admin_headers, user_headers, user_id

def test_farm_crud(user_headers):
    log.info("=== FARM CRUD ===")
    create = rr(session.post(f"{API}/farms", headers=user_headers,
                             json={"name":"Greenhouse","location":"Sector 9"}),
                expect=200).json()
    fid = create["id"]
    rr(session.get(f"{API}/farms", headers=user_headers), expect=200)
    rr(session.get(f"{API}/farms/{fid}", headers=user_headers), expect=200)
    rr(session.delete(f"{API}/farms/{fid}", headers=user_headers), expect=200)

def test_device_registration_and_discovery(user_headers):
    log.info("=== DEVICE REGISTRATION & DISCOVERY ===")
    # SSE discover-all (just pull a couple of lines)
    resp = session.get(f"{API}/devices/discover-all", stream=True, timeout=10)
    for i, line in enumerate(resp.iter_lines()):
        if i >= 2: break
    resp.close()
    rr(session.get(f"{API}/devices/discover?ip=127.0.0.1:8080"), expect=200)

    # register dosing unit
    dosing = rr(session.post(f"{API}/devices/dosing", headers=user_headers, json={
        "mac_id":"AA:BB:CC:DD","name":"Dosatron","type":"dosing_unit",
        "http_endpoint":DEVICE,
        "pump_configurations":[{"pump_number":1,"chemical_name":"Nutrient A"}]
    }), expect=200).json()
    did = dosing["id"]

    # register two sensors
    sid = rr(session.post(f"{API}/devices/sensor", headers=user_headers, json={
        "mac_id":"88:77:66:55","name":"pH-TDS","type":"ph_tds_sensor",
        "http_endpoint":DEVICE,"sensor_parameters":{"ph":"1","tds":"1"}
    }), expect=200).json()["id"]
    esid = rr(session.post(f"{API}/devices/sensor", headers=user_headers, json={
        "mac_id":"EE:FF:00:11","name":"Env Sensor","type":"environment_sensor",
        "http_endpoint":DEVICE,"sensor_parameters":{"temperature":"1","humidity":"1"}
    }), expect=200).json()["id"]

    # register valve controller
    _ = rr(session.post(f"{API}/devices/valve", headers=user_headers, json={
        "mac_id":"VV:AA:LL:11","name":"Valve Ctrl","type":"valve_controller",
        "http_endpoint":DEVICE,"valve_configurations":[{"valve_id":1},{"valve_id":2}]
    }), expect=200).json()["id"]

    # device list & detail & readings & version
    rr(session.get(f"{API}/devices", headers=user_headers), expect=200)
    rr(session.get(f"{API}/devices/{did}", headers=user_headers), expect=200)
    rr(session.get(f"{API}/devices/sensoreading/{sid}", headers=user_headers), expect=200)
    rr(session.get(f"{API}/devices/device/{did}/version", headers=user_headers), expect=200)
    rr(session.get(f"{API}/devices/sensoreading/{esid}", headers=user_headers), expect=200)
    rr(session.get(f"{API}/devices/device/{esid}/version", headers=user_headers), expect=200)

    # list my active devices (no subscription yet → empty list)
    rr(session.get(f"{API}/devices/my", headers=user_headers), expect=200)
    return did, sid, esid

def test_device_comm_endpoints(user_headers, admin_headers):
    log.info("=== DEVICE COMM ===")
    rr(session.get(f"{API}/device_comm/update", params={"device_id":"AA:BB:CC:DD"}), expect=200)
    rr(session.get(f"{API}/device_comm/update/pull", params={"device_id":"AA:BB:CC:DD"}),
       allow=[200,404])
    rr(session.get(f"{API}/device_comm/pending_tasks", params={"device_id":"AA:BB:CC:DD"}),
       expect=200)
    rr(session.post(f"{API}/device_comm/tasks",
                    params={"device_id":"AA:BB:CC:DD"},
                    json={"pump":1,"amount":50}),
       expect=200)
    rr(session.post(f"{API}/device_comm/heartbeat",
                    headers=admin_headers,
                    json={"device_id":"AA:BB:CC:DD","type":"dosing_unit","version":"2.1.0"}),
       expect=200)
    rr(session.post(f"{API}/device_comm/valve_event",
                    json={"device_id":"VV:AA:LL:11","valve_id":2,"state":"on"}),
       expect=200)
    rr(session.get(f"{API}/device_comm/valve/VV:AA:LL:11/state"), expect=200)
    rr(session.post(f"{API}/device_comm/valve/VV:AA:LL:11/toggle", json={"valve_id":3}),
       expect=200)

def test_config_and_dosing_profile(user_headers, did):
    log.info("=== CONFIG & DOSING PROFILE ===")
    rr(session.get(f"{API}/config/system-info", headers=user_headers), expect=200)
    prof = rr(session.post(f"{API}/config/dosing-profile",
                           headers=user_headers,
                           json={
                               "device_id":did,"plant_name":"Tomato","plant_type":"Tomato",
                               "growth_stage":"seedling",
                               "seeding_date":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                               "target_ph_min":5.5,"target_ph_max":6.5,
                               "target_tds_min":400,"target_tds_max":800,
                               "dosing_schedule":{"08:00":10.0}
                           }), expect=200).json()
    pid = prof["id"]
    rr(session.get(f"{API}/config/dosing-profiles/{did}", headers=user_headers), expect=200)
    rr(session.delete(f"{API}/config/dosing-profiles/{pid}", headers=user_headers), expect=200)

def test_dosing_endpoints(user_headers, did):
    log.info("=== DOSING OPERATIONS ===")
    rr(session.post(f"{API}/dosing/execute/{did}", headers=user_headers), expect=200)
    rr(session.post(f"{API}/dosing/cancel/{did}", headers=user_headers), expect=200)
    rr(session.get(f"{API}/dosing/history/{did}", headers=user_headers), expect=200)
    rr(session.post(f"{API}/dosing/profile", headers=user_headers,
                    json={
                      "device_id":did,"plant_name":"Pepper","plant_type":"Capsicum",
                      "growth_stage":"flowering",
                      "seeding_date":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                      "target_ph_min":5.0,"target_ph_max":6.0,
                      "target_tds_min":300,"target_tds_max":700,
                      "dosing_schedule":{"12:00":8.0}
                    }), expect=200)

def test_plants(user_headers):
    log.info("=== PLANTS CRUD & EXECUTE ===")
    # note prefix → /api/v1/plants/plants for list
    rr(session.get(f"{API}/plants/plants", headers=user_headers), expect=200)
    created = rr(session.post(f"{API}/plants",
                              headers=user_headers,
                              json={
                                "name":"Basil","type":"Herb","growth_stage":"seedling",
                                "seeding_date":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                                "region":"Rewa","location":"Greenhouse"
                              }), expect=200).json()
    pid = created["id"]
    rr(session.get(f"{API}/plants/{pid}", headers=user_headers), expect=200)
    rr(session.post(f"{API}/plants/execute-dosing/{pid}", headers=user_headers),
       expect=400)
    rr(session.delete(f"{API}/plants/{pid}", headers=user_headers), expect=200)

def test_subscriptions(user_headers):
    log.info("=== SUBSCRIPTIONS ===")
    rr(session.get(f"{API}/subscriptions/plans", headers=user_headers), expect=200)
    rr(session.get(f"{API}/subscriptions", headers=user_headers), expect=200)

def test_payments(user_headers, admin_headers):
    log.info("=== PAYMENTS ===")
    # create (201)
    order = rr(session.post(f"{API}/payments/create",
                            headers=user_headers,
                            json={"device_id":1,"plan_id":1}),
               expect=201).json()
    oid = order["id"]
    rr(session.post(f"{API}/payments/confirm/{oid}",
                    headers=user_headers,
                    json={"upi_transaction_id":"TXN123"}),
       expect=200)
    rr(session.get(f"{ADMIN}/payments", headers=admin_headers), expect=200)
    rr(session.post(f"{ADMIN}/payments/approve/{oid}", headers=admin_headers), expect=200)

def test_cloud(admin_headers, user_headers):
    log.info("=== CLOUD AUTH ===")
    # generate new key
    new = rr(session.post(f"{API}/cloud/admin/generate_cloud_key", headers=admin_headers),
             expect=200).json()
    key = new["cloud_key"]
    # verify
    rr(session.post(f"{API}/cloud/verify_key", json={"device_id":"X","cloud_key":key}),
       expect=200)
    # authenticate
    rr(session.post(f"{API}/cloud/authenticate",
                    json={"device_id":"X","cloud_key":key}),
       expect=200)
    # invalid event
    rr(session.post(f"{API}/cloud/dosing_cancel", json={"device_id":"X","event":"foo"}),
       expect=400)
    rr(session.post(f"{API}/cloud/dosing_cancel",
                    json={"device_id":"X","event":"dosing_cancelled"}),
       expect=200)

def test_supply_chain(user_headers):
    log.info("=== SUPPLY CHAIN ===")
    rr(session.post(f"{API}/supply_chain",
                    headers=user_headers,
                    json={
                      "origin":"Rewa","destination":"Delhi",
                      "produce_type":"Mango","weight_kg":100,
                      "transport_mode":"railway"
                    }),
       expect=200)

def test_admin_devices():
    log.info("=== ADMIN DEVICES (in-memory) ===")
    rr(session.get(f"{ADMIN}/devices"), expect=200)

def test_cameras():
    log.info("=== CAMERAS ===")
    cam_id = "test_cam"
    # upload day & night
    rr(session.post(f"{API}/cameras/upload/{cam_id}/day",
                    data=IMG_BYTES,
                    headers={"Content-Type":"image/jpeg"}),
       expect=200)
    rr(session.post(f"{API}/cameras/upload/{cam_id}/night",
                    data=IMG_BYTES,
                    headers={"Content-Type":"image/jpeg"}),
       expect=200)
    # still & status
    rr(session.get(f"{API}/cameras/still/{cam_id}"), expect=200)
    rr(session.get(f"{API}/cameras/api/status/{cam_id}"), expect=200)
    # list clips & nonexistent serve
    rr(session.get(f"{API}/cameras/api/clips/{cam_id}"), expect=200)
    rr(session.get(f"{API}/cameras/clips/{cam_id}/nope.mp4"), allow=[404])
    # report
    rr(session.get(f"{API}/cameras/api/report/{cam_id}"), expect=200)

if __name__ == "__main__":
    test_health()
    admin_headers, user_headers, user_id = test_auth_and_users()
    test_farm_crud(user_headers)
    did, sid, esid = test_device_registration_and_discovery(user_headers)
    test_device_comm_endpoints(user_headers, admin_headers)
    test_config_and_dosing_profile(user_headers, did)
    test_dosing_endpoints(user_headers, did)
    test_plants(user_headers)
    test_subscriptions(user_headers)
    test_payments(user_headers, admin_headers)
    test_cloud(admin_headers, user_headers)
    test_supply_chain(user_headers)
    test_admin_devices()
    test_cameras()

    log.info(f"All {len(tested)} endpoints tested successfully.")
