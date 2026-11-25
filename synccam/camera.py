"""USB camera capture and video writing (OpenCV). No Qt here.

A CameraThread grabs frames as fast as the camera delivers them and stamps each
one with time.perf_counter() the moment read() returns. While recording, frames
go through a queue to a VideoWriterThread so a slow disk never stalls capture.
"""
from __future__ import annotations

import math
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

    `led_fn(t)` returns the IR LED state at computer time t; the spot at LED_RECT is bright when
    on. With `ir_scene` the whole picture is lit by the IR LED (it dims to half when the LED is
    off), and what is shown is the LED state `delay_s` earlier, like a real camera whose frames
    reach the computer some time after their exposure (used to test the IR sync blinks).
    """
    SIM_DELAY_S = 0.050
    LED_RECT = (40, 40, 40, 40)      # x, y, w, h
    RULER_PX_PER_MM = 10.0           # the test ruler: one mark per mm
    RULER_ORIGIN = (40, 420)         # x, y of its 0 mm mark
    BLINK_EVERY = 150                # frames: a synthetic blink lasting BLINK_LEN frames
    BLINK_LEN = 4

    def eye_box(self):
        """Box around the synthetic eye, for pupil tracking on the test pattern."""
        cx, cy = int(self.w * 0.62), int(self.h * 0.45)
        return (cx - 75, cy - 45, 150, 90)

    def eye_truth(self, n: int):
        """(x, y, diameter, open) of the synthetic pupil in frame n."""
        cx, cy = self.w * 0.62, self.h * 0.45
        t = n / 30.0
        x = cx + 12 * math.sin(2 * math.pi * 0.31 * t)
        y = cy + 5 * math.sin(2 * math.pi * 0.47 * t + 1.0)
        d = 30 + 10 * math.sin(2 * math.pi * 0.11 * t)
        blink = (n % self.BLINK_EVERY) < self.BLINK_LEN and n >= self.BLINK_EVERY
        return x, y, d, not blink

    def _draw_ruler(self, img: np.ndarray) -> None:
        cv2 = _cv2()
        x0, y0 = self.RULER_ORIGIN
        k = self.RULER_PX_PER_MM
        cv2.rectangle(img, (x0 - 8, y0 - 28), (x0 + int(30 * k) + 8, y0 + 6), (225, 225, 225), -1)
        for mm in range(31):
            x = int(round(x0 + mm * k))
            h = 20 if mm % 10 == 0 else (14 if mm % 5 == 0 else 8)
            cv2.line(img, (x, y0), (x, y0 - h), (25, 25, 25), 1)
        for mm in (0, 10, 20, 30):
            cv2.putText(img, str(mm), (int(x0 + mm * k) + 2, y0 - 16), cv2.FONT_HERSHEY_PLAIN, 0.7, (25, 25, 25), 1)

    def _draw_eye(self, img: np.ndarray, n: int) -> None:
        cv2 = _cv2()
        cx, cy = int(self.w * 0.62), int(self.h * 0.45)
        x, y, d, is_open = self.eye_truth(n)
        cv2.ellipse(img, (cx, cy), (68, 38), 0, 0, 360, (135, 135, 135), -1)     # skin around
        if not is_open:
            cv2.ellipse(img, (cx, cy), (60, 6), 0, 0, 360, (70, 70, 70), -1)     # closed lid
            return
        eye = np.zeros(img.shape[:2], np.uint8)
        cv2.ellipse(eye, (cx, cy), (60, 32), 0, 0, 360, 255, -1)                   # eye opening
        iris = np.full_like(img, 95)
        cv2.circle(iris, (int(round(x)), int(round(y))), 34, (80, 80, 80), -1)   # iris, darker ring
        sub = 8                                                                   # pupil drawn at 1/8 px
        cv2.circle(iris, (int(round(x * sub)), int(round(y * sub))), int(round(d / 2 * sub)), (22, 22, 22), -1,
                   lineType=cv2.LINE_AA, shift=3)
        cv2.circle(iris, (int(x - d * 0.2), int(y - d * 0.2)), 2, (230, 230, 230), -1)   # small reflection
        img[eye > 0] = iris[eye > 0]
        cv2.ellipse(img, (cx, cy), (60, 32), 0, 200, 340, (215, 215, 215), 2)    # bright upper lid rim

    def __init__(self, size=(640, 480), fps: float = 30.0, led_fn: Optional[Callable] = None,
                 ir_scene: bool = False, delay_s: float = SIM_DELAY_S):
        self.w, self.h = size
        self.fps = fps
        self.led_fn = led_fn or (lambda t=None: False)
        self.ir_scene = ir_scene
        self.delay_s = delay_s
        self._n = 0
        self._next = time.perf_counter()
        yy, xx = np.mgrid[0:self.h, 0:self.w]
        self._bg = ((xx * 60 // max(1, self.w)) + (yy * 40 // max(1, self.h)) + 30).astype(np.uint8)
        rng = np.random.default_rng(0)
        self._noise = [rng.integers(-6, 7, (self.h, self.w), dtype=np.int16) for _ in range(6)]

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
        lit = bool(self.led_fn(time.perf_counter() - self.delay_s))   # the moment this frame was exposed
        img[y0:y0 + h, x0:x0 + w] = 250 if lit else 20
        self._draw_eye(img, self._n)
        self._draw_ruler(img)
        noise = self._noise[self._n % len(self._noise)]          # pre-made: cheap on slow machines
        img = np.clip(img.astype(np.int16) + noise[:, :, None], 0, 255).astype(np.uint8)
        if self.ir_scene and not lit:
            img = (img >> 1)                                      # IR light off: the scene dims
        try:
            cv2 = _cv2()
            cv2.putText(img, f"TEST {self._n:06d}", (14, self.h - 14),
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
class ImageSettings:
    """Picture adjustments, applied in software (works the same with every camera)."""
    grey: bool = True
    brightness: int = 0        # -150 .. 150, added to every pixel
    contrast: float = 1.0      # 0.2 .. 4, around mid-grey
    gamma: float = 1.0         # 0.3 .. 3, >1 brightens the shadows

    def is_identity(self) -> bool:
        return self.brightness == 0 and abs(self.contrast - 1) < 1e-6 and abs(self.gamma - 1) < 1e-6

    def lut(self) -> np.ndarray:
        x = np.arange(256, dtype=np.float64) / 255.0
        y = np.power(x, 1.0 / max(self.gamma, 1e-3)) * 255.0
        y = (y - 128.0) * self.contrast + 128.0 + self.brightness
        return np.clip(np.round(y), 0, 255).astype(np.uint8)

    def as_dict(self) -> dict:
        return {"greyscale": self.grey, "brightness": self.brightness,
                "contrast": round(self.contrast, 3), "gamma": round(self.gamma, 3)}


def process_image(img: np.ndarray, settings: ImageSettings, lut: Optional[np.ndarray] = None) -> np.ndarray:
    """BGR camera image -> adjusted image (2-D uint8 if greyscale, else BGR)."""
    cv2 = _cv2()
    if settings.grey and img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if not settings.is_identity():
        img = cv2.LUT(img, lut if lut is not None else settings.lut())
    return img


def auto_levels(img: np.ndarray) -> Tuple[int, float]:
    """(brightness, contrast) that stretch the 1st..99th percentile of the picture to the full range."""
    g = img if img.ndim == 2 else img.mean(axis=2)
    lo, hi = np.percentile(g[::2, ::2], [1, 99])
    if hi - lo < 4:
        return 0, 1.0
    contrast = float(np.clip(255.0 / (hi - lo), 0.2, 4.0))
    brightness = int(np.clip(round(-128 - (lo - 128) * contrast), -150, 150))
    return brightness, round(contrast, 2)


@dataclass
class Frame:
    image: np.ndarray       # adjusted picture (2-D if greyscale): what the preview shows
    t: float                # perf_counter when read() returned
    n: int                  # frame number since the camera was opened
    roi_mean: float         # mean brightness in the LED ROI (nan if none)
    raw: Optional[np.ndarray] = None   # untouched camera image (BGR)
    pupil: Optional[object] = None     # PupilResult when pupil tracking is on
    kin: Optional[dict] = None         # Kinematics output (moving / size states, speeds)
    brightness: float = float("nan")   # mean grey level of the picture (for the IR sync blinks)


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
        self._settings = ImageSettings()
        self._lut = self._settings.lut()
        self.pupil_tracker = None          # synccam.pupil.PupilTracker, set by the GUI
        self.pupil_want_mask = False       # keep the dark-pixel mask for display (while tuning)
        self.pupil_kin = None              # synccam.pupil.Kinematics, set together with the tracker
        self.pupil_log: deque = deque(maxlen=30 * 120)   # (t, found, x, y, diameter): last ~2 min
        self._times: deque = deque(maxlen=120)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.name = "Test pattern" if index == TEST_PATTERN else f"Camera {index}"

    def open(self) -> None:
        if self.index == TEST_PATTERN:
            self.cap = TestPatternSource(self.size_req or (640, 480), self.fps_req or 30.0, self.led_fn,
                                         ir_scene=self.led_fn is not None)
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

    @property
    def image_settings(self) -> ImageSettings:
        return self._settings

    def set_image_settings(self, settings: ImageSettings) -> None:
        lut = settings.lut()
        self._lut, self._settings = lut, settings      # swapped as a pair by reference

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
                settings, lut = self._settings, self._lut
                pr = None
                tracker = self.pupil_tracker
                if tracker is not None and tracker.params.box is not None:
                    try:
                        pr = tracker.detect(img, keep_mask=self.pupil_want_mask)
                    except Exception:            # never let tracking stop the camera
                        pr = None
                    if pr is not None:
                        self.pupil_log.append((t, pr.found, pr.x, pr.y, pr.diameter))
                kin = None
                kinem = self.pupil_kin
                if pr is not None and kinem is not None:
                    try:
                        kin = kinem.update(t, pr)
                    except Exception:
                        kin = None
                small = img[::4, ::4]                       # brightness on a 1/16 subsample: ~0.05 ms
                frame = Frame(process_image(img, settings, lut), t, n, self._roi_mean(img), img, pr, kin,
                              float(small.mean()))
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

    def __init__(self, path: str, fps: float, size: Tuple[int, int], codec: str = "MJPG", maxq: int = 240,
                 grey: bool = False):
        cv2 = _cv2()
        fourcc, _ext = CODECS.get(codec, CODECS["MJPG"])
        self.path = path
        self.grey = grey
        self._expand = False          # grey frames written as 3-channel (writer without grey support)
        cc = cv2.VideoWriter_fourcc(*fourcc)
        self.writer = cv2.VideoWriter(path, cc, float(fps), size, isColor=not grey)
        if grey and not self.writer.isOpened():
            self.writer = cv2.VideoWriter(path, cc, float(fps), size, isColor=True)
            self._expand = True
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
                if self.grey and img.ndim == 3:
                    img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                if img.ndim == 2 and (self._expand or not self.grey):
                    img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
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
