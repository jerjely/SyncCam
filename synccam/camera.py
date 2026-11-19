"""USB camera capture and video writing (OpenCV). No Qt here.

A CameraThread grabs frames as fast as the camera delivers them and stamps each
one with time.perf_counter() the moment read() returns. While recording, frames
go through a queue to a VideoWriterThread so a slow disk never stalls capture.
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import numpy as np

TEST_PATTERN = -1
RESOLUTIONS = [("Camera default", None), ("640 x 480", (640, 480)), ("800 x 600", (800, 600)),
               ("1280 x 720", (1280, 720)), ("1920 x 1080", (1920, 1080))]
CODECS = {"MJPG": ("MJPG", ".avi"), "MP4V": ("mp4v", ".mp4")}


def _cv2():
    import cv2
    return cv2


def backend() -> int:
    cv2 = _cv2()
    if sys.platform == "win32":
        return cv2.CAP_DSHOW
    if sys.platform == "darwin":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_V4L2 if hasattr(cv2, "CAP_V4L2") else cv2.CAP_ANY


def scan_cameras(max_index: int = 6) -> List[Tuple[int, str]]:
    """[(index, label)] of cameras that open and deliver a frame."""
    cv2 = _cv2()
    found = []
    misses = 0
    for i in range(max_index):
        cap = cv2.VideoCapture(i, backend())
        ok = cap.isOpened()
        label = ""
        if ok:
            ok, frame = cap.read()
            if ok and frame is not None:
                label = f"Camera {i}  ({frame.shape[1]} x {frame.shape[0]})"
        cap.release()
        if ok and label:
            found.append((i, label))
            misses = 0
        else:
            misses += 1
            if misses >= 2 and i >= 1:
                break
    return found


# --------------------------------------------------------------------------
class TestPatternSource:
    """Synthetic camera: moving bar, frame counter, and an 'IR LED' spot.

    `led_fn` returns the current LED state; the spot at LED_RECT is bright when on.
    """
    LED_RECT = (40, 40, 40, 40)      # x, y, w, h

    def __init__(self, size=(640, 480), fps: float = 30.0, led_fn: Optional[Callable[[], bool]] = None):
        self.w, self.h = size
        self.fps = fps
        self.led_fn = led_fn or (lambda: False)
        self._n = 0
        self._next = time.perf_counter()
        yy, xx = np.mgrid[0:self.h, 0:self.w]
        self._bg = ((xx * 60 // max(1, self.w)) + (yy * 40 // max(1, self.h)) + 30).astype(np.uint8)

    def isOpened(self) -> bool:
        return True

    def read(self):
        now = time.perf_counter()
        if self._next > now:
            time.sleep(self._next - now)
        self._next = max(self._next + 1.0 / self.fps, time.perf_counter() - 0.5)
        img = np.repeat(self._bg[:, :, None], 3, axis=2)
        x = int((self._n * 6) % self.w)
        img[:, max(0, x - 6):x + 6] = (90, 160, 90)
        x0, y0, w, h = self.LED_RECT
        img[y0:y0 + h, x0:x0 + w] = 250 if self.led_fn() else 20
        try:
            cv2 = _cv2()
            cv2.putText(img, f"TEST {self._n:06d}", (self.w - 230, self.h - 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        except Exception:
            pass
        self._n += 1
        return True, img

    def get(self, prop):
        return 0

    def release(self) -> None:
        pass


# --------------------------------------------------------------------------
@dataclass
class Frame:
    image: np.ndarray       # BGR
    t: float                # perf_counter when read() returned
    n: int                  # frame number since the camera was opened
    roi_mean: float         # mean brightness in the LED ROI (nan if none)


class CameraThread:
    """Opens a camera and keeps grabbing. `latest` always holds the newest Frame."""

    def __init__(self, index: int, size: Optional[Tuple[int, int]] = None, fps: float = 30.0,
                 led_fn: Optional[Callable[[], bool]] = None):
        self.index = index
        self.size_req = size
        self.fps_req = fps
        self.led_fn = led_fn
        self.cap = None
        self.latest: Optional[Frame] = None
        self.error: Optional[str] = None
        self.frame_size: Tuple[int, int] = (0, 0)
        self.roi: Optional[Tuple[int, int, int, int]] = None     # x, y, w, h in image pixels
        self.on_frame: Optional[Callable[[Frame], None]] = None  # called in the capture thread
        self._times: deque = deque(maxlen=120)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.name = "Test pattern" if index == TEST_PATTERN else f"Camera {index}"

    def open(self) -> None:
        if self.index == TEST_PATTERN:
            self.cap = TestPatternSource(self.size_req or (640, 480), self.fps_req or 30.0, self.led_fn)
        else:
            cv2 = _cv2()
            cap = cv2.VideoCapture(self.index, backend())
            if not cap.isOpened():
                raise RuntimeError(f"Camera {self.index} could not be opened. Is another program using it?"
                                   + (" On a Mac, allow camera access for SyncCam in System Settings > "
                                      "Privacy & Security > Camera." if sys.platform == "darwin" else ""))
            if sys.platform == "win32":
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))  # most USB cams need MJPG for HD at 30 fps
            if self.size_req:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.size_req[0])
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.size_req[1])
            if self.fps_req:
                cap.set(cv2.CAP_PROP_FPS, float(self.fps_req))
            self.cap = cap
        ok, img = self.cap.read()
        if not ok or img is None:
            self.cap.release()
            raise RuntimeError(f"{self.name} opened but delivered no image.")
        self.frame_size = (img.shape[1], img.shape[0])
        self._thread = threading.Thread(target=self._main, name="synccam-camera", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(3.0)
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def measured_fps(self) -> float:
        ts = list(self._times)
        if len(ts) < 5:
            return 0.0
        return (len(ts) - 1) / (ts[-1] - ts[0]) if ts[-1] > ts[0] else 0.0

    def _roi_mean(self, img: np.ndarray) -> float:
        roi = self.roi
        if roi is None:
            return float("nan")
        x, y, w, h = roi
        patch = img[y:y + h, x:x + w]
        return float(patch.mean()) if patch.size else float("nan")

    def _main(self) -> None:
        n = 0
        fails = 0
        try:
            while not self._stop.is_set():
                ok, img = self.cap.read()
                t = time.perf_counter()
                if not ok or img is None:
                    fails += 1
                    if fails > 50:
                        raise RuntimeError(f"{self.name} stopped delivering images (unplugged?)")
                    time.sleep(0.01)
                    continue
                fails = 0
                frame = Frame(img, t, n, self._roi_mean(img))
                n += 1
                self._times.append(t)
                self.latest = frame
                cb = self.on_frame
                if cb is not None:
                    cb(frame)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
class VideoWriterThread:
    """Writes frames from a queue; drops (and counts) frames if the disk falls behind."""

    def __init__(self, path: str, fps: float, size: Tuple[int, int], codec: str = "MJPG", maxq: int = 240):
        cv2 = _cv2()
        fourcc, _ext = CODECS.get(codec, CODECS["MJPG"])
        self.path = path
        self.writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*fourcc), float(fps), size)
        if not self.writer.isOpened():
            raise RuntimeError(f"Could not create the video file {path} with codec {codec}.")
        self.size = size
        self.q: "queue.Queue" = queue.Queue(maxsize=maxq)
        self.written = 0
        self.dropped = 0
        self.error: Optional[str] = None
        self._thread = threading.Thread(target=self._main, name="synccam-writer", daemon=True)
        self._thread.start()

    def put(self, img: np.ndarray) -> bool:
        """Queue a frame. Returns False (and counts a drop) if the queue is full."""
        try:
            self.q.put_nowait(img)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def _main(self) -> None:
        cv2 = _cv2()
        try:
            while True:
                img = self.q.get()
                if img is None:
                    break
                if (img.shape[1], img.shape[0]) != self.size:
                    img = cv2.resize(img, self.size)
                self.writer.write(img)
                self.written += 1
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.writer.release()

    def close(self, timeout: float = 60.0) -> None:
        while True:
            try:
                self.q.put(None, timeout=0.5)
                break
            except queue.Full:
                if not self._thread.is_alive():
                    break
        self._thread.join(timeout)

    @property
    def backlog(self) -> int:
        return self.q.qsize()
