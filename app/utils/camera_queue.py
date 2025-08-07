import asyncio
from pathlib import Path
from datetime import datetime, timezone
from app.utils.detectors import get_detector
try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None
from app.core.config    import DATA_ROOT, RAW_DIR, PROCESSED_DIR, YOLO_MODEL_PATH, CAM_DETECTION_WORKERS
from app.core.database  import AsyncSessionLocal
from app.models         import DetectionRecord

class CameraQueue:
    def __init__(self):
        self.queue    = asyncio.Queue()
        self.detector = get_detector()
        self.workers  = CAM_DETECTION_WORKERS

    async def enqueue(self, camera_id: str, frame_path: Path):
        """Push a newly saved raw frame into the detection queue."""
        await self.queue.put((camera_id, frame_path))

    async def _worker(self):
        while True:
            camera_id, frame_path = await self.queue.get()
            try:
                if cv2 is None:
                    # CV stack not available in CI – skip gracefully
                    continue
                frame = cv2.imread(str(frame_path))                
                if frame is None:
                    continue

                # Run YOLO inference
                annotated, dets = self.detector.detect_and_annotate(frame)
                if dets:
                    # save annotated
                    proc_dir = Path(DATA_ROOT)/camera_id/PROCESSED_DIR
                    proc_dir.mkdir(parents=True, exist_ok=True)
                    out_path  = proc_dir/frame_path.name
                    cv2.imwrite(str(out_path), annotated)

                    # Record each detection
                    async with AsyncSessionLocal() as session:
                        for det in dets:
                            name      = det["name"]
                            record    = DetectionRecord(
                                camera_id=camera_id,
                                object_name=name,
                                timestamp=datetime.now(timezone.utc)
                            )
                            session.add(record)
                        await session.commit()

            except Exception as e:
                # you’d normally use proper logging
                print(f"[camera_queue] error: {e}")
            finally:
                self.queue.task_done()

    def start_workers(self):
        """Spawn N background tasks on the running loop."""
        loop = asyncio.get_event_loop()
        for _ in range(self.workers):
            loop.create_task(self._worker())

# Singleton queue
camera_queue = CameraQueue()
