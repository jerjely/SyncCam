"""Live pupil detection and eye-movement / dilation readout.

The detector is VisRig's (visrig/pupil/detector.py, PupilDetector), ported into SyncCam's
interface. Under IR light the pupil is the darkest round blob in the eye image. Per frame,
inside the eye box the user drew:

1. blur a little (5 px Gaussian);
2. threshold: darker than T counts as pupil, T = the darkest 0.2 % of the box + an offset
   (default 20 grey levels; the Threshold slider moves the offset);
3. a 5 px opening removes specks and cuts thin bridges to the dark iris edge or lashes;
4. among the dark blobs keep the round, solid, plausibly sized ones; score =
   roundness x solidity x ellipse fill x sqrt(area), weighted by the distance to the last pupil;
   fit an ellipse to the convex hull (the hull ignores the notch an eyelid or the corneal
   reflection leaves on the edge);
5. the confidence (roundness x solidity x ellipse fill, a clear pupil gives ~0.9) must be at
   least 0.6, otherwise the frame counts as lost (blink, half-closed lid, fur).

One change from VisRig, found on the rig's eye video (pupil_video2025-12-08T15_59_24):
only blobs that pass the confidence limit compete for "the pupil". In VisRig the distance
weighting could make a low-confidence blob near a wrongly remembered position win; the frame was
then rejected and the remembered position never updated, so one dark speck in the fur locked the
detector for ~3000 frames (1.7 min) while the pupil was clearly visible. With the change the
centres are identical to VisRig's wherever VisRig is right. (Also tried: forgetting the last
position after 0.5 s - no effect; rejecting implausible jumps - fewer diameter jumps but 19 good
frames lost, so not kept.)

Diameter: `diameter` is the ellipse's LONG axis. The camera looks at the eye from an angle,
so a round pupil appears as an ellipse whose long axis is the true diameter and whose short
axis is foreshortened (and also cut by the lower lid when the pupil is large). VisRig's
diameter, sqrt(long x short), therefore reads smaller and changes with gaze direction;
`minor` is kept so it can be computed (see the README).

`Kinematics` (VisRig's, unchanged) turns the stream into the centre's velocity over 0.2 s and
the diameter slope over 1.0 s, and says whether the eye is moving (and which way) and whether
the pupil is dilating, constricting or stable. No Qt here.
"""
from __future__ import annotations

import collections
import math
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np

NAN = float("nan")

AUTO_OFFSET = 20          # VisRig default: darkest 0.2 % + 20 grey levels
BLUR = 5
OPEN = 5
MIN_CONFIDENCE = 0.6
MIN_DIAM = 6.0
MAX_DIAM_FRAC = 0.8
MIN_CIRCULARITY = 0.5



def _cv2():
    import cv2
    return cv2


@dataclass
class PupilParams:
    box: Optional[Tuple[int, int, int, int]] = None   # x, y, w, h of the eye box (image pixels)
    sensitivity: int = 0           # Threshold slider, -50 .. 50: moves the auto offset by sensitivity / 2
    min_confidence: float = MIN_CONFIDENCE

    @property
    def offset(self) -> float:
        return AUTO_OFFSET + self.sensitivity / 2.0

    def as_dict(self) -> dict:
        return {"eye_box": list(self.box) if self.box else None, "sensitivity": self.sensitivity,
                "threshold": f"auto: darkest 0.2 % of the box + {self.offset:g}",
                "min_confidence": self.min_confidence}


@dataclass
class PupilResult:
    found: bool = False
    x: float = NAN                 # centre, image pixels
    y: float = NAN
    diameter: float = NAN          # long axis of the fitted ellipse, pixels
    minor: float = NAN             # short axis, pixels
    angle: float = NAN             # direction of the short axis, degrees (for drawing)
    area: float = NAN              # pixels of the dark blob
    quality: float = 0.0           # confidence 0..1 (roundness x solidity x ellipse fill)
    threshold: float = NAN
    mask: Optional[np.ndarray] = field(default=None, repr=False)   # dark-pixel mask of the box (for display)

    @property
    def major(self) -> float:
        return self.diameter

    @property
    def diameter_geo(self) -> float:
        """VisRig's diameter: sqrt(long x short axis)."""
        return math.sqrt(self.diameter * self.minor) if self.found else NAN


def to_gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return img
    return _cv2().cvtColor(img, _cv2().COLOR_BGR2GRAY)


class PupilTracker:
    """VisRig's PupilDetector behind SyncCam's interface (detect() -> PupilResult)."""

    def __init__(self, params: Optional[PupilParams] = None):
        self.params = params or PupilParams()
        self._prev: Optional[Tuple[float, float, float]] = None   # last good x, y, diameter (image px)
        self._missed = 0
        cv2 = _cv2()
        self._k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (OPEN, OPEN))

    def reset(self) -> None:
        self._prev = None
        self._missed = 0

    def set_params(self, params: PupilParams) -> None:
        if params.box != self.params.box:
            self.reset()
        self.params = params

    def detect(self, img: np.ndarray, keep_mask: bool = False) -> PupilResult:
        p = self.params
        if p.box is None:
            return PupilResult()
        cv2 = _cv2()
        gray = to_gray(img)
        H, W = gray.shape
        bx, by, bw, bh = (int(round(v)) for v in p.box)
        bx, by = max(0, min(bx, W - 2)), max(0, min(by, H - 2))
        bw, bh = max(2, min(bw, W - bx)), max(2, min(bh, H - by))
        if bw < 8 or bh < 8:
            return PupilResult()
        roi = gray[by:by + bh, bx:bx + bw]
        roi = cv2.GaussianBlur(roi, (BLUR, BLUR), 0)
        thr = min(254.0, float(np.percentile(roi, 0.2)) + p.offset)
        mask = (roi < thr).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._k_open)
        best = self._pick(mask, bx, by, min(bw, bh), p.min_confidence)
        shown = mask if keep_mask else None
        if best is None:
            self._miss()
            return PupilResult(threshold=thr, mask=shown)
        cx, cy, a1, a2, ang, area, conf = best
        major, minor = max(a1, a2), min(a1, a2)
        # OpenCV's angle is the direction of its first axis; make `angle` the direction of the
        # SHORT axis, so drawing rotate(angle) + ellipse(rx=minor/2, ry=diameter/2) is right
        angle = ang if a2 >= a1 else ang + 90.0
        self._prev = (cx, cy, math.sqrt(a1 * a2))
        self._missed = 0
        return PupilResult(True, cx, cy, major, minor, angle, area, round(float(conf), 3), thr, shown)

    def _pick(self, mask, x0, y0, roi_min, min_conf):
        cv2 = _cv2()
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        best, best_score = None, 0.0
        amin = math.pi * (MIN_DIAM / 2) ** 2
        amax = math.pi * (MAX_DIAM_FRAC * roi_min / 2) ** 2
        for cnt in cnts:
            if len(cnt) < 5:
                continue
            area = cv2.contourArea(cnt)
            if area < amin or area > amax:
                continue
            hull = cv2.convexHull(cnt)
            if len(hull) < 5:
                continue
            harea = cv2.contourArea(hull)
            per = cv2.arcLength(hull, True)
            if harea <= 0 or per <= 0:
                continue
            circ = 4 * math.pi * harea / (per * per)
            solidity = area / harea
            if circ < MIN_CIRCULARITY:
                continue
            (cx, cy), (a1, a2), ang = cv2.fitEllipse(hull)
            if not (math.isfinite(cx) and math.isfinite(cy)) or min(a1, a2) <= 0:
                continue
            ell_area = math.pi * a1 * a2 / 4
            fill = min(area / ell_area, ell_area / area) if ell_area > 0 else 0.0
            conf = circ * solidity * fill
            if conf < min_conf:            # SyncCam change: only confident blobs compete
                continue
            score = conf * math.sqrt(area)
            if self._prev is not None:
                px, py, pd = self._prev
                score /= 1.0 + math.hypot(cx + x0 - px, cy + y0 - py) / max(pd, 1.0)
            if score > best_score:
                best_score = score
                best = (cx + x0, cy + y0, a1, a2, ang, area, min(1.0, conf))
        return best

    def _miss(self) -> None:
        self._missed += 1


class Kinematics:
    """VisRig's Kinematics, unchanged: velocity of the pupil centre (least-squares slope over the
    last `window` s) and rate of diameter change (slope over `size_window` s), with state words.

    Defaults calibrated on the rig's eye video (30 fps, pupil 20-55 px): at rest the centre
    speed is ~2 px/s (99th percentile ~40 px/s), so 20 px/s marks real movements; diameter
    changes over 1 s are mostly within +-5 %/s, so 8 %/s marks dilation / constriction."""

    def __init__(self, window=0.2, size_window=1.0, move_thresh=20.0, size_thresh=8.0, min_conf=0.3):
        self.window = window
        self.size_window = size_window
        self.move_thresh = move_thresh      # px/s
        self.size_thresh = size_thresh      # % of diameter per second
        self.min_conf = min_conf
        self.buf = collections.deque()

    @staticmethod
    def _slope(a, col):
        tt = a[:, 0] - a[:, 0].mean()
        den = float((tt * tt).sum())
        return float((tt * (a[:, col] - a[:, col].mean())).sum() / den) if den > 0 else math.nan

    def update(self, t: float, res: PupilResult) -> dict:
        out = {"vx": math.nan, "vy": math.nan, "speed": math.nan, "dd": math.nan, "dd_pct": math.nan,
               "moving": "", "size": ""}
        if res.found and res.quality >= self.min_conf:
            self.buf.append((t, res.x, res.y, res.diameter))
        keep = max(self.window, self.size_window)
        while self.buf and t - self.buf[0][0] > keep:
            self.buf.popleft()
        if not res.found:
            out["moving"] = "lost"
            out["size"] = "lost"
            return out
        a = np.array(self.buf) if self.buf else np.zeros((0, 4))
        recent = a[a[:, 0] >= t - self.window] if len(a) else a
        if len(recent) >= 3:
            vx, vy = self._slope(recent, 1), self._slope(recent, 2)
            sp = math.hypot(vx, vy)
            out.update(vx=vx, vy=vy, speed=sp)
            if sp > self.move_thresh:
                ang = math.degrees(math.atan2(-vy, vx))          # image y grows downwards
                names = ["right", "up-right", "up", "up-left", "left", "down-left", "down", "down-right"]
                out["moving"] = names[int(((ang + 22.5) % 360) // 45)]
            else:
                out["moving"] = "still"
        sw = a[a[:, 0] >= t - self.size_window] if len(a) else a
        if len(sw) >= 5 and sw[-1, 0] - sw[0, 0] > 0.5 * self.size_window:
            dd = self._slope(sw, 3)
            dmean = float(sw[:, 3].mean())
            pct = 100.0 * dd / dmean if dmean > 0 else math.nan
            out.update(dd=dd, dd_pct=pct)
            if pct > self.size_thresh:
                out["size"] = "dilating"
            elif pct < -self.size_thresh:
                out["size"] = "constricting"
            else:
                out["size"] = "stable"
        return out
