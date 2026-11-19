"""Built-in self-test: `SyncCam --selftest`.

Runs the real code paths with a synthetic test-pattern camera and the simulated
Arduino (no hardware), then drives the whole GUI offscreen. The release workflow
runs this against every freshly built and installed app, so a broken installer
is never published. Exit code 0 means everything works.
"""
from __future__ import annotations

import csv
import faulthandler
import json
import os
import sys
import tempfile
import time
import traceback
import wave
from pathlib import Path

import numpy as np


class _Ctx:
    def __init__(self, tmp: Path, expect_version, log):
        self.tmp = tmp
        self.expect_version = expect_version
        self.log = log


def _pump(app, condition, timeout: float, what: str) -> None:
    end = time.time() + timeout
    while not condition():
        if time.time() > end:
            raise AssertionError(f"timed out waiting for: {what}")
        app.processEvents()
        time.sleep(0.005)


def _read_csv(path: Path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# --------------------------------------------------------------------------
def t_version(ctx):
    from . import __version__
    assert __version__, "empty version"
    if ctx.expect_version:
        want = ctx.expect_version.lstrip("v")
        assert __version__ == want, f"built-in version {__version__!r} != expected {want!r}"
    ctx.log(f"    version {__version__}")


def t_imports(ctx):
    import cv2
    import PySide6  # noqa: F401
    import serial  # noqa: F401
    import serial.urlhandler.protocol_loop  # noqa: F401   (needs bundling by PyInstaller)
    from serial.tools import list_ports
    ctx.log(f"    OpenCV {cv2.__version__}, {len(list(list_ports.comports()))} serial port(s) visible")


def t_video_codecs(ctx):
    """Both video formats can be written and read back in this build."""
    import cv2
    from .camera import CODECS
    for codec, (fourcc, ext) in CODECS.items():
        path = str(ctx.tmp / f"codec_test{ext}")
        w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*fourcc), 30.0, (160, 120))
        assert w.isOpened(), f"cannot create a {codec} video"
        for i in range(15):
            img = np.full((120, 160, 3), i * 15, dtype=np.uint8)
            w.write(img)
        w.release()
        cap = cv2.VideoCapture(path)
        n = 0
        while True:
            ok, _ = cap.read()
            if not ok:
                break
            n += 1
        cap.release()
        assert n == 15, f"{codec}: wrote 15 frames, read back {n}"


def t_packets(ctx):
    from .arduino import PacketParser, SeqUnwrapper, encode_packet
    pk = b"".join(encode_packet(i, (7 * i) % 1024, (13 * i) % 1024, i % 2, i % 3 == 0) for i in range(700))
    p = PacketParser()
    got = []
    data = b"\x00\xa5noise" + pk[:252] + b"\xa5\x01" + pk[252 + 6:]   # packet 42 replaced by junk
    for k in range(0, len(data), 37):
        got.append(p.feed(data[k:k + 37]))
    s = np.concatenate(got)
    assert len(s) == 699, len(s)
    u = SeqUnwrapper()
    idx = u.unwrap(s[:, 0])
    assert idx[-1] == 699 and u.dropped == 1, (idx[-1], u.dropped)
    ok = idx[s[:, 1] == (7 * idx) % 1024]
    assert len(ok) == 699, "decoded values are wrong"
    assert np.array_equal(s[:, 3], idx % 2) and np.array_equal(s[:, 4], (idx % 3 == 0).astype(int))


def t_edges_and_clock(ctx):
    from .arduino import ClockFit, detect_edges
    x = np.r_[np.zeros(10), np.full(5, 900), [600, 300], np.zeros(5), np.full(3, 1000)]
    r, f, st = detect_edges(x, 552, 472)
    assert list(r) == [10, 22] and list(f) == [16] and st, (r, f, st)
    c = ClockFit(5000)
    rng = np.random.default_rng(0)
    for i in range(0, 50000, 100):
        c.add(i, 3.0 + i * 0.0002 * 1.0001 + abs(rng.normal(0.004, 0.002)))
    off, per = c.solve()
    assert abs(per / (0.0002 * 1.0001) - 1) < 2e-5, per
    assert abs(off - 3.0) < 0.003, off


def t_simulated_stream(ctx):
    from .arduino import IR_FOLLOW, IR_PULSE, ArduinoStream, SimulatedArduino
    st = ArduinoStream(SimulatedArduino(), 5000)
    info = st.connect(IR_FOLLOW)
    assert info.firmware.startswith("SYNCCAM"), info.firmware
    time.sleep(1.1)
    d = st.recent(1.0)
    assert st.error is None, st.error
    assert len(d) == 5000, len(d)
    assert d[:, 0].max() > 900 and d[:, 0].min() < 50, "TTL channel missing"
    assert 0.1 < d[:, 2].mean() < 0.35, f"LED should follow the TTL ({d[:, 2].mean():.2f})"
    st.set_ir(IR_PULSE, (200, 50))
    time.sleep(0.7)
    d = st.recent(0.4)
    assert 0.15 < d[:, 2].mean() < 0.35, f"LED pulses wrong ({d[:, 2].mean():.2f})"
    st.close()
    ctx.log(f"    {st.total} samples, {st.unwrap.dropped} lost")


def t_serial_commands(ctx):
    """Commands go out on a real pySerial port (loopback) in the firmware's format."""
    from .arduino import SerialArduino
    dev = SerialArduino("loop://")
    dev.open()
    dev.write(b"P1000,50\nI3\n")
    time.sleep(0.05)
    got = dev.read() + dev.read()
    assert b"P1000,50\nI3\n" in got, got
    dev.close()


def t_recording(ctx):
    """Full recording: test-pattern camera + simulated Arduino, LED followed in the video."""
    from .arduino import IR_PULSE, ArduinoStream, SimulatedArduino
    from .camera import TEST_PATTERN, CameraThread, TestPatternSource
    from .session import Recording
    sim = SimulatedArduino()
    st = ArduinoStream(sim, 5000)
    st.connect(IR_PULSE, (400, 120))
    cam = CameraThread(TEST_PATTERN, (320, 240), 30, sim.current_led)
    cam.open()
    cam.roi = TestPatternSource.LED_RECT
    time.sleep(0.5)
    rec = Recording(str(ctx.tmp), "selftest", cam, st, "MJPG", 512, {"name": "selftest"})
    rec.start()
    time.sleep(3.0)
    info = rec.stop()
    cam.close()
    st.close()
    folder = rec.folder
    for name in ("video.avi", "frames.csv", "analog.wav", "microphone.wav", "ttl_events.csv",
                 "led_events.csv", "session.json"):
        assert (folder / name).exists(), f"{name} missing"
    js = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    frames = _read_csv(folder / "frames.csv")
    assert 70 <= len(frames) <= 100, f"{len(frames)} frames in 3 s at 30 fps"
    import cv2
    cap = cv2.VideoCapture(str(folder / "video.avi"))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    assert abs(n - len(frames)) <= 1, f"video has {n} frames, frames.csv {len(frames)}"
    with wave.open(str(folder / "analog.wav")) as w:
        assert w.getnchannels() == 3 and w.getframerate() == 5000
        ns = w.getnframes()
        raw = np.frombuffer(w.readframes(ns), dtype="<i2").reshape(-1, 3)
    assert abs(ns - 15000) < 900, f"{ns} samples in 3 s at 5 kHz"
    assert raw[:, 0].max() > 900 and set(np.unique(raw[:, 2])) <= {0, 1}
    assert js["arduino"]["samples_lost_and_filled"] == 0, f"{js['arduino']['samples_lost_and_filled']} samples lost"
    ttl = [r for r in _read_csv(folder / "ttl_events.csv") if r["edge"] == "rise"]
    assert 4 <= len(ttl) <= 7, f"{len(ttl)} TTL pulses in 3 s"
    # TTL edge times match the simulator's true TTL rises to within a few ms
    true = []
    for cyc in range(0, 40):
        for k in range(4):
            true.append(sim._t0 + cyc * 2.25 + k * 0.5 - rec.t0)
    true = np.array(true)
    errs = [np.min(np.abs(true - float(r["time_s"]))) for r in ttl]
    assert max(errs) < 0.006, f"TTL edge times off by up to {max(errs) * 1000:.1f} ms"
    lag = js.get("led_video_lag", {})
    assert lag.get("matched_onsets", 0) >= 4, f"LED not found in the video: {lag}"
    assert -5 <= lag["median_ms"] <= 120, f"LED-to-video lag {lag['median_ms']} ms"
    ctx.log(f"    {len(frames)} frames, {ns} samples, {len(ttl)} TTL pulses, "
            f"LED video lag {lag['median_ms']} ms")


def t_gui(ctx):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from .arduino import IR_FOLLOW, SIM_PORT
    from .camera import TEST_PATTERN, TestPatternSource
    from .gui import MainWindow, app_icon, apply_style

    app = QApplication.instance() or QApplication(["synccam-selftest"])
    apply_style(app)
    ctx.log(f"    Qt platform: {app.platformName()}")
    assert not app_icon().isNull(), "application icon missing from the bundle"
    settings = QSettings(str(ctx.tmp / "gui.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings)
    win.show()
    app.processEvents()
    assert "SyncCam" in win.windowTitle()
    win.dir_edit.setText(str(ctx.tmp / "gui_recordings"))
    win.name_edit.setText("gui test")

    win.cam_combo.setCurrentIndex(win.cam_combo.findData(TEST_PATTERN))
    win.res_combo.setCurrentIndex(1)
    win.cam_btn.click()
    assert win.camera is not None, "test-pattern camera did not open"
    win.view.set_roi(TestPatternSource.LED_RECT)
    win.port_combo.setCurrentIndex(win.port_combo.findData(SIM_PORT))
    win.ir_combo.setCurrentIndex(win.ir_combo.findData(IR_FOLLOW))
    win.ard_btn.click()
    assert win.stream is not None, "simulated Arduino did not connect"
    _pump(app, lambda: win.camera.latest is not None and win.stream.total > 2000, 5, "camera and Arduino data")
    assert win.rec_btn.isEnabled()
    win.rec_btn.click()
    assert win.recording is not None
    t_end = time.time() + 2.5
    _pump(app, lambda: time.time() > t_end, 10, "recording")
    assert win.rec_status.text().startswith("Recording")
    img = win.grab().toImage()
    assert img.width() > 800 and img.height() > 500
    colours = {img.pixel(x, y) for x in range(0, img.width(), 37) for y in range(0, img.height(), 29)}
    assert len(colours) > 8, "window rendered blank"
    win.rec_btn.click()
    assert win.recording is None and win.last_folder is not None
    assert win.rec_status.text().startswith("Saved"), win.rec_status.text()
    js = json.loads((win.last_folder / "session.json").read_text(encoding="utf-8"))
    assert js["video"]["frames"] > 40 and js["arduino"]["samples"] > 10000, js
    assert js["ir_led"]["mode"] == "Follow TTL"
    win.close()
    app.processEvents()


TESTS = [
    ("version", t_version),
    ("imports", t_imports),
    ("video codecs", t_video_codecs),
    ("serial packets", t_packets),
    ("TTL edges and clock fit", t_edges_and_clock),
    ("simulated Arduino stream", t_simulated_stream),
    ("serial commands (loopback)", t_serial_commands),
    ("recording to disk", t_recording),
    ("GUI end to end", t_gui),
]


def run_selftest(log_path=None, expect_version=None) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    path = Path(log_path) if log_path else Path(tempfile.gettempdir()) / "synccam-selftest.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w", buffering=1, encoding="utf-8")
    faulthandler.enable(file=fh)        # a hard crash leaves a traceback in the log

    def log(msg: str) -> None:
        fh.write(msg + "\n")
        if sys.stdout is not None:
            try:
                print(msg, flush=True)
            except Exception:
                pass

    from . import __version__
    log(f"SyncCam {__version__} self-test  (python {sys.version.split()[0]}, {sys.platform})")
    failures = 0
    with tempfile.TemporaryDirectory(prefix="synccam-selftest-") as tmp:
        ctx = _Ctx(Path(tmp), expect_version, log)
        for name, fn in TESTS:
            t0 = time.perf_counter()
            try:
                fn(ctx)
                log(f"PASS  {name}  ({(time.perf_counter() - t0) * 1000:.0f} ms)")
            except Exception:
                failures += 1
                log(f"FAIL  {name}")
                log(traceback.format_exc())
    log("SELFTEST OK" if failures == 0 else f"SELFTEST FAILED ({failures} failed)")
    faulthandler.disable()
    fh.close()
    return 0 if failures == 0 else 1
