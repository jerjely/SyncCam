"""SyncCam main window (PySide6)."""
from __future__ import annotations

import platform
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6 import QtCore
from PySide6.QtCore import QPointF, QRectF, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QIcon, QImage, QKeySequence,
                           QPainter, QPalette, QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider,
                               QSpinBox, QVBoxLayout, QWidget)

from . import APP_AUTHOR, APP_NAME, APP_URL, __version__
from .app import resource_path
from .arduino import (ANALOG_PINS, DEFAULT_PINS, IR_OFF, IR_ON, IR_PINS, SAMPLE_RATES, SIM_PORT,
                      ArduinoStream, SimulatedArduino, list_serial_ports, make_arduino)
from .camera import (CODECS, RESOLUTIONS, TEST_PATTERN, CameraThread, ImageSettings, auto_levels,
                     scan_cameras)
from .session import Recording

APP_ID = "io.github.jerjely.SyncCam"
SCOPE_S = 10.0          # seconds of signal kept for the plot (longest zoom-out)
ACCENT = "#12a4b8"

STYLE = """
QMainWindow, QDialog { background: #f2f4fa; }
QGroupBox { background: #ffffff; border: 1px solid #d6dbe8; border-radius: 8px;
            margin-top: 11px; padding: 6px 8px 4px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #101d3d;
                   font-weight: bold; }
QPushButton { background: #ffffff; border: 1px solid #c4cbe0; border-radius: 6px;
              padding: 6px 14px; }
QPushButton:hover { background: #eef1fb; }
QPushButton:disabled { color: #9aa3bd; background: #f3f5fa; border-color: #dfe3ee; }
QPushButton#primary { background: #2f46d9; border-color: #2f46d9; color: #ffffff; font-weight: bold; }
QPushButton#primary:hover { background: #2639b8; }
QPushButton#primary:disabled { background: #aab3ec; border-color: #aab3ec; color: #ffffff; }
QPushButton#record { background: #d6334a; border-color: #d6334a; color: #ffffff; font-weight: bold;
                     padding: 10px 14px; font-size: 15px; }
QPushButton#record:hover { background: #b82a3e; }
QPushButton#record:disabled { background: #eab5bd; border-color: #eab5bd; color: #ffffff; }
QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit { background: #ffffff; border: 1px solid #c4cbe0;
                                      border-radius: 6px; padding: 4px 6px; min-height: 20px; }
QComboBox:disabled, QDoubleSpinBox:disabled, QSpinBox:disabled, QLineEdit:disabled
    { background: #f3f5fa; color: #8a93ad; }
QStatusBar { color: #5a6482; }
"""


def fmt_time(s: float) -> str:
    total = int(s)
    m, sec = divmod(total, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def app_icon() -> QIcon:
    icon = QIcon()
    for name in ("icon_256.png", "icon_512.png"):
        path = resource_path(name)
        if path.exists():
            icon.addFile(str(path))
    return icon


def apply_style(app: QApplication) -> None:
    """Same light look on every OS (ignores the system dark mode)."""
    app.setStyle("Fusion")
    pal = QPalette()
    R = QPalette.ColorRole
    G = QPalette.ColorGroup
    ink, paper = QColor("#1b2440"), QColor("#ffffff")
    for role, colour in [(R.Window, "#f2f4fa"), (R.WindowText, ink), (R.Base, paper),
                         (R.AlternateBase, "#f6f8fd"), (R.Text, ink), (R.Button, paper),
                         (R.ButtonText, ink), (R.ToolTipBase, "#ffffe8"), (R.ToolTipText, ink),
                         (R.Highlight, "#2f46d9"), (R.HighlightedText, "#ffffff"),
                         (R.PlaceholderText, "#8a93ad")]:
        pal.setColor(role, QColor(colour))
    for role in (R.WindowText, R.Text, R.ButtonText):
        pal.setColor(G.Disabled, role, QColor("#9aa3bd"))
    app.setPalette(pal)
    app.setStyleSheet(STYLE)


def hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet("color: #5a6482;")
    lab.setWordWrap(True)
    return lab


# --------------------------------------------------------------------------
class VideoView(QWidget):
    """Live camera picture."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._img: Optional[QImage] = None
        self._arr = None
        self.recording = False
        self.message = "No camera open"
        self.overlay = ""

    def set_frame(self, arr: Optional[np.ndarray]) -> None:
        if arr is None:
            self._img, self._arr = None, None
        else:
            arr = np.ascontiguousarray(arr)
            self._arr = arr
            h, w = arr.shape[:2]
            fmt = QImage.Format.Format_Grayscale8 if arr.ndim == 2 else QImage.Format.Format_BGR888
            self._img = QImage(arr.data, w, h, arr.strides[0], fmt)
        self.update()

    def _target(self) -> Optional[QRectF]:
        if self._img is None:
            return None
        iw, ih = self._img.width(), self._img.height()
        s = min(self.width() / iw, self.height() / ih)
        w, h = iw * s, ih * s
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0b1226"))
        r = self._target()
        if r is None:
            p.setPen(QColor("#8a93ad"))
            p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self.message)
            p.end()
            return
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(r, self._img)
        if self.recording:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#e0334d"))
            badge = QRectF(r.right() - 74, r.top() + 10, 64, 26)
            p.drawRoundedRect(badge, 6, 6)
            p.setPen(QColor("#ffffff"))
            f = p.font()
            f.setBold(True)
            p.setFont(f)
            p.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), "REC")
        if self.overlay:
            p.setPen(QColor("#ffffff"))
            p.drawText(r.adjusted(0, 0, -10, -8), int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom),
                       self.overlay)
        p.end()


# --------------------------------------------------------------------------
class ScopeWidget(QWidget):
    """Scrolling plot of the recent signals: TTL, microphone, IR LED. Zoomable in time and mic scale."""
    LANES = [("TTL (A0)", "#2f46d9"), ("Microphone (A1)", ACCENT), ("IR LED", "#d98a00")]
    WINDOWS = [0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0]     # seconds
    MIC_SCALES = [0, 512, 256, 128, 64, 32, 16]               # +- ADC units around the mean, 0 = auto

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(135)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.data = np.zeros((0, 3), dtype=np.int16)
        self.fs = 5000
        self.threshold = 512
        self.message = "Arduino not connected"
        self.lanes = list(self.LANES)
        self.window_s = 5.0
        self.mic_scale = 0
        self.on_wheel = None        # callback(kind, step): kind "window" or "mic"
        self.setToolTip("Mouse wheel: zoom time.  Shift + wheel: microphone scale.")

    def sizeHint(self):
        return QtCore.QSize(600, 185)

    def set_pins(self, ttl: int, mic: int) -> None:
        self.lanes = [(f"TTL (A{ttl})", self.LANES[0][1]), (f"Microphone (A{mic})", self.LANES[1][1]),
                      self.LANES[2]]
        self.update()

    def set_data(self, data: np.ndarray, fs: int) -> None:
        self.data, self.fs = data, fs
        self.update()

    def wheelEvent(self, e):
        d = e.angleDelta()
        delta = d.y() or d.x()          # Shift+wheel arrives as horizontal on some systems
        if not delta or self.on_wheel is None:
            return
        step = -1 if delta > 0 else 1   # wheel up = zoom in
        shift = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self.on_wheel("mic" if shift else "window", step if not shift else -step)
        e.accept()

    @staticmethod
    def fmt_window(w: float) -> str:
        return f"{w * 1000:g} ms" if w < 1 else f"{w:g} s"

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("#d6dbe8"), 1))
        p.setBrush(QColor("#fbfcff"))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 8, 8)
        left, right = 118, self.width() - 10
        lane_h = (self.height() - 16) / 3
        width = max(10, right - left)
        n_win = max(2, int(self.window_s * self.fs))
        d = self.data[-n_win:]
        for k, (name, colour) in enumerate(self.lanes):
            top = 8 + k * lane_h
            p.setPen(QColor("#5a6482"))
            p.drawText(QRectF(10, top, left - 16, lane_h), int(Qt.AlignmentFlag.AlignVCenter), name)
            p.setPen(QPen(QColor("#e6e9f3"), 1))
            p.drawLine(QPointF(left, top + lane_h - 2), QPointF(right, top + lane_h - 2))
            if len(d) < 2:
                continue
            col = d[:, k].astype(np.float64)
            if k == 0:
                lo_v, hi_v = 0.0, 1023.0
            elif k == 1:
                c = col.mean()
                amp = self.mic_scale or max(30.0, np.abs(col - c).max())
                lo_v, hi_v = c - amp, c + amp
            else:
                lo_v, hi_v = -0.1, 1.1
            n = len(col)
            x0 = right - n / n_win * width
            span_px = right - x0

            def ypix(v):
                v = np.clip(v, lo_v, hi_v)
                return top + 4 + (1 - (v - lo_v) / (hi_v - lo_v)) * (lane_h - 10)

            if k == 2:
                cols = int(max(2, min(span_px, n)))
                edges = np.linspace(0, n, cols + 1).astype(np.int64)
                maxs = np.maximum.reduceat(col, edges[:-1])
                xs = x0 + (np.arange(cols) + 0.5) / cols * span_px
                poly = QPolygonF([QPointF(x0, ypix(0))] +
                                 [QPointF(x, ypix(m)) for x, m in zip(xs, maxs)] +
                                 [QPointF(right, ypix(0))])
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(217, 138, 0, 110))
                p.drawPolygon(poly)
                continue
            p.setPen(QPen(QColor(colour), 1.2))
            if n <= span_px:                    # zoomed in: every sample, joined by a line
                xs = x0 + (np.arange(n) + 0.5) / n * span_px
                p.drawPolyline(QPolygonF([QPointF(x, ypix(v)) for x, v in zip(xs, col)]))
            else:                               # zoomed out: min/max per pixel column
                cols = int(max(2, span_px))
                edges = np.linspace(0, n, cols + 1).astype(np.int64)
                mins = np.minimum.reduceat(col, edges[:-1])
                maxs = np.maximum.reduceat(col, edges[:-1])
                xs = x0 + (np.arange(cols) + 0.5) / cols * span_px
                for x, a, b in zip(xs, mins, maxs):
                    ya, yb = ypix(a), ypix(b)
                    p.drawLine(QPointF(x, ya), QPointF(x, yb + (0.8 if abs(ya - yb) < 0.8 else 0)))
            if k == 0:
                p.setPen(QPen(QColor("#d6334a"), 1, Qt.PenStyle.DashLine))
                y = ypix(self.threshold)
                p.drawLine(QPointF(left, y), QPointF(right, y))
        p.setPen(QColor("#8a93ad"))
        if len(d) < 2:
            p.drawText(QRectF(left, 0, width, self.height()), int(Qt.AlignmentFlag.AlignCenter), self.message)
        else:
            p.drawText(QRectF(left, self.height() - 16, width, 14), int(Qt.AlignmentFlag.AlignLeft),
                       f"-{self.fmt_window(self.window_s)}")
            p.drawText(QRectF(left, self.height() - 16, width, 14), int(Qt.AlignmentFlag.AlignRight), "now")
        p.end()


# --------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, settings: Optional[QSettings] = None):
        super().__init__()
        self.settings = settings if settings is not None else QSettings("jerjely", APP_NAME)
        self.camera: Optional[CameraThread] = None
        self.stream: Optional[ArduinoStream] = None
        self.recording: Optional[Recording] = None
        self.last_folder: Optional[Path] = None
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(1000, 540)
        self.resize(*self._fitting_size(1240, 800))
        self._build_ui()
        self._build_menu()
        self._restore_settings()
        self.refresh_ports()
        self._ir_toggled()
        self._pins_changed()
        self._image_changed()
        self._zoom_changed()
        self._update_enabled()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    def _fitting_size(self, w: int, h: int):
        """Window size that fits on the current screen (after Windows display scaling)."""
        scr = QApplication.primaryScreen()
        if scr is None:
            return w, h
        avail = scr.availableGeometry()
        return max(1000, min(w, avail.width() - 20)), max(540, min(h, avail.height() - 40))

    # ---- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(12, 6, 12, 8)
        outer.setSpacing(12)
        # The left column scrolls instead of being squashed on short screens
        # (small laptops, or Windows display scaling at 125-150 %).
        W = 350
        left_panel = QWidget()
        left = QVBoxLayout(left_panel)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(6)
        self.left_scroll = QScrollArea()
        self.left_scroll.setWidget(left_panel)
        self.left_scroll.setWidgetResizable(True)
        self.left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.left_scroll.setFixedWidth(W + 6 + self.left_scroll.verticalScrollBar().sizeHint().width())
        left_panel.setObjectName("leftPanel")
        self.left_scroll.setObjectName("leftScroll")
        self.left_scroll.setStyleSheet("#leftScroll, #leftPanel { background: transparent; }")
        self.left_scroll.viewport().setAutoFillBackground(False)
        left_col = QVBoxLayout()
        left_col.setSpacing(6)
        left_col.addWidget(self.left_scroll, 1)
        right = QVBoxLayout()
        right.setSpacing(6)
        outer.addLayout(left_col, 0)
        outer.addLayout(right, 1)

        # 1 camera
        box = QGroupBox("1  Camera")
        box.setFixedWidth(W)
        form = QFormLayout(box)
        self.cam_combo = QComboBox()
        self.scan_btn = QPushButton("Scan")
        row = QHBoxLayout()
        row.addWidget(self.cam_combo, 1)
        row.addWidget(self.scan_btn)
        form.addRow("Source", row)
        self.res_combo = QComboBox()
        for label, size in RESOLUTIONS:
            self.res_combo.addItem(label, size)
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 240)
        self.fps_spin.setValue(30)
        self.fps_spin.setSuffix(" fps")
        row = QHBoxLayout()
        row.addWidget(self.res_combo, 1)
        row.addWidget(self.fps_spin)
        form.addRow("Size / rate", row)
        self.codec_combo = QComboBox()
        self.codec_combo.addItem("MJPG (.avi) - safest", "MJPG")
        self.codec_combo.addItem("MPEG-4 (.mp4) - smaller", "MP4V")
        form.addRow("Video file", self.codec_combo)
        self.cam_btn = QPushButton("Open camera")
        self.cam_btn.setObjectName("primary")
        form.addRow(self.cam_btn)
        self.cam_status = QLabel()
        self.cam_status.setWordWrap(True)
        form.addRow(self.cam_status)
        left.addWidget(box)

        # 2 arduino
        box = QGroupBox("2  Arduino")
        box.setFixedWidth(W)
        form = QFormLayout(box)
        self.port_combo = QComboBox()
        self.refresh_btn = QPushButton("Refresh")
        row = QHBoxLayout()
        row.addWidget(self.port_combo, 1)
        row.addWidget(self.refresh_btn)
        form.addRow("Port", row)
        self.rate_combo = QComboBox()
        for r in SAMPLE_RATES:
            self.rate_combo.addItem(f"{r} Hz", r)
        self.rate_combo.setCurrentIndex(SAMPLE_RATES.index(5000))
        self.thr_spin = QDoubleSpinBox()
        self.thr_spin.setRange(0.2, 4.8)
        self.thr_spin.setDecimals(2)
        self.thr_spin.setSingleStep(0.1)
        self.thr_spin.setValue(2.5)
        self.thr_spin.setSuffix(" V")
        self.thr_spin.setToolTip("TTL threshold: the TTL counts as high above this voltage")
        row = QHBoxLayout()
        row.addWidget(self.rate_combo, 1)
        row.addWidget(QLabel("TTL at"))
        row.addWidget(self.thr_spin)
        form.addRow("Sampling", row)
        self.ttl_pin = QComboBox()
        self.mic_pin = QComboBox()
        for combo in (self.ttl_pin, self.mic_pin):
            for n in ANALOG_PINS:
                combo.addItem(f"A{n}", n)
        self.ir_pin = QComboBox()
        for n in IR_PINS:
            self.ir_pin.addItem(f"D{n}", n)
        self.ttl_pin.setCurrentIndex(self.ttl_pin.findData(DEFAULT_PINS[0]))
        self.mic_pin.setCurrentIndex(self.mic_pin.findData(DEFAULT_PINS[1]))
        self.ir_pin.setCurrentIndex(self.ir_pin.findData(DEFAULT_PINS[2]))
        self.ttl_pin.setToolTip("Analog input with the TTL sync signal")
        self.mic_pin.setToolTip("Analog input with the microphone (A6/A7: Nano only)")
        self.ir_pin.setToolTip("Digital pin that drives the IR LED")
        row = QHBoxLayout()
        row.setSpacing(4)
        for lab, combo in (("TTL", self.ttl_pin), ("Mic", self.mic_pin), ("IR", self.ir_pin)):
            row.addWidget(QLabel(lab))
            row.addWidget(combo, 1)
        form.addRow("Pins", row)
        self.ard_btn = QPushButton("Connect")
        self.ard_btn.setObjectName("primary")
        form.addRow(self.ard_btn)
        self.ard_status = QLabel()
        self.ard_status.setWordWrap(True)
        form.addRow(self.ard_status)
        left.addWidget(box)

        # 3 IR LED
        box = QGroupBox("3  IR LED")
        box.setFixedWidth(W)
        v = QVBoxLayout(box)
        self.ir_btn = QPushButton("IR LED: OFF")
        self.ir_btn.setCheckable(True)
        self.ir_btn.setMinimumHeight(36)
        v.addWidget(self.ir_btn)
        self.ir_btn.setToolTip("Switched by the Arduino. It stays as you set it until you switch it "
                               "or disconnect.")
        left.addWidget(box)

        # 4 recording
        box = QGroupBox("4  Recording")
        box.setFixedWidth(self.left_scroll.width())
        v = QVBoxLayout(box)
        form = QFormLayout()
        self.dir_edit = QLineEdit(str(Path.home() / "SyncCam recordings"))
        self.dir_btn = QPushButton("Browse")
        self.dir_btn.setStyleSheet("padding: 6px 8px;")
        row = QHBoxLayout()
        row.addWidget(self.dir_edit, 1)
        row.addWidget(self.dir_btn)
        form.addRow("Folder", row)
        self.name_edit = QLineEdit("session")
        self.name_edit.setPlaceholderText("animal / session name")
        form.addRow("Name", self.name_edit)
        v.addLayout(form)
        self.rec_btn = QPushButton("Record")
        self.rec_btn.setObjectName("record")
        self.open_btn = QPushButton("Open folder")
        self.open_btn.setToolTip("Open the last recording's folder")
        self.open_btn.setMinimumHeight(40)
        row = QHBoxLayout()
        row.addWidget(self.rec_btn, 1)
        row.addWidget(self.open_btn)
        v.addLayout(row)
        self.rec_status = QLabel("Not recording.")
        self.rec_status.setWordWrap(True)
        self.rec_status.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.rec_status.setMinimumHeight(3 * self.rec_status.fontMetrics().lineSpacing() + 4)
        v.addWidget(self.rec_status)
        box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        left_col.addWidget(box, 0)
        left.addStretch(1)

        # preview
        box = QGroupBox("Camera preview")
        v = QVBoxLayout(box)
        self.view = VideoView()
        v.addWidget(self.view, 1)
        row = QHBoxLayout()
        self.grey_chk = QCheckBox("Greyscale")
        self.grey_chk.setChecked(True)
        row.addWidget(self.grey_chk)
        self.sliders = {}
        for key, label, lo, hi, val in (("brightness", "Brightness", -150, 150, 0),
                                        ("contrast", "Contrast", 20, 400, 100),
                                        ("gamma", "Gamma", 30, 300, 100)):
            sl = QSlider(Qt.Orientation.Horizontal)
            sl.setRange(lo, hi)
            sl.setValue(val)
            sl.setMinimumWidth(50)
            lab = QLabel()
            lab.setMinimumWidth(34)
            lab.setStyleSheet("color: #5a6482;")
            row.addSpacing(6)
            row.addWidget(QLabel(label))
            row.addWidget(sl, 1)
            row.addWidget(lab)
            self.sliders[key] = (sl, lab)
            sl.valueChanged.connect(self._image_changed)
        self.auto_btn = QPushButton("Auto")
        self.auto_btn.setToolTip("Set brightness and contrast from the current picture")
        self.reset_btn = QPushButton("Reset")
        row.addWidget(self.auto_btn)
        row.addWidget(self.reset_btn)
        v.addLayout(row)
        self.adjust_rec_chk = QCheckBox("Record the adjusted picture")
        self.adjust_rec_chk.setToolTip("Untick to record the camera's original image; the adjustments then "
                                       "only change the preview")
        self.adjust_rec_chk.setChecked(True)
        v.addWidget(self.adjust_rec_chk)
        right.addWidget(box, 1)

        # signals
        box = QGroupBox("Arduino signals")
        v = QVBoxLayout(box)
        self.scope = ScopeWidget()
        self.scope.on_wheel = self._scope_wheel
        v.addWidget(self.scope)
        row = QHBoxLayout()
        row.addWidget(QLabel("Show"))
        self.window_combo = QComboBox()
        for w in ScopeWidget.WINDOWS:
            self.window_combo.addItem(f"last {ScopeWidget.fmt_window(w)}", w)
        self.window_combo.setCurrentIndex(ScopeWidget.WINDOWS.index(5.0))
        row.addWidget(self.window_combo)
        row.addSpacing(10)
        row.addWidget(QLabel("Mic scale"))
        self.mic_combo = QComboBox()
        for m in ScopeWidget.MIC_SCALES:
            self.mic_combo.addItem("Auto" if m == 0 else f"\u00b1{m}", m)
        row.addWidget(self.mic_combo)
        row.addSpacing(10)
        self.freeze_btn = QPushButton("Freeze")
        self.freeze_btn.setCheckable(True)
        row.addWidget(self.freeze_btn)
        row.addStretch(1)
        wheel = QLabel("Wheel: zoom  ·  Shift+wheel: mic")
        wheel.setStyleSheet("color: #8a93ad;")
        row.addWidget(wheel)
        v.addLayout(row)
        self.sig_label = hint("")
        v.addWidget(self.sig_label)
        right.addWidget(box, 0)

        self.statusBar().addWidget(hint("Frame times, Arduino samples and TTL edges are all saved on the "
                                        "computer clock, so the video lines up with the Neuropixels TTL."), 1)

        # signals
        self.scan_btn.clicked.connect(self.scan)
        self.cam_btn.clicked.connect(self._toggle_camera)
        self.refresh_btn.clicked.connect(self.refresh_ports)
        self.ard_btn.clicked.connect(self._toggle_arduino)
        self.ir_btn.toggled.connect(self._ir_toggled)
        for combo in (self.ttl_pin, self.mic_pin, self.ir_pin):
            combo.currentIndexChanged.connect(self._pins_changed)
        self.thr_spin.valueChanged.connect(self._threshold_changed)
        self.grey_chk.toggled.connect(self._image_changed)
        self.auto_btn.clicked.connect(self._auto_image)
        self.reset_btn.clicked.connect(self._reset_image)
        self.window_combo.currentIndexChanged.connect(self._zoom_changed)
        self.mic_combo.currentIndexChanged.connect(self._zoom_changed)
        self.freeze_btn.toggled.connect(
            lambda on: self.freeze_btn.setText("Resume" if on else "Freeze"))
        self.dir_btn.clicked.connect(self._pick_dir)
        self.rec_btn.clicked.connect(self._toggle_record)
        self.open_btn.clicked.connect(self._open_folder)
        self._set_cam_list([(i, f"Camera {i}") for i in range(3)])

    def _build_menu(self) -> None:
        m = self.menuBar().addMenu("&File")
        a = QAction("Choose recording folder...", self)
        a.triggered.connect(self._pick_dir)
        m.addAction(a)
        a = QAction("Open last recording folder", self)
        a.triggered.connect(self._open_folder)
        m.addAction(a)
        m.addSeparator()
        q = QAction("Quit", self)
        q.setShortcut(QKeySequence.StandardKey.Quit)
        q.triggered.connect(self.close)
        m.addAction(q)
        h = self.menuBar().addMenu("&Help")
        a = QAction(f"About {APP_NAME}", self)
        a.setMenuRole(QAction.MenuRole.AboutRole)
        a.triggered.connect(self.show_about)
        h.addAction(a)

    # ---- settings ----------------------------------------------------------
    def _restore_settings(self) -> None:
        s = self.settings

        def pick(combo, key):
            val = s.value(key)
            if val is None:
                return
            for i in range(combo.count()):
                if str(combo.itemData(i)) == str(val):
                    combo.setCurrentIndex(i)
                    return

        pick(self.res_combo, "resolution")
        pick(self.codec_combo, "codec")
        pick(self.rate_combo, "rate")
        pick(self.ttl_pin, "ttl_pin")
        pick(self.mic_pin, "mic_pin")
        pick(self.ir_pin, "ir_pin")
        self.ir_btn.setChecked(str(s.value("ir_on", "false")).lower() == "true")
        for spin, key, cast in ((self.fps_spin, "fps", int), (self.thr_spin, "threshold", float)):
            val = s.value(key)
            if val is not None:
                try:
                    spin.setValue(cast(val))
                except (TypeError, ValueError):
                    pass
        for edit, key in ((self.dir_edit, "folder"), (self.name_edit, "name")):
            val = s.value(key)
            if val:
                edit.setText(str(val))
        self.grey_chk.setChecked(str(s.value("grey", "true")).lower() == "true")
        self.adjust_rec_chk.setChecked(str(s.value("adjust_recording", "true")).lower() == "true")
        for key, (sl, _lab) in self.sliders.items():
            val = s.value(f"image_{key}")
            if val is not None:
                try:
                    sl.setValue(int(val))
                except (TypeError, ValueError):
                    pass
        pick(self.window_combo, "scope_window")
        pick(self.mic_combo, "scope_mic")
        geo = s.value("geometry")
        if geo is not None:
            try:
                self.restoreGeometry(geo)
            except Exception:
                pass

    def _save_settings(self) -> None:
        s = self.settings
        s.setValue("resolution", str(self.res_combo.currentData()))
        s.setValue("codec", self.codec_combo.currentData())
        s.setValue("rate", self.rate_combo.currentData())
        s.setValue("ttl_pin", self.ttl_pin.currentData())
        s.setValue("mic_pin", self.mic_pin.currentData())
        s.setValue("ir_pin", self.ir_pin.currentData())
        s.setValue("ir_on", self.ir_btn.isChecked())
        s.setValue("fps", self.fps_spin.value())
        s.setValue("threshold", self.thr_spin.value())
        s.setValue("folder", self.dir_edit.text())
        s.setValue("name", self.name_edit.text())
        s.setValue("port", self.port_combo.currentData() or "")
        s.setValue("camera", self.cam_combo.currentData())
        s.setValue("grey", self.grey_chk.isChecked())
        s.setValue("adjust_recording", self.adjust_rec_chk.isChecked())
        for key, (sl, _lab) in self.sliders.items():
            s.setValue(f"image_{key}", sl.value())
        s.setValue("scope_window", self.window_combo.currentData())
        s.setValue("scope_mic", self.mic_combo.currentData())
        s.setValue("geometry", self.saveGeometry())
        s.sync()

    # ---- camera ------------------------------------------------------------
    def _set_cam_list(self, cams) -> None:
        wanted = self.cam_combo.currentData()
        if wanted is None:
            wanted = self.settings.value("camera")
        self.cam_combo.clear()
        for i, label in cams:
            self.cam_combo.addItem(label, i)
        self.cam_combo.addItem("Test pattern (no camera)", TEST_PATTERN)
        for k in range(self.cam_combo.count()):
            if str(self.cam_combo.itemData(k)) == str(wanted):
                self.cam_combo.setCurrentIndex(k)
                break

    def scan(self) -> None:
        if self.camera is not None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            cams = scan_cameras()
        finally:
            QApplication.restoreOverrideCursor()
        self._set_cam_list(cams)
        self.cam_status.setText(f"{len(cams)} camera(s) found." if cams else
                                "No camera found. Check the USB cable"
                                + (" and camera permission in System Settings > Privacy & Security."
                                   if sys.platform == "darwin" else "."))

    def _led_fn(self):
        st = self.stream
        if st is not None and isinstance(st.device, SimulatedArduino):
            return st.device.current_led()
        return bool(st.last_led) if st is not None else False

    def _toggle_camera(self) -> None:
        if self.camera is None:
            self.open_camera()
        else:
            self.close_camera()

    def open_camera(self) -> bool:
        idx = self.cam_combo.currentData()
        if idx is None:
            return False
        cam = CameraThread(int(idx), self.res_combo.currentData(), self.fps_spin.value(), self._led_fn)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            cam.open()
        except Exception as exc:
            QMessageBox.critical(self, "Cannot open camera", str(exc))
            return False
        finally:
            QApplication.restoreOverrideCursor()
        cam.set_image_settings(self._image_settings())
        self.camera = cam
        self._update_enabled()
        return True

    def close_camera(self) -> None:
        cam, self.camera = self.camera, None
        if cam is not None:
            cam.close()
        self.view.set_frame(None)
        self.view.message = "No camera open"
        self._update_enabled()

    # ---- picture adjustments -----------------------------------------------
    def _image_settings(self) -> ImageSettings:
        return ImageSettings(grey=self.grey_chk.isChecked(),
                             brightness=self.sliders["brightness"][0].value(),
                             contrast=self.sliders["contrast"][0].value() / 100.0,
                             gamma=self.sliders["gamma"][0].value() / 100.0)

    def _image_changed(self, *_a) -> None:
        st = self._image_settings()
        self.sliders["brightness"][1].setText(f"{st.brightness:+d}")
        self.sliders["contrast"][1].setText(f"{st.contrast:.2f}")
        self.sliders["gamma"][1].setText(f"{st.gamma:.2f}")
        if self.camera is not None:
            self.camera.set_image_settings(st)
        if self.recording is not None:
            self.recording.image_changed = True

    def _auto_image(self) -> None:
        f = self.camera.latest if self.camera is not None else None
        if f is None or f.raw is None:
            return
        self.sliders["gamma"][0].setValue(100)
        b, c = auto_levels(f.raw)
        self.sliders["brightness"][0].setValue(b)
        self.sliders["contrast"][0].setValue(int(round(c * 100)))

    def _reset_image(self) -> None:
        for key, val in (("brightness", 0), ("contrast", 100), ("gamma", 100)):
            self.sliders[key][0].setValue(val)

    # ---- signal zoom --------------------------------------------------------
    def _zoom_changed(self, *_a) -> None:
        self.scope.window_s = float(self.window_combo.currentData())
        self.scope.mic_scale = int(self.mic_combo.currentData())
        self.scope.update()

    def _scope_wheel(self, kind: str, step: int) -> None:
        combo = self.window_combo if kind == "window" else self.mic_combo
        i = min(max(combo.currentIndex() + step, 0), combo.count() - 1)
        combo.setCurrentIndex(i)

    # ---- arduino -----------------------------------------------------------
    def refresh_ports(self) -> None:
        wanted = self.port_combo.currentData() or self.settings.value("port", "")
        self.port_combo.clear()
        ports = list_serial_ports()
        # most likely Arduino first: USB serial chips; built-in COM1 / Bluetooth ports last
        ports.sort(key=lambda p: -self._arduino_score(*p))
        for dev, desc in ports:
            label = dev if desc in (dev, "n/a", "") else f"{dev}  -  {desc}"
            self.port_combo.addItem(label, dev)
        self.port_combo.addItem("Simulator (no hardware)", SIM_PORT)
        idx = self.port_combo.findData(wanted)
        self.port_combo.setCurrentIndex(idx if idx >= 0 else 0)

    @staticmethod
    def _arduino_score(dev: str, desc: str) -> int:
        text = f"{dev} {desc}".lower()
        if any(k in text for k in ("arduino", "ch340", "ch341", "wch", "usbmodem", "usbserial", "usb serial",
                                    "usb-serial", "ftdi", "cp210", "uart")):
            return 2
        if any(k in text for k in ("communications port", "bluetooth", "debug-console", "wlan")):
            return 0
        return 1

    def _threshold_adc(self) -> int:
        return int(round(self.thr_spin.value() / 5.0 * 1023))

    def _toggle_arduino(self) -> None:
        if self.stream is None:
            self.connect_arduino()
        else:
            self.disconnect_arduino()

    def connect_arduino(self) -> bool:
        port = self.port_combo.currentData()
        if not port:
            return False
        pins = self._pins()
        if pins[0] == pins[1]:
            QMessageBox.warning(self, "Pins", "The TTL and the microphone need different analog inputs.")
            return False
        st = ArduinoStream(make_arduino(port), int(self.rate_combo.currentData()))
        self.ard_status.setText("Connecting (the Arduino restarts, about 2 s)...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            st.connect(self._ir_mode(), ttl_threshold=self._threshold_adc(), pins=pins)
        except Exception as exc:
            self.ard_status.setText("")
            QMessageBox.critical(self, "Cannot connect", f"{port}:\n\n{exc}\n\nIs another program "
                                 "(for example the Arduino IDE's Serial Monitor) using this port?")
            return False
        finally:
            QApplication.restoreOverrideCursor()
        self.stream = st
        self._update_enabled()
        return True

    def disconnect_arduino(self) -> None:
        st, self.stream = self.stream, None
        if st is not None:
            st.close()
        self.scope.set_data(np.zeros((0, 3), dtype=np.int16), 5000)
        self._update_enabled()

    def _pins(self):
        return (int(self.ttl_pin.currentData()), int(self.mic_pin.currentData()),
                int(self.ir_pin.currentData()))

    def _pins_changed(self, *_a) -> None:
        ttl, mic, ir = self._pins()
        self.scope.set_pins(ttl, mic)
        self._ir_toggled()

    def _ir_mode(self) -> int:
        return IR_ON if self.ir_btn.isChecked() else IR_OFF

    def _ir_toggled(self, *_a) -> None:
        ir = self.ir_pin.currentData() if hasattr(self, "ir_pin") else 8
        self.ir_btn.setText(f"IR LED (D{ir}): " + ("ON" if self.ir_btn.isChecked() else "OFF"))
        if self.stream is not None:
            try:
                self.stream.set_ir(self._ir_mode())
            except Exception as exc:
                self._arduino_lost(str(exc))

    def _threshold_changed(self, *_a) -> None:
        self.scope.threshold = self._threshold_adc()
        if self.stream is not None:
            try:
                self.stream.set_threshold(self._threshold_adc())
            except Exception as exc:
                self._arduino_lost(str(exc))

    def _arduino_lost(self, msg: str) -> None:
        was_recording = self.recording is not None
        if was_recording:
            self.stop_recording()
        self.disconnect_arduino()
        QMessageBox.warning(self, "Arduino connection lost",
                            f"{msg}\n\n" + ("The recording was stopped and saved. " if was_recording else "")
                            + "Check the cable and connect again.")

    def _camera_lost(self, msg: str) -> None:
        was_recording = self.recording is not None
        if was_recording:
            self.stop_recording()
        self.close_camera()
        QMessageBox.warning(self, "Camera lost", f"{msg}\n\n" +
                            ("The recording was stopped and saved." if was_recording else ""))

    # ---- recording ---------------------------------------------------------
    def _pick_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Recording folder", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def _toggle_record(self) -> None:
        if self.recording is None:
            self.start_recording()
        else:
            self.stop_recording()

    def start_recording(self, ask: bool = True) -> bool:
        if self.camera is None and self.stream is None:
            QMessageBox.information(self, "Nothing to record", "Open a camera and/or connect the Arduino first.")
            return False
        missing = "the camera" if self.camera is None else ("the Arduino" if self.stream is None else "")
        if missing and ask:
            ans = QMessageBox.question(self, "Record anyway?",
                                       f"{missing[0].upper() + missing[1:]} is not connected. Record without it?")
            if ans != QMessageBox.StandardButton.Yes:
                return False
        extra = {"name": self.name_edit.text(),
                 "ir_led_at_start": "on" if self.ir_btn.isChecked() else "off"}
        try:
            Path(self.dir_edit.text()).mkdir(parents=True, exist_ok=True)
            rec = Recording(self.dir_edit.text(), self.name_edit.text(), self.camera, self.stream,
                            self.codec_combo.currentData(), self._threshold_adc(), extra,
                            adjust_video=self.adjust_rec_chk.isChecked())
            rec.start()
        except Exception as exc:
            QMessageBox.critical(self, "Cannot start recording", str(exc))
            return False
        self.recording = rec
        self.view.recording = True
        self._update_enabled()
        return True

    def stop_recording(self) -> Optional[dict]:
        rec, self.recording = self.recording, None
        if rec is None:
            return None
        self.view.recording = False
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            info = rec.stop()
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Problem while saving", f"{exc}\n\nFiles so far are in {rec.folder}")
            self._update_enabled()
            return None
        QApplication.restoreOverrideCursor()
        self.last_folder = rec.folder
        bits = [f"Saved {rec.folder.name}  ({fmt_time(rec.elapsed())})"]
        if "video" in info:
            vid = info["video"]
            bits.append(f"{vid['frames']} frames at {vid['measured_fps'] or 0:.2f} fps"
                        + (f", {vid['frames_dropped_disk']} dropped" if vid["frames_dropped_disk"] else ""))
        if "arduino" in info:
            a = info["arduino"]
            bits.append(f"{a['samples']} samples, {info.get('ttl_rising_edges', 0)} TTL pulses"
                        + (f", {a['samples_lost_and_filled']} samples lost" if a["samples_lost_and_filled"] else ""))
        self.rec_status.setStyleSheet("color: #1a7a3a;")
        self.rec_status.setText("\n".join(bits))
        self._update_enabled()
        return info

    def _open_folder(self) -> None:
        target = self.last_folder or Path(self.dir_edit.text())
        if target.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    # ---- poll --------------------------------------------------------------
    def _poll(self) -> None:
        cam, st = self.camera, self.stream
        if cam is not None:
            if cam.error:
                self._camera_lost(cam.error)
                return
            f = cam.latest
            if f is not None:
                self.view.set_frame(f.image)
                self.view.overlay = f"{cam.measured_fps():.1f} fps"
            w, h = cam.frame_size
            self.cam_status.setText(f'<span style="color:#1a9a4a">&#9679;</span> {cam.name}, {w} x {h}, '
                                    f"{cam.measured_fps():.1f} fps")
        if st is not None:
            if st.error or not st.running:
                self._arduino_lost(st.error or "The reader stopped.")
                return
            data = st.recent(SCOPE_S)
            self.scope.threshold = self._threshold_adc()
            if not self.freeze_btn.isChecked():
                self.scope.set_data(data, st.fs)
            err = st.clock.rate_error()
            slow = err is not None and abs(err) > 0.01
            self.ard_status.setText(f'<span style="color:#1a9a4a">&#9679;</span> {st.device.name}, '
                                    f"{st.fs} Hz, {st.total:,} samples"
                                    + (f", {st.unwrap.dropped} lost" if st.unwrap.dropped else "")
                                    + (f'<br><span style="color:#a32035">Board only reaches '
                                       f"{st.fs * (1 + err):.0f} Hz: choose a lower sample rate.</span>"
                                       if slow else ""))
            if len(data):
                ttl_v = data[-1, 0] / 1023 * 5
                mic = data[-st.fs // 5:, 1].astype(float)
                self.sig_label.setText(f"TTL now {ttl_v:.2f} V ({'high' if data[-1, 0] > self._threshold_adc() else 'low'})   |   "
                                       f"microphone level {mic.std():.0f} ADC units (rms)")
        on = self.ir_btn.isChecked()
        self.ir_btn.setStyleSheet(("background: #d98a00; border-color: #d98a00; color: #ffffff;" if on else "")
                                  + "font-weight: bold;")
        rec = self.recording
        if rec is not None:
            s = rec.status()
            self.rec_status.setStyleSheet("color: #a32035;")
            txt = f"Recording  {fmt_time(s['elapsed'])}"
            if rec.camera is not None:
                txt += f"\n{s['frames']} frames" + (f", {s['frames_dropped']} dropped (disk too slow)"
                                                     if s["frames_dropped"] else "")
            if rec.stream is not None:
                txt += f"\n{s['samples']:,} samples, {s['ttl_edges']} TTL pulses"
            self.rec_status.setText(txt)

    # ---- state -------------------------------------------------------------
    def _update_enabled(self) -> None:
        cam = self.camera is not None
        ard = self.stream is not None
        rec = self.recording is not None
        for w in (self.cam_combo, self.scan_btn, self.res_combo, self.fps_spin):
            w.setEnabled(not cam)
        self.codec_combo.setEnabled(not rec)
        self.cam_btn.setText("Close camera" if cam else "Open camera")
        self.cam_btn.setEnabled(not rec)
        if not cam:
            self.cam_status.setText('<span style="color:#8a93ad">&#9679;</span> No camera open')
        for w in (self.port_combo, self.refresh_btn, self.rate_combo, self.ttl_pin, self.mic_pin, self.ir_pin):
            w.setEnabled(not ard)
        self.ard_btn.setText("Disconnect" if ard else "Connect")
        self.ard_btn.setEnabled(not rec)
        if not ard:
            self.ard_status.setText('<span style="color:#8a93ad">&#9679;</span> Not connected')
            self.sig_label.setText("")
        self.thr_spin.setEnabled(not rec)
        self.grey_chk.setEnabled(not rec)
        self.adjust_rec_chk.setEnabled(not rec)
        for w in (self.dir_edit, self.dir_btn, self.name_edit):
            w.setEnabled(not rec)
        self.rec_btn.setText("Stop recording" if rec else "Record")
        self.rec_btn.setEnabled(rec or cam or ard)
        self.open_btn.setEnabled(not rec)

    # ---- misc --------------------------------------------------------------
    def show_about(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(f"About {APP_NAME}")
        pm = QPixmap(str(resource_path("icon_256.png")))
        if not pm.isNull():
            box.setIconPixmap(pm.scaled(96, 96, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation))
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            f"<h3>{APP_NAME} {__version__}</h3>"
            "<p>USB camera recording with an Arduino-sampled TTL sync signal, microphone and IR LED.</p>"
            f"<p>&copy; 2026 {APP_AUTHOR}. MIT License.<br>"
            f'<a href="{APP_URL}">{APP_URL}</a></p>'
            f'<p style="color:#5a6482">Python {platform.python_version()}, Qt {QtCore.qVersion()}, '
            f"{platform.system()} {platform.machine()}</p>")
        box.exec()

    def shutdown(self) -> None:
        self._timer.stop()
        if self.recording is not None:
            self.stop_recording()
        self.close_camera()
        self.disconnect_arduino()

    def closeEvent(self, event) -> None:
        if self.recording is not None:
            ans = QMessageBox.question(self, "Recording in progress",
                                       "Stop the recording, save it and quit?")
            if ans != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        try:
            self._save_settings()
        finally:
            self.shutdown()
        event.accept()


def run_gui() -> int:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("jerjely")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())
    apply_style(app)
    win = MainWindow()
    win.show()
    rc = app.exec()
    try:
        win.settings.sync()      # the process may leave without interpreter teardown
    except Exception:
        pass
    return rc
