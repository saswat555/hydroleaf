# app/utils/image_utils.py
import cv2
import numpy as np

def is_day(frame: np.ndarray, thresh: float = 50.0) -> bool:
    """
    Convert to grayscale and use mean intensity to decide day vs night.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(gray.mean()) >= thresh

def clean_frame(frame: np.ndarray, day: bool) -> np.ndarray:
    """
    Apply a simple cleanup depending on day/night:
      - day: histogram‐equalize the V channel (improve contrast)
      - night: denoise with fastNlMeans
    """
    if day:
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        hsv[:, :, 2] = cv2.equalizeHist(hsv[:, :, 2])
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    else:
        # parameters (10,10,7,21) tuned for mild denoising
        return cv2.fastNlMeansDenoisingColored(frame, None, 10, 10, 7, 21)
