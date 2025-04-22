**Hydroleaf API Documentation**

**Overview**
Hydroleaf is a production‑grade FastAPI backend for managing hydroponic and aquaponic IoT devices, sensors, dosing operations, and AI‑driven supply‑chain analyses. It provides modular routers for authentication, user and admin management, device discovery/registration, real‑time sensor monitoring, dosing execution (including LLM‑powered plans), plant profiling, supply‑chain optimization, camera integrations, and device communication.

---

## Table of Contents
1. [Logical Flow](#logical-flow)
2. [Prerequisites & Setup](#prerequisites--setup)
3. [Configuration & Environment](#configuration--environment)
4. [Database & Migrations](#database--migrations)
5. [Running the Server](#running-the-server)
6. [API Endpoints](#api-endpoints)
   - [Health](#health)
   - [Authentication](#authentication)
   - [User Management](#user-management)
   - [Admin Management](#admin-management)
   - [Subscription & Activation](#subscription--activation)
   - [Farms](#farms)
   - [Device Discovery & Registration](#device-discovery--registration)
   - [Device Communication](#device-communication)
   - [Dosing](#dosing)
   - [Configuration](#configuration)
   - [Plants](#plants)
   - [Supply Chain Analysis](#supply-chain-analysis)
   - [Camera Streaming & Upload](#camera-streaming--upload)
   - [Heartbeat](#heartbeat)

---

## Logical Flow
1. **User Signup/Login** → Obtain JWT.
2. **Farm Creation** → Associate farms to users.
3. **Device Registration** → Register dosing units, sensors, or valve controllers with hardware discovery.
4. **Activation Key / Subscription** → Redeem activation key for device capability and billing.
5. **Sensor Monitoring & Dosing Execution**:
   - Fetch live readings (`/devices/sensoreading/{id}`).
   - Execute dosing via HTTP endpoints (`/dosing/execute/{id}`) or LLM‑driven plans (`/dosing/llm-request`).
6. **Configuration & Profiles** → Manage dosing profiles under `/config`.
7. **Plant Management** → CRUD plant profiles and trigger dosing per plant.
8. **Supply Chain Optimization** → Analyze transport costs with LLM support.
9. **Camera Integration** → Upload day/night frames, stream MJPEG, list clips.
10. **Device Communication & Heartbeat** → Poll pending tasks, OTA firmware updates, and status via `/device_comm` and `/heartbeat`.

---

## Prerequisites & Setup
- **Python 3.12+**
- **SQLite** (default) or **PostgreSQL**
- **Ollama** (optional, for LLM calls)
- **Serper API Key** for supply‑chain searches
- **.env** file in `app/` with:
  - `DATABASE_URL`
  - `SECRET_KEY`, `SESSION_KEY`
  - LLM / Ollama and Serper settings

### Virtual Environment
```bash
python3 -m venv env
source env/bin/activate      # Linux / Mac
# or
env\Scripts\Activate.ps1    # Windows PowerShell
```

### Install Dependencies
```bash
pip install -r requirements.txt
```

---

## Configuration & Environment
Copy `.env.example` to `.env` and set values:
```ini
ENVIRONMENT=development
DEBUG=True
PORT=8000
DATABASE_URL=sqlite+aiosqlite:///./Hydroleaf.db
SECRET_KEY=your-secret
SESSION_KEY=your-session-key
SERPER_API_KEY=...
OLLAMA_URL=http://localhost:11434/api/generate
USE_OLLAMA=false
```

---

## Database & Migrations
Hydroleaf uses Alembic for migrations plus SQLAlchemy metadata creation.
- Ensure `alembic.ini` and `alembic/` directory exist.
- On startup, migrations run automatically.

To run manually:
```bash
alembic upgrade head
```

---

## Running the Server
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port ${PORT}
```

---

## API Endpoints
### Health
- **GET** `/api/v1/health` → System status
- **GET** `/api/v1/health/database` → DB connectivity
- **GET** `/api/v1/health/system` → Full health report

### Authentication (`/api/v1/auth`)
- **POST** `/login` (form-data): `username`, `password` → `{ access_token, token_type }`
- **POST** `/signup` (JSON `UserCreate`) → `UserResponse`

### User Management
- **GET** `/api/v1/users/me` → `UserResponse` (JWT required)
- **PUT** `/api/v1/users/me` (JSON `UserUpdate`) → updates profile

### Admin Management (`/admin/users`)
- **GET** `/admin/users/` → List all users
- **GET** `/admin/users/{id}` → Single user
- **PUT** `/admin/users/{id}` (JSON `UserUpdate`) → Update email/role
- **DELETE** `/admin/users/{id}` → Delete user
- **POST** `/admin/users/impersonate/{id}` → JWT for impersonation

### Subscription & Activation (`/admin` & `/api/v1/subscriptions`)
- **POST** `/admin/generate_activation_key` → `{ activation_key }`
- **POST** `/admin/subscription_plans` (JSON `SubscriptionPlanCreate`) → New plan
- **POST** `/api/v1/subscriptions/redeem?activation_key=&device_id=` → `SubscriptionResponse`

### Farms (`/api/v1/farms`)
- **POST** `/` (JSON `FarmCreate`) → `FarmResponse`
- **GET** `/` → List user farms
- **GET** `/{farm_id}` → Single farm
- **DELETE** `/{farm_id}` → Remove farm

### Device Discovery & Registration (`/api/v1/devices`)
- **GET** `/discover-all` → SSE of discovery progress
- **GET** `/discover?ip=` → Single device discovery
- **POST** `/dosing` (JSON `DosingDeviceCreate`) → Register dosing unit
- **POST** `/sensor` (JSON `SensorDeviceCreate`) → Register sensor
- **POST** `/valve` (JSON `ValveDeviceCreate`) → Register valve controller
- **GET** `/sensoreading/{device_id}` → Real‑time sensor data
- **GET** `/` → List all devices
- **GET** `/{device_id}` → Device details
- **GET** `/device/{id}/version` → Firmware version

### Device Communication (`/api/v1/device_comm`)
- **GET** `/pending_tasks?device_id=` → List pending pump tasks
- **GET** `/update?device_id=` → Check OTA availability
- **GET** `/update/pull?device_id=` → Download firmware
- **POST** `/tasks?device_id=` (JSON `SimpleDosingCommand`) → Enqueue pump task
- **POST** `/valve_event` (JSON payload) → Log valve event
- **GET** `/valve/{device_id}/state` → Current valve states
- **POST** `/valve/{device_id}/toggle` (JSON `{ valve_id }`) → Toggle valve
- **POST** `/heartbeat` → Device heartbeat (JWT required)

### Dosing (`/api/v1/dosing`)
- **POST** `/execute/{device_id}` → Trigger direct dosing
- **POST** `/cancel/{device_id}` → Cancel dosing
- **GET** `/history/{device_id}` → Retrieve dosing history
- **POST** `/profile` (JSON `DosingProfileCreate`) → Create dosing profile
- **POST** `/llm-request?device_id=` (JSON `LlmDosingRequest`) → LLM‑powered dosing
- **POST** `/llm-plan?device_id=` (JSON `llmPlaningRequest`) → LLM‑driven growth plan
- **POST** `/unified-dosing` (JSON `DosingProfileServiceRequest`) → Auto‑profile via sensor+LLM

### Configuration (`/api/v1/config`)
- **GET** `/system-info` → Version & device counts
- **POST** `/dosing-profile` → Alias for create profile
- **GET** `/dosing-profiles/{device_id}` → List profiles
- **DELETE** `/dosing-profiles/{profile_id}` → Remove profile

### Plants (`/plants`)
- **GET** `/plants` → All plant profiles
- **GET** `/plants/{id}` → Single plant
- **POST** `/` (JSON `PlantCreate`) → New plant
- **DELETE** `/plants/{id}` → Delete plant
- **POST** `/execute-dosing/{plant_id}` → Auto‑dose per plant

### Supply Chain Analysis (`/supply_chain`)
- **POST** `/` (JSON `TransportRequest`) → `SupplyChainAnalysisResponse`

### Camera Streaming & Upload (`/api/v1/cameras`)
- **POST** `/upload/{camera_id}/day` → Upload day frame
- **POST** `/upload/{camera_id}/night` → Upload night frame
- **GET** `/stream/{camera_id}` → MJPEG live stream
- **GET** `/still/{camera_id}` → Latest frame
- **GET** `/api/clips/{camera_id}` → List MP4 clips
- **GET** `/clips/{camera_id}/{clip_name}` → Download clip
- **GET** `/api/status/{camera_id}` → Online status

### Heartbeat (`/heartbeat`)
- **POST** `/heartbeat` → Device heartbeat & update registry

---

**Happy deploying & growing!**

