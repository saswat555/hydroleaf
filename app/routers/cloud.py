# app/routers/cloud.py
import secrets
import logging
from fastapi import APIRouter, HTTPException, status
from app.schemas import CloudAuthenticationRequest, CloudAuthenticationResponse, DosingCancellationRequest

logger = logging.getLogger(__name__)

router = APIRouter()

# For demonstration purposes, we use a fixed cloud key.
EXPECTED_CLOUD_KEY = "my_cloud_secret"  # In production, load from environment variables

@router.post("/authenticate", response_model=CloudAuthenticationResponse)
async def authenticate_cloud(auth_request: CloudAuthenticationRequest):
    """
    Authenticate a device using its cloud key.
    Returns a token if the provided key is valid.
    """
    if auth_request.cloud_key != EXPECTED_CLOUD_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid cloud key"
        )
    
    # Generate a token (in production, use JWT or a more secure method)
    token = secrets.token_hex(16)
    logger.info(f"Device {auth_request.device_id} authenticated successfully. Token: {token}")
    return CloudAuthenticationResponse(token=token, message="Authentication successful")

@router.post("/dosing_cancel")
async def dosing_cancel(request: DosingCancellationRequest):
    """
    Endpoint to receive a dosing cancellation callback.
    Validates the event type and logs the cancellation.
    """
    if request.event != "dosing_cancelled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid event type"
        )
    logger.info(f"Dosing cancelled for device {request.device_id}")
    # Here you can add additional processing (e.g., update DB state)
    return {"message": "Dosing cancellation received", "device_id": request.device_id}
