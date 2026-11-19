"""Makes the README screenshots with the test-pattern camera and the simulated Arduino.

    QT_QPA_PLATFORM=offscreen python tools/make_screenshots.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from synccam.arduino import SIM_PORT  # noqa: E402
from synccam.camera import TEST_PATTERN  # noqa: E402
from synccam.gui import MainWindow, apply_style  # noqa: E402

OUT = ROOT / "docs" / "screenshots"


def wait(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv)
    apply_style(app)
    tmp = Path(tempfile.mkdtemp())
    win = MainWindow(QSettings(str(tmp / "s.ini"), QSettings.Format.IniFormat))
    win.resize(1240, 790)
    win.show()
    win.dir_edit.setText(str(tmp / "recordings"))
    win.name_edit.setText("mouse42_day1")
    wait(app, 0.2)
    win.grab().save(str(OUT / "1_start.png"))

    win.cam_combo.setCurrentIndex(win.cam_combo.findData(TEST_PATTERN))
    win.res_combo.setCurrentIndex(1)
    win.cam_btn.click()
    win.port_combo.setCurrentIndex(win.port_combo.findData(SIM_PORT))
    win.ard_btn.click()
    win.ir_btn.click()
    wait(app, 5.5)
    win.grab().save(str(OUT / "2_live.png"))

    win.rec_btn.click()
    wait(app, 3.0)
    win.grab().save(str(OUT / "3_recording.png"))
    win.rec_btn.click()
    wait(app, 0.3)
    win.grab().save(str(OUT / "4_saved.png"))
    win.close()
    print("wrote", sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
