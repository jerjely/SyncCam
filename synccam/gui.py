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
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog,
                               QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QProgressDialog, QPushButton, QScrollArea,
                               QSizePolicy, QSlider, QSpinBox, QVBoxLayout, QWidget)

from . import APP_AUTHOR, APP_NAME, APP_URL, __version__
from .app import resource_path
from .arduino import (ANALOG_PINS, DEFAULT_PINS, IR_OFF, IR_ON, IR_PINS, SAMPLE_RATES, SIM_PORT,
                      ArduinoStream, SimulatedArduino, list_serial_ports, make_arduino)
from .camera import (CODECS, RESOLUTIONS, TEST_PATTERN, CameraThread, ImageSettings, auto_levels,
                     scan_cameras)
from .probe import ProbeSet
from .pupil import Kinematics, PupilParams, PupilTracker
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
    """Live camera picture. With pupil tracking on, drag a box around the eye."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._img: Optional[QImage] = None
        self._arr = None
        self.recording = False
        self.message = "No camera open"
        self.overlay = ""
        self.drawing_enabled = False       # pupil tracking on: dragging draws the eye box
        self.show_pupil = True             # draw the box / ellipse
        self.eye_box = None                # x, y, w, h in image pixels
        self.on_eye_box = None             # callback(box)
        self.pupil = None                  # latest PupilResult
        self.mask = None                   # dark-pixel mask of the box (shown while tuning)
        self._drag = None

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

    def _to_img(self, pos) -> Optional[QPointF]:
        r = self._target()
        if r is None:
            return None
        s = r.width() / self._img.width()
        x = min(max((pos.x() - r.left()) / s, 0), self._img.width() - 1)
        y = min(max((pos.y() - r.top()) / s, 0), self._img.height() - 1)
        return QPointF(x, y)

    def mousePressEvent(self, e):
        if not self.drawing_enabled or e.button() != Qt.MouseButton.LeftButton:
            return
        p = self._to_img(e.position())
        if p is not None:
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
            if w >= 12 and h >= 12:
                self.eye_box = (x0, y0, w, h)
                if self.on_eye_box:
                    self.on_eye_box(self.eye_box)
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

        def to_view(x, y):
            return QPointF(r.left() + x * s, r.top() + y * s)

        if self.drawing_enabled and self.show_pupil:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            if self.eye_box:
                bx, by, bw, bh = self.eye_box
                if self.mask is not None and self.mask.shape[0] <= bh and self.mask.shape[1] <= bw:
                    m = np.ascontiguousarray(self.mask)
                    tint = np.zeros((m.shape[0], m.shape[1], 4), np.uint8)
                    tint[m > 0] = (60, 60, 255, 110)          # BGRA: red, see-through
                    qi = QImage(tint.data, m.shape[1], m.shape[0], tint.strides[0], QImage.Format.Format_ARGB32)
                    p.drawImage(QRectF(to_view(bx, by), to_view(bx + m.shape[1], by + m.shape[0])), qi)
                p.setPen(QPen(QColor("#ffd23f"), 1, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(QRectF(to_view(bx, by), to_view(bx + bw, by + bh)))
            pr = self.pupil
            if pr is not None and pr.found:
                c = to_view(pr.x, pr.y)
                p.save()
                p.translate(c)
                p.rotate(pr.angle)
                p.setPen(QPen(QColor("#2ee59d"), 1.6))
                p.drawEllipse(QPointF(0, 0), pr.minor / 2 * s, pr.diameter / 2 * s)
                p.restore()
                p.setPen(QPen(QColor("#ff4d6a"), 1.4))
                p.drawLine(c + QPointF(-4, 0), c + QPointF(4, 0))
                p.drawLine(c + QPointF(0, -4), c + QPointF(0, 4))
            if not self.eye_box:
                p.setPen(QColor("#ffd23f"))
                p.drawText(r.adjusted(10, 8, -10, 0), int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft),
                           "Drag a box around the eye to start pupil tracking")
        if self._drag:
            a, b = self._drag
            p.setPen(QPen(QColor("#ffd23f"), 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(to_view(min(a.x(), b.x()), min(a.y(), b.y())),
                              to_view(max(a.x(), b.x()), max(a.y(), b.y()))))
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
class PupilPlot(QWidget):
    """Live pupil view: x-y trail on the left, x / y / diameter traces on the right."""
    WINDOWS = [5.0, 10.0, 30.0, 60.0, 120.0]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.data = np.zeros((0, 5))       # t, found, x, y, diameter
        self.window_s = 10.0
        self._close = None                 # (QImage, numpy buffer, box, PupilResult)
        self.px_per_mm: Optional[float] = None   # set by the calibration: show mm instead of px
        self.on_wheel = None
        self.setToolTip("Mouse wheel: zoom time")

    def sizeHint(self):
        return QtCore.QSize(600, 160)

    def set_data(self, rows) -> None:
        self.data = np.asarray(rows, float).reshape(-1, 5) if len(rows) else np.zeros((0, 5))
        self.update()

    def set_closeup(self, img: Optional[np.ndarray], box, result) -> None:
        """Enlarged view of the eye box with the fitted outline."""
        if img is None or box is None:
            self._close = None
            return
        x, y, w, h = box
        crop = img[y:y + h, x:x + w]
        if crop.ndim == 3:
            crop = crop.mean(axis=2).astype(np.uint8)
        crop = np.ascontiguousarray(crop)
        if crop.size == 0:
            self._close = None
            return
        qi = QImage(crop.data, crop.shape[1], crop.shape[0], crop.strides[0], QImage.Format.Format_Grayscale8)
        self._close = (qi, crop, box, result)

    def wheelEvent(self, e):
        d = e.angleDelta().y()
        if d and self.on_wheel:
            self.on_wheel(-1 if d > 0 else 1)
            e.accept()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("#d6dbe8"), 1))
        p.setBrush(QColor("#fbfcff"))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 8, 8)
        d = self.data
        if len(d):
            d = d[d[:, 0] >= d[-1, 0] - self.window_s]
        ok = d[:, 1] > 0 if len(d) else np.zeros(0, bool)
        H = self.height()
        side = H - 16
        x_left = 10
        if self._close is not None:                     # eye close-up first
            qi, _buf, (bx, by, bw, bh), pr = self._close
            cw = min(side * bw / max(bh, 1), side * 2.2)
            ch = cw * bh / max(bw, 1)
            cr = QRectF(x_left, 8 + (side - ch) / 2, cw, ch)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(cr, qi)
            if pr is not None and pr.found:
                k = cw / bw
                p.save()
                p.translate(cr.left() + (pr.x - bx) * k, cr.top() + (pr.y - by) * k)
                p.rotate(pr.angle)
                p.setPen(QPen(QColor("#2ee59d"), 1.6))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(0, 0), pr.minor / 2 * k, pr.diameter / 2 * k)
                p.restore()
            elif pr is not None:
                p.setPen(QColor("#ff4d6a"))
                p.drawText(cr, int(Qt.AlignmentFlag.AlignCenter), "not found")
            x_left = cr.right() + 10
        sq = QRectF(x_left, 8, side, side)
        p.setPen(QPen(QColor("#e6e9f3"), 1))
        p.setBrush(QColor("#ffffff"))
        p.drawRect(sq)
        if not ok.any():
            p.setPen(QColor("#8a93ad"))
            p.drawText(QRectF(0, 0, self.width(), H), int(Qt.AlignmentFlag.AlignCenter),
                       "No pupil yet" if len(d) else "Pupil tracking is starting...")
            p.end()
            return
        x, y, dia, t = d[:, 2], d[:, 3], d[:, 4], d[:, 0]
        # x-y trail, centred on the mean position, same scale on both axes
        cx, cy = np.nanmean(x[ok]), np.nanmean(y[ok])
        span = max(4.0, np.nanmax(np.abs(np.r_[x[ok] - cx, y[ok] - cy])) * 1.15)
        def to_sq(xx, yy):
            return QPointF(sq.center().x() + (xx - cx) / span * side / 2, sq.center().y() + (yy - cy) / span * side / 2)
        p.setPen(QPen(QColor("#e6e9f3"), 1, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(sq.center().x(), sq.top()), QPointF(sq.center().x(), sq.bottom()))
        p.drawLine(QPointF(sq.left(), sq.center().y()), QPointF(sq.right(), sq.center().y()))
        idx = np.flatnonzero(ok)
        n = len(idx)
        for k in range(1, n):
            i0, i1 = idx[k - 1], idx[k]
            if i1 - i0 > 3:
                continue
            alpha = int(40 + 215 * k / n)
            p.setPen(QPen(QColor(47, 70, 217, alpha), 1.2))
            p.drawLine(to_sq(x[i0], y[i0]), to_sq(x[i1], y[i1]))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ff4d6a"))
        p.drawEllipse(to_sq(x[idx[-1]], y[idx[-1]]), 3.5, 3.5)
        p.setPen(QColor("#8a93ad"))
        k_mm = self.px_per_mm
        p.drawText(QRectF(sq.left() + 3, sq.bottom() - 15, side, 14), int(Qt.AlignmentFlag.AlignLeft),
                   f"\u00b1{span / k_mm:.2f} mm" if k_mm else f"\u00b1{span:.0f} px")
        # traces
        left = sq.right() + 92
        right = self.width() - 10
        width = max(10, right - left)
        lanes = [("Horizontal (x)", x, "#2f46d9"), ("Vertical (y)", y, "#7a4cff"), ("Diameter", dia, "#2ee59d")]
        lane_h = (H - 16) / 3
        t1 = t[-1]
        for k, (name, v, colour) in enumerate(lanes):
            top = 8 + k * lane_h
            vv = v[ok]
            p.setPen(QColor("#5a6482"))
            p.drawText(QRectF(sq.right() + 8, top, 84, lane_h / 2), int(Qt.AlignmentFlag.AlignVCenter), name)
            p.setPen(QColor(colour))
            p.drawText(QRectF(sq.right() + 8, top + lane_h / 2 - 2, 84, lane_h / 2),
                       int(Qt.AlignmentFlag.AlignVCenter),
                       f"{v[idx[-1]] / k_mm:.3f} mm" if k_mm else f"{v[idx[-1]]:.1f} px")
            p.setPen(QPen(QColor("#e6e9f3"), 1))
            p.drawLine(QPointF(left, top + lane_h - 2), QPointF(right, top + lane_h - 2))
            lo_v, hi_v = np.nanmin(vv), np.nanmax(vv)
            pad = max(1.0, (hi_v - lo_v) * 0.1)
            lo_v, hi_v = lo_v - pad, hi_v + pad

            def pt(tt, val):
                return QPointF(right - (t1 - tt) / self.window_s * width,
                               top + 3 + (1 - (val - lo_v) / (hi_v - lo_v)) * (lane_h - 8))

            p.setPen(QPen(QColor(colour), 1.3))
            seg = []
            for i in range(len(t)):
                if ok[i]:
                    seg.append(pt(t[i], v[i]))
                elif seg:
                    p.drawPolyline(QPolygonF(seg)) if len(seg) > 1 else None
                    seg = []
            if len(seg) > 1:
                p.drawPolyline(QPolygonF(seg))
            # gaps (blinks, lost pupil) as a light band
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(214, 51, 74, 35))
            miss = np.flatnonzero(~ok)
            if len(miss):
                runs = np.split(miss, np.flatnonzero(np.diff(miss) > 1) + 1)
                for r_ in runs:
                    xa, xb = pt(t[r_[0]], lo_v).x(), pt(t[r_[-1]], lo_v).x()
                    p.drawRect(QRectF(xa, top + 2, max(1.5, xb - xa), lane_h - 6))
        p.setPen(QColor("#8a93ad"))
        p.drawText(QRectF(left, H - 16, width, 14), int(Qt.AlignmentFlag.AlignLeft), f"-{self.window_s:g} s")
        p.drawText(QRectF(left, H - 16, width, 14), int(Qt.AlignmentFlag.AlignRight), "now")
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
class MeasureCanvas(QWidget):
    """Still image where two points are clicked (and can be dragged); wheel zooms around the mouse."""

    def __init__(self, img: np.ndarray, parent=None):
        super().__init__(parent)
        self.setMinimumSize(560, 380)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.points = []                  # image coordinates (QPointF), at most 2
        self.zoom = 1.0
        self._drag = None
        self.on_change = None
        self.on_key = None                # arrow keys, for scrubbing
        self.message = ""
        self.set_image(img)
        self.center = QPointF(self._img.width() / 2, self._img.height() / 2)   # image point in the middle

    def set_image(self, img: np.ndarray) -> None:
        arr = np.ascontiguousarray(img)
        self._arr = arr
        fmt = QImage.Format.Format_Grayscale8 if arr.ndim == 2 else QImage.Format.Format_BGR888
        self._img = QImage(arr.data, arr.shape[1], arr.shape[0], arr.strides[0], fmt)
        self.update()

    def keyPressEvent(self, e):
        if self.on_key and e.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self.on_key(-1 if e.key() == Qt.Key.Key_Left else 1, bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier))
        else:
            super().keyPressEvent(e)

    def _scale(self) -> float:
        base = min(self.width() / self._img.width(), self.height() / self._img.height())
        return base * self.zoom

    def to_view(self, pt: QPointF) -> QPointF:
        s = self._scale()
        return QPointF(self.width() / 2 + (pt.x() - self.center.x()) * s,
                       self.height() / 2 + (pt.y() - self.center.y()) * s)

    def to_img(self, pos) -> QPointF:
        s = self._scale()
        x = self.center.x() + (pos.x() - self.width() / 2) / s
        y = self.center.y() + (pos.y() - self.height() / 2) / s
        return QPointF(min(max(x, 0), self._img.width() - 1), min(max(y, 0), self._img.height() - 1))

    def length_px(self) -> Optional[float]:
        if len(self.points) < 2:
            return None
        a, b = self.points
        return float(np.hypot(a.x() - b.x(), a.y() - b.y()))

    def _near(self, pos) -> Optional[int]:
        for i, pt in enumerate(self.points):
            v = self.to_view(pt)
            if abs(v.x() - pos.x()) < 9 and abs(v.y() - pos.y()) < 9:
                return i
        return None

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        i = self._near(e.position())
        if i is None:
            if len(self.points) >= 2:
                self.points = []
            self.points.append(self.to_img(e.position()))
            i = len(self.points) - 1
        self._drag = i
        self._changed()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.points[self._drag] = self.to_img(e.position())
            self._changed()
        else:
            self.setCursor(Qt.CursorShape.SizeAllCursor if self._near(e.position()) is not None
                           else Qt.CursorShape.CrossCursor)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def wheelEvent(self, e):
        d = e.angleDelta().y()
        if not d:
            return
        before = self.to_img(e.position())
        self.zoom = float(np.clip(self.zoom * (1.25 if d > 0 else 0.8), 1.0, 12.0))
        after = self.to_img(e.position())
        self.center = QPointF(self.center.x() + before.x() - after.x(), self.center.y() + before.y() - after.y())
        if self.zoom == 1.0:
            self.center = QPointF(self._img.width() / 2, self._img.height() / 2)
        self.update()

    def _changed(self):
        self.update()
        if self.on_change:
            self.on_change()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0b1226"))
        s = self._scale()
        tl = self.to_view(QPointF(0, 0))
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.zoom < 3)
        p.drawImage(QRectF(tl.x(), tl.y(), self._img.width() * s, self._img.height() * s), self._img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pts = [self.to_view(pt) for pt in self.points]
        if len(pts) == 2:
            p.setPen(QPen(QColor("#ffd23f"), 2))
            p.drawLine(pts[0], pts[1])
        for v in pts:
            p.setPen(QPen(QColor("#ffd23f"), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(v, 6, 6)
            p.drawLine(v + QPointF(-10, 0), v + QPointF(-3, 0))
            p.drawLine(v + QPointF(3, 0), v + QPointF(10, 0))
            p.drawLine(v + QPointF(0, -10), v + QPointF(0, -3))
            p.drawLine(v + QPointF(0, 3), v + QPointF(0, 10))
        p.setPen(QColor("#ffffff"))
        msg = self.message or ("Click both ends of something of known length (e.g. a ruler held where the eye is)."
                               if len(self.points) < 2 else "Drag the points to fine-tune. Click elsewhere to start again.")
        p.drawText(self.rect().adjusted(10, 8, -10, 0), int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft),
                   msg + "   Mouse wheel: zoom.")
        p.end()


def _cv2_imencode(img):
    import cv2
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])


def _cv2_imdecode(buf):
    import cv2
    return cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)


class CalibrationDialog(QDialog):
    """Set the image scale (pixels per mm) by measuring a known distance, or by typing it."""

    MAX_CLIP_S = 60

    def __init__(self, camera, current: Optional[float], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Calibrate: pixels per millimetre")
        self.resize(860, 700)
        self.camera = camera
        self.clip: list = []              # JPEG-compressed frames of the calibration clip
        self.clip_t: list = []
        self.recording = False
        self.frame_index: Optional[int] = None
        v = QVBoxLayout(self)
        steps = QLabel("1  Hold a ruler in front of the eye, at the eye's distance from the camera.   "
                       "2  Record a short clip.   3  Scrub to a sharp frame.   4  Click both ends of a "
                       "known distance and type it in.")
        steps.setWordWrap(True)
        steps.setStyleSheet("color: #5a6482;")
        v.addWidget(steps)
        first = camera.latest.image if camera is not None and camera.latest is not None else np.zeros((480, 640), np.uint8)
        self.canvas = MeasureCanvas(first)
        self.canvas.on_change = self._update
        self.canvas.on_key = self._step
        v.addWidget(self.canvas, 1)
        row = QHBoxLayout()
        self.rec_btn = QPushButton("\u25cf  Record clip")
        self.rec_btn.setObjectName("record")
        self.rec_btn.setStyleSheet("padding: 6px 14px; font-size: 13px;")
        self.rec_btn.clicked.connect(self._toggle_record)
        row.addWidget(self.rec_btn)
        self.live_btn = QPushButton("Live")
        self.live_btn.setToolTip("Back to the live picture")
        self.live_btn.clicked.connect(self._go_live)
        row.addWidget(self.live_btn)
        self.scrub = QSlider(Qt.Orientation.Horizontal)
        self.scrub.setEnabled(False)
        self.scrub.valueChanged.connect(self._show_frame)
        row.addWidget(self.scrub, 1)
        self.frame_label = QLabel("live")
        self.frame_label.setMinimumWidth(130)
        row.addWidget(self.frame_label)
        v.addLayout(row)
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        form = QFormLayout()
        self.mm_spin = QDoubleSpinBox()
        self.mm_spin.setRange(0.01, 1000)
        self.mm_spin.setDecimals(2)
        self.mm_spin.setValue(10.0)
        self.mm_spin.setSuffix(" mm")
        self.mm_spin.valueChanged.connect(self._update)
        form.addRow("Known distance between the points", self.mm_spin)
        self.direct = QDoubleSpinBox()
        self.direct.setRange(0, 100000)
        self.direct.setDecimals(3)
        self.direct.setSuffix(" px/mm")
        self.direct.setSpecialValueText("measure above")
        self.direct.setValue(current or 0)
        self.direct.valueChanged.connect(self._update)
        form.addRow("Or type the scale", self.direct)
        self.result = QLabel()
        form.addRow(self.result)
        v.addLayout(form)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        v.addWidget(self.buttons)
        self._typed = bool(current)
        self._update()

    # ---- clip ------------------------------------------------------------------
    def _tick(self) -> None:
        cam = self.camera
        if cam is None or cam.latest is None or self.frame_index is not None:
            return
        f = cam.latest
        self.canvas.set_image(f.image)
        if self.recording:
            if not self.clip_t or f.t != self.clip_t[-1]:
                ok, buf = _cv2_imencode(f.image)
                if ok:
                    self.clip.append(buf)
                    self.clip_t.append(f.t)
            dur = self.clip_t[-1] - self.clip_t[0] if len(self.clip_t) > 1 else 0.0
            self.frame_label.setText(f"recording {dur:4.1f} s")
            if dur >= self.MAX_CLIP_S:
                self._toggle_record()

    def _toggle_record(self) -> None:
        if self.camera is None:
            return
        if not self.recording:
            self.clip, self.clip_t = [], []
            self.frame_index = None
            self.recording = True
            self.rec_btn.setText("\u25a0  Stop")
            self.scrub.setEnabled(False)
            self.canvas.message = "Recording - hold the ruler steady in front of the eye, then click Stop."
        else:
            self.recording = False
            self.rec_btn.setText("\u25cf  Record clip")
            self.canvas.message = ""
            if self.clip:
                self.scrub.blockSignals(True)
                self.scrub.setRange(0, len(self.clip) - 1)
                self.scrub.setValue(len(self.clip) // 2)
                self.scrub.blockSignals(False)
                self.scrub.setEnabled(True)
                self._show_frame(self.scrub.value())
                self.canvas.setFocus()
        self.canvas.update()

    def _show_frame(self, i: int) -> None:
        if not self.clip:
            return
        img = _cv2_imdecode(self.clip[i])
        if img is None:
            return
        self.frame_index = i
        self.canvas.set_image(img)
        t = self.clip_t[i] - self.clip_t[0]
        self.frame_label.setText(f"frame {i + 1} / {len(self.clip)}  ({t:.2f} s)")

    def _step(self, step: int, big: bool) -> None:
        if self.scrub.isEnabled():
            self.scrub.setValue(self.scrub.value() + step * (10 if big else 1))

    def _go_live(self) -> None:
        if self.recording:
            return
        self.frame_index = None
        self.frame_label.setText("live")

    def done(self, r) -> None:
        self._timer.stop()
        self.clip = []
        super().done(r)

    def px_per_mm(self) -> Optional[float]:
        L = self.canvas.length_px()
        if L is not None and L >= 5:
            return L / self.mm_spin.value()
        if self.direct.value() > 0:
            return self.direct.value()
        return None

    def method(self) -> str:
        L = self.canvas.length_px()
        if L is not None and L >= 5:
            where = (f" on frame {self.frame_index + 1} of a {len(self.clip)}-frame calibration clip"
                     if self.frame_index is not None else " on the live picture")
            return f"measured: {L:.1f} px over {self.mm_spin.value():g} mm{where}"
        return "typed in"

    def _update(self, *_a):
        L = self.canvas.length_px()
        if L is not None and L >= 5:
            self.direct.blockSignals(True)
            self.direct.setValue(L / self.mm_spin.value())
            self.direct.blockSignals(False)
            self.result.setText(f"Line: {L:.1f} px = {self.mm_spin.value():g} mm  ->  "
                                f"<b>{L / self.mm_spin.value():.3f} px/mm</b>  ({self.mm_spin.value() / L * 1000:.1f} \u00b5m per pixel)")
        elif self.direct.value() > 0:
            self.result.setText(f"Scale: <b>{self.direct.value():.3f} px/mm</b>")
        else:
            self.result.setText("Click two points in the picture, or type the scale.")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.px_per_mm() is not None)


# --------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, settings: Optional[QSettings] = None):
        super().__init__()
        self.settings = settings if settings is not None else QSettings("jerjely", APP_NAME)
        self.probe_set = ProbeSet()
        self.probe_window = None
        self.probe_atlas_folder = None        # None = the per-user config folder
        self.camera: Optional[CameraThread] = None
        self.scale: Optional[dict] = None       # {"px_per_mm", "size", "set", "method"}
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
        self._apply_pupil()
        self._update_scale_label()
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
        self.scale_label = QLabel()
        self.scale_label.setWordWrap(True)
        self.calib_btn = QPushButton("Calibrate...")
        self.calib_btn.setToolTip("Measure a ruler held in front of the eye to get pupil size and position in mm")
        row = QHBoxLayout()
        row.addWidget(self.scale_label, 1)
        row.addWidget(self.calib_btn)
        form.addRow("Scale", row)
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
        self.flash_btn = QPushButton("Upload sketch")
        self.flash_btn.setToolTip("Put the SyncCam sketch on an Arduino Uno or Nano (replaces e.g. Firmata). "
                                  "No Arduino IDE needed.")
        row = QHBoxLayout()
        row.addWidget(self.ard_btn, 1)
        row.addWidget(self.flash_btn)
        form.addRow(row)
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
        self.blink_chk = QCheckBox("IR sync blinks at start and end")
        self.blink_chk.setChecked(True)
        self.blink_chk.setToolTip("With the IR LED on: 0.5 s after Record and just before Stop, the IR LED goes off "
                                  "for 0.1 s. From the dimmed frames and the Arduino-recorded LED moments SyncCam "
                                  "measures the camera delay and saves exposure-corrected frame times "
                                  "(time_exposure_s in frames.csv).")
        v.addWidget(self.blink_chk)
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
        self.probe_label = QLabel()
        self.probe_label.setStyleSheet("color: #4a5568;")
        self.probe_label.setToolTip("Probe plan saved into session.json (Tools > Probe...)")
        self.probe_label.setWordWrap(True)
        v.addWidget(self.probe_label)
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
        row = QHBoxLayout()
        row.addWidget(self.adjust_rec_chk)
        row.addSpacing(16)
        self.pupil_chk = QCheckBox("Track pupil")
        self.pupil_chk.setToolTip("Find the pupil inside the eye box in every frame. Drag a box around the "
                                  "eye in the picture. Pupil x, y and diameter are saved in frames.csv.")
        row.addWidget(self.pupil_chk)
        self.pupil_show_chk = QCheckBox("Show pupil view")
        self.pupil_show_chk.setChecked(True)
        self.pupil_show_chk.setToolTip("Hide or show the outline and the pupil plots. Tracking and saving "
                                       "carry on either way.")
        row.addWidget(self.pupil_show_chk)
        row.addWidget(QLabel("Threshold"))
        self.pupil_sens = QSlider(Qt.Orientation.Horizontal)
        self.pupil_sens.setRange(-50, 50)
        self.pupil_sens.setValue(0)
        self.pupil_sens.setMinimumWidth(60)
        self.pupil_sens.setToolTip("Move right if the outline is smaller than the pupil, left if it spills "
                                   "over the iris. The pixels counted as pupil show in red while you move it.")
        row.addWidget(self.pupil_sens, 1)
        v.addLayout(row)
        right.addWidget(box, 1)

        # pupil
        self.pupil_box = QGroupBox("Pupil")
        v = QVBoxLayout(self.pupil_box)
        self.pupil_plot = PupilPlot()
        self.pupil_plot.on_wheel = self._pupil_wheel
        v.addWidget(self.pupil_plot)
        self.pupil_state = QLabel()
        self.pupil_state.setTextFormat(Qt.TextFormat.RichText)
        self.pupil_state.setToolTip("Eye movement from the pupil centre's speed over the last 0.2 s (moving above "
                                    "20 px/s); pupil from its diameter trend over the last 1 s (dilating or "
                                    "constricting beyond 8 % per second). Saved per frame in frames.csv.")
        v.addWidget(self.pupil_state)
        self.pupil_box.setVisible(False)
        right.addWidget(self.pupil_box, 0)

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
        self.calib_btn.clicked.connect(self.calibrate)
        self.refresh_btn.clicked.connect(self.refresh_ports)
        self.ard_btn.clicked.connect(self._toggle_arduino)
        self.flash_btn.clicked.connect(self.upload_sketch)
        self.ir_btn.toggled.connect(self._ir_toggled)
        for combo in (self.ttl_pin, self.mic_pin, self.ir_pin):
            combo.currentIndexChanged.connect(self._pins_changed)
        self.thr_spin.valueChanged.connect(self._threshold_changed)
        self.grey_chk.toggled.connect(self._image_changed)
        self.auto_btn.clicked.connect(self._auto_image)
        self.reset_btn.clicked.connect(self._reset_image)
        self.window_combo.currentIndexChanged.connect(self._zoom_changed)
        self.pupil_chk.toggled.connect(self._pupil_toggled)
        self.pupil_show_chk.toggled.connect(self._pupil_view_changed)
        self.pupil_sens.valueChanged.connect(self._pupil_params_changed)
        self.view.on_eye_box = self._eye_box_drawn
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
        am = self.menuBar().addMenu("&Arduino")
        a = QAction("Upload SyncCam sketch to board (Uno / Nano)...", self)
        a.triggered.connect(self.upload_sketch)
        am.addAction(a)
        a = QAction("Save sketch (.ino) for the Arduino IDE...", self)
        a.triggered.connect(self._save_sketch)
        am.addAction(a)
        tm = self.menuBar().addMenu("&Tools")
        a = QAction("Probe...", self)
        a.setToolTip("Angled Neuropixels insertion planner (one or two probes) with an atlas view")
        a.triggered.connect(self.show_probe_planner)
        tm.addAction(a)
        h = self.menuBar().addMenu("&Help")
        a = QAction(f"About {APP_NAME}", self)
        a.setMenuRole(QAction.MenuRole.AboutRole)
        a.triggered.connect(self.show_about)
        h.addAction(a)

    # ---- settings ----------------------------------------------------------
    # ---- probe planner -----------------------------------------------------
    def show_probe_planner(self):
        if self.probe_window is None:
            from .probe_gui import ProbePlannerWindow
            self.probe_window = ProbePlannerWindow(self.probe_set, APP_NAME, self,
                                                   atlas_folder=self.probe_atlas_folder)
            self.probe_window.changed.connect(self._probe_changed)
        self.probe_window.show()
        self.probe_window.raise_()
        self.probe_window.activateWindow()
        return self.probe_window

    def _probe_atlas(self):
        return self.probe_window.atlas if self.probe_window is not None else None

    def _probe_changed(self) -> None:
        if self.probe_window is not None:
            self.probe_set = self.probe_window.pset
        text = "\n".join("Probe " + p.summary() for p in self.probe_set.used)
        ca = self.probe_set.closest_approach()
        if ca is not None:
            text += f"  ·  closest A–B {ca:.0f} µm"
        self.probe_label.setText(text)
        self.probe_label.setToolTip(text + "\nSaved into session.json at every recording (Tools > Probe...)")
        self.settings.setValue("probe_plan", self.probe_set.to_json())

    def _restore_settings(self) -> None:
        s = self.settings
        self.probe_set = ProbeSet.from_json(str(s.value("probe_plan", "") or ""))
        if self.probe_window is not None:
            self.probe_window.set_probe_set(self.probe_set)
        self._probe_changed()

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
        self.blink_chk.setChecked(str(s.value("ir_blinks", "true")).lower() == "true")
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
        try:
            import json as _json
            sc = s.value("scale", "")
            self.scale = _json.loads(sc) if sc else None
        except Exception:
            self.scale = None
        box = s.value("eye_box")
        if box:
            try:
                self.view.eye_box = tuple(int(v) for v in str(box).split(","))
            except ValueError:
                pass
        try:
            self.pupil_sens.setValue(int(s.value("pupil_sensitivity", 0)))
        except (TypeError, ValueError):
            pass
        self.pupil_show_chk.setChecked(str(s.value("pupil_show", "true")).lower() == "true")
        self.pupil_chk.setChecked(str(s.value("pupil_track", "false")).lower() == "true")
        pick(self.mic_combo, "scope_mic")
        geo = s.value("geometry")
        if geo is not None:
            try:
                self.restoreGeometry(geo)
            except Exception:
                pass

    def _save_settings(self) -> None:
        s = self.settings
        s.setValue("probe_plan", self.probe_set.to_json())
        s.setValue("resolution", str(self.res_combo.currentData()))
        s.setValue("codec", self.codec_combo.currentData())
        s.setValue("rate", self.rate_combo.currentData())
        s.setValue("ttl_pin", self.ttl_pin.currentData())
        s.setValue("mic_pin", self.mic_pin.currentData())
        s.setValue("ir_pin", self.ir_pin.currentData())
        s.setValue("ir_on", self.ir_btn.isChecked())
        s.setValue("ir_blinks", self.blink_chk.isChecked())
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
        import json as _json
        s.setValue("scale", _json.dumps(self.scale) if self.scale else "")
        s.setValue("eye_box", ",".join(map(str, self.view.eye_box)) if self.view.eye_box else "")
        s.setValue("pupil_sensitivity", self.pupil_sens.value())
        s.setValue("pupil_show", self.pupil_show_chk.isChecked())
        s.setValue("pupil_track", self.pupil_chk.isChecked())
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

    def _led_fn(self, t=None):
        """IR LED state for the test-pattern camera (at computer time t, if given)."""
        st = self.stream
        if st is not None and isinstance(st.device, SimulatedArduino):
            return st.device.led_at(t) if t is not None else st.device.current_led()
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
        self._apply_pupil()
        self._update_scale_label()
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

    # ---- calibration ---------------------------------------------------------
    def _valid_scale(self) -> Optional[dict]:
        """The saved scale, if it was measured at the current camera picture size."""
        sc = self.scale
        if not sc:
            return None
        if self.camera is not None and list(self.camera.frame_size) != list(sc.get("size", [])):
            return None
        return sc

    def _update_scale_label(self) -> None:
        sc = self.scale
        if not sc:
            self.scale_label.setText('<span style="color:#8a93ad">not set (sizes in pixels)</span>')
        elif self.camera is not None and self._valid_scale() is None:
            w, h = sc["size"]
            self.scale_label.setText(f'<span style="color:#a32035">measured at {w} x {h}: calibrate again '
                                     f'for this picture size</span>')
        else:
            w, h = sc["size"]
            self.scale_label.setText(f'{sc["px_per_mm"]:.2f} px/mm <span style="color:#8a93ad">'
                                     f'({w} x {h}, {sc["set"][:10]})</span>')
        vs = self._valid_scale()
        self.pupil_plot.px_per_mm = vs["px_per_mm"] if vs else None

    def calibrate(self) -> bool:
        if self.camera is None:
            QMessageBox.information(self, "Calibrate", "Open the camera first.")
            return False
        vs = self._valid_scale()
        dlg = CalibrationDialog(self.camera, vs["px_per_mm"] if vs else None, self)
        self._calib_dialog = dlg                      # for the self-test
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        return self.apply_calibration(dlg)

    def apply_calibration(self, dlg) -> bool:
        if dlg.px_per_mm() is None or self.camera is None:
            return False
        from datetime import datetime as _dt
        self.scale = {"px_per_mm": round(float(dlg.px_per_mm()), 4), "size": list(self.camera.frame_size),
                      "set": _dt.now().astimezone().isoformat(timespec="seconds"), "method": dlg.method()}
        self._update_scale_label()
        return True

    # ---- pupil tracking ------------------------------------------------------
    def _pupil_params(self) -> PupilParams:
        box = self.view.eye_box
        if box and self.camera is not None:
            W, H = self.camera.frame_size
            x, y, w, h = box
            if x >= W or y >= H:
                box = None                  # box from a camera with a larger picture
            else:
                box = (x, y, min(w, W - x), min(h, H - y))
        return PupilParams(box=box, sensitivity=self.pupil_sens.value())

    def _apply_pupil(self) -> None:
        cam = self.camera
        on = self.pupil_chk.isChecked()
        self.view.drawing_enabled = on
        if cam is not None:
            if on:
                if cam.pupil_tracker is None:
                    cam.pupil_log.clear()
                    cam.pupil_kin = Kinematics()
                    cam.pupil_tracker = PupilTracker(self._pupil_params())
                else:
                    cam.pupil_tracker.set_params(self._pupil_params())
            else:
                cam.pupil_tracker = None
                cam.pupil_kin = None
        self._pupil_view_changed()

    def _pupil_toggled(self, *_a) -> None:
        self._apply_pupil()

    def _pupil_view_changed(self, *_a) -> None:
        on = self.pupil_chk.isChecked()
        show = self.pupil_show_chk.isChecked()
        self.view.show_pupil = show
        self.pupil_box.setVisible(on and show)
        self.pupil_sens.setEnabled(on)
        self.pupil_show_chk.setEnabled(on)
        self.view.update()

    def _eye_box_drawn(self, box) -> None:
        if self.camera is not None:
            self.camera.pupil_log.clear()
            if self.camera.pupil_kin is not None:
                self.camera.pupil_kin = Kinematics()
        self._pupil_params_changed()

    def _pupil_params_changed(self, *_a) -> None:
        cam = self.camera
        if cam is not None and cam.pupil_tracker is not None:
            cam.pupil_tracker.set_params(self._pupil_params())
            cam.pupil_want_mask = True                   # show the red pixels while tuning
            self._mask_until = time.time() + 2.5
        if self.recording is not None:
            self.recording.pupil_changed = True

    def _kin_text(self, kin) -> str:
        if not kin:
            return '<span style="color:#8a93ad">Eye: -    Pupil: -</span>'
        k_mm = self.pupil_plot.px_per_mm
        mv, st = kin.get("moving", ""), kin.get("size", "")
        sp, dd = kin.get("speed", float("nan")), kin.get("dd_pct", float("nan"))
        if mv == "lost":
            return '<span style="color:#a32035"><b>Pupil not found</b> (blink, closed eye or out of the box)</span>'
        if mv in ("", "still"):
            eye = '<span style="color:#1a7a3a"><b>Eye still</b></span>' if mv else "Eye: ..."
        else:
            speed = (f"{sp / k_mm:.2f} mm/s" if k_mm else f"{sp:.0f} px/s") if np.isfinite(sp) else ""
            eye = f'<span style="color:#2f46d9"><b>Eye moving {mv}</b></span> ({speed})'
        colours = {"dilating": "#d98a00", "constricting": "#7a4cff", "stable": "#1a7a3a"}
        if st in colours:
            rate = f" ({dd:+.1f} %/s)" if np.isfinite(dd) else ""
            pupil = f'<span style="color:{colours[st]}"><b>Pupil {st}</b></span>{rate}'
        else:
            pupil = "Pupil: ..."
        return f"{eye}&nbsp;&nbsp;&nbsp;&nbsp;{pupil}"

    def _pupil_wheel(self, step: int) -> None:
        w = PupilPlot.WINDOWS
        i = min(max(w.index(self.pupil_plot.window_s) + step if self.pupil_plot.window_s in w else 1, 0), len(w) - 1)
        self.pupil_plot.window_s = w[i]
        self.pupil_plot.update()

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

    # ---- putting the sketch on the board --------------------------------------
    def upload_sketch(self, ask: bool = True) -> bool:
        import threading
        from .uploader import UploadError, firmware_matches_sketch, upload
        port = self.port_combo.currentData()
        if self.stream is not None:
            QMessageBox.information(self, "Upload sketch", "Disconnect from the Arduino first.")
            return False
        if not port or port == SIM_PORT:
            QMessageBox.information(self, "Upload sketch", "Choose the Arduino's port first "
                                    "(plug it in and click Refresh if it is not listed).")
            return False
        if ask:
            ans = QMessageBox.question(
                self, "Upload sketch",
                f"Put the SyncCam sketch on the Arduino at {port}?\n\n"
                "This replaces what is on the board now (for example Firmata for Bonsai). "
                "Works with the Arduino Uno and Nano. Close Bonsai, the Arduino IDE and any "
                "other program using this port first.")
            if ans != QMessageBox.StandardButton.Yes:
                return False
        if not firmware_matches_sketch():
            QMessageBox.warning(self, "Upload sketch", "The built-in firmware does not match the built-in "
                                "sketch. Please report this; meanwhile use the .ino with the Arduino IDE.")
            return False
        dlg = QProgressDialog("Resetting the board...", None, 0, 1000, self)
        dlg.setWindowTitle("Upload sketch")
        dlg.setMinimumDuration(0)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setValue(0)
        state = {"frac": 0.0, "msg": "", "result": None, "error": None}

        def work():
            try:
                state["result"] = upload(port, progress=lambda f, m: state.update(frac=f, msg=m))
            except UploadError as exc:
                state["error"] = str(exc)
            except Exception as exc:                    # serial port busy, unplugged, ...
                state["error"] = f"{type(exc).__name__}: {exc}"

        th = threading.Thread(target=work, name="synccam-upload", daemon=True)
        th.start()
        while th.is_alive():
            dlg.setValue(int(state["frac"] * 1000))
            if state["msg"]:
                dlg.setLabelText(state["msg"])
            QApplication.processEvents()
            time.sleep(0.02)
        dlg.close()
        if state["error"]:
            QMessageBox.critical(self, "Upload failed", state["error"])
            return False
        r = state["result"]
        self.ard_status.setText(f'<span style="color:#1a9a4a">&#9679;</span> Sketch uploaded '
                                f'({r["bytes"]} bytes, {r["chip"]}, checked).')
        if ask:
            ans = QMessageBox.question(self, "Upload sketch", "The SyncCam sketch is on the board and was "
                                       "checked. Connect now?")
            if ans == QMessageBox.StandardButton.Yes:
                time.sleep(0.5)
                self.connect_arduino()
        return True

    def _save_sketch(self) -> None:
        import shutil
        from .uploader import INO_NAME, firmware_path
        d = QFileDialog.getExistingDirectory(self, "Save the sketch into this folder",
                                             str(Path.home() / "Documents"))
        if not d:
            return
        folder = Path(d) / "SyncCam_Arduino"           # the Arduino IDE wants folder name = sketch name
        try:
            folder.mkdir(exist_ok=True)
            shutil.copy(firmware_path(INO_NAME), folder / INO_NAME)
        except OSError as exc:
            QMessageBox.critical(self, "Cannot save", str(exc))
            return
        QMessageBox.information(self, "Sketch saved", f"Saved {folder / INO_NAME}.\n\nOpen it in the Arduino "
                                "IDE, choose your board and port, and click Upload.")

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
            extra["probe"] = self.probe_set.session_block(self._probe_atlas())
        except Exception as exc:                      # never block a recording because of the plan
            extra["probe"] = {"error": str(exc)}
        try:
            Path(self.dir_edit.text()).mkdir(parents=True, exist_ok=True)
            rec = Recording(self.dir_edit.text(), self.name_edit.text(), self.camera, self.stream,
                            self.codec_combo.currentData(), self._threshold_adc(), extra,
                            adjust_video=self.adjust_rec_chk.isChecked(), scale=self._valid_scale(),
                            ir_blinks=(self.blink_chk.isChecked() and self.ir_btn.isChecked()
                                       and self.camera is not None and self.stream is not None))
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
        if rec.ir_blinks:
            self.rec_status.setText("Finishing: IR sync blink...")
            QApplication.processEvents()
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
        cd = info.get("camera_delay") or {}
        if cd.get("measured"):
            bits.append(f"Camera delay {cd['delay_ms']:.0f} ms (spread {cd['delay_spread_ms']:.0f} ms)")
        elif rec.ir_blinks and cd.get("reason"):
            bits.append(f"Camera delay not measured: {cd['reason']}")
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
                self.view.pupil = f.pupil
                if cam.pupil_want_mask and time.time() > getattr(self, "_mask_until", 0):
                    cam.pupil_want_mask = False
                self.view.mask = f.pupil.mask if (f.pupil is not None and cam.pupil_want_mask) else None
                if cam.pupil_tracker is not None and self.pupil_box.isVisible():
                    self.pupil_state.setText(self._kin_text(f.kin))
                    self.pupil_plot.set_closeup(f.raw if f.raw is not None else f.image,
                                                cam.pupil_tracker.params.box, f.pupil)
                    self.pupil_plot.set_data(list(cam.pupil_log))
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
        self.calib_btn.setEnabled(cam and not rec)
        self.cam_btn.setEnabled(not rec)
        if not cam:
            self.cam_status.setText('<span style="color:#8a93ad">&#9679;</span> No camera open')
        for w in (self.port_combo, self.refresh_btn, self.rate_combo, self.ttl_pin, self.mic_pin, self.ir_pin):
            w.setEnabled(not ard)
        self.ard_btn.setText("Disconnect" if ard else "Connect")
        self.ard_btn.setEnabled(not rec)
        self.flash_btn.setEnabled(not ard and not rec)
        if not ard:
            self.ard_status.setText('<span style="color:#8a93ad">&#9679;</span> Not connected')
            self.sig_label.setText("")
        self.thr_spin.setEnabled(not rec)
        self.blink_chk.setEnabled(not rec)
        self.grey_chk.setEnabled(not rec)
        self.pupil_chk.setEnabled(not rec)
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
