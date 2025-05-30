# Hydroleaf Platform – Comprehensive Technical Documentation

---

## 1. Purpose & Scope

Hydroleaf is an end‑to‑end, cloud‑connected agriculture platform.  It combines:

* **FastAPI backend** – multi‑tenant REST API, PostgreSQL, async I/O.
* **Embedded firmware** – ESP32‑CAM (Smart‑Cam), ESP32 (Smart Dosing Unit) and ESP8266 (Valve Controller, Smart Switch).
* **Front‑end / Portal** – (out‑of‑scope for this document).

This document covers the **backend application**, **device protocol**, **database schema**, and **operational flows**.  Source listings are omitted – the focus is behaviour, contracts, and integration guidelines.

---

## 2. High‑Level Architecture

```
┌──────────────┐        JWT / OAuth2       ┌──────────────┐
│  Web / App   │  ───────────────────────▶ │ FastAPI API  │
│   Clients    │    REST (JSON)           │  (backend)   │
└──────────────┘                           └─────┬────────┘
                                                │ SQLAlchemy (async)    
                                                ▼
                                         ┌──────────────┐
                                         │ PostgreSQL   │
                                         └──────────────┘
                                                ▲
                              OTA, Heartbeat    │ ActivationKey Bearer Token
┌──────────────┐  HTTP           ┌──────────────┴──────────────┐   
│   Devices    │ ──────────────▶ │  Device Communication API   │
│ (ESP boards) │ (JSON payload)  │  (/api/v1/device_comm/*)    │
└──────────────┘                 └─────────────────────────────┘
```

Key points:

* **Single API service** (stateless); database migrations handled via SQLAlchemy bootstrap in dev / Alembic in prod.
* **Async device communication** – plain HTTP on local network or routed through the cloud.
* **Two deployment modes** configurable via `DEPLOYMENT_MODE`:

  * **LAN** – devices discovered by scanning a /24 subnet.
  * **CLOUD** – devices call‑home; backend stores their static endpoints.
* **Device Authentication** – Devices use a unique `ActivationKey` as a Bearer Token for system-level communication (OTA, heartbeat). Specific operations like camera image uploads may use separate token mechanisms derived from this key.

---

## 3. Database Schema (PostgreSQL)

### 3.1 Core Tables

| Table               | Purpose                           | Key Columns                                    |
| ------------------- | --------------------------------- | ---------------------------------------------- |
| `users`             | Account & login                   | `email`, `hashed_password`, `role`             |
| `user_profiles`     | Extended profile (1‑to‑1)         | contact & address fields                       |
| `farms`             | User‑owned collections of devices | `name`, `location`                             |
| `devices`           | Physical hardware (any type)      | `mac_id`, `type`, `http_endpoint`, `is_active`, `firmware_version`, `last_seen` |
| `dosing_profiles`   | Crop‑specific nutrient strategy   | pH/TDS targets, schedule JSON                  |
| `sensor_readings`   | Time‑series snapshots             | `reading_type`, `value`, `timestamp`           |
| `dosing_operations` | Executed dosing runs              | `operation_id`, `actions`, `status`            |
| `tasks`             | Asynchronous commands for devices | `parameters`, `status`                         |

### 3.2 Commerce & Licensing

| Table                | Purpose                                                           |
| -------------------- | ----------------------------------------------------------------- |
| `subscription_plans` | SKU – price & allowed device types                                |
| `activation_keys`    | Pre‑paid keys tying a plan to a device (used for device auth)     |
| `subscriptions`      | Active entitlement (user × device × plan)                         |
| `payment_orders`     | UPI‑based purchase flow – QR code stored in `app/static/qr_codes` |

### 3.3 Camera & CV

| Table               | Purpose                                     |
| ------------------- | ------------------------------------------- |
| `cameras`           | Physical ESP32‑CAM units                    |
| `detection_records` | YOLO‑based object sightings                 |
| `camera_tokens`     | Short‑lived bearer tokens for upload/stream (derived from an ActivationKey) |

(See `app/models.py` for full list of relationships.)

---

## 4. Authentication & Authorisation

* **End‑user login** – `/api/v1/auth/login` (OAuth2‑Password) returns JWT (`access_token`).
* **Admin‑only routes** decorated with `Depends(get_current_admin)` (role == `superadmin`).

### 4.1 Device Authentication

*   **System-Level Communication (OTA, Heartbeat):**
    *   Devices authenticate using their unique `ActivationKey` (from the `activation_keys` table, associated with their `Device` entry upon redemption).
    *   The `ActivationKey` is sent as a Bearer token in the `Authorization` header for endpoints under `/api/v1/device_comm/`.
    *   The backend uses the `get_ota_authorized_device` dependency to validate this token and ensure the device has an active subscription.
*   **Camera Image Uploads:**
    *   The ESP32-CAM device first authenticates using its `device_id` and its `cloudKey` (which is an `ActivationKey` from the `activation_keys` table, historically referred to as "cloud key" in camera firmware) via `/api/v1/cloud/authenticate`.
    *   This endpoint issues a short-lived JWT, which is then stored in the `camera_tokens` table. This JWT is subsequently used as a Bearer token for image uploads to `/upload/{cam}/day` etc. This process is specific to camera media operations and is distinct from the general system-level authentication.

JWT payload (for user tokens):

```json
{
  "user_id": 123,
  "role": "user|admin|superadmin",
  "exp": 1714752000
}
```

---

## 5. REST API Surface (v1)

### 5.1 Users & Profiles

| Method | Path               | Description             |
| ------ | ------------------ | ----------------------- |
| `GET`  | `/api/v1/users/me` | Current profile         |
| `PUT`  | `/api/v1/users/me` | Update permitted fields |

### 5.2 Devices

| Method | Path                           | Notes                                       |
| ------ | ------------------------------ | ------------------------------------------- |
| `POST` | `/api/v1/devices/dosing`       | Register ESP32 dosing unit                  |
| `POST` | `/api/v1/devices/sensor`       | Generic pH/TDS or env sensor                |
| `POST` | `/api/v1/devices/valve`        | 4‑way valve controller                      |
| `POST` | `/api/v1/devices/smart_switch` | Multi-channel smart switch                  |
| `GET`  | `/api/v1/devices`              | List all                                    |
| `GET`  | `/api/v1/devices/my`           | Only user’s **active + subscribed** devices |
| `GET`  | `/api/v1/devices/{id}`         | Details (includes firmware version from DB) |

### 5.3 Dosing Workflow

| Step | Endpoint                                        | Remarks                               |
| ---- | ----------------------------------------------- | ------------------------------------- |
| 1    | `POST /api/v1/dosing/llm-request?device_id=...` | Build prompt → LLM → execute plan     |
| 2    | `POST /api/v1/dosing/cancel/{device_id}`        | Abort mid‑run via `/pump_calibration` |
| 3    | `GET  /api/v1/dosing/history/{device_id}`       | Completed operations                  |

### 5.4 Subscription & Billing

| Method | Path                                    | Purpose                                 |
| ------ | --------------------------------------- | --------------------------------------- |
| `POST` | `/api/v1/payments/create`               | Generates order + UPI QR PNG            |
| `POST` | `/api/v1/payments/confirm/{order_id}`   | User submits UPI TXN ID                 |
| `POST` | `/admin/payments/approve/{order_id}`    | Admin marks COMPL → subscription row    |
| `POST` | `/admin/generate_device_activation_key` | Mint key locked to device & plan        |
| `POST` | `/api/v1/subscriptions/redeem`          | User redeems key, device becomes active |

### 5.5 Camera Service (Media Operations)

| Path                | Use‑case                                    | Auth Method                     |
| ------------------- | ------------------------------------------- |---------------------------------|
| `/upload/{cam}/day` | JPEG frame upload                           | Bearer token (from `camera_tokens`) |
| `/stream/{cam}`     | Live MJPEG (authenticated)                  | Bearer token (from `camera_tokens`) |
| `/api/report/{cam}` | Object‑detection ranges merged by event gap | Backend internal / User token   |

(Additional endpoints: clip list, still capture, status.)

---

## 6. Device HTTP Contract

### 6.1 Common (All Devices)

*   **Discovery (Local Network):** `GET /discovery` (on device) → `{device_id,type,version,status,ip}`.
*   **Heartbeat:** `POST /api/v1/device_comm/heartbeat` (to backend)
    *   Payload: `{"device_id": "DEVICE_MAC_OR_UNIQUE_ID", "type": "DEVICE_TYPE_CONST", "version": "CURRENT_FIRMWARE_VERSION"}`
    *   Auth: `Authorization: Bearer <ActivationKey>`
    *   Response: Includes current tasks and update availability status (e.g., `{"status": "ok", "tasks": [], "update": {"current": "0.0.1", "latest": "0.0.2", "available": true}}`).
    *   `DEVICE_TYPE_CONST` (e.g., "camera", "dosing_unit", "smart_switch", "valve_controller") and `CURRENT_FIRMWARE_VERSION` (e.g., "0.0.1") are constants defined in each device's firmware.
*   **Firmware Update Check (Optional/Alternative to Heartbeat):** `GET /api/v1/device_comm/update` (to backend)
    *   Auth: `Authorization: Bearer <ActivationKey>`
    *   Response: `{ "current_version": "...", "latest_version": "...", "update_available": ..., "download_url": "..." }`.
*   **Firmware Download:** `GET /api/v1/device_comm/update/pull` (to backend, URL usually obtained from Heartbeat or Update Check response)
    *   Auth: `Authorization: Bearer <ActivationKey>`
    *   Response: Binary firmware file.

### 6.2 Dosing Unit

| Endpoint                 | Body              | Result                         |             |
| ------------------------ | ----------------- | ------------------------------ | ----------- |
| `POST /pump`             | `{pump, amount}`  | Runs pump for *amount ms*      |             |
| `POST /dose_monitor`     | pump + amount     | Same + returns averaged pH/TDS |             |
| `POST /pump_calibration` | \`{command\:start | stop}\`                        | Bulk ON/OFF |
| `GET  /monitor`          | —                 | `{ph, tds}` rolling average    |             |
*OTA and Heartbeat are handled by common endpoints (see 6.1).*

### 6.3 Valve Controller

| Endpoint       | Body         | Notes                      |
| -------------- | ------------ | -------------------------- |
| `GET /state`   | —            | Array of `{id,state}`      |
| `POST /toggle` | `{valve_id}` | Toggles & echoes new state |
*OTA and Heartbeat are handled by common endpoints (see 6.1).*

### 6.4 Smart Switch

| Endpoint       | Body                         | Notes                      |
| -------------- | ---------------------------- | -------------------------- |
| `GET /state`   | —                            | Array of `{channel,state}` |
| `POST /toggle` | `{channel: X, state: "on"}`  | Toggles & echoes new state |
*OTA and Heartbeat are handled by common endpoints (see 6.1).*

### 6.5 Smart‑Cam (Device-side HTTP services)
*   The ESP32-CAM firmware provides local HTTP endpoints for configuration (Wi-Fi, Cloud Key) and status.
*   Media uploads (`/upload/...`) and streaming (`/stream/...`) are initiated by authorized clients to the backend, not directly from the device to other clients on the LAN via these endpoints. The device sends frames to the backend.
*   OTA and Heartbeat are handled by common endpoints (see 6.1). Authentication for these uses the `ActivationKey` directly.

---

## 7. Firmware Update (OTA) Flow

The Over-the-Air (OTA) update mechanism is unified across all device types, employing a pull-based approach.

1.  **Device Identity & Firmware Versioning:** Each device firmware defines two key constants:
    *   `DEVICE_TYPE`: A string identifying the type of device (e.g., "camera", "dosing_unit", "smart_switch", "valve_controller"). This must match the values expected by the backend for locating firmware.
    *   `FW_VERSION`: The current semantic version of the firmware (e.g., "0.0.1", "1.2.3").

2.  **Authentication:** All OTA and heartbeat communications are authenticated using the device's unique `ActivationKey`. This key is provided as a Bearer token in the `Authorization` header of HTTP requests to the backend.

3.  **Heartbeat & Update Check Trigger:**
    *   Devices periodically send a heartbeat POST request to `/api/v1/device_comm/heartbeat`. The payload includes their `device_id`, `type`, and current `version`.
    *   The backend's response to the heartbeat (e.g., `{"update": {"available": true, "latest": "0.0.2"}}`) informs the device if an update is available.
    *   Alternatively, or in addition, devices can periodically call `GET /api/v1/device_comm/update`. This endpoint also returns update availability status and the specific download URL.

4.  **Firmware Storage & Version Comparison:**
    *   The backend stores firmware binaries in a structured directory: `firmware/<device_type>/<version>/firmware.bin`. For example, firmware for a smart switch version 0.1.0 would be at `firmware/smart_switch/0.1.0/firmware.bin`.
    *   Upon receiving a heartbeat or update check, the backend compares the `version` reported by the device with the latest semantic version available for its `device_type` in the `firmware/` directory.

5.  **Downloading Firmware:**
    *   If an update is available, the response from `/heartbeat` (in the `update` object) or `/update` (as `download_url`) will provide the URL to fetch the new firmware. This URL is typically `/api/v1/device_comm/update/pull`.
    *   The device makes a GET request to this `download_url`, again including the `Authorization: Bearer <ActivationKey>` header.

6.  **Applying Update:**
    *   The device receives the binary firmware file in the HTTP response body.
    *   It uses its platform-specific update mechanism:
        *   **ESP32:** Typically uses the `Update.begin()`, `Update.writeStream()`, and `Update.end()` methods.
        *   **ESP8266:** Typically uses `ESP8266httpUpdate.update(client, download_url, current_firmware_version_string)`. The `ESP8266httpUpdate` library can often handle the HTTP GET and flashing internally, including setting the authorization header.

7.  **Reboot:** A successful firmware flash automatically triggers a device reboot, after which it runs the new firmware version. The device will then report its new version in subsequent heartbeats.

8.  **Subscription Requirement:** The backend, via the `get_ota_authorized_device` dependency, ensures that only devices with an active subscription (verified through the `ActivationKey`) are provided with firmware updates.

---

## 8. Finite State of a Device

```
UNREGISTERED ──┬─► REGISTERED (inactive) ──┬─► ACTIVE
               │                           │   ▲
               │                           ▼   │
               └─► OFFLINE (missed heartbeat) ◄─┘
```

* **REGISTERED** – row exists in `devices` but no paid plan or key not yet redeemed.
* **ACTIVE** – `subscriptions.active=true` for the associated device and within the subscription date window.
* **OFFLINE** – background watcher (or logic within heartbeat processing) sets `is_online` false after `OFFLINE_TIMEOUT` or if heartbeats cease. `last_seen` timestamp in `devices` table is updated on each heartbeat.

---

## 9. Environment & Configuration

| Variable          | Default      | Meaning                                 |
| ----------------- | ------------ | --------------------------------------- |
| `DATABASE_URL`    | *none*       | `postgresql+asyncpg://…` (mandatory)    |
| `DEPLOYMENT_MODE` | `LAN`        | LAN scanning vs cloud‑static            |
| `LAN_SUBNET`      | auto‑derived | e.g. `192.168.1.0/24`                   |
| `USE_OLLAMA`      | `true`       | LLM backend (Ollama vs OpenAI)          |
| `SERPER_API_KEY`  | —            | Google Serper for web‑augmented prompts |
| `SECRET_KEY`      | —            | JWT signing (for user tokens)           |

---

## 10. Operational Tasks

* **Bootstrap local DB** – on first start `Base.metadata.create_all()` protected by PostgreSQL advisory lock `0x6A7971`.
* **Workers**

  * Camera detection `CameraQueue` (Ultralytics YOLO) – N workers.
  * `offline_watcher` – flips camera `is_online` & cleans frames/clips (may be replaced by per-device `last_seen` tracking).
* **Health** – `/api/v1/health` (`system`, `database`, `uptime`).

---

## 11. Testing Aids

* `app/simulated_esp.py` – single FastAPI server emulating all device routes on port 8080.
* `device code/**/code.ino` – production firmware reference; compile & flash with **Arduino IDE 2.x** or **PlatformIO**.

---

## 12. Extensibility & TODO

* **Migrations** – Integrate Alembic; current create‑all not prod‑safe.
* **RBAC** – Expand roles beyond `user / superadmin`.
* **Metrics** – Expose Prometheus under `/metrics`.
* **WebSockets** – Replace SSE for progressive discovery.
* **Retry/Back‑off** – Device controller to implement exponential retry on transient errors (partially done in firmware).
* **Unit test coverage** – Add PyTest with HTTPX & pytest‑asyncio.

---

*Document last updated: 6 May 2024*
