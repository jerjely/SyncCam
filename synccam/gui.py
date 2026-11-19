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
from PySide6.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPushButton, QSizePolicy, QSpinBox, QVBoxLayout, QWidget)

from . import APP_AUTHOR, APP_NAME, APP_URL, __version__
from .app import resource_path
from .arduino import (IR_FOLLOW, IR_MODE_NAMES, IR_OFF, IR_PULSE, SAMPLE_RATES, SIM_PORT,
                      ArduinoStream, SimulatedArduino, list_serial_ports, make_arduino)
from .camera import CODECS, RESOLUTIONS, TEST_PATTERN, CameraThread, scan_cameras
from .session import Recording

APP_ID = "io.github.jerjely.SyncCam"
SCOPE_S = 5.0
ACCENT = "#12a4b8"

STYLE = """
QMainWindow, QDialog { background: #f2f4fa; }
QGroupBox { background: #ffffff; border: 1px solid #d6dbe8; border-radius: 8px;
            margin-top: 12px; padding: 8px 8px 6px 8px; }
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
    """Live picture. Drag a rectangle to mark the IR LED."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(480, 220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._img: Optional[QImage] = None
        self._arr = None
        self.roi = None              # x, y, w, h in image pixels
        self.on_roi = None           # callback(roi or None)
        self._drag = None
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
            self._img = QImage(arr.data, w, h, arr.strides[0], QImage.Format.Format_BGR888)
        self.update()

    def _target(self) -> Optional[QRectF]:
        if self._img is None:
            return None
        iw, ih = self._img.width(), self._img.height()
        s = min(self.width() / iw, self.height() / ih)
        w, h = iw * s, ih * s
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _to_img(self, pos) -> Optional[QPointF]:
        r = self._target()
        if r is None:
            return None
        s = r.width() / self._img.width()
        x = min(max((pos.x() - r.left()) / s, 0), self._img.width() - 1)
        y = min(max((pos.y() - r.top()) / s, 0), self._img.height() - 1)
        return QPointF(x, y)

    def mousePressEvent(self, e):
        p = self._to_img(e.position())
        if p is not None and e.button() == Qt.MouseButton.LeftButton:
            self._drag = (p, p)
            self.update()

    def mouseMoveEvent(self, e):
        if self._drag:
            p = self._to_img(e.position())
            if p is not None:
                self._drag = (self._drag[0], p)
                self.update()

    def mouseReleaseEvent(self, e):
        if self._drag:
            a, b = self._drag
            self._drag = None
            x0, y0 = int(min(a.x(), b.x())), int(min(a.y(), b.y()))
            w, h = int(abs(a.x() - b.x())), int(abs(a.y() - b.y()))
            if w >= 3 and h >= 3:
                self.set_roi((x0, y0, w, h))
            self.update()

    def set_roi(self, roi) -> None:
        self.roi = roi
        if self.on_roi:
            self.on_roi(roi)
        self.update()

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
        s = r.width() / self._img.width()

        def box(x, y, w, h):
            return QRectF(r.left() + x * s, r.top() + y * s, w * s, h * s)

        if self.roi:
            p.setPen(QPen(QColor("#ffd23f"), 2))
            p.drawRect(box(*self.roi))
            p.drawText(box(*self.roi).adjusted(0, -18, 60, 0).topLeft() + QPointF(0, 14), "IR LED")
        if self._drag:
            a, b = self._drag
            p.setPen(QPen(QColor("#ffd23f"), 1, Qt.PenStyle.DashLine))
            p.drawRect(box(min(a.x(), b.x()), min(a.y(), b.y()), abs(a.x() - b.x()), abs(a.y() - b.y())))
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
    """Scrolling plot of the last few seconds: TTL, microphone, IR LED."""
    LANES = [("TTL (A0)", "#2f46d9"), ("Microphone (A1)", ACCENT), ("IR LED", "#d98a00")]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.data = np.zeros((0, 3), dtype=np.int16)
        self.fs = 5000
        self.threshold = 512
        self.message = "Arduino not connected"

    def set_data(self, data: np.ndarray, fs: int) -> None:
        self.data, self.fs = data, fs
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("#d6dbe8"), 1))
        p.setBrush(QColor("#fbfcff"))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 8, 8)
        left, right = 118, self.width() - 10
        lane_h = (self.height() - 16) / 3
        width = max(10, right - left)
        n_win = int(SCOPE_S * self.fs)
        d = self.data[-n_win:]
        for k, (name, colour) in enumerate(self.LANES):
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
                amp = max(30.0, np.abs(col - c).max())
                lo_v, hi_v = c - amp, c + amp
            else:
                lo_v, hi_v = -0.1, 1.1
            n = len(col)
            x0 = right - n / n_win * width
            cols = int(max(2, min(width * (n / n_win), n)))
            edges = np.linspace(0, n, cols + 1).astype(np.int64)
            mins = np.minimum.reduceat(col, edges[:-1])
            maxs = np.maximum.reduceat(col, edges[:-1])

            def ypix(v):
                return top + 4 + (1 - (v - lo_v) / (hi_v - lo_v)) * (lane_h - 10)

            xs = x0 + (np.arange(cols) + 0.5) / cols * (right - x0)
            if k == 2:
                poly = QPolygonF([QPointF(x0, ypix(0))] +
                                 [QPointF(x, ypix(m)) for x, m in zip(xs, maxs)] +
                                 [QPointF(right, ypix(0))])
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(217, 138, 0, 110))
                p.drawPolygon(poly)
            else:
                pen = QPen(QColor(colour), 1.2)
                p.setPen(pen)
                for x, a, b in zip(xs, mins, maxs):
                    ya, yb = ypix(a), ypix(b)
                    p.drawLine(QPointF(x, ya), QPointF(x, yb + (0.8 if abs(ya - yb) < 0.8 else 0)))
                if k == 0:
                    p.setPen(QPen(QColor("#d6334a"), 1, Qt.PenStyle.DashLine))
                    y = ypix(self.threshold)
                    p.drawLine(QPointF(left, y), QPointF(right, y))
        if len(d) < 2:
            p.setPen(QColor("#8a93ad"))
            p.drawText(QRectF(left, 0, width, self.height()), int(Qt.AlignmentFlag.AlignCenter), self.message)
        else:
            p.setPen(QColor("#8a93ad"))
            p.drawText(QRectF(left, self.height() - 16, width, 14), int(Qt.AlignmentFlag.AlignLeft),
                       f"-{SCOPE_S:g} s")
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
        self.resize(1240, 800)
        self.setMinimumSize(1080, 700)
        self._build_ui()
        self._build_menu()
        self._restore_settings()
        self.refresh_ports()
        self._ir_mode_changed()
        self._update_enabled()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    # ---- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(12, 6, 12, 8)
        outer.setSpacing(12)
        left, right = QVBoxLayout(), QVBoxLayout()
        left.setSpacing(6)
        right.setSpacing(6)
        outer.addLayout(left, 0)
        outer.addLayout(right, 1)
        W = 350

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
        form = QFormLayout(box)
        self.ir_combo = QComboBox()
        for m, name in IR_MODE_NAMES.items():
            self.ir_combo.addItem(name, m)
        self.per_spin = QSpinBox()
        self.per_spin.setRange(2, 600000)
        self.per_spin.setValue(1000)
        self.per_spin.setSuffix(" ms")
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 599999)
        self.width_spin.setValue(100)
        self.width_spin.setSuffix(" ms")
        self.led_pill = QLabel("LED OFF")
        self.led_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.led_pill.setMinimumSize(110, 28)
        row = QHBoxLayout()
        row.addWidget(self.ir_combo, 1)
        row.addWidget(self.led_pill)
        form.addRow("Mode", row)
        row = QHBoxLayout()
        row.addWidget(self.per_spin, 1)
        row.addWidget(QLabel("on for"))
        row.addWidget(self.width_spin, 1)
        form.addRow("Every", row)
        left.addWidget(box)

        # 4 recording
        box = QGroupBox("4  Recording")
        box.setFixedWidth(W)
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
        self.rec_status.setMinimumHeight(4 * self.rec_status.fontMetrics().lineSpacing() + 4)
        v.addWidget(self.rec_status)
        left.addWidget(box)
        left.addStretch(1)

        # preview
        box = QGroupBox("Camera preview")
        v = QVBoxLayout(box)
        self.view = VideoView()
        self.view.on_roi = self._roi_changed
        v.addWidget(self.view, 1)
        row = QHBoxLayout()
        self.roi_label = hint("Drag a box around the IR LED in the image to log its brightness per frame.")
        row.addWidget(self.roi_label, 1)
        self.roi_clear = QPushButton("Clear LED box")
        row.addWidget(self.roi_clear)
        v.addLayout(row)
        right.addWidget(box, 1)

        # signals
        box = QGroupBox("Arduino signals")
        v = QVBoxLayout(box)
        self.scope = ScopeWidget()
        v.addWidget(self.scope)
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
        self.ir_combo.currentIndexChanged.connect(self._ir_mode_changed)
        self.per_spin.valueChanged.connect(self._ir_mode_changed)
        self.width_spin.valueChanged.connect(self._ir_mode_changed)
        self.thr_spin.valueChanged.connect(self._threshold_changed)
        self.dir_btn.clicked.connect(self._pick_dir)
        self.rec_btn.clicked.connect(self._toggle_record)
        self.open_btn.clicked.connect(self._open_folder)
        self.roi_clear.clicked.connect(lambda: self.view.set_roi(None))
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
        pick(self.ir_combo, "ir_mode")
        for spin, key, cast in ((self.fps_spin, "fps", int), (self.per_spin, "pulse_period", int),
                                (self.width_spin, "pulse_width", int), (self.thr_spin, "threshold", float)):
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
        s.setValue("ir_mode", self.ir_combo.currentData())
        s.setValue("fps", self.fps_spin.value())
        s.setValue("pulse_period", self.per_spin.value())
        s.setValue("pulse_width", self.width_spin.value())
        s.setValue("threshold", self.thr_spin.value())
        s.setValue("folder", self.dir_edit.text())
        s.setValue("name", self.name_edit.text())
        s.setValue("port", self.port_combo.currentData() or "")
        s.setValue("camera", self.cam_combo.currentData())
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
        cam.roi = self._clip_roi(self.view.roi, cam.frame_size)
        self.view.roi = cam.roi
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

    @staticmethod
    def _clip_roi(roi, size):
        if not roi:
            return None
        x, y, w, h = roi
        W, H = size
        if x >= W or y >= H:
            return None
        return (x, y, min(w, W - x), min(h, H - y))

    def _roi_changed(self, roi) -> None:
        if self.camera is not None:
            self.camera.roi = roi

    # ---- arduino -----------------------------------------------------------
    def refresh_ports(self) -> None:
        wanted = self.port_combo.currentData() or self.settings.value("port", "")
        self.port_combo.clear()
        for dev, desc in list_serial_ports():
            label = dev if desc in (dev, "n/a", "") else f"{dev}  -  {desc}"
            self.port_combo.addItem(label, dev)
        self.port_combo.addItem("Simulator (no hardware)", SIM_PORT)
        idx = self.port_combo.findData(wanted)
        self.port_combo.setCurrentIndex(idx if idx >= 0 else 0)

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
        st = ArduinoStream(make_arduino(port), int(self.rate_combo.currentData()))
        self.ard_status.setText("Connecting (the Arduino restarts, about 2 s)...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            st.connect(self.ir_combo.currentData(), (self.per_spin.value(), self.width_spin.value()),
                       self._threshold_adc())
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

    def _ir_mode_changed(self, *_a) -> None:
        mode = self.ir_combo.currentData()
        pulses = mode == IR_PULSE
        self.per_spin.setEnabled(pulses)
        self.width_spin.setEnabled(pulses)
        if self.width_spin.value() >= self.per_spin.value():
            self.width_spin.setValue(max(1, self.per_spin.value() // 2))
        if self.stream is not None:
            try:
                self.stream.set_ir(mode, (self.per_spin.value(), self.width_spin.value()))
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
                 "ir_led": {"mode": IR_MODE_NAMES[self.ir_combo.currentData()],
                            "pulse_period_ms": self.per_spin.value(),
                            "pulse_width_ms": self.width_spin.value()},
                 "led_roi": list(self.camera.roi) if self.camera is not None and self.camera.roi else None}
        try:
            Path(self.dir_edit.text()).mkdir(parents=True, exist_ok=True)
            rec = Recording(self.dir_edit.text(), self.name_edit.text(), self.camera, self.stream,
                            self.codec_combo.currentData(), self._threshold_adc(), extra)
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
        lag = info.get("led_video_lag")
        if lag and "median_ms" in lag:
            bits.append(f"IR LED to video delay: {lag['median_ms']:.0f} ms (median)")
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
                roi_txt = "" if np.isnan(f.roi_mean) else f"   LED box brightness {f.roi_mean:.0f}"
                self.view.overlay = f"{cam.measured_fps():.1f} fps"
                self.roi_label.setText(("LED box set." + roi_txt) if cam.roi else
                                       "Drag a box around the IR LED in the image to log its brightness per frame.")
            w, h = cam.frame_size
            self.cam_status.setText(f'<span style="color:#1a9a4a">&#9679;</span> {cam.name}, {w} x {h}, '
                                    f"{cam.measured_fps():.1f} fps")
        if st is not None:
            if st.error or not st.running:
                self._arduino_lost(st.error or "The reader stopped.")
                return
            data = st.recent(SCOPE_S)
            self.scope.threshold = self._threshold_adc()
            self.scope.set_data(data, st.fs)
            self.ard_status.setText(f'<span style="color:#1a9a4a">&#9679;</span> {st.device.name}, '
                                    f"{st.fs} Hz, {st.total:,} samples"
                                    + (f", {st.unwrap.dropped} lost" if st.unwrap.dropped else ""))
            if len(data):
                ttl_v = data[-1, 0] / 1023 * 5
                mic = data[-st.fs // 5:, 1].astype(float)
                self.sig_label.setText(f"TTL now {ttl_v:.2f} V ({'high' if st.last_ttl else 'low'})   |   "
                                       f"microphone level {mic.std():.0f} ADC units (rms)")
        on = bool(st.last_led) if st is not None else False
        self.led_pill.setText("LED ON" if on else ("LED OFF" if st is not None else "LED  -"))
        self.led_pill.setStyleSheet(
            ("background: #d98a00; color: #ffffff;" if on else "background: #e6e9f3; color: #5a6482;")
            + "border-radius: 8px; font-size: 14px; font-weight: bold; letter-spacing: 1px;")
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
        for w in (self.port_combo, self.refresh_btn, self.rate_combo):
            w.setEnabled(not ard)
        self.ard_btn.setText("Disconnect" if ard else "Connect")
        self.ard_btn.setEnabled(not rec)
        if not ard:
            self.ard_status.setText('<span style="color:#8a93ad">&#9679;</span> Not connected')
            self.sig_label.setText("")
        self.thr_spin.setEnabled(not rec)
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
