import asyncio
from pathlib import Path
from datetime import datetime, timezone
import cv2
from sqlalchemy.ext.asyncio import AsyncSession
from ultralytics import YOLO

from app.core.config    import DATA_ROOT, RAW_DIR, PROCESSED_DIR, YOLO_MODEL_PATH, CAM_DETECTION_WORKERS
from app.core.database  import AsyncSessionLocal
from app.models         import DetectionRecord

class CameraQueue:
    def __init__(self):
        self.queue    = asyncio.Queue()
        self.model    = YOLO(YOLO_MODEL_PATH)
        self.workers  = CAM_DETECTION_WORKERS

    async def enqueue(self, camera_id: str, frame_path: Path):
        """Push a newly saved raw frame into the detection queue."""
        await self.queue.put((camera_id, frame_path))

    async def _worker(self):
        while True:
            camera_id, frame_path = await self.queue.get()
            try:
                frame = cv2.imread(str(frame_path))
                if frame is None:
                    continue

                # Run YOLO inference
                results = self.model(frame)[0]
                boxes   = results.boxes
                if boxes and len(boxes) > 0:
                    # Annotate & save to processed dir
                    proc_dir = Path(DATA_ROOT)/camera_id/PROCESSED_DIR
                    proc_dir.mkdir(parents=True, exist_ok=True)
                    annotated = results.plot()  # returns an np.ndarray
                    out_path  = proc_dir/frame_path.name
                    cv2.imwrite(str(out_path), annotated)

                    # Record each detection
                    async with AsyncSessionLocal() as session:
                        for box in boxes:
                            cls       = int(box.cls.cpu().numpy())
                            name      = self.model.names[cls]
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
