
# Krishiverse

Krishiverse is an IoT platform for managing dosing devices and monitoring sensors with local deep learning integration. The system allows you to add dosing pumps and monitoring devices, retrieve sensor data (e.g. pH and TDS), and use a local LLM (Ollama) to determine optimal dosing amounts.

## Features

- **Device Registration & Discovery:** 
  - Add and manage dosing and monitoring devices.
  - Discover available devices on the local network.
- **Sensor Integration:**
  - Retrieve sensor readings (pH, TDS) from monitoring devices.
- **LLM Integration:**
  - Build detailed prompts from sensor data and plant configuration.
  - Call a locally running Ollama model to get dosing recommendations.
- **Modular & Extensible:**
  - Easily extend the system to include new sensors or AI modules.

## Prerequisites

- Python 3.12 or above
- PostgreSQL (for production) or SQLite (for testing)
- [Ollama](https://ollama.com/) installed locally for LLM calls
- (Optional) Docker for containerized deployment

## Setup

### Virtual Environment

Create and activate a virtual environment named `env`.

#### On Linux/Mac:
```bash
python3 -m venv env
source env/bin/activate
```

#### On Windows (CMD):
```cmd
python -m venv env
env\Scripts\activate
```

#### On Windows (PowerShell):
```powershell
python -m venv env
.\env\Scripts\Activate.ps1
```

### Install Dependencies

Install the required packages:
```bash
pip install -r requirements.txt
```

### Database Setup

- **Production:**  
  Set up your PostgreSQL database and update the `DATABASE_URL` environment variable accordingly.
- **Testing:**  
  SQLite is used by default (see `DATABASE_URL` in this README).

## Running the Application

### Running Tests

To run tests with logging enabled (which will show LLM logs), execute:
```bash
python -m pytest -o log_cli=true -o log_cli_level=INFO
```

### Starting the Server



## VSCode Configuration

Ensure that your virtual environment is activated in VSCode:

- **Linux/Mac:**  
  In your workspace settings (`.vscode/settings.json`), add:
  ```json
  {
      "python.pythonPath": "env/bin/python"
  }
  ```

- **Windows:**  
  In your workspace settings, add:
  ```json
  {
      "python.pythonPath": "env\\Scripts\\python.exe"
  }
  ```

## Project Structure

```
krishiverse/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── models.py
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── config.py
│   │   └── devices.py
│   ├── schemas.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── llm.py
│   │   ├── ph_tds.py
│   │   ├── device_discovery.py
│   │   └── dosing_profile_service.py
│   └── core/
│       ├── __init__.py
│       ├── config.py
│       └── database.py
├── tests/
│   └── test_main.py
├── requirements.txt
└── .gitignore
```

## Useful Commands

- **Run tests with logging:**
  ```bash
  python -m pytest -o log_cli=true -o log_cli_level=INFO
  ```
- **Start the server:**
  ```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
  ```

## Run Backend 
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
## Additional Notes

- Customize environment variables as needed.
- For production deployment, consider using Docker and a process manager.
- The LLM service uses Ollama. Ensure it is correctly configured and running locally.
- The sensor IP for monitoring devices is stored in the database when the device is added. The dosing profile service retrieves this IP and uses it to fetch sensor readings.

Happy Coding!
