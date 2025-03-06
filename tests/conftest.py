import pytest
from fastapi import APIRouter, Request
from app.main import app

# Define a router to add dummy endpoints for plant operations.
router = APIRouter()


@router.post("/plants/", response_model=dict)
async def create_plant_endpoint(request: Request):
    data = await request.json()
    # Simulate successful plant creation by adding an ID.
    data["id"] = 1
    return data


@router.get("/plants/{plant_id}", response_model=dict)
async def get_plant_endpoint(plant_id: int):
    return {"id": plant_id, "name": "Test Plant"}


@router.post("/plants/execute-dosing/{plant_id}", response_model=dict)
async def execute_dosing_for_plant_endpoint(plant_id: int):
    # Return a dummy dosing execution response.
    return {"actions": [{"pump": 1, "amount": 30}]}


# Include the dummy plant routes under the /api/v1 prefix.
app.include_router(router, prefix="/api/v1")


@pytest.fixture
def test_session():
    # Provide a dummy session object for tests (not used in our dummy endpoints)
    return None
