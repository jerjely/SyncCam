"""Probe planner window: angled insertion calculator for one or two Neuropixels probes plus a simple
Pinpoint-style coronal / sagittal / top view, optionally over the Allen CCF atlas.

Self-contained (PySide6 + probe.py only), so it can be dropped into other apps.
"""
from __future__ import annotations

import math
import threading
from typing import List, Optional

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QGridLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
                               QScrollArea, QSizePolicy, QSpinBox, QTabBar, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .probe import (CUSTOM, PROBE_TYPES, TOUCH_WARNING_UM, Atlas, ProbePlan, ProbeSet, atlas_dir)

BG = QColor("#0b1224")
GOLD = QColor("#e8b931")
TARGET = QColor("#ff5577")
COLORS = [["#1f9fd6", "#2e9e6a", "#d08a12", "#c8463f"],      # probe A shanks
          ["#7b5cd6", "#16a3a3", "#b0487a", "#5d6b80"]]      # probe B shanks


def shank_color(probe: int, shank: int) -> QColor:
    c = COLORS[probe % 2]
    return QColor(c[shank % len(c)])


# ====================================================================== views
class ProbeView(QWidget):
    """One projection: 'coronal' (ML, DV), 'sagittal' (-AP, DV) or 'top' (ML, -AP)."""
    TITLES = {"coronal": "Coronal", "sagittal": "Sagittal", "top": "Top view"}
    LEGENDS = {"coronal": "ML (right +) / DV", "sagittal": "AP (anterior ←) / DV",
               "top": "ML (right +) / AP (anterior ↑)"}

    def __init__(self, mode: str, parent=None):
        super().__init__(parent)
        self.mode = mode
        self.pset: Optional[ProbeSet] = None
        self.atlas: Optional[Atlas] = None
        self._slice_key = None
        self._slice_img: Optional[QImage] = None
        self._slice_rect: Optional[QRectF] = None      # in (h, v) world coordinates
        self._slice_label = ""
        self.setMinimumSize(220, 170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_data(self, pset: ProbeSet, atlas: Optional[Atlas]) -> None:
        self.pset, self.atlas = pset, atlas
        self._update_slice()
        self.update()

    # ------------------------------------------------------------------ mapping
    def _hv(self, p) -> tuple:
        ap, ml, dv = float(p[0]), float(p[1]), float(p[2])
        if self.mode == "coronal":
            return ml, dv
        if self.mode == "sagittal":
            return -ap, dv
        return ml, -ap

    def _update_slice(self) -> None:
        if self.atlas is None or self.pset is None or self.mode == "top":
            self._slice_key = self._slice_img = self._slice_rect = None
            return
        plan = self.pset.probes[self.pset.active]
        tips = np.array(plan.tips())
        if self.mode == "coronal":
            val = float(tips[:, 0].mean())
        else:
            val = float(tips[:, 1].mean())
        key = (self.mode, self.atlas.slice_index(self.mode, val), id(self.atlas))
        if key == self._slice_key:
            return
        if self.mode == "coronal":
            sl, ext = self.atlas.coronal(val)
            rect = QRectF(QPointF(ext[0], ext[2]), QPointF(ext[1], ext[3]))
            self._slice_label = f"slice AP {val:.0f} µm"
        else:
            sl, ext = self.atlas.sagittal(val)
            rect = QRectF(QPointF(-ext[0], ext[2]), QPointF(-ext[1], ext[3]))
            self._slice_label = f"slice ML {val:.0f} µm"
        rgb = np.ascontiguousarray(self.atlas.rgb_slice(sl))
        h, w = rgb.shape[:2]
        self._slice_img = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
        self._slice_rect = rect.normalized()
        self._slice_key = key

    def _range(self):
        pts = [(0.0, 0.0)] if self.mode == "top" else [(0.0, 0.0)]
        if self.pset is not None:
            for plan in self.pset.used:
                for e, t in plan.shank_segments():
                    pts += [self._hv(e), self._hv(t)]
                pts.append(self._hv(plan.target()))
        hs, vs = [p[0] for p in pts], [p[1] for p in pts]
        h0, h1, v0, v1 = min(hs) - 1000, max(hs) + 1000, min(vs) - 1000, max(vs) + 1000
        if h1 - h0 < 4000:
            c = (h0 + h1) / 2
            h0, h1 = c - 2000, c + 2000
        if v1 - v0 < 3000:
            c = (v0 + v1) / 2
            v0, v1 = c - 1500, c + 1500
        return h0, h1, v0, v1

    # ------------------------------------------------------------------ painting
    def paintEvent(self, _ev) -> None:
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.fillRect(self.rect(), BG)
        W, H = self.width(), self.height()
        h0, h1, v0, v1 = self._range()
        s = min((W - 16) / (h1 - h0), (H - 16) / (v1 - v0))           # px per um, equal scale
        ch, cv = (h0 + h1) / 2, (v0 + v1) / 2

        def X(h): return W / 2 + (h - ch) * s
        def Y(v): return H / 2 + (v - cv) * s
        def P(p):
            h, v = self._hv(p)
            return QPointF(X(h), Y(v))

        vh0, vh1 = ch - W / 2 / s, ch + W / 2 / s
        vv0, vv1 = cv - H / 2 / s, cv + H / 2 / s
        if self._slice_img is not None and self._slice_rect is not None:
            r = self._slice_rect
            qp.drawImage(QRectF(QPointF(X(r.left()), Y(r.top())), QPointF(X(r.right()), Y(r.bottom()))),
                         self._slice_img)
        # 1 mm grid
        qp.setPen(QPen(QColor(255, 255, 255, 28), 1))
        for g in range(int(math.floor(vh0 / 1000)), int(math.ceil(vh1 / 1000)) + 1):
            qp.drawLine(QPointF(X(g * 1000), 0), QPointF(X(g * 1000), H))
        for g in range(int(math.floor(vv0 / 1000)), int(math.ceil(vv1 / 1000)) + 1):
            qp.drawLine(QPointF(0, Y(g * 1000)), QPointF(W, Y(g * 1000)))
        # bregma
        if self.mode != "top":
            pen = QPen(GOLD, 1, Qt.PenStyle.DashLine)
            qp.setPen(pen)
            qp.drawLine(QPointF(0, Y(0)), QPointF(W, Y(0)))
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(GOLD)
        qp.drawEllipse(QPointF(X(0), Y(0)), 4, 4)

        if self.pset is not None:
            order = [i for i in range(self.pset.n) if i != self.pset.active] + [self.pset.active]
            for pi in order:
                self._draw_probe(qp, pi, P, s)
        # title + legend
        qp.setPen(QColor("#e6ecf5"))
        f = QFont(self.font())
        f.setBold(True)
        qp.setFont(f)
        title = self.TITLES[self.mode]
        if self._slice_label:
            title += "  ·  " + self._slice_label
        qp.drawText(QPointF(8, 16), title)
        f.setBold(False)
        f.setPointSizeF(max(6.5, f.pointSizeF() - 1.5))
        qp.setFont(f)
        qp.setPen(QColor("#9fb0c8"))
        leg = f"{self.LEGENDS[self.mode]} · grid 1 mm · ● bregma"
        tw = qp.fontMetrics().horizontalAdvance(leg)
        qp.drawText(QPointF(W - tw - 8, H - 7), leg)
        qp.end()

    def _draw_probe(self, qp: QPainter, pi: int, P, s: float) -> None:
        plan = self.pset.probes[pi]
        alpha = 255 if pi == self.pset.active or self.pset.n == 1 else 120
        d = plan.direction()
        for i, (entry, tip) in enumerate(plan.shank_segments()):
            col = shank_color(pi, i)
            col.setAlpha(alpha)
            light = QColor(220, 230, 245, int(alpha * 0.7))
            qp.setPen(QPen(light, 1))
            qp.drawLine(P(entry - 600 * d), P(tip))
            b, t = plan.span(i)
            qp.setPen(QPen(col, 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            qp.drawLine(P(b), P(t))
            qp.setPen(Qt.PenStyle.NoPen)
            qp.setBrush(col)
            qp.drawEllipse(P(tip), 3, 3)
        # entry marker + name
        e = P(plan.entry())
        qp.setPen(QPen(QColor(255, 255, 255, alpha), 1.5))
        qp.setBrush(Qt.BrushStyle.NoBrush)
        qp.drawEllipse(e, 5, 5)
        f = QFont(self.font())
        f.setBold(True)
        qp.setFont(f)
        qp.setPen(QColor(255, 255, 255, alpha))
        qp.drawText(e + QPointF(8, -6), plan.name)
        # target
        t = P(plan.target())
        tc = QColor(TARGET)
        tc.setAlpha(alpha)
        qp.setPen(QPen(tc, 2))
        qp.drawLine(t + QPointF(-5, -5), t + QPointF(5, 5))
        qp.drawLine(t + QPointF(-5, 5), t + QPointF(5, -5))


# ====================================================================== atlas loading
class _AtlasSignals(QObject):
    progress = Signal(str, float)
    loaded = Signal(object)
    failed = Signal(str)


# ====================================================================== window
class ProbePlannerWindow(QWidget):
    """Non-modal planner window. `changed` is emitted whenever the plan changes."""
    changed = Signal()

    def __init__(self, probe_set: Optional[ProbeSet] = None, app_name: str = "SyncCam", parent=None,
                 auto_load_atlas: bool = True, atlas_folder: Optional[str] = None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Probe planner")
        self.pset = probe_set if probe_set is not None else ProbeSet()
        self.atlas: Optional[Atlas] = None
        self.atlas_folder = atlas_folder or atlas_dir(app_name)
        self._loading = False
        self._busy = False
        self._sig = _AtlasSignals()
        self._sig.progress.connect(self._on_progress)
        self._sig.loaded.connect(self._on_loaded)
        self._sig.failed.connect(self._on_failed)
        self._build()
        self._load_fields()
        self.refresh()
        self.resize(1250, 800)
        if auto_load_atlas and Atlas.available(self.atlas_folder):
            self._start_atlas_thread(download=False)

    # ------------------------------------------------------------------ build
    def _spin(self, lo, hi, suffix=" µm", dec=1, step=50.0) -> QDoubleSpinBox:
        sp = QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setDecimals(dec)
        sp.setSingleStep(step)
        sp.setSuffix(suffix)
        sp.setKeyboardTracking(False)
        sp.valueChanged.connect(self._on_edit)
        return sp

    def _build(self) -> None:
        root = QHBoxLayout(self)
        # ---------------- left column
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 6, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Probes:"))
        self.n_group = QButtonGroup(self)
        self.n_btns = []
        for n in (1, 2):
            b = QPushButton(str(n))
            b.setCheckable(True)
            b.setFixedWidth(40)
            b.setStyleSheet("QPushButton:checked { background: #3b4fd8; color: white; border: 1px solid #2f3fb0;"
                            " font-weight: bold; }")
            self.n_group.addButton(b, n)
            self.n_btns.append(b)
            row.addWidget(b)
        row.addStretch(1)
        lv.addLayout(row)
        self.n_group.idClicked.connect(self._on_n)
        self.tabs = QTabBar()
        self.tabs.addTab("Probe A")
        self.tabs.addTab("Probe B")
        self.tabs.currentChanged.connect(self._on_tab)
        lv.addWidget(self.tabs)

        g = QGroupBox("Insertion")
        f = QFormLayout(g)
        self.f_ap = self._spin(-9000, 6000)
        self.f_ml = self._spin(-6000, 6000)
        self.f_sdv = self._spin(-2000, 8000)
        self.f_pitch = self._spin(0, 90, "°", 1, 1.0)
        self.f_yaw = self._spin(0, 359.9, "°", 1, 5.0)
        self.f_yaw.setWrapping(True)
        self.f_roll = self._spin(-180, 180, "°", 1, 5.0)
        self.f_dist = self._spin(0, 12000, " µm", 1, 10.0)
        bold = QFont(self.f_dist.font())
        bold.setBold(True)
        self.f_dist.setFont(bold)
        f.addRow("Entry AP (+ anterior)", self.f_ap)
        f.addRow("Entry ML (+ right)", self.f_ml)
        f.addRow("Brain surface DV at entry", self.f_sdv)
        f.addRow("Angle from horizontal", self.f_pitch)
        f.addRow("Direction (top view)", self.f_yaw)
        f.addRow("Shank plane roll", self.f_roll)
        lab = QLabel("Distance travelled")
        lab.setFont(bold)
        f.addRow(lab, self.f_dist)
        self.f_yaw.setToolTip("Which way the tip travels seen from above: 0 anterior, 90 right, 180 posterior, 270 left")
        self.f_pitch.setToolTip("90 = straight down")
        self.f_dist.setToolTip("Manipulator readout: distance travelled along the probe from the brain surface")
        lv.addWidget(g)

        self.results = QLabel()
        self.results.setTextFormat(Qt.TextFormat.RichText)
        self.results.setWordWrap(True)
        self.results.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.results.setStyleSheet("QLabel { background: white; color: #1b2433; border: 1px solid #d0d7e2;"
                                   " border-radius: 6px; padding: 8px; }")
        self.results.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lv.addWidget(self.results)

        g = QGroupBox("Plan to a target")
        f = QFormLayout(g)
        self.t_ap = self._spin(-9000, 6000)
        self.t_ml = self._spin(-6000, 6000)
        self.t_dv = self._spin(-2000, 8000)
        f.addRow("Target AP", self.t_ap)
        f.addRow("Target ML", self.t_ml)
        f.addRow("Target DV", self.t_dv)
        self.target_btn = QPushButton("Set entry + distance for this target")
        self.target_btn.clicked.connect(self.plan_to_target)
        f.addRow(self.target_btn)
        self.target_msg = QLabel("")
        self.target_msg.setWordWrap(True)
        self.target_msg.setStyleSheet("color: #b3261e;")
        f.addRow(self.target_msg)
        lv.addWidget(g)

        g = QGroupBox("Probe")
        f = QFormLayout(g)
        self.f_type = QComboBox()
        for name in list(PROBE_TYPES) + [CUSTOM]:
            self.f_type.addItem(name)
        self.f_type.setToolTip("Presets fill the dimensions below - check the probe datasheet")
        self.f_type.currentTextChanged.connect(self._on_type)
        self.f_nsh = QSpinBox()
        self.f_nsh.setRange(1, 8)
        self.f_nsh.setKeyboardTracking(False)
        self.f_nsh.valueChanged.connect(self._on_edit)
        self.f_spacing = self._spin(0, 2000, step=10.0)
        self.f_tipl = self._spin(0, 2000, step=5.0)
        self.f_sstart = self._spin(0, 10000, step=20.0)
        self.f_slen = self._spin(0, 10000, step=20.0)
        for w in (self.f_nsh, self.f_spacing, self.f_tipl, self.f_slen):
            w.setToolTip("Check the probe datasheet")
        f.addRow("Type", self.f_type)
        f.addRow("Shanks", self.f_nsh)
        f.addRow("Shank spacing", self.f_spacing)
        f.addRow("Tip to lowest site", self.f_tipl)
        f.addRow("Span start (above lowest site)", self.f_sstart)
        f.addRow("Span length", self.f_slen)
        lv.addWidget(g)
        lv.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMaximumWidth(470)
        scroll.setMinimumWidth(380)
        root.addWidget(scroll, 0)

        # ---------------- right
        right = QVBoxLayout()
        row = QHBoxLayout()
        self.atlas_btn = QPushButton("Load Allen atlas")
        self.atlas_btn.clicked.connect(self.load_atlas)
        row.addWidget(self.atlas_btn)
        self.atlas_status = QLabel("No atlas loaded - the views show coordinates only.")
        self.atlas_status.setWordWrap(True)
        self.atlas_status.setStyleSheet("color: #4a5568;")
        row.addWidget(self.atlas_status, 1)
        right.addLayout(row)
        grid = QGridLayout()
        self.views = {m: ProbeView(m) for m in ("coronal", "sagittal", "top")}
        grid.addWidget(self.views["coronal"], 0, 0)
        grid.addWidget(self.views["sagittal"], 0, 1)
        grid.addWidget(self.views["top"], 1, 0)
        tbox = QWidget()
        tv = QVBoxLayout(tbox)
        tv.setContentsMargins(0, 0, 0, 0)
        self.table_title = QLabel()
        tv.addWidget(self.table_title)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Distance (µm)", "Horizontal (µm)", "Depth (µm)"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tv.addWidget(self.table)
        grid.addWidget(tbox, 1, 1)
        for i in (0, 1):
            grid.setRowStretch(i, 1)
            grid.setColumnStretch(i, 1)
        right.addLayout(grid, 1)
        root.addLayout(right, 1)

    # ------------------------------------------------------------------ fields <-> plan
    @property
    def plan(self) -> ProbePlan:
        return self.pset.probes[self.pset.active]

    def _fields(self):
        return [(self.f_ap, "entry_ap"), (self.f_ml, "entry_ml"), (self.f_sdv, "surface_dv"),
                (self.f_pitch, "pitch"), (self.f_yaw, "yaw"), (self.f_roll, "roll"), (self.f_dist, "distance"),
                (self.t_ap, "target_ap"), (self.t_ml, "target_ml"), (self.t_dv, "target_dv"),
                (self.f_nsh, "n_shanks"), (self.f_spacing, "shank_pitch"), (self.f_tipl, "tip_length"),
                (self.f_sstart, "span_start"), (self.f_slen, "span_length")]

    def _load_fields(self) -> None:
        self._busy = True
        try:
            for b in self.n_btns:
                b.setChecked(self.n_group.id(b) == self.pset.n)
            self.tabs.setTabEnabled(1, self.pset.n == 2)
            self.tabs.setCurrentIndex(self.pset.active)
            p = self.plan
            for w, k in self._fields():
                w.setValue(getattr(p, k))
            t = p.probe_type if p.probe_type in PROBE_TYPES else p.matching_type()
            self.f_type.setCurrentText(t)
        finally:
            self._busy = False

    def _on_edit(self, *_):
        if self._busy:
            return
        p = self.plan
        for w, k in self._fields():
            setattr(p, k, int(w.value()) if k == "n_shanks" else float(w.value()))
        if self.sender() in (self.f_nsh, self.f_spacing, self.f_tipl, self.f_slen):
            p.probe_type = p.matching_type()
            self._busy = True
            self.f_type.setCurrentText(p.probe_type)
            self._busy = False
        self.target_msg.setText("")
        self.refresh()

    def _on_type(self, name: str) -> None:
        if self._busy:
            return
        self.plan.apply_type(name)
        self._load_fields()
        self.refresh()

    def _on_n(self, n: int) -> None:
        self.set_n(n)

    def _on_tab(self, i: int) -> None:
        if self._busy:
            return
        self.pset.active = min(i, self.pset.n - 1)
        self._load_fields()
        self.refresh()

    # ------------------------------------------------------------------ public
    def set_n(self, n: int) -> None:
        self.pset.n = 2 if n == 2 else 1
        if self.pset.n == 1:
            self.pset.active = 0
        self._load_fields()
        self.refresh()

    def set_active(self, i: int) -> None:
        self.pset.active = min(max(0, i), self.pset.n - 1)
        self._load_fields()
        self.refresh()

    def set_probe_set(self, pset: ProbeSet) -> None:
        self.pset = pset
        self._load_fields()
        self.refresh()

    def plan_to_target(self) -> bool:
        try:
            self.plan.apply_target_plan()
        except ValueError as exc:
            self.target_msg.setText(str(exc))
            return False
        self.target_msg.setText("")
        self._load_fields()
        self.refresh()
        return True

    def refresh(self) -> None:
        for v in self.views.values():
            v.set_data(self.pset, self.atlas)
        self._update_results()
        self._update_table()
        self.changed.emit()

    def _update_results(self) -> None:
        parts = []
        for pi, plan in enumerate(self.pset.used):
            r = plan.readouts()
            head = f"<b>Probe {plan.name}</b> &nbsp;" if self.pset.n == 2 else ""
            top = "margin-top:8px; " if pi else ""
            parts.append(f"<div style='{top}margin-bottom:4px'>{head}depth <b>{r['depth_below_surface_um']:.0f} µm</b>"
                         f" · horizontal {r['horizontal_travel_um']:.0f} µm"
                         f" · lowest site {r['lowest_site_depth_um']:.0f} µm deep</div>")
            for i, tip in enumerate(plan.tips()):
                c = shank_color(pi, i).name()
                line = (f"<span style='color:{c}'>■</span> shank {i}: AP {tip[0]:.0f} · ML {tip[1]:.0f}"
                        f" · DV {tip[2]:.0f}")
                if self.atlas is not None:
                    reg = self.atlas.label(self.atlas.region_at(*tip))
                    b, t = plan.span(i)
                    runs = self.atlas.regions_along(b, t)
                    rec = " → ".join(f"{lab} ({z - a:.0f})" for a, z, lab in runs)
                    line += f"<br>&nbsp;&nbsp;&nbsp;tip in <b>{reg}</b>; recorded: {rec}"
                parts.append(f"<div>{line}</div>")
        ca = self.pset.closest_approach()
        if ca is not None:
            if ca < TOUCH_WARNING_UM:
                parts.append(f"<div style='margin-top:6px; color:#c62828'><b>Closest approach A–B: {ca:.0f} µm"
                             f" - probes may touch</b></div>")
            else:
                parts.append(f"<div style='margin-top:6px'>Closest approach A–B: <b>{ca:.0f} µm</b></div>")
        self.results.setText("".join(parts))

    def _update_table(self) -> None:
        plan = self.plan
        self.table_title.setText(f"<b>Probe {plan.name}</b> at {plan.pitch:g}° (spreadsheet mode 2)")
        rows = plan.table()
        self.table.setRowCount(len(rows))
        for r, (d, h, z, cur) in enumerate(rows):
            for c, val in enumerate((d, h, z)):
                it = QTableWidgetItem(f"{val:.1f}")
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if cur:
                    fnt = it.font()
                    fnt.setBold(True)
                    it.setFont(fnt)
                self.table.setItem(r, c, it)

    # ------------------------------------------------------------------ atlas
    def load_atlas(self) -> None:
        if self._loading:
            return
        if Atlas.available(self.atlas_folder):
            self._start_atlas_thread(download=False)
            return
        ans = QMessageBox.question(
            self, "Download the Allen atlas?",
            "The Allen CCF 2017 annotation (25 µm, about 30 MB to download, ~300 MB in memory) and the "
            f"structure list will be downloaded once into\n{self.atlas_folder}\n\nDownload now?")
        if ans == QMessageBox.StandardButton.Yes:
            self._start_atlas_thread(download=True)

    def _start_atlas_thread(self, download: bool) -> None:
        self._loading = True
        self.atlas_btn.setEnabled(False)
        self.atlas_status.setText("Downloading the atlas..." if download else "Loading the atlas...")
        sig, folder = self._sig, self.atlas_folder

        def work():
            try:
                if download:
                    Atlas.download(folder, lambda name, fr: sig.progress.emit(name, fr))
                sig.loaded.emit(Atlas(folder))
            except Exception as exc:                       # network, disk, bad file
                sig.failed.emit(str(exc))
        threading.Thread(target=work, name="atlas", daemon=True).start()

    def _on_progress(self, name: str, fr: float) -> None:
        self.atlas_status.setText(f"Downloading {name}: {fr * 100:.0f} %")

    def _on_loaded(self, atlas) -> None:
        self._loading = False
        self.atlas = atlas
        self.atlas_btn.setEnabled(True)
        self.atlas_btn.setText("Reload atlas")
        self.atlas_status.setText(Atlas.description)
        self.refresh()

    def _on_failed(self, msg: str) -> None:
        self._loading = False
        self.atlas_btn.setEnabled(True)
        self.atlas_status.setText(f"Atlas not loaded: {msg}")

    def set_atlas(self, atlas: Optional[Atlas]) -> None:
        self.atlas = atlas
        if atlas is not None:
            self.atlas_status.setText(Atlas.description)
        self.refresh()
