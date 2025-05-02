# Hydroleaf Platform – Comprehensive Technical Documentation

---

## 1. Purpose & Scope

Hydroleaf is an end‑to‑end, cloud‑connected agriculture platform.  It combines:

* **FastAPI backend** – multi‑tenant REST API, PostgreSQL, async I/O.
* **Embedded firmware** – ESP32‑CAM (Smart‑Cam), ESP32 (Smart Dosing Unit) and ESP8266 (Valve Controller).
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
                              OTA, SSE, JSON    │
┌──────────────┐  HTTP+Bearer    ┌──────────────┴──────────────┐   
│   Devices    │ ──────────────▶ │  Device / Camera services   │
│ (ESP boards) │                 │  (discovery, pumps, OTA…)   │
└──────────────┘                 └─────────────────────────────┘
```

Key points:

* **Single API service** (stateless); database migrations handled via SQLAlchemy bootstrap in dev / Alembic in prod.
* **Async device communication** – plain HTTP on local network or routed through the cloud.
* **Two deployment modes** configurable via `DEPLOYMENT_MODE`:

  * **LAN** – devices discovered by scanning a /24 subnet.
  * **CLOUD** – devices call‑home; backend stores their static endpoints.
* **Edge‑auth** – devices authenticate with short‑lived **JWT** obtained via a one‑time **Cloud Key**.

---

## 3. Database Schema (PostgreSQL)

### 3.1 Core Tables

| Table               | Purpose                           | Key Columns                                    |
| ------------------- | --------------------------------- | ---------------------------------------------- |
| `users`             | Account & login                   | `email`, `hashed_password`, `role`             |
| `user_profiles`     | Extended profile (1‑to‑1)         | contact & address fields                       |
| `farms`             | User‑owned collections of devices | `name`, `location`                             |
| `devices`           | Physical hardware (any type)      | `mac_id`, `type`, `http_endpoint`, `is_active` |
| `dosing_profiles`   | Crop‑specific nutrient strategy   | pH/TDS targets, schedule JSON                  |
| `sensor_readings`   | Time‑series snapshots             | `reading_type`, `value`, `timestamp`           |
| `dosing_operations` | Executed dosing runs              | `operation_id`, `actions`, `status`            |
| `tasks`             | Asynchronous commands for devices | `parameters`, `status`                         |

### 3.2 Commerce & Licensing

| Table                | Purpose                                                           |
| -------------------- | ----------------------------------------------------------------- |
| `subscription_plans` | SKU – price & allowed device types                                |
| `activation_keys`    | Pre‑paid keys tying a plan to a device                            |
| `subscriptions`      | Active entitlement (user × device × plan)                         |
| `payment_orders`     | UPI‑based purchase flow – QR code stored in `app/static/qr_codes` |

### 3.3 Camera & CV

| Table               | Purpose                                     |
| ------------------- | ------------------------------------------- |
| `cameras`           | Physical ESP32‑CAM units                    |
| `detection_records` | YOLO‑based object sightings                 |
| `camera_tokens`     | Short‑lived bearer tokens for upload/stream |

(See `app/models.py` for full list of relationships.)

---

## 4. Authentication & Authorisation

* **End‑user login** – `/api/v1/auth/login` (OAuth2‑Password) returns JWT (`access_token`).
* **Device login** – `/api/v1/cloud/authenticate` accepts `{device_id, cloud_key}` and issues a 32‑byte token stored in `camera_tokens`.
* **Admin‑only routes** decorated with `Depends(get_current_admin)` (role == `superadmin`).

JWT payload:

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
| `GET`  | `/api/v1/devices`              | List all                                    |
| `GET`  | `/api/v1/devices/my`           | Only user’s **active + subscribed** devices |
| `GET`  | `/api/v1/devices/{id}`         | Details                                     |
| `GET`  | `/api/v1/devices/{id}/version` | Firmware version fetched via HTTP probe     |

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

### 5.5 Camera Service

| Path                | Use‑case                                    |
| ------------------- | ------------------------------------------- |
| `/upload/{cam}/day` | JPEG frame upload (Bearer token)            |
| `/stream/{cam}`     | Live MJPEG (authenticated)                  |
| `/api/report/{cam}` | Object‑detection ranges merged by event gap |

(Additional endpoints: clip list, still capture, status.)

---

## 6. Device HTTP Contract

### 6.1 Common

* **Discovery** `GET /discovery` → `{device_id,type,version,status,ip}`.
* **Firmware check** performed by device via
  `GET /api/v1/device_comm/update?device_id=MAC`.

### 6.2 Dosing Unit

| Endpoint                 | Body              | Result                         |             |
| ------------------------ | ----------------- | ------------------------------ | ----------- |
| `POST /pump`             | `{pump, amount}`  | Runs pump for *amount ms*      |             |
| `POST /dose_monitor`     | pump + amount     | Same + returns averaged pH/TDS |             |
| `POST /pump_calibration` | \`{command\:start | stop}\`                        | Bulk ON/OFF |
| `GET  /monitor`          | —                 | `{ph, tds}` rolling average    |             |

### 6.3 Valve Controller

| Endpoint       | Body         | Notes                      |
| -------------- | ------------ | -------------------------- |
| `GET /state`   | —            | Array of `{id,state}`      |
| `POST /toggle` | `{valve_id}` | Toggles & echoes new state |

### 6.4 Smart‑Cam

\| Upload  | `POST /upload/{cam_id}/{day|night}` – JPEG binary |
\| Token   | Sent in `Authorization: Bearer <token>` header |
\| OTA     | Device GETs `/api/v1/device_comm/update/pull?device_id=CAM_ID` (binary) |

---

## 7. Firmware Update (OTA) Flow

1. Device heartbeat (`/device_comm/heartbeat`) reports current `version`.
2. Backend compares to latest semantic version in `/firmware/<type>/<ver>/firmware.bin`.
3. If `update_available`, device calls the `…/pull` URL and streams the binary into `Update` API.
4. Successful flash reboots automatically.

---

## 8. Finite State of a Device

```
UNREGISTERED ──┬─► REGISTERED (inactive) ──┬─► ACTIVE
               │                           │   ▲
               │                           ▼   │
               └─► OFFLINE (missed heartbeat) ◄─┘
```

* **REGISTERED** – row exists in `devices` but no paid plan.
* **ACTIVE** – `subscriptions.active=true` and within date window.
* **OFFLINE** – background watcher sets `is_online` false after `OFFLINE_TIMEOUT`.

---

## 9. Environment & Configuration

| Variable          | Default      | Meaning                                 |
| ----------------- | ------------ | --------------------------------------- |
| `DATABASE_URL`    | *none*       | `postgresql+asyncpg://…` (mandatory)    |
| `DEPLOYMENT_MODE` | `LAN`        | LAN scanning vs cloud‑static            |
| `LAN_SUBNET`      | auto‑derived | e.g. `192.168.1.0/24`                   |
| `USE_OLLAMA`      | `true`       | LLM backend (Ollama vs OpenAI)          |
| `SERPER_API_KEY`  | —            | Google Serper for web‑augmented prompts |
| `SECRET_KEY`      | —            | JWT signing                             |

---

## 10. Operational Tasks

* **Bootstrap local DB** – on first start `Base.metadata.create_all()` protected by PostgreSQL advisory lock `0x6A7971`.
* **Workers**

  * Camera detection `CameraQueue` (Ultralytics YOLO) – N workers.
  * `offline_watcher` – flips camera `is_online` & cleans frames/clips.
* **Health** – `/api/v1/health` (`system`, `database`, `uptime`).

---

## 11. Testing Aids

* `app/simulated_esp.py` – single FastAPI server emulating all device routes on port 8080.
* `device code/**/code.ino` – production firmware reference; compile & flash with **Arduino IDE 2.x**.

---

## 12. Extensibility & TODO

* **Migrations** – Integrate Alembic; current create‑all not prod‑safe.
* **RBAC** – Expand roles beyond `user / superadmin`.
* **Metrics** – Expose Prometheus under `/metrics`.
* **WebSockets** – Replace SSE for progressive discovery.
* **Retry/Back‑off** – Device controller to implement exponential retry on transient errors.
* **Unit test coverage** – Add PyTest with HTTPX & pytest‑asyncio.

---

*Document last updated: 2 May 2025*
