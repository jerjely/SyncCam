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
import math
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


def t_image(ctx):
    from .camera import ImageSettings, auto_levels, process_image
    img = np.zeros((60, 80, 3), np.uint8)
    img[:, :40] = (60, 100, 140)
    img[:, 40:] = (90, 130, 170)
    st = ImageSettings()
    assert st.grey and st.is_identity(), "greyscale should be the default, with no adjustment"
    g = process_image(img, st)
    assert g.ndim == 2 and g.shape == (60, 80), g.shape
    col = process_image(img, ImageSettings(grey=False))
    assert col.shape == img.shape and np.array_equal(col, img)
    lut = ImageSettings(brightness=20).lut()
    assert lut[100] == 120 and lut[250] == 255
    lut = ImageSettings(contrast=2.0).lut()
    assert lut[128] == 128 and lut[160] == 192 and lut[0] == 0
    lut = ImageSettings(gamma=2.0).lut()
    assert lut[0] == 0 and lut[255] == 255 and lut[64] > 64
    img2 = np.zeros((60, 80, 3), np.uint8)
    img2[:, :40], img2[:, 40:] = 70, 170          # dull picture: grey levels 70..170
    g = process_image(img2, st)
    b, c = auto_levels(g)
    stretched = process_image(img2, ImageSettings(brightness=b, contrast=c))
    assert stretched.min() <= 10 and stretched.max() >= 245, (b, c, stretched.min(), stretched.max())


def t_firmware(ctx):
    """The bundled Arduino firmware is there, matches the sketch, and uploads through STK500."""
    from .uploader import (APP_LIMIT, INO_NAME, FakeBootloader, firmware_matches_sketch, firmware_path,
                           parse_ihex, upload_image)
    hexf, ino = firmware_path(), firmware_path(INO_NAME)
    assert hexf.exists(), f"firmware missing from the app: {hexf}"
    assert ino.exists(), f"Arduino sketch missing from the app: {ino}"
    assert firmware_matches_sketch(), "the bundled firmware was not built from the bundled sketch " \
                                      "(run tools/build_firmware.py)"
    img = parse_ihex(hexf.read_text(encoding="ascii"))
    assert 1000 < len(img) <= APP_LIMIT and b"SYNCCAM " in img, len(img)
    for baud in (115200, 57600):                 # Uno / new Nano, then old-bootloader Nano
        fb = FakeBootloader(baud_ok=baud)
        r = upload_image(fb.factory, img)
        assert r["baud"] == baud and bytes(fb.flash[:len(img)]) == img and fb.left_progmode, r
    ctx.log(f"    firmware {len(img)} bytes, uploads at 115200 and 57600 baud")


def t_pupil(ctx):
    """Pupil detection (VisRig detector) and Kinematics on the synthetic eye: accuracy against the
    true values, blinks rejected, movement and dilation states."""
    from .camera import TestPatternSource
    from .pupil import Kinematics, PupilParams, PupilTracker
    src = TestPatternSource((640, 480), 10000)
    tr = PupilTracker(PupilParams(box=src.eye_box()))
    kin = Kinematics()
    err_xy, err_d, opens, found, blinks, rejected = [], [], 0, 0, 0, 0
    moving, sizes, lost_ok = set(), set(), 0
    t0 = time.perf_counter()
    for n in range(900):
        _ok, img = src.read()
        r = tr.detect(img)
        k = kin.update(n / 30.0, r)
        moving.add("moving" if k["moving"] not in ("still", "lost", "") else k["moving"])
        sizes.add(k["size"])
        x, y, d, is_open = src.eye_truth(n)
        if not is_open:
            lost_ok += k["moving"] == "lost" and k["size"] == "lost"
        if is_open:
            opens += 1
            if r.found:
                found += 1
                err_xy.append(math.hypot(r.x - x, r.y - y))
                err_d.append(abs(r.diameter - d))
        else:
            blinks += 1
            rejected += not r.found
    ms = (time.perf_counter() - t0) / 900 * 1000
    assert found >= 0.98 * opens, f"pupil found in only {found} of {opens} open-eye frames"
    assert blinks and rejected == blinks, f"{blinks - rejected} of {blinks} blink frames gave a pupil"
    assert max(err_xy) < 1.5 and np.mean(err_xy) < 0.4, f"centre error mean {np.mean(err_xy):.2f}, max {max(err_xy):.2f} px"
    assert max(err_d) < 3.0 and np.mean(err_d) < 1.5, f"diameter error mean {np.mean(err_d):.2f}, max {max(err_d):.2f} px"
    # synthetic eye: centre swings at up to ~23 px/s, diameter changes at up to ~23 %/s
    assert {"still", "moving"} <= moving, f"eye states seen: {moving}"
    assert {"stable", "dilating", "constricting"} <= sizes, f"pupil states seen: {sizes}"
    assert lost_ok == blinks, "blink frames must be reported as lost"
    ctx.log(f"    centre error {np.mean(err_xy):.2f} px, diameter error {np.mean(err_d):.2f} px, "
            f"{rejected}/{blinks} blink frames rejected, {ms:.1f} ms per frame (incl. drawing); "
            f"states {sorted(moving)} / {sorted(sizes - {''})}")


def t_camera_delay(ctx):
    """IR sync blink analysis (VisRig's blink_delays) on made-up frame times and brightness."""
    from .session import blink_delays
    fps, delay = 30.0, 0.060
    t = np.arange(0, 6, 1 / fps)
    blinks = [(1.0, 1.1), (4.5, 4.6)]
    lit = np.ones_like(t, bool)
    for a, b in blinks:                       # frame i shows the light at its exposure, t - delay
        lit &= ~((t - delay >= a) & (t - delay < b))
    lum = np.where(lit, 90.0, 45.0) + np.random.default_rng(1).normal(0, 1, len(t))
    res = blink_delays(t, lum, blinks)
    ds = [v for r in res for k_, v in r.items() if k_.endswith("delay_ms")]
    assert len(ds) == 4, res
    assert all(delay * 1000 - 1 <= d_ <= (delay + 1 / fps) * 1000 + 1 for d_ in ds), ds
    flat = blink_delays(t, np.full_like(t, 90.0), blinks)
    assert all("no visible dip" in r.get("error", "") for r in flat), flat
    ctx.log(f"    true 60 ms -> {np.median(ds):.1f} ms; flat brightness -> 'no visible dip'")


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
    from .arduino import IR_OFF, IR_ON, ArduinoStream, SimulatedArduino
    from .arduino import parse_identify
    st = ArduinoStream(SimulatedArduino(), 5000)
    info = st.connect(IR_OFF, pins=(5, 2, 9))
    ident = parse_identify(info.firmware)
    assert ident["version"] == 2 and ident["ttl"] == "A5" and ident["mic"] == "A2" and ident["ir"] == "D9", \
        info.firmware
    time.sleep(1.1)
    d = st.recent(1.0)
    assert st.error is None, st.error
    assert len(d) == 5000, len(d)
    assert d[:, 0].max() > 900 and d[:, 0].min() < 50, "TTL channel missing"
    assert d[:, 2].max() == 0, "IR LED should be off"
    st.set_ir(IR_ON)
    time.sleep(0.4)
    assert st.recent(0.2)[:, 2].min() == 1, "IR LED did not switch on"
    st.set_ir(IR_OFF)
    time.sleep(0.4)
    assert st.recent(0.2)[:, 2].max() == 0, "IR LED did not switch off"
    st.close()
    ctx.log(f"    {st.total} samples, {st.unwrap.dropped} lost")


def t_serial_commands(ctx):
    """Commands go out on a real pySerial port (loopback) in the firmware's format."""
    from .arduino import SerialArduino
    dev = SerialArduino("loop://")
    dev.open()
    dev.write(b"A5,1\nL9\nI1\n")
    time.sleep(0.05)
    got = dev.read() + dev.read()
    assert b"A5,1\nL9\nI1\n" in got, got
    dev.close()


def t_recording(ctx):
    """Full recording: test-pattern camera + simulated Arduino, LED followed in the video."""
    from .arduino import IR_ON, ArduinoStream, SimulatedArduino
    from .camera import TEST_PATTERN, CameraThread, TestPatternSource
    from .session import Recording
    sim = SimulatedArduino()
    st = ArduinoStream(sim, 5000)
    st.connect(IR_ON)
    from .pupil import PupilParams, PupilTracker
    cam = CameraThread(TEST_PATTERN, (640, 480), 30, sim.led_at)
    cam.open()
    cam.pupil_tracker = PupilTracker(PupilParams(box=cam.cap.eye_box()))
    time.sleep(0.5)
    rec = Recording(str(ctx.tmp), "selftest", cam, st, "MJPG", 512, {"name": "selftest"}, ir_blinks=True)
    rec.start()
    time.sleep(3.0)
    info = rec.stop()
    cam.close()
    st.close()
    folder = rec.folder
    for name in ("video.avi", "frames.csv", "analog.wav", "microphone.wav", "ttl_events.csv",
                 "session.json"):
        assert (folder / name).exists(), f"{name} missing"
    js = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    frames = _read_csv(folder / "frames.csv")
    fps = js["video"]["measured_fps"] or 0
    assert fps > 10, f"camera delivered only {fps} fps"
    expect = (float(frames[-1]["time_s"]) - float(frames[0]["time_s"])) * fps + 1
    assert abs(len(frames) - expect) <= 3 and len(frames) >= 30, f"{len(frames)} frames, expected about {expect:.0f}"
    import cv2
    cap = cv2.VideoCapture(str(folder / "video.avi"))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    assert abs(n - len(frames)) <= 1, f"video has {n} frames, frames.csv {len(frames)}"
    cap = cv2.VideoCapture(str(folder / "video.avi"))
    ok, first = cap.read()
    cap.release()
    assert ok and (first.ndim == 2 or (np.abs(first[..., 0].astype(int) - first[..., 2]).max() <= 2)), \
        "the default recording should be greyscale"
    assert js["video"]["image_adjustments"]["greyscale"] is True
    # pupil columns, one value per video frame
    assert "pupil_diameter" in frames[0], list(frames[0].keys())
    ok = [r for r in frames if r["pupil_found"] == "1"]
    assert len(ok) >= 0.9 * len(frames), f"pupil found in {len(ok)} of {len(frames)} frames"
    dia = [float(r["pupil_diameter"]) for r in ok]
    assert 18 < min(dia) and max(dia) < 42, (min(dia), max(dia))
    assert js["pupil"]["found_in_percent_of_frames"] >= 85, js["pupil"]
    for col in ("eye_moving", "pupil_state", "pupil_minor", "brightness", "time_exposure_s"):
        assert col in frames[0], f"frames.csv has no {col} column"
    # IR sync blinks: the test pattern shows the light 50 ms late (simulated camera delay)
    cd = js["camera_delay"]
    true_ms = TestPatternSource.SIM_DELAY_S * 1000
    fi = cd["frame_interval_ms"]
    assert cd["measured"] and cd["n_transitions"] == 4, cd
    # true delay + up to one frame; a loaded CI machine adds frame-generation / scheduling lag to the
    # time stamps (that lag is real camera delay too), so allow a second frame + 15 ms. Real bugs (wrong
    # sign, wrong blink, wrong clock) are off by hundreds of ms.
    assert true_ms - 5 <= cd["delay_ms"] <= true_ms + 2 * fi + 15, \
        f"camera delay {cd['delay_ms']} ms, true {true_ms} ms, frame {fi} ms"
    r_ = frames[len(frames) // 2]
    assert abs(float(r_["time_s"]) - float(r_["time_exposure_s"]) - cd["delay_ms"] / 1000) < 1e-5
    with open(folder / "ttl_events.csv", newline="", encoding="utf-8") as fh:
        assert "video_frame_exposure" in fh.readline()
    ctx.log(f"    camera delay {cd['delay_ms']} ms (simulated {true_ms:.0f} ms + about one frame of {fi:.0f} ms)")
    with wave.open(str(folder / "analog.wav")) as w:
        assert w.getnchannels() == 3 and w.getframerate() == 5000
        ns = w.getnframes()
        raw = np.frombuffer(w.readframes(ns), dtype="<i2").reshape(-1, 3)
    dur = js["duration_s"]
    want = dur * js["arduino"]["measured_rate_hz"]
    assert abs(ns - want) <= 0.01 * want + 60, f"{ns} samples in a {dur:.3f} s recording (expected about {want:.0f})"
    assert ns == js["arduino"]["samples"]
    # the Arduino data ends at Stop, together with the video
    last = js["arduino"]["first_sample_time_s"] + (ns - 1) / js["arduino"]["measured_rate_hz"]
    assert abs(last - dur) < 0.03, f"Arduino data ends at {last:.3f} s, recording at {dur:.3f} s"
    assert raw[:, 0].max() > 900, "TTL missing"
    led_off = np.flatnonzero(raw[:, 2] == 0)
    off_runs = np.split(led_off, np.flatnonzero(np.diff(led_off) > 1) + 1) if len(led_off) else []
    assert len(off_runs) == 2 and all(abs(len(r_) - 500) < 60 for r_ in off_runs), \
        f"IR LED channel should show exactly the two 0.1 s sync blinks, got {[len(r_) for r_ in off_runs]} samples off"
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
    # every TTL edge names the frame at that moment, and that frame's time
    ft = np.array([float(r["time_s"]) for r in frames])
    for r in _read_csv(folder / "ttl_events.csv"):
        if r["video_frame"] == "":
            continue
        i, t, tf = int(r["video_frame"]), float(r["time_s"]), float(r["frame_time_s"])
        assert abs(tf - ft[i]) < 1e-5, "frame_time_s does not match frames.csv"
        assert tf >= t and (i == 0 or ft[i - 1] < t), f"wrong frame {i} for TTL at {t:.4f} s"
    # the sound lines up with the TTL sample by sample: the simulator plays a tone burst
    # exactly while every third TTL pulse is high
    mic = raw[:, 1].astype(float) - 512
    bursts = 0
    for r in ttl:
        k = int(r["sample"])
        if k < 100 or k + 600 > len(mic):
            continue
        before = mic[k - 100:k - 5].std()
        after = mic[k + 5:k + 100].std()
        if after > 3 * before:
            bursts += 1
            onset = k - 50 + int(np.argmax(np.abs(mic[k - 50:k + 50]) > 150))
            assert abs(onset - k) <= 3, f"sound onset {onset} vs TTL sample {k}"
    assert bursts >= 1, "no tone burst found at a TTL pulse"
    ctx.log(f"    {len(frames)} frames, {ns} samples, {len(ttl)} TTL pulses, {bursts} tone burst(s) aligned")


def _fake_atlas(folder: Path):
    """A tiny synthetic Allen-style atlas (gzip NRRD + structure tree) around bregma's CCF corner."""
    import gzip
    folder.mkdir(parents=True, exist_ok=True)
    vol = np.ones((40, 40, 40), np.uint32)            # AP, DV, ML at 25 um: region 1 above, 2 below DV voxel 20
    vol[:, 20:, :] = 2
    vol[:, :, :5] = 0                                  # a strip of "outside"
    hdr = (b"NRRD0004\n# synthetic\ntype: uint32\ndimension: 3\nsizes: 40 40 40\n"
           b"endian: little\nencoding: gzip\n\n")
    (folder / "annotation_25.nrrd").write_bytes(hdr + gzip.compress(vol.tobytes(order="F")))
    tree = {"msg": [{"id": 997, "acronym": "root", "name": "root", "color_hex_triplet": "FFFFFF", "children": [
        {"id": 1, "acronym": "SCs", "name": "superficial SC", "color_hex_triplet": "FF90FF", "children": []},
        {"id": 2, "acronym": "SCm", "name": "motor SC", "color_hex_triplet": "FF7AFF", "children": []}]}]}
    (folder / "structure_graph.json").write_text(json.dumps(tree), encoding="utf-8")
    return vol


def t_probe(ctx):
    from .probe import (BREGMA_CCF, PROBE_TYPES, Atlas, ProbePlan, ProbeSet, read_nrrd, segment_distance)
    p = ProbePlan(pitch=45, distance=1230.3658)
    r = p.readouts()
    assert abs(r["depth_below_surface_um"] - 870) < 0.01 and abs(r["horizontal_travel_um"] - 870) < 0.01, r
    p.target_dv = p.surface_dv + 870
    assert abs(p.plan_to_target()["distance"] - 1230.37) < 0.01
    p.apply_target_plan()
    assert np.allclose(np.mean(p.tips(), axis=0), p.target(), atol=1e-6), "plan to target misses the target"
    for bad in (dict(pitch=0), dict(target_dv=p.surface_dv - 10)):
        q = ProbePlan(**bad)
        try:
            q.plan_to_target()
            raise AssertionError(f"plan to target should refuse {bad}")
        except ValueError:
            pass
    tips = ProbePlan().tips()
    assert len(tips) == 4 and abs(np.linalg.norm(tips[0] - tips[3]) - 750) < 1e-6
    q = ProbePlan()
    q.apply_type("2 shanks")
    t2 = q.tips()
    assert len(t2) == 2 and abs(np.linalg.norm(t2[0] - t2[1]) - 250) < 1e-6
    assert q.matching_type() == "2 shanks"
    q.shank_pitch = 300
    assert q.matching_type() == "Custom"
    assert set(PROBE_TYPES) >= {"NP2.0 4-shank", "NP2.0 1-shank", "NP1.0", "2 shanks"}
    tab = ProbePlan(distance=1230).table()
    assert [round(x[0]) for x in tab] == [0, 500, 1000, 1230, 1500, 2000, 2500] and tab[3][3]
    # two probes
    ps = ProbeSet(n=2)
    a, b = ps.probes
    assert b.entry_ml == -a.entry_ml and b.target_ml == -a.target_ml, "B is not mirrored"
    assert np.allclose([t[1] for t in b.tips()], [-t[1] for t in a.tips()]), "B tips are not mirrored"
    v1 = dict(pitch=90, n_shanks=1, distance=2000)
    ca = ProbeSet(n=2, probes=[ProbePlan(entry_ml=-1000, **v1), ProbePlan(entry_ml=1000, **v1)]).closest_approach()
    assert abs(ca - 2000) < 1, ca
    v4 = dict(pitch=90, yaw=0, distance=2000)          # 4 shanks spread 750 um along ML
    ca = ProbeSet(n=2, probes=[ProbePlan(entry_ml=-1000, **v4), ProbePlan(entry_ml=1000, **v4)]).closest_approach()
    assert abs(ca - (2000 - 750)) < 1, ca
    assert segment_distance([0, 0, 0], [2, 0, 0], [1, -1, 0], [1, 1, 0]) < 1e-9, "crossing segments"
    assert ProbeSet(n=1).closest_approach() is None
    # serialisation: both probes, and an old single-plan dict
    ps.active = 1
    ps.probes[1].distance = 1777
    back = ProbeSet.from_json(ps.to_json())
    assert back.n == 2 and back.active == 1 and back.probes[1].distance == 1777 and back.probes[1].name == "B"
    old = ProbeSet.from_dict(ProbePlan(entry_ap=-3000, distance=999).to_dict())
    assert old.n == 1 and old.probes[0].entry_ap == -3000 and old.probes[0].distance == 999
    blk = ps.session_block()
    assert blk["n"] == 2 and len(blk["probes"]) == 2 and blk["closest_approach_um"] is not None
    assert len(blk["probes"][0]["shanks"]) == 4 and "tip_dv" in blk["probes"][1]["shanks"][0]
    json.dumps(blk)
    # atlas reader + region lookup on a synthetic volume
    folder = ctx.tmp / "fake_atlas"
    vol = _fake_atlas(folder)
    arr, hdr = read_nrrd(str(folder / "annotation_25.nrrd"))
    assert arr.shape == (40, 40, 40) and np.array_equal(arr, vol), "NRRD reader"
    assert Atlas.available(str(folder))
    at = Atlas(str(folder))
    bap, bdv, bml = BREGMA_CCF
    ap, ml = bap - 20 * 25, -bml + 20 * 25               # voxel i = 20, k = 20
    assert at.label(at.region_at(ap, ml, -bdv + 10 * 25)) == "SCs"
    assert at.label(at.region_at(ap, ml, -bdv + 30 * 25)) == "SCm"
    assert at.label(at.region_at(ap, -bml + 2 * 25, -bdv + 30 * 25)) == "outside"
    assert at.label(at.region_at(0, 0, 0)) == "outside"
    runs = at.regions_along([ap, ml, -bdv + 5 * 25], [ap, ml, -bdv + 35 * 25])
    assert [x[2] for x in runs] == ["SCs", "SCm"] and abs(runs[1][0] - 362.5) <= 25, runs
    rgb = at.rgb_slice(at.coronal(ap)[0])
    assert rgb.shape == (40, 40, 3) and tuple(rgb[30, 20]) == (0xFF, 0x7A, 0xFF) and tuple(rgb[30, 0]) == (0, 0, 0)
    assert tuple(rgb[19, 20]) == tuple(int(c * 0.55) for c in (0xFF, 0x90, 0xFF)), "border not darkened"
    sl, ext = at.sagittal(ml)
    assert sl.shape == (40, 40) and ext[0] == bap
    plan = ProbePlan(entry_ap=ap, entry_ml=ml, surface_dv=-bdv + 5 * 25, pitch=90, n_shanks=1, distance=700,
                     tip_length=0, span_length=700)
    rep = ProbeSet(n=1, probes=[plan]).session_block(at)["probes"][0]["shanks"][0]
    assert rep["tip_region"] == "SCm" and [x["region"] for x in rep["recorded_regions"]] == ["SCm", "SCs"], rep
    # the planner window, offscreen, with 1 and 2 probes (no atlas download: dialogs would fail the test)
    from PySide6.QtWidgets import QApplication
    from .probe_gui import ProbePlannerWindow
    app = QApplication.instance() or QApplication(["synccam-selftest"])
    w = ProbePlannerWindow(ProbeSet(), auto_load_atlas=False, atlas_folder=str(ctx.tmp / "no_atlas"))
    w.show()
    app.processEvents()
    assert "B" not in w.results.text() or "Probe B" not in w.results.text()
    w.set_n(2)
    w.set_active(1)
    app.processEvents()
    assert "Probe B" in w.results.text() and "Closest approach" in w.results.text(), w.results.text()
    w.f_dist.setValue(1500)
    assert w.pset.probes[1].distance == 1500 and w.pset.probes[0].distance != 1500
    w.f_type.setCurrentText("2 shanks")
    assert w.pset.probes[1].n_shanks == 2
    w.f_spacing.setValue(300)
    assert w.f_type.currentText() == "Custom" and w.pset.probes[1].probe_type == "Custom"
    w.set_atlas(at)
    app.processEvents()
    assert "recorded:" in w.results.text()
    img = w.grab().toImage()
    assert img.width() > 600 and img.height() > 400
    w.set_n(1)
    assert w.pset.active == 0 and "Probe B" not in w.results.text()
    w.close()
    app.processEvents()


def t_gui(ctx):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from .arduino import SIM_PORT
    from .camera import TEST_PATTERN
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
    win.probe_atlas_folder = str(ctx.tmp / "no_atlas")
    pw = win.show_probe_planner()
    app.processEvents()
    pw.set_n(2)
    pw.f_dist.setValue(1500)
    assert "Probe A:" in win.probe_label.text() and "Probe B:" in win.probe_label.text(), win.probe_label.text()
    assert win.probe_set.n == 2 and win.probe_set.probes[0].distance == 1500
    pw.close()

    from . import uploader as upl
    fb = upl.FakeBootloader()
    real_upload = upl.upload
    upl.upload = lambda port, hex_file=None, progress=None: upl.upload_image(
        fb.factory, upl.parse_ihex(upl.firmware_path().read_text(encoding="ascii")), progress)
    try:
        win.port_combo.addItem("Test board", "test://board")
        win.port_combo.setCurrentIndex(win.port_combo.findData("test://board"))
        assert win.flash_btn.isEnabled()
        assert win.upload_sketch(ask=False), "upload from the button failed"
        assert fb.left_progmode and "uploaded" in win.ard_status.text(), win.ard_status.text()
    finally:
        upl.upload = real_upload
    win.cam_combo.setCurrentIndex(win.cam_combo.findData(TEST_PATTERN))
    win.res_combo.setCurrentIndex(1)
    win.cam_btn.click()
    assert win.camera is not None, "test-pattern camera did not open"
    win.port_combo.setCurrentIndex(win.port_combo.findData(SIM_PORT))
    win.ttl_pin.setCurrentIndex(win.ttl_pin.findData(5))
    win.ard_btn.click()
    win.ir_btn.click()
    assert win.stream is not None, "simulated Arduino did not connect"
    _pump(app, lambda: win.camera.latest is not None and win.stream.total > 2000, 5, "camera and Arduino data")
    assert win.ir_btn.text() == "IR LED (D8): ON", win.ir_btn.text()
    _pump(app, lambda: win.stream.last_led, 3, "IR LED on")
    assert win.rec_btn.isEnabled()
    assert win.camera.latest.image.ndim == 2, "preview should be greyscale by default"
    win.sliders["contrast"][0].setValue(150)
    _pump(app, lambda: win.camera.image_settings.contrast == 1.5, 2, "contrast setting")
    win.auto_btn.click()
    win.grey_chk.setChecked(False)
    _pump(app, lambda: win.camera.latest.image.ndim == 3, 2, "colour preview")
    win.grey_chk.setChecked(True)
    _pump(app, lambda: win.camera.latest.image.ndim == 2, 2, "greyscale preview")
    # calibration: record a clip, scrub, measure the test ruler (10 px per mm)
    from PySide6.QtCore import QPointF
    from .camera import TestPatternSource
    from .gui import CalibrationDialog
    assert "not set" in win.scale_label.text()
    dlg = CalibrationDialog(win.camera, None, win)
    dlg.show()
    dlg._toggle_record()
    # record until the clip has 20 frames (slow build machines deliver fewer frames per second)
    _pump(app, lambda: len(dlg.clip) >= 20, 10, "20 frames in the calibration clip")
    dlg._toggle_record()
    assert len(dlg.clip) >= 20 and dlg.scrub.isEnabled(), f"{len(dlg.clip)} frames in the clip"
    dlg._step(1, False)
    dlg._step(1, True)
    assert dlg.frame_index == dlg.scrub.value() and "frame" in dlg.frame_label.text()
    x0, y0 = TestPatternSource.RULER_ORIGIN
    k_true = TestPatternSource.RULER_PX_PER_MM
    dlg.canvas.points = [QPointF(x0, y0 - 5), QPointF(x0 + 25 * k_true, y0 - 5)]
    dlg.mm_spin.setValue(25.0)
    dlg._update()
    app.processEvents()
    dlg.grab()                                   # paints the frame, line and points
    assert abs(dlg.px_per_mm() - k_true) < 1e-6 and "calibration clip" in dlg.method(), (dlg.px_per_mm(), dlg.method())
    assert win.apply_calibration(dlg)
    dlg.done(0)
    assert win.scale["px_per_mm"] == k_true and "10.00 px/mm" in win.scale_label.text(), win.scale_label.text()
    assert win.pupil_plot.px_per_mm == k_true

    # pupil tracking on the test pattern's synthetic eye
    assert not win.pupil_box.isVisible()
    win.pupil_chk.setChecked(True)
    win.view.eye_box = win.camera.cap.eye_box()
    win._eye_box_drawn(win.view.eye_box)
    _pump(app, lambda: sum(1 for r in win.camera.pupil_log if r[1]) > 15, 5, "pupil found")
    assert win.pupil_box.isVisible() and win.view.pupil is not None and win.view.pupil.found
    app.processEvents()
    win.grab()                                   # paints outline, close-up and plots
    win.pupil_show_chk.setChecked(False)
    assert not win.pupil_box.isVisible() and win.camera.pupil_tracker is not None, \
        "hiding the pupil view must not stop tracking"
    win.pupil_show_chk.setChecked(True)
    win.window_combo.setCurrentIndex(win.window_combo.findData(0.1))
    win.mic_combo.setCurrentIndex(win.mic_combo.findData(64))
    assert win.scope.window_s == 0.1 and win.scope.mic_scale == 64
    win._scope_wheel("window", 1)
    assert win.scope.window_s == 0.25
    app.processEvents()
    win.grab()                                   # paints the zoomed-in plot
    win.freeze_btn.click()
    frozen = win.scope.data.copy()
    t_f = time.time() + 0.3
    _pump(app, lambda: time.time() > t_f, 2, "frozen plot")
    assert np.array_equal(win.scope.data, frozen), "frozen plot kept updating"
    win.freeze_btn.click()
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
    assert js["video"]["frames"] > 20 and js["arduino"]["samples"] > 10000, js
    assert js["ir_led_at_start"] == "on"
    assert js["arduino"]["pins"] == {"ttl": "A5", "microphone": "A1", "ir_led": "D8"}, js["arduino"]["pins"]
    assert js.get("pupil", {}).get("found_in_percent_of_frames", 0) >= 80, js.get("pupil")
    assert win.blink_chk.isChecked() and js["camera_delay"]["measured"], js["camera_delay"]
    assert "Camera delay" in win.rec_status.text(), win.rec_status.text()
    pr = js["probe"]
    assert pr["n"] == 2 and len(pr["probes"]) == 2 and pr["closest_approach_um"] is not None, pr
    assert pr["probes"][0]["plan"]["distance"] == 1500 and pr["probes"][1]["plan"]["name"] == "B"
    assert len(pr["probes"][0]["shanks"]) == 4 and "summary" in pr["probes"][1]
    assert js["scale"]["px_per_mm"] == 10.0 and js["scale"]["size"] == [640, 480], js.get("scale")
    with open(win.last_folder / "frames.csv", newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r["pupil_found"] == "1"]
    r0 = rows[len(rows) // 2]
    assert abs(float(r0["pupil_diameter_mm"]) - float(r0["pupil_diameter"]) / 10.0) < 1e-3, r0
    assert 1.5 < float(r0["pupil_diameter_mm"]) < 4.5, r0      # synthetic pupil: 20-40 px = 2-4 mm
    # short screen (small laptop / Windows scaling): nothing squashed, Record still visible
    win.resize(1100, 560)
    t_end = time.time() + 0.3
    _pump(app, lambda: time.time() > t_end, 2, "resize")
    for b in (win.rec_btn, win.cam_btn, win.ard_btn, win.ir_btn):
        assert b.height() >= b.minimumSizeHint().height() - 1, f"{b.text()} squashed to {b.height()} px"
    assert not win.rec_btn.visibleRegion().isEmpty(), "Record button scrolled out of view"
    win.close()
    app.processEvents()
    # the probe plan comes back from the settings; an old single-plan entry still loads
    win2 = MainWindow(settings)
    assert win2.probe_set.n == 2 and win2.probe_set.probes[0].distance == 1500, win2.probe_set.to_dict()
    assert "B:" in win2.probe_label.text()
    win2.close()
    from .probe import ProbePlan
    settings.setValue("probe_plan", json.dumps(ProbePlan(distance=1111).to_dict()))
    win3 = MainWindow(settings)
    assert win3.probe_set.n == 1 and win3.probe_set.probes[0].distance == 1111
    win3.close()
    app.processEvents()


TESTS = [
    ("version", t_version),
    ("imports", t_imports),
    ("video codecs", t_video_codecs),
    ("picture adjustments", t_image),
    ("Arduino firmware and uploader", t_firmware),
    ("pupil detection and movement", t_pupil),
    ("camera delay from IR blinks", t_camera_delay),
    ("serial packets", t_packets),
    ("TTL edges and clock fit", t_edges_and_clock),
    ("simulated Arduino stream", t_simulated_stream),
    ("serial commands (loopback)", t_serial_commands),
    ("recording to disk", t_recording),
    ("probe planner", t_probe),
    ("GUI end to end", t_gui),
]


class DialogDuringTest(AssertionError):
    pass


def _no_dialogs() -> None:
    """A message box nobody can click would hang the test: make every one fail it instead."""
    from PySide6.QtWidgets import QMessageBox

    def refuse(kind):
        def f(*args, **kwargs):
            texts = [a for a in args if isinstance(a, str)]
            raise DialogDuringTest(f"the app tried to show a {kind} dialog: " + " | ".join(texts))
        return staticmethod(f)

    for kind in ("information", "warning", "critical", "question"):
        setattr(QMessageBox, kind, refuse(kind))
    QMessageBox.exec = lambda self, *a: (_ for _ in ()).throw(
        DialogDuringTest(f"the app tried to show a dialog: {self.text()}"))


def _watchdog(seconds: float, fh) -> None:
    """If the self-test is stuck, write where it is stuck and stop with exit code 3."""
    import threading

    def fire():
        try:
            fh.write(f"SELFTEST TIMEOUT after {seconds:.0f} s - stuck here:\n")
            faulthandler.dump_traceback(file=fh, all_threads=True)
            fh.write("SELFTEST FAILED (timeout)\n")
            fh.flush()
        finally:
            os._exit(3)

    t = threading.Timer(seconds, fire)
    t.daemon = True
    t.start()


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

    _watchdog(300, fh)
    try:
        _no_dialogs()
    except Exception as exc:                 # Qt missing: the imports test reports it
        log(f"    (could not guard dialogs: {exc})")
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
