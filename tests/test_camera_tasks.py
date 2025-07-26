# tests/test_camera_tasks.py
"""
Quick sanity-check that the camera_tasks pipeline

• moves raw → processed
• starts (or appends to) a clip writer
"""

import asyncio
from pathlib import Path

import cv2
import numpy as np
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Point CAM_DATA_ROOT at pytest’s tmp dir for every test in this module
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _temp_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CAM_DATA_ROOT", str(tmp_path))
    # ensure any module-level constants are regenerated
    from importlib import reload
    reload(__import__("app.core.config"))
    yield


@pytest.mark.asyncio
async def test_encode_and_cleanup_creates_processed_and_clip(tmp_path, monkeypatch):
    cam_id = "cam_test"

    # Import after env var is set so camera_tasks sees the new CAM_DATA_ROOT.
    from app.utils import camera_tasks

    # Ask the library for the canonical folder layout ↓↓↓
    raw_dir, processed_dir, clips_dir = camera_tasks._ensure_dirs(cam_id)

    # ── make one dummy 320 × 240 JPEG in the *right* raw folder ──────────────
    img = np.zeros((240, 320, 3), dtype=np.uint8)
    cv2.putText(img, "dummy", (50, 120),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)

    raw_path = raw_dir / "1.jpg"
    raw_dir.mkdir(parents=True, exist_ok=True)          # in case helper skipped it
    cv2.imwrite(str(raw_path), img)

    # Skip YOLO – make _annotate() a cheap no-op that returns zero detections
    monkeypatch.setattr(camera_tasks, "_annotate", lambda x: (x, []))

    # ── run the pipeline ────────────────────────────────────────────────────
    await camera_tasks.encode_and_cleanup(cam_id)

    # ── assertions ─────────────────────────────────────────────────────────
    # processed JPEG present
    proc_files = list(processed_dir.glob("*_processed.jpg"))
    assert len(proc_files) == 1, "processed frame was not written"

    # at least one .mp4 in the clip directory
    clip_files = list(clips_dir.glob("*.mp4"))
    assert clip_files, "video clip was not created"

    # raw frame should be gone
    assert not raw_path.exists(), "raw frame should have been deleted"
