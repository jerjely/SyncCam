"""Makes the README / website screenshots with the test-pattern camera and the simulated Arduino.

    python tools/make_screenshots.py [eye_video.avi]     (runs offscreen at 1.5x for sharp images)

With an eye video (640 x 480, 30 fps), the live, recording, pupil and saved screenshots play that
video through the app instead of the synthetic test-pattern eye. The calibration screenshot always
uses the test pattern's ruler. Signals come from the simulated Arduino.

Writes docs/screenshots/*.png.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "1.5")

from PySide6.QtCore import QPointF, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from synccam.arduino import SIM_PORT  # noqa: E402
import numpy as np  # noqa: E402

import synccam.camera as camera  # noqa: E402
from synccam.camera import TEST_PATTERN, TestPatternSource  # noqa: E402
from synccam.gui import CalibrationDialog, MainWindow, apply_style  # noqa: E402

OUT = ROOT / "docs" / "screenshots"
FOLDER = "D:/Data/SyncCam"          # shown in the Recording box instead of a temp folder


VIDEO_START = 43200                 # a clean stretch with a clear pupil dilation
VIDEO_EYE_BOX = (350, 170, 140, 95)


class VideoFileSource(TestPatternSource):
    """Plays an eye video in real time as if it were the camera. The picture dims while the IR LED is
    off (shown SIM_DELAY_S late), like the IR-lit rig, so the sync blinks and camera delay work."""
    path = None

    def __init__(self, size=(640, 480), fps=30.0, led_fn=None, ir_scene=False, delay_s=TestPatternSource.SIM_DELAY_S):
        super().__init__(size, fps, led_fn, ir_scene, delay_s)
        import cv2
        self._cap = cv2.VideoCapture(str(self.path))
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, VIDEO_START)

    def eye_box(self):
        return VIDEO_EYE_BOX

    def read(self):
        now = time.perf_counter()
        if self._next > now:
            time.sleep(self._next - now)
        self._next = max(self._next + 1.0 / self.fps, time.perf_counter() - 0.5)
        ok, img = self._cap.read()
        if not ok:
            import cv2
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, VIDEO_START)
            ok, img = self._cap.read()
        lit = bool(self.led_fn(time.perf_counter() - self.delay_s))
        if self.ir_scene and not lit:
            img = (img >> 1).astype(np.uint8)
        self._n += 1
        return True, img


def wait(app, seconds, cond=None):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if cond is not None and cond():
            return
        time.sleep(0.01)


def rename(win):
    """Show a camera and a port the way they look on the rig instead of 'Test pattern' / 'Simulator'."""
    if VideoFileSource.path:
        win.cam_combo.setItemText(win.cam_combo.currentIndex(), "Camera 0")
        win.port_combo.setItemText(win.port_combo.findData(SIM_PORT), "COM3 - Arduino Uno")
        if win.camera is not None:
            win.camera.name = "Camera 0"
        dev = getattr(getattr(win, "stream", None), "device", None)
        if dev is not None:
            dev.name = "COM3"


def save(widget, name):
    widget.grab().save(str(OUT / name))
    print("  ", name)


def main():
    if len(sys.argv) > 1:
        VideoFileSource.path = Path(sys.argv[1])
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.png"):
        old.unlink()
    app = QApplication.instance() or QApplication(sys.argv)
    apply_style(app)
    tmp = Path(tempfile.mkdtemp())
    win = MainWindow(QSettings(str(tmp / "s.ini"), QSettings.Format.IniFormat))
    win.probe_atlas_folder = str(tmp / "atlas")
    win.resize(1400, 940)
    win.show()
    win.dir_edit.setText(FOLDER)
    win.name_edit.setText("mouse42_day1")
    wait(app, 0.3)
    save(win, "1_start.png")

    # camera + Arduino + IR LED
    win.cam_combo.setCurrentIndex(win.cam_combo.findData(TEST_PATTERN))
    win.res_combo.setCurrentIndex(1)
    win.cam_btn.click()
    rename(win)
    win.port_combo.setCurrentIndex(win.port_combo.findData(SIM_PORT))
    win.ard_btn.click()
    win.ir_btn.click()
    rename(win)
    wait(app, 1.5)

    # calibration with the test ruler
    dlg = CalibrationDialog(win.camera, None, win)
    dlg.resize(1000, 760)
    dlg.show()
    dlg._toggle_record()
    wait(app, 10, lambda: len(dlg.clip) >= 45)
    dlg._toggle_record()
    dlg.scrub.setValue(len(dlg.clip) // 2)
    x0, y0 = TestPatternSource.RULER_ORIGIN
    k = TestPatternSource.RULER_PX_PER_MM
    dlg.canvas.points = [QPointF(x0, y0 - 5), QPointF(x0 + 25 * k, y0 - 5)]
    dlg.mm_spin.setValue(25.0)
    dlg._update()
    wait(app, 0.3)
    save(dlg, "6_calibration.png")
    win.apply_calibration(dlg)
    dlg.done(0)

    if VideoFileSource.path:                         # from here on: the real eye video, sizes in pixels
        win.cam_btn.click()                          # close the test pattern
        wait(app, 0.5)
        win.scale = None
        win._update_scale_label()
        camera.TestPatternSource = VideoFileSource
        win.cam_btn.click()
        wait(app, 0.5)
        camera.TestPatternSource = TestPatternSource
        rename(win)

    # pupil tracking
    win.pupil_chk.setChecked(True)
    win.view.eye_box = win.camera.cap.eye_box()
    win._eye_box_drawn(win.view.eye_box)
    wait(app, 11.5)                                  # fill the 10 s pupil traces (start-up scrolls off)
    save(win, "2_live.png")
    save(win.pupil_box, "5_pupil.png")

    # recording (into a temp folder; the box shows FOLDER)
    win.dir_edit.setText(str(tmp / "recordings"))
    win.rec_btn.click()
    win.dir_edit.setText(FOLDER)
    wait(app, 4.0)
    save(win, "3_recording.png")
    win.rec_btn.click()
    wait(app, 0.5)
    win.dir_edit.setText(FOLDER)
    save(win, "4_saved.png")

    # zoomed signals: TTL pulse with the microphone burst, sample by sample
    win.window_combo.setCurrentIndex(win.window_combo.findData(1.0))
    wait(app, 3.0)
    win.freeze_btn.setChecked(True)
    wait(app, 0.3)
    save(win.scope.parentWidget(), "7_signals_zoom.png")
    win.freeze_btn.setChecked(False)

    # probe planner, two probes in the SC
    pw = win.show_probe_planner()
    pw.set_n(2)
    for i in (0, 1):                                 # aim both probes at their targets in the SC
        pw.set_active(i)
        pw.plan_to_target()
    pw.set_active(0)
    pw.resize(1500, 950)
    wait(app, 0.5)
    save(pw, "8_probe_planner.png")
    pw.close()
    win.close()
    wait(app, 0.3)
    print("wrote", sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
