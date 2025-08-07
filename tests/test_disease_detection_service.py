# tests/test_disease_detection_service.py
import pytest
from fastapi import HTTPException
from app.services.disease_detection_service import detect_disease_from_image

@pytest.fixture
def dummy_png():
    # minimal valid PNG header + padding
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

@pytest.mark.asyncio
async def test_detect_disease_happy_path(monkeypatch, dummy_png):
    async def fake_model(img_bytes, meta):
        return {"disease": "healthy", "confidence": 0.99}

    monkeypatch.setattr(
        "app.services.disease_detection_service._call_model",
        fake_model,
    )
    out = await detect_disease_from_image(dummy_png, {"plant": "tomato"})
    assert out["disease"] == "healthy"
    assert out["confidence"] > 0.5

@pytest.mark.asyncio
async def test_detect_disease_invalid_image_raises():
    with pytest.raises(HTTPException):
        await detect_disease_from_image(b"not-an-image", {"plant": "tomato"})

@pytest.mark.asyncio
async def test_detect_disease_low_confidence_warning(monkeypatch, dummy_png):
    async def fake_model(img_bytes, meta):
        # simulate borderline confidence
        return {"disease": "blight", "confidence": 0.49}

    monkeypatch.setattr(
        "app.services.disease_detection_service._call_model",
        fake_model,
    )
    out = await detect_disease_from_image(dummy_png, {"plant": "potato"})
    # even if confidence is low, we should still get a result structure
    assert out["disease"] == "blight"
    assert out["confidence"] == pytest.approx(0.49)

@pytest.mark.asyncio
async def test_detect_disease_unsupported_format_raises(monkeypatch):
    # supply a JPEG header but pass as PNG to force decode error
    bad_jpeg = b"\xff\xd8\xff" + b"\x00"*50
    with pytest.raises(HTTPException):
        await detect_disease_from_image(bad_jpeg, {"plant": "tomato"})