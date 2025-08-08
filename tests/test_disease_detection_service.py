# tests/test_disease_detection_service.py
import os
import pytest
import httpx
from fastapi import HTTPException
from app.services.disease_detection_service import detect_disease_from_image


@pytest.mark.asyncio
@pytest.mark.xfail(reason="Will pass once backend uses PlantVillage model", strict=False)
async def test_detect_disease_real_tomato_image_returns_expected_label():
    """
    Integration-style test:
      - downloads a *real* plant image (URL can be overridden)
      - passes real-ish plant metadata (what we store in DB)
      - expects the model to return the correct PlantVillage disease label
    """
    url = os.getenv(
        "PLANT_TEST_IMAGE_URL",
        # Default: tomato bacterial spot photo on Wikipedia; override to PlantVillage sample in your env
        "https://upload.wikimedia.org/wikipedia/commons/3/3f/Bacterial_spot_of_tomato_BC1.JPG",
    )
    expected = os.getenv("PLANT_TEST_EXPECTED_LABEL", "Tomato___Bacterial_spot")

    # mirror the Plant table fields we store
    meta = {
        "name": "Tomato – test leaf",
        "type": "fruit",
        "growth_stage": "veg",
        "seeding_date": "2025-07-01T00:00:00Z",
        "region": "Greenhouse",
        "location_description": "Rack 1",
        "target_ph_min": 5.5,
        "target_ph_max": 6.5,
        "target_tds_min": 300,
        "target_tds_max": 700,
        # also include a simple hint for the model
        "plant": "tomato",
    }

    # fetch image bytes (skip if network blocked/unavailable)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            if r.status_code != 200 or not r.content:
                pytest.skip(f"Could not fetch test image: HTTP {r.status_code}")
            img_bytes = r.content
    except httpx.RequestError:
        pytest.skip("Network required to fetch real plant image")

    out = await detect_disease_from_image(img_bytes, meta)

    # Once your backend calls PlantVillage, this xfail should flip to pass.
    assert isinstance(out, dict), out
    assert out["disease"] == expected
    assert 0.0 <= out.get("confidence", 0.0) <= 1.0


@pytest.mark.asyncio
async def test_detect_disease_invalid_image_raises():
    with pytest.raises(HTTPException):
        await detect_disease_from_image(b"not-an-image", {"plant": "tomato"})


@pytest.mark.asyncio
async def test_detect_disease_garbage_jpeg_header_raises():
    # truncated/invalid JPEG bytes → should be rejected by your image loader
    bad_jpeg = b"\xff\xd8\xff" + b"\x00" * 32
    with pytest.raises(HTTPException):
        await detect_disease_from_image(bad_jpeg, {"plant": "tomato"})
