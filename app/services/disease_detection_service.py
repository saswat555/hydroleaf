from fastapi import HTTPException

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

async def _call_model(img_bytes: bytes, metadata: dict):
    """
    Placeholder – in tests this is monkeypatched.
    """
    return {"disease": "unknown", "confidence": 0.0}

def _validate_png(img_bytes: bytes) -> None:
    if not isinstance(img_bytes, (bytes, bytearray)) or len(img_bytes) < len(PNG_MAGIC):
        raise HTTPException(status_code=400, detail="Invalid image bytes")
    if not img_bytes.startswith(PNG_MAGIC):
        # tests deliberately feed JPEG header to ensure we reject it
        raise HTTPException(status_code=400, detail="Unsupported or corrupt image format")

async def detect_disease_from_image(img_bytes: bytes, metadata: dict) -> dict:
    """
    Minimal validation + delegate to async model call.
    - Accepts only PNG-like bytes (tests provide a minimal header)
    - Raises HTTPException on invalid/unsupported bytes
    """
    _validate_png(img_bytes)
    # Model call is monkeypatched in tests; just pass through.
    result = await _call_model(img_bytes, metadata or {})
    # Ensure a stable structure even for low confidence
    if not isinstance(result, dict) or "disease" not in result or "confidence" not in result:
        raise HTTPException(status_code=500, detail="Model response malformed")
    return result
