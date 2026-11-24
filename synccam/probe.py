"""Angled Neuropixels insertion planner: geometry for one or two probes (like the
NPX_angled_distance_calculator spreadsheet) and an optional Allen CCF atlas lookup (a simpler
Pinpoint).

Self-contained: needs only numpy and the standard library (no Qt, nothing else from the app),
so the same file can be used in other apps (VisRig, SyncCam).

Coordinates are stereotaxic, relative to bregma, in micrometres:
    AP  + anterior      ML  + right      DV  + down (below bregma level)

A probe enters the brain at (entry AP, entry ML, surface DV) and travels `distance` along its
axis (the manipulator readout). Its direction:
    pitch - angle from horizontal (90 = straight down, 0 = horizontal), as in the spreadsheet;
    yaw   - which way the tip travels seen from above: 0 anterior, 90 right, 180 posterior, 270 left;
    roll  - rotation of the shank plane about the probe axis (0 = shanks side by side, horizontal).
Shank 0 is on the left when looking along the travel direction.

Spreadsheet (mode 2, yaw along X): horizontal = distance cos(pitch), depth = distance sin(pitch).
Check: pitch 45, distance 1230.3658 -> 870 deep and 870 horizontal.
"""
from __future__ import annotations

import copy
import gzip
import json
import math
import os
import sys
import urllib.request
from dataclasses import asdict, dataclass, fields
from typing import List, Optional, Tuple

import numpy as np

PROBE_TYPES = {
    # name: (n_shanks, shank_pitch, tip_length, span_length) - tip lengths are placeholders: check the datasheet
    "NP2.0 4-shank": (4, 250.0, 175.0, 720.0),     # 96 channels per shank
    "NP2.0 1-shank": (1, 250.0, 175.0, 2880.0),
    "NP1.0": (1, 250.0, 175.0, 3840.0),
    "2 shanks": (2, 250.0, 175.0, 720.0),
}
CUSTOM = "Custom"
TOUCH_WARNING_UM = 300.0          # closest approach below this: "probes may touch"


@dataclass
class ProbePlan:
    name: str = "A"
    probe_type: str = "NP2.0 4-shank"
    entry_ap: float = -3800.0       # um from bregma
    entry_ml: float = 500.0
    surface_dv: float = 800.0        # brain surface at the entry point, um below bregma
    pitch: float = 45.0              # deg from horizontal (90 = straight down)
    yaw: float = 90.0                # deg, travel direction seen from above: 0 anterior, 90 right
    roll: float = 0.0                # deg, rotation of the shank plane about the probe axis
    distance: float = 1230.0         # um travelled along the probe from the surface (manipulator readout)
    n_shanks: int = 4
    shank_pitch: float = 250.0       # um between shanks
    tip_length: float = 175.0        # um from the tip to the lowest site (check the probe datasheet)
    span_start: float = 0.0          # recorded sites start this many um above the lowest site ...
    span_length: float = 720.0       # ... and run over this length
    target_ap: float = -3800.0
    target_ml: float = 1000.0
    target_dv: float = 1600.0

    # ------------------------------------------------------------------ (de)serialisation
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "ProbePlan":
        names = {f.name: f for f in fields(cls)}
        kw = {}
        for k, v in (d or {}).items():
            if k in names:
                try:
                    kw[k] = type(getattr(cls(), k))(v)
                except (TypeError, ValueError):
                    pass
        return cls(**kw)

    def apply_type(self, probe_type: str) -> None:
        """Fill the probe dimensions from a preset (a 'Custom' type keeps the current numbers)."""
        self.probe_type = probe_type
        if probe_type in PROBE_TYPES:
            self.n_shanks, self.shank_pitch, self.tip_length, self.span_length = PROBE_TYPES[probe_type]
            self.n_shanks = int(self.n_shanks)

    def matching_type(self) -> str:
        """The preset these dimensions match, else 'Custom'."""
        dims = (int(self.n_shanks), float(self.shank_pitch), float(self.tip_length), float(self.span_length))
        for name, preset in PROBE_TYPES.items():
            if dims == (int(preset[0]),) + tuple(float(x) for x in preset[1:]):
                return name
        return CUSTOM

    def mirrored(self, name: str = "B") -> "ProbePlan":
        """The same plan in the other hemisphere (mirrored left-right): ML negated and the direction mirrored,
        yaw -> 360 - yaw (a probe travelling right, 90, then travels left, 270; anterior/posterior is kept)."""
        b = copy.deepcopy(self)
        b.name = name
        b.entry_ml, b.target_ml = -self.entry_ml, -self.target_ml
        b.yaw = (360.0 - self.yaw) % 360.0
        return b

    # ------------------------------------------------------------------ vectors
    def direction(self) -> np.ndarray:
        """Unit vector along the probe, tip-wards (AP, ML, DV)."""
        p, y = math.radians(self.pitch), math.radians(self.yaw)
        return np.array([math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), math.sin(p)])

    def width_axis(self) -> np.ndarray:
        """Unit vector across the shanks (towards higher shank numbers)."""
        d = self.direction()
        y, r = math.radians(self.yaw), math.radians(self.roll)
        w0 = np.array([-math.sin(y), math.cos(y), 0.0])          # horizontal, perpendicular to the travel
        return w0 * math.cos(r) + np.cross(d, w0) * math.sin(r)

    def entry(self) -> np.ndarray:
        return np.array([self.entry_ap, self.entry_ml, self.surface_dv], float)

    def target(self) -> np.ndarray:
        return np.array([self.target_ap, self.target_ml, self.target_dv], float)

    def shank_offsets(self) -> List[float]:
        n = max(1, int(self.n_shanks))
        return [(i - (n - 1) / 2.0) * self.shank_pitch for i in range(n)]

    def tips(self) -> List[np.ndarray]:
        c = self.entry() + self.distance * self.direction()
        w = self.width_axis()
        return [c + o * w for o in self.shank_offsets()]

    def shank_segments(self) -> List[Tuple[np.ndarray, np.ndarray]]:
        """Part of every shank inside the brain: (entry point of the shank, its tip)."""
        w = self.width_axis()
        e = self.entry()
        return [(e + o * w, tip) for o, tip in zip(self.shank_offsets(), self.tips())]

    def span(self, shank: int) -> Tuple[np.ndarray, np.ndarray]:
        """(bottom, top) of the recorded sites of one shank."""
        tip = self.tips()[shank]
        d = self.direction()
        bottom = tip - (self.tip_length + self.span_start) * d
        return bottom, bottom - self.span_length * d

    # ------------------------------------------------------------------ readouts
    def readouts(self) -> dict:
        p = math.radians(self.pitch)
        d = self.distance
        tip = self.entry() + d * self.direction()
        return {"depth_below_surface_um": d * math.sin(p), "horizontal_travel_um": d * math.cos(p),
                "lowest_site_depth_um": (d - self.tip_length) * math.sin(p),
                "tip_ap": tip[0], "tip_ml": tip[1], "tip_dv": tip[2]}

    def summary(self) -> str:
        r = self.readouts()
        return f"{self.name}: {self.pitch:g}° · {self.distance:.0f} µm → depth {r['depth_below_surface_um']:.0f} µm"

    def plan_to_target(self) -> dict:
        """Distance and entry point that bring the probe's centre line to the target, keeping the angles
        and the surface depth. Raises ValueError if that is impossible."""
        p = math.radians(self.pitch)
        if math.sin(p) <= 1e-6:
            raise ValueError("the angle from horizontal must be above 0 to reach a depth")
        dz = self.target_dv - self.surface_dv
        if dz <= 0:
            raise ValueError("the target is not below the brain surface")
        L = dz / math.sin(p)
        e = self.target() - L * self.direction()
        return {"distance": L, "entry_ap": float(e[0]), "entry_ml": float(e[1]), "horizontal_travel": L * math.cos(p)}

    def apply_target_plan(self) -> dict:
        r = self.plan_to_target()
        self.distance, self.entry_ap, self.entry_ml = r["distance"], r["entry_ap"], r["entry_ml"]
        return r

    def table(self, distances=(0, 500, 1000, 1500, 2000, 2500)) -> List[Tuple[float, float, float, bool]]:
        """Spreadsheet mode 2 at this pitch: (distance, horizontal, depth, is_current), sorted."""
        p = math.radians(self.pitch)
        ds = sorted(set(float(x) for x in distances) | {float(self.distance)})
        return [(d, d * math.cos(p), d * math.sin(p), abs(d - self.distance) < 1e-9) for d in ds]

    def report(self, atlas: Optional["Atlas"] = None) -> dict:
        """Everything about this probe for session.json."""
        out = {"plan": self.to_dict(), "summary": self.summary(), **{k: round(v, 2) for k, v in self.readouts().items()},
               "shanks": []}
        for i, tip in enumerate(self.tips()):
            sh = {"shank": i, "tip_ap": round(float(tip[0]), 2), "tip_ml": round(float(tip[1]), 2),
                  "tip_dv": round(float(tip[2]), 2)}
            if atlas is not None:
                sh["tip_region"] = atlas.label(atlas.region_at(*tip))
                b, t = self.span(i)
                sh["recorded_regions"] = [{"from_um": round(a, 1), "to_um": round(z, 1), "region": r}
                                          for a, z, r in atlas.regions_along(b, t)]
            out["shanks"].append(sh)
        return out


# ====================================================================== two probes
def segment_distance(p1, q1, p2, q2) -> float:
    """Smallest distance between the segments p1-q1 and p2-q2 (3-D)."""
    p1, q1, p2, q2 = (np.asarray(v, float) for v in (p1, q1, p2, q2))
    d1, d2, r = q1 - p1, q2 - p2, p1 - p2
    a, e, f = float(d1 @ d1), float(d2 @ d2), float(d2 @ r)
    eps = 1e-12
    if a <= eps and e <= eps:
        return float(np.linalg.norm(r))
    if a <= eps:
        s, t = 0.0, min(max(f / e, 0.0), 1.0)
    else:
        c = float(d1 @ r)
        if e <= eps:
            t, s = 0.0, min(max(-c / a, 0.0), 1.0)
        else:
            b = float(d1 @ d2)
            den = a * e - b * b
            s = min(max((b * f - c * e) / den, 0.0), 1.0) if den > eps else 0.0
            t = (b * s + f) / e
            if t < 0.0:
                t, s = 0.0, min(max(-c / a, 0.0), 1.0)
            elif t > 1.0:
                t, s = 1.0, min(max((b - c) / a, 0.0), 1.0)
    return float(np.linalg.norm((p1 + d1 * s) - (p2 + d2 * t)))


@dataclass
class ProbeSet:
    n: int = 1                       # 1 or 2 probes used
    active: int = 0                  # probe being edited / shown in the atlas slices
    probes: Optional[List[ProbePlan]] = None

    def __post_init__(self):
        if not self.probes:
            a = ProbePlan()
            self.probes = [a, a.mirrored("B")]
        if len(self.probes) == 1:
            self.probes.append(self.probes[0].mirrored("B"))
        self.probes[0].name, self.probes[1].name = "A", "B"
        self.n = 2 if int(self.n) == 2 else 1
        self.active = min(max(int(self.active), 0), self.n - 1)

    @property
    def used(self) -> List[ProbePlan]:
        return self.probes[:self.n]

    def closest_approach(self) -> Optional[float]:
        """Smallest distance between any shank of A and any shank of B (in-brain parts), or None."""
        if self.n < 2:
            return None
        return min(segment_distance(a0, a1, b0, b1)
                   for a0, a1 in self.probes[0].shank_segments() for b0, b1 in self.probes[1].shank_segments())

    def summary(self) -> str:
        return " | ".join(p.summary() for p in self.used)

    def to_dict(self) -> dict:
        return {"n": self.n, "active": self.active, "probes": [p.to_dict() for p in self.probes]}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "ProbeSet":
        """Accepts a ProbeSet dict or an old single-plan dict (one ProbePlan)."""
        d = d or {}
        if "probes" in d:
            plans = [ProbePlan.from_dict(x) for x in (d.get("probes") or [])][:2]
            return cls(n=d.get("n", 1), active=d.get("active", 0), probes=plans or None)
        if any(k in d for k in ("entry_ap", "pitch", "distance")):
            return cls(n=1, active=0, probes=[ProbePlan.from_dict(d)])
        return cls()

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, text: str) -> "ProbeSet":
        try:
            return cls.from_dict(json.loads(text)) if text else cls()
        except (ValueError, TypeError):
            return cls()

    def session_block(self, atlas: Optional["Atlas"] = None) -> dict:
        """The 'probe' entry for session.json."""
        ca = self.closest_approach()
        out = {"n": self.n, "probes": [p.report(atlas) for p in self.used],
               "closest_approach_um": None if ca is None else round(ca, 1),
               "coordinates": "stereotaxic um from bregma: AP + anterior, ML + right, DV + down"}
        if atlas is not None:
            out["atlas"] = atlas.description
        return out


# ====================================================================== Allen CCF atlas
ATLAS_URL = ("http://download.alleninstitute.org/informatics-archive/current-release/mouse_ccf/annotation/"
             "ccf_2017/annotation_25.nrrd")
TREE_URL = "http://api.brain-map.org/api/v2/structure_graph_download/1.json"
ANNOTATION_FILE = "annotation_25.nrrd"
TREE_FILE = "structure_graph.json"
BREGMA_CCF = (5400.0, 332.0, 5739.0)       # bregma in CCF um (AP from the front, DV from the top, ML from the left), IBL


def read_nrrd(path: str):
    """Minimal NRRD reader (raw or gzip encoding), enough for the Allen annotation volumes.
    Returns (array with the NRRD axes in order, header dict)."""
    with open(path, "rb") as f:
        if not f.readline().startswith(b"NRRD"):
            raise ValueError("not an NRRD file")
        header = {}
        while True:
            line = f.readline()
            if line in (b"\n", b"\r\n", b""):
                break
            s = line.decode("ascii", "replace").strip()
            if s.startswith("#") or ":" not in s:
                continue
            k, v = s.split(":", 1)
            header[k.strip().lower()] = v.strip().lstrip("=").strip()
        data = f.read()
    sizes = [int(x) for x in header["sizes"].split()]
    endian = ">" if header.get("endian", "little").lower() == "big" else "<"
    types = {"uint32": "u4", "unsigned int": "u4", "uint16": "u2", "unsigned short": "u2", "ushort": "u2",
             "uint8": "u1", "uchar": "u1", "unsigned char": "u1", "int32": "i4", "int": "i4",
             "int16": "i2", "short": "i2", "int8": "i1", "float": "f4", "double": "f8"}
    dt = np.dtype(endian + types[header["type"].lower()])
    if header.get("encoding", "raw").lower() in ("gzip", "gz"):
        data = gzip.decompress(data)
    arr = np.frombuffer(data, dtype=dt, count=int(np.prod(sizes)))
    return arr.reshape(sizes[::-1]).transpose(), header             # NRRD lists the fastest axis first


class Atlas:
    """Allen CCF annotation (25 um) with structure names and colours. Volume axes: AP, DV, ML (CCF)."""
    description = ("Allen CCF 2017 (25 µm). Bregma at CCF (5400, 332, 5739) µm, no stereotaxic "
                   "scaling - treat regions near borders as approximate.")

    def __init__(self, folder: str):
        self.folder = folder
        self.vol, _hdr = read_nrrd(os.path.join(folder, ANNOTATION_FILE))
        self.res = 25.0
        with open(os.path.join(folder, TREE_FILE), encoding="utf-8") as f:
            tree = json.load(f)
        self.names, self.acr, self.color = {}, {}, {}
        stack = list(tree.get("msg", tree) if isinstance(tree, dict) else tree)
        if isinstance(stack, dict):
            stack = [stack]
        while stack:                                   # walk the structure tree (no recursion limit)
            node = stack.pop()
            i = int(node["id"])
            self.names[i] = node.get("name", "")
            self.acr[i] = node.get("acronym", "")
            self.color[i] = "#" + str(node.get("color_hex_triplet", "999999"))
            stack.extend(node.get("children") or [])

    @staticmethod
    def available(folder: str) -> bool:
        return all(os.path.exists(os.path.join(folder, f)) for f in (ANNOTATION_FILE, TREE_FILE))

    @staticmethod
    def download(folder: str, progress=None) -> None:
        """Download both files (to .part, then renamed). progress(file name, fraction 0..1)."""
        os.makedirs(folder, exist_ok=True)
        for url, name in ((TREE_URL, TREE_FILE), (ATLAS_URL, ANNOTATION_FILE)):
            dst = os.path.join(folder, name)
            tmp = dst + ".part"

            def hook(blocks, bs, total, name=name):
                if progress and total > 0:
                    progress(name, min(1.0, blocks * bs / total))
            urllib.request.urlretrieve(url, tmp, hook)
            os.replace(tmp, dst)

    # ------------------------------------------------------------------ lookups
    def to_index(self, ap, ml, dv):
        bap, bdv, bml = BREGMA_CCF
        return (int(round((bap - ap) / self.res)), int(round((bdv + dv) / self.res)),
                int(round((bml + ml) / self.res)))

    def region_at(self, ap, ml, dv) -> int:
        i, j, k = self.to_index(ap, ml, dv)
        s = self.vol.shape
        if not (0 <= i < s[0] and 0 <= j < s[1] and 0 <= k < s[2]):
            return 0
        return int(self.vol[i, j, k])

    def label(self, rid: int) -> str:
        return (self.acr.get(rid) or str(rid)) if rid else "outside"

    def regions_along(self, a, b, step: float = 20.0):
        """[(from_um, to_um, acronym)] along the segment a -> b, distances from a, sampled every `step` um."""
        a, b = np.asarray(a, float), np.asarray(b, float)
        L = float(np.linalg.norm(b - a))
        n = max(2, int(L / step) + 1)
        out = []
        for i in range(n):
            t = i / (n - 1)
            lab = self.label(self.region_at(*(a + (b - a) * t)))
            if out and out[-1][2] == lab:
                out[-1][1] = t * L
            else:
                out.append([t * L, t * L, lab])
        return [tuple(x) for x in out]

    def slice_index(self, view: str, value: float) -> int:
        if view == "coronal":
            return max(0, min(self.vol.shape[0] - 1, int(round((BREGMA_CCF[0] - value) / self.res))))
        return max(0, min(self.vol.shape[2] - 1, int(round((BREGMA_CCF[2] + value) / self.res))))

    def coronal(self, ap):
        """(slice DV x ML, extent (ml0, ml1, dv0, dv1) in stereotaxic um)."""
        sl = self.vol[self.slice_index("coronal", ap)]
        bap, bdv, bml = BREGMA_CCF
        return sl, (-bml, self.vol.shape[2] * self.res - bml, -bdv, self.vol.shape[1] * self.res - bdv)

    def sagittal(self, ml):
        """(slice DV x AP with the most anterior column first, extent (ap0, ap1, dv0, dv1))."""
        sl = self.vol[:, :, self.slice_index("sagittal", ml)].T
        bap, bdv, bml = BREGMA_CCF
        return sl, (bap, bap - self.vol.shape[0] * self.res, -bdv, self.vol.shape[1] * self.res - bdv)

    def rgb_slice(self, sl) -> np.ndarray:
        """Colour an annotation slice with the Allen colours, borders darkened to 55 % (H x W x 3 uint8)."""
        ids, inv = np.unique(sl, return_inverse=True)
        lut = np.zeros((len(ids), 3), np.uint8)
        for n, i in enumerate(ids):
            c = self.color.get(int(i), "#000000") if i else "#000000"
            lut[n] = (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16))
        out = lut[inv.reshape(sl.shape)]
        edge = np.zeros(sl.shape, bool)
        edge[:-1] |= sl[:-1] != sl[1:]
        edge[:, :-1] |= sl[:, :-1] != sl[:, 1:]
        m = edge & (sl > 0)
        out[m] = (out[m] * 0.55).astype(np.uint8)
        return out


def config_dir(app: str = "SyncCam") -> str:
    """Per-user folder for app data (Windows %APPDATA%, macOS Application Support, Linux ~/.config)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, app)


def atlas_dir(app: str = "SyncCam") -> str:
    return os.path.join(config_dir(app), "atlas")
