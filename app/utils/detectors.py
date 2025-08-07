from __future__ import annotations
import os
from typing import Any, List, Tuple
try:
    import numpy as np  # type: ignore
except Exception:  # very thin stub for type hints
    class _NPStub:
        ndarray = object
    np = _NPStub()  # type: ignore
# Env knobs:
#   DETECTOR_BACKEND=YOLO|STUB (default: YOLO if ultralytics present & model exists)
#   YOLO_MODEL_PATH follows app.core.config (can be overridden here)
BACKEND = os.getenv("DETECTOR_BACKEND", "").upper()
YOLO_MODEL_PATH = os.getenv("YOLO_MODEL_PATH", "yolov5s.pt")

class BaseDetector:
    def detect_and_annotate(self, img: np.ndarray) -> Tuple[np.ndarray, List[dict]]:
        raise NotImplementedError

class StubDetector(BaseDetector):
    # Simple brightness-based fake "leaf" hotspot – good enough for CI
    def detect_and_annotate(self, img: np.ndarray):
        return img, []  # return no detections in tests

class YoloDetector(BaseDetector):
    def __init__(self, model_path: str):
        from ultralytics import YOLO  # lazy import
        self.model = YOLO(model_path)
        self.names = self.model.names

    def detect_and_annotate(self, img: np.ndarray):
        res = self.model(img, imgsz=640, conf=0.35, verbose=False)[0]
        annotated = res.plot()
        out = []
        for box in res.boxes:
            cls = int(box.cls.cpu().numpy())
            conf = float(box.conf.cpu().numpy())
            x1, y1, x2, y2 = map(int, box.xyxy.cpu().numpy()[0])
            out.append({"name": self.names[cls], "conf": conf, "bbox": (x1, y1, x2, y2)})
        return annotated, out

_detector: BaseDetector | None = None

def get_detector() -> BaseDetector:
    global _detector
    if _detector is not None:
        return _detector

    # Force stub explicitly
    if BACKEND == "STUB":
        _detector = StubDetector()
        return _detector

    # Try YOLO, fallback to stub on any issue (no model, no ultralytics, etc.)
    try:
        if not os.path.exists(YOLO_MODEL_PATH):
            raise FileNotFoundError(YOLO_MODEL_PATH)
        _detector = YoloDetector(YOLO_MODEL_PATH)
        return _detector
    except Exception:
        _detector = StubDetector()
        return _detector
