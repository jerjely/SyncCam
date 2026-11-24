"""Live pupil detection: centre and diameter of the dark pupil inside an eye box.

Works on the infrared eye videos of the rig: a dark, round pupil on a grey iris.
For every frame, inside the eye box the user drew:

1. blur a little and find the dark pixels (a threshold between the darkest pixels
   and the typical brightness of the box, adjustable with `sensitivity`),
2. clean the mask (remove specks such as iris flecks and hairs, fill small gaps),
3. pick the dark blob that is round enough and big enough, preferring the one closest
   to the pupil in the previous frame,
4. fit an ellipse to its convex outline (so a lid cutting into the pupil or a small
   reflection does not bite into the measurement).

The diameter is the ellipse's long axis, in image pixels. Frames without a believable
pupil (blinks, eye closed, eye out of the box) give `found = False` and NaN values.
No Qt here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np

NAN = float("nan")


def _cv2():
    import cv2
    return cv2


@dataclass
class PupilParams:
    box: Optional[Tuple[int, int, int, int]] = None   # x, y, w, h of the eye box (image pixels)
    sensitivity: int = 0           # -50 .. 50: higher = a lighter threshold (more pixels count as pupil)
    min_diameter: float = 4.0      # pixels
    max_fraction: float = 0.85     # pupil diameter at most this fraction of the box's smaller side
    min_quality: float = 0.2       # below this (blink, closed or blurred eye) the frame counts as "not found"

    def as_dict(self) -> dict:
        return {"eye_box": list(self.box) if self.box else None, "sensitivity": self.sensitivity}


@dataclass
class PupilResult:
    found: bool = False
    x: float = NAN                 # centre, image pixels
    y: float = NAN
    diameter: float = NAN          # long axis, pixels
    minor: float = NAN             # short axis, pixels
    angle: float = NAN             # ellipse angle, degrees (OpenCV convention)
    area: float = NAN              # pixels of the dark blob
    quality: float = 0.0           # 0..1: roundness x contrast
    threshold: float = NAN
    mask: Optional[np.ndarray] = field(default=None, repr=False)   # dark-pixel mask of the box (for display)


def to_gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return img
    return _cv2().cvtColor(img, _cv2().COLOR_BGR2GRAY)


class PupilTracker:
    """Keeps the previous position so the right blob is chosen when there are several."""

    def __init__(self, params: Optional[PupilParams] = None):
        self.params = params or PupilParams()
        self._prev: Optional[Tuple[float, float, float]] = None   # x, y, diameter in box coordinates
        self._missed = 0

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
        bx, by, bw, bh = p.box
        bx, by = max(0, int(bx)), max(0, int(by))
        bw, bh = min(int(bw), W - bx), min(int(bh), H - by)
        if bw < 8 or bh < 8:
            return PupilResult()
        roi = gray[by:by + bh, bx:bx + bw]
        k = max(3, int(round(min(bw, bh) / 40)) | 1)                  # blur scales with the box
        blur = cv2.GaussianBlur(roi, (k, k), 0)
        lo = float(np.percentile(blur, 1))
        mid = float(np.median(blur))
        if mid - lo < 12:                                             # no contrast: dark frame or eye closed
            self._miss()
            return PupilResult(threshold=NAN)
        r = max(1, int(round(min(bw, bh) / 60)))
        k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 3, 2 * r + 3))
        min_area = math.pi * (p.min_diameter / 2) ** 2
        max_d = p.max_fraction * min(bw, bh)
        # Threshold sweep, darkest first. The pupil appears as a round core and grows to its
        # edge, where it stays nearly the same size over several thresholds (its edge is sharp),
        # until it merges with darker iris / lid shadow. Pick the most *stable* step (smallest
        # relative growth), like MSER: that is the pupil's real outline.
        offset = p.sensitivity / 200.0
        steps = []
        for frac in np.arange(0.08, 0.53, 0.03):
            thr = lo + (frac + offset) * (mid - lo)
            m = (blur <= thr).astype(np.uint8)
            m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k_open)
            m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k_close)
            cand = self._best_blob(m, bw, bh, min_area, max_d)
            if cand is None:
                if steps:
                    break
                continue
            if steps:
                prev = steps[-1][0]
                moved = math.hypot(cand[2] - prev[2], cand[3] - prev[3])
                if cand[7] > 2.5 * prev[7] or moved > 0.5 * prev[4]:
                    break                   # jumped to a different / merged blob
            steps.append((cand, thr, m))
        best, mask = None, None
        if steps:
            areas = np.array([st[0][7] for st in steps], float)
            if len(steps) >= 3:
                growth = np.full(len(steps), np.inf)
                growth[1:-1] = (areas[2:] - areas[:-2]) / areas[1:-1]
                growth[-1] = (areas[-1] - areas[-2]) / areas[-1] * 2      # one-sided at the end
                ok = np.array([st[0][8] >= 0.80 for st in steps])
                growth[~ok] = np.inf
                i = int(np.argmin(growth)) if np.isfinite(growth).any() else len(steps) - 1
            else:
                i = len(steps) - 1
            cand, thr, mask = steps[i]
            best = cand + (thr,)
        if best is None:
            self._miss()
            return PupilResult(threshold=NAN, mask=mask if keep_mask else None)
        c, hull, cx, cy, d_major, d_minor, ang, area, circ, thr = best
        # contrast: pupil vs a ring of iris around it
        blob = np.zeros_like(mask)
        cv2.drawContours(blob, [hull], -1, 1, -1)
        ring = cv2.dilate(blob, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 7, 2 * r + 7))) - blob
        inside = float(blur[blob > 0].mean())
        around = float(blur[ring > 0].mean()) if ring.any() else mid
        contrast = max(0.0, min(1.0, (around - inside) / max(1.0, mid - lo)))
        quality = round(max(0.0, min(1.0, circ)) * contrast, 3)
        if quality < p.min_quality:      # blink / eye closed: a faint blob, not a pupil
            self._miss()
            return PupilResult(quality=quality, threshold=thr, mask=mask if keep_mask else None)
        if self._prev is not None and self._missed <= 3:
            # a pupil cannot halve, double or jump by more than its width between two frames
            px, py, pd = self._prev
            if not (0.6 * pd <= d_major <= 1.6 * pd) or math.hypot(cx - px, cy - py) > max(pd, 6.0):
                self._miss()
                return PupilResult(quality=quality, threshold=thr, mask=mask if keep_mask else None)
        self._prev = (cx, cy, d_major)
        self._missed = 0
        return PupilResult(True, bx + cx, by + cy, d_major, d_minor, ang, area, quality, thr,
                           mask if keep_mask else None)

    def _best_blob(self, mask, bw, bh, min_area, max_d):
        cv2 = _cv2()
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        best, best_score = None, 0.0
        for c in contours:
            area = cv2.contourArea(c)
            if area < min_area or len(c) < 5:
                continue
            hull = cv2.convexHull(c)
            hull_area = cv2.contourArea(hull)
            perim = cv2.arcLength(hull, True)
            if hull_area <= 0 or perim <= 0 or len(hull) < 5:
                continue
            circ = 4 * math.pi * hull_area / perim ** 2               # 1 = circle
            solidity = area / hull_area
            if circ < 0.6 or solidity < 0.7:
                continue
            (cx, cy), (a1, a2), ang = cv2.fitEllipse(hull)
            d_major, d_minor = max(a1, a2), min(a1, a2)
            if d_major > max_d or d_minor < 0.4 * d_major:
                continue
            x0, y0, w0, h0 = cv2.boundingRect(c)
            edge = x0 <= 0 or y0 <= 0 or x0 + w0 >= bw or y0 + h0 >= bh
            score = area * circ * solidity * (0.2 if edge else 1.0)
            if self._prev is not None:
                px, py, pd = self._prev
                score /= 1.0 + (math.hypot(cx - px, cy - py) / max(pd, 4.0)) ** 2
            if score > best_score:
                best, best_score = (c, hull, cx, cy, d_major, d_minor, ang, area, circ * solidity), score
        return best

    def _miss(self) -> None:
        self._missed += 1
        if self._missed > 15:            # after half a second without a pupil, forget where it was
            self._prev = None
