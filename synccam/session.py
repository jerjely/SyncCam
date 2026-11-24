"""One recording: video + frame times + Arduino channels, saved into one folder.

Files written (all times in seconds from the moment Record was pressed, on the
computer's clock; sample numbers count from the first saved Arduino sample):

    video.avi / video.mp4   the camera video
    frames.csv              per video frame: time, wall clock, matching Arduino sample
    analog.wav              3 channels, raw values: TTL (A0, 0-1023), microphone (A1, 0-1023), IR LED (0/1)
    microphone.wav          the microphone alone, centred and scaled so it can be listened to
    ttl_events.csv          TTL rising/falling edges: sample, time, and the video frame at that moment
    session.json            settings, clock fit, frame rate, drop counts, camera delay

IR sync blinks: with the IR LED on, the IR LED is switched off for 0.1 s about 0.5 s after the
start and again just before the end. The picture dims; the Arduino records the exact off / on
moments (analog.wav channel 3). First dark frame and first bright frame again minus those
moments = the camera delay (frame reaches the computer later than its exposure). frames.csv then
gets time_exposure_s = time_s - delay. Ported from VisRig (visrig/camsync.py, _ir_blink).

The sample number is the same in analog.wav, microphone.wav, ttl_events.csv and
frames.csv, so sound, TTL and video line up through it.
"""
from __future__ import annotations

import csv
import json
import os
import platform
import threading
import time
import wave
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import numpy as np

from . import __version__
from .arduino import IR_OFF, IR_ON, ArduinoStream, ClockFit, detect_edges
from .camera import CODECS, CameraThread, Frame, VideoWriterThread

TTL_HYST = 40
BLINK_OFF_S = 0.1           # IR LED off this long for a sync blink (VisRig: off_s)
BLINK_LEAD_S = 0.5          # normal frames before the first blink / after the last (VisRig: after_s)


def blink_delays(t: np.ndarray, lum: np.ndarray, blinks) -> list:
    """VisRig's camsync.blink_delays: [(t_off, t_on)] (LED edge times on the frame clock) ->
    per-blink dicts with off_delay_ms (first dark frame - LED off) and on_delay_ms (first bright
    frame again - LED on), or an "error" saying why it could not be measured."""
    out = []
    for t_off, t_on in blinks:
        pre = (t > t_off - 0.4) & (t < t_off - 0.02)
        win = (t >= t_off - 0.02) & (t < t_on + 0.6)
        if pre.sum() < 3 or win.sum() < 3:
            out.append({"t_off_s": round(t_off, 6), "t_on_s": round(t_on, 6),
                        "error": "not enough camera frames around this blink"})
            continue
        base = float(np.median(lum[pre]))
        low = float(np.min(lum[win]))
        if base - low < max(3.0, 0.15 * base):
            out.append({"t_off_s": round(t_off, 6), "t_on_s": round(t_on, 6), "baseline": round(base, 2),
                        "dip": round(low, 2),
                        "error": "no visible dip in image brightness: switching the IR LED off did not darken "
                                 "the picture (is the IR LED the camera's light?)"})
            continue
        thr = (base + low) / 2
        idx = np.flatnonzero(win)
        dark = [i for i in idx if lum[i] < thr]
        first_dark = dark[0]
        after = [i for i in idx if i > first_dark and t[i] > t_on - 0.02 and lum[i] >= thr]
        d = {"t_off_s": round(t_off, 6), "t_on_s": round(t_on, 6), "baseline": round(base, 2),
             "dip": round(low, 2), "off_delay_ms": round(float(t[first_dark] - t_off) * 1000, 2)}
        if after:
            d["on_delay_ms"] = round(float(t[after[0]] - t_on) * 1000, 2)
        out.append(d)
    return out


def safe_name(text: str) -> str:
    keep = "".join(c if (c.isalnum() or c in "-_") else "_" for c in text.strip())
    return keep.strip("_") or "session"


class Recording:
    def __init__(self, out_dir: str, name: str, camera: Optional[CameraThread],
                 stream: Optional[ArduinoStream], codec: str = "MJPG",
                 ttl_threshold: int = 512, extra: Optional[dict] = None, adjust_video: bool = True,
                 scale: Optional[dict] = None, ir_blinks: bool = False):
        if camera is None and stream is None:
            raise ValueError("Nothing to record: open a camera or connect the Arduino.")
        self.started_wall = datetime.now().astimezone()
        self.folder = Path(out_dir) / f"{safe_name(name)}_{self.started_wall:%Y%m%d_%H%M%S}"
        self.camera = camera
        self.stream = stream
        self.codec = codec
        self.ttl_threshold = int(ttl_threshold)
        self.extra = extra or {}
        self.adjust_video = adjust_video
        self.scale = scale                  # {"px_per_mm", "size", "set", "method"} or None
        self.image_changed = False
        self.ir_blinks = bool(ir_blinks and camera is not None and stream is not None)
        self.blink_cmds: List[tuple] = []      # (when the off / on command was sent, perf_counter)
        self._blink_thread: Optional[threading.Thread] = None
        self.frame_bright: List[float] = []
        self.t0 = 0.0
        self.t_stop: Optional[float] = None
        self._lock = threading.Lock()
        # video
        self.writer: Optional[VideoWriterThread] = None
        self.frame_t: List[float] = []
        self.pupil_rows: List[tuple] = []      # per saved frame, when pupil tracking is on
        self.track_pupil = False
        self.pupil_params_at_start: Optional[dict] = None
        self.pupil_changed = False
        self.frames_seen = 0
        self.video_fps = 0.0
        # analog
        self.idx0: Optional[int] = None
        self.idx_stop: Optional[int] = None     # last sample to keep (set by stop())
        self.next_idx: Optional[int] = None
        self.n_samples = 0
        self.filled = 0
        self._last_vals = np.zeros(3, dtype=np.int16)
        self.clock: Optional[ClockFit] = None
        self._wav: Optional[wave.Wave_write] = None
        self._mic: Optional[wave.Wave_write] = None
        self._ttl_state = False
        self._led_state = False
        self.led_rises: List[np.ndarray] = []
        self.led_falls: List[np.ndarray] = []
        self.ttl_rises: List[np.ndarray] = []
        self.ttl_falls: List[np.ndarray] = []
        self.summary: dict = {}

    # ---- start -------------------------------------------------------------
    def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=False)
        self.t0 = time.perf_counter()
        self.started_wall = datetime.now().astimezone()
        if self.camera is not None:
            fps = self.camera.measured_fps() or float(self.camera.fps_req or 30.0)
            self.video_fps = round(fps, 3)
            ext = CODECS.get(self.codec, CODECS["MJPG"])[1]
            self.image_at_start = self.camera.image_settings
            grey = self.adjust_video and self.image_at_start.grey
            self.writer = VideoWriterThread(str(self.folder / f"video{ext}"), self.video_fps,
                                            self.camera.frame_size, self.codec, grey=grey)
            tr = self.camera.pupil_tracker
            self.track_pupil = tr is not None and tr.params.box is not None
            if self.track_pupil:
                self.pupil_params_at_start = tr.params.as_dict()
            self.camera.on_frame = self._on_frame
        if self.stream is not None:
            fs = self.stream.fs
            self.clock = ClockFit(fs, maxlen=2_000_000)
            off, per = self.stream.clock.solve()
            self.idx0 = max(0, int(np.ceil((self.t0 - off) / per)))
            self._wav = wave.open(str(self.folder / "analog.wav"), "wb")
            self._wav.setnchannels(3)
            self._wav.setsampwidth(2)
            self._wav.setframerate(fs)
            self._mic = wave.open(str(self.folder / "microphone.wav"), "wb")
            self._mic.setnchannels(1)
            self._mic.setsampwidth(2)
            self._mic.setframerate(fs)
            hi = self.ttl_threshold + TTL_HYST
            self._ttl_state = bool(self.stream.recent(0.01)[-1:, 0].mean() > hi) if self.stream.total else False
            self._led_state = bool(self.stream.last_led)
            self.stream.add_sink(self._on_samples, replay_from=self.idx0)
        if self.ir_blinks:
            if not self.stream.last_led:
                self.ir_blinks = False          # IR LED is off: nothing to blink
                self.blink_skipped = "IR LED was off at the start"
            else:
                self._blink_thread = threading.Thread(target=self._start_blink, name="synccam-blink", daemon=True)
                self._blink_thread.start()

    # ---- IR sync blinks -------------------------------------------------------
    def _blink(self) -> None:
        """IR LED off for BLINK_OFF_S, then on again (VisRig's _ir_blink)."""
        st = self.stream
        st.set_ir(IR_OFF)
        t_off = time.perf_counter()
        time.sleep(BLINK_OFF_S)
        st.set_ir(IR_ON)
        self.blink_cmds.append((t_off, time.perf_counter()))

    def _start_blink(self) -> None:
        time.sleep(max(0.0, self.t0 + BLINK_LEAD_S - time.perf_counter()))
        if self.t_stop is None and self.stream.error is None:
            try:
                self._blink()
            except Exception as exc:
                self.blink_error = f"{type(exc).__name__}: {exc}"

    # ---- callbacks (other threads) ------------------------------------------
    def _on_frame(self, f: Frame) -> None:
        if f.t < self.t0 or self.t_stop is not None:
            return
        self.frames_seen += 1
        img = f.image if (self.adjust_video or f.raw is None) else f.raw
        if self.writer.put(img):
            self.frame_t.append(f.t)
            self.frame_bright.append(f.brightness)
            if self.track_pupil:
                pr, k = f.pupil, (f.kin or {})
                nan = float("nan")
                self.pupil_rows.append(
                    (pr.found, pr.x, pr.y, pr.diameter, pr.quality, pr.minor,
                     k.get("speed", nan), k.get("moving", ""), k.get("dd_pct", nan), k.get("size", ""))
                    if pr is not None else (False, nan, nan, nan, 0.0, nan, nan, "lost", nan, "lost"))

    def _on_samples(self, idx: np.ndarray, vals: np.ndarray, t: float) -> None:
        with self._lock:
            if self._wav is None:
                return
            keep = idx >= self.idx0
            if self.idx_stop is not None:
                keep &= idx <= self.idx_stop
            if not keep.any():
                return
            idx, vals = idx[keep], vals[keep]
            if self.next_idx is None:
                self.next_idx = int(idx[0]) if idx[0] - self.idx0 > self.stream.fs else self.idx0
            n = int(idx[-1]) - self.next_idx + 1
            if n <= 0:
                return
            full = np.empty((n, 3), dtype=np.int16)
            have = np.zeros(n, dtype=bool)
            pos = idx - self.next_idx
            ok = pos >= 0
            full[pos[ok]] = vals[ok]
            have[pos[ok]] = True
            if not have.all():                      # lost samples: hold the last value
                self.filled += int((~have).sum())
                src = np.where(have, np.arange(n), -1)
                src = np.maximum.accumulate(src)
                prev = np.vstack([self._last_vals[None, :], full])
                full = prev[src + 1]
            self._last_vals = full[-1].copy()
            base = self.next_idx - self.idx0
            self.clock.add(int(idx[-1]), t)
            # edges
            hi, lo = self.ttl_threshold + TTL_HYST, self.ttl_threshold - TTL_HYST
            r, f_, self._ttl_state = detect_edges(full[:, 0], hi, lo, self._ttl_state)
            self.ttl_rises.append(r + base)
            self.ttl_falls.append(f_ + base)
            r, f_, self._led_state = detect_edges(full[:, 2], 0.5, 0.5, self._led_state)
            self.led_rises.append(r + base)
            self.led_falls.append(f_ + base)
            # files
            self._wav.writeframes(full.astype("<i2").tobytes())
            mic = np.clip((full[:, 1].astype(np.int32) - 512) * 64, -32768, 32767).astype("<i2")
            self._mic.writeframes(mic.tobytes())
            self.n_samples += n
            self.next_idx += n

    # ---- live numbers -------------------------------------------------------
    def elapsed(self) -> float:
        return (self.t_stop or time.perf_counter()) - self.t0

    def status(self) -> dict:
        w = self.writer
        return {"elapsed": self.elapsed(), "frames": len(self.frame_t),
                "frames_dropped": w.dropped if w else 0, "backlog": w.backlog if w else 0,
                "samples": self.n_samples, "samples_lost": self.filled,
                "ttl_edges": sum(len(a) for a in self.ttl_rises)}

    # ---- stop ---------------------------------------------------------------
    def stop(self) -> dict:
        if self.ir_blinks:
            if self._blink_thread is not None:
                self._blink_thread.join(2.0)          # a very short recording: let the first blink finish
            if self.stream.error is None and self.stream.last_led:
                try:
                    self._blink()
                    time.sleep(BLINK_LEAD_S)              # normal frames after the last blink
                except Exception as exc:
                    self.blink_error = f"{type(exc).__name__}: {exc}"
        self.t_stop = time.perf_counter()
        if self.camera is not None:
            self.camera.on_frame = None
        if self.stream is not None:
            # keep samples up to the Stop moment, exactly like the start is cut at Record
            off, per = self.clock.solve() if self.clock.pairs else self.stream.clock.solve()
            with self._lock:
                self.idx_stop = max(self.idx0 - 1, int(np.floor((self.t_stop - off) / per)))
            deadline = time.perf_counter() + 1.0          # wait for those samples to arrive
            while time.perf_counter() < deadline:
                with self._lock:
                    done = self.next_idx is not None and self.next_idx > self.idx_stop
                if done or self.stream.error:
                    break
                time.sleep(0.005)
            self.stream.remove_sink(self._on_samples)
        if self.writer is not None:
            self.writer.close()
        with self._lock:
            for w in (self._wav, self._mic):
                if w is not None:
                    w.close()
            self._wav = self._mic = None
        return self._write_files()

    def _sample_time(self, rel_idx: np.ndarray) -> np.ndarray:
        off, per = self.clock.solve()
        return off + (np.asarray(rel_idx, dtype=np.float64) + self.idx0) * per - self.t0

    def _write_files(self) -> dict:
        ft = np.array(self.frame_t, dtype=np.float64) - self.t0
        info: dict = {
            "app": f"SyncCam {__version__}",
            "started": self.started_wall.isoformat(timespec="milliseconds"),
            "duration_s": round(self.elapsed(), 4),
            "computer": f"{platform.system()} {platform.release()} {platform.machine()}",
            "time_reference": "seconds since Record was pressed, computer clock (time.perf_counter)",
            **self.extra,
        }
        fit = None
        if self.stream is not None and self.n_samples:
            off, per = self.clock.solve()
            fit = (off, per)
            info["arduino"] = {
                "firmware": self.stream.info.firmware if self.stream.info else "",
                "port": self.stream.device.name,
                "sample_rate_hz": self.stream.fs,
                "samples": self.n_samples,
                "samples_lost_and_filled": self.filled,
                "measured_rate_hz": round(1.0 / per, 4),
                "rate_warning": ("The Arduino did not keep up with the chosen sample rate. Use "
                                 "measured_rate_hz (not the WAV header rate) for timing, or pick a "
                                 "lower rate." if abs(per * self.stream.fs - 1) > 0.01 else None),
                "first_sample_time_s": round(off + self.idx0 * per - self.t0, 6),
                "clock_fit": "time_s = first_sample_time_s + sample / measured_rate_hz",
                "ttl_threshold_adc": self.ttl_threshold,
                "pins": {"ttl": f"A{self.stream.pins[0]}", "microphone": f"A{self.stream.pins[1]}",
                         "ir_led": f"D{self.stream.pins[2]}"},
                "analog_wav_channels": ["TTL (A0, raw 0-1023)", "microphone (A1, raw 0-1023)",
                                        "IR LED (0/1)"],
            }
        # frames.csv
        if self.camera is not None:
            n = len(ft)
            dur = ft[-1] - ft[0] if n > 1 else 0.0
            if self.scale:
                info["scale"] = {**self.scale, "note": "mm = pixels / px_per_mm (measured with a ruler "
                                                        "at the eye's distance from the camera)"}
            info["video"] = {
                "file": Path(self.writer.path).name,
                "camera": self.camera.name,
                "size": list(self.camera.frame_size),
                "codec": self.codec,
                "requested_fps": self.camera.fps_req,
                "fps_in_file_header": self.video_fps,
                "measured_fps": round((n - 1) / dur, 4) if dur > 0 else None,
                "frames": n,
                "frames_dropped_disk": self.writer.dropped,
                "writer_error": self.writer.error,
                "image_adjustments": (self.image_at_start.as_dict() if self.adjust_video
                                      else "none (raw camera picture recorded)"),
                "image_adjustments_changed_during_recording": self.image_changed and self.adjust_video,
                "note": "Use frames.csv for frame times, not the file's nominal frame rate.",
            }
            lum = np.array(self.frame_bright[:n], float)
            delay_s = None
            cd = self._camera_delay(ft, lum, fit)
            info["camera_delay"] = cd
            if "delay_ms" in cd:
                delay_s = cd["delay_ms"] / 1000.0
            self.camera_delay_s = delay_s
            wall0 = self.started_wall
            if fit is not None:
                off, per = fit
                est = np.round((ft + self.t0 - off) / per - self.idx0).astype(np.int64)
            else:
                est = None
            with open(self.folder / "frames.csv", "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                head = ["frame", "time_s", "wall_time", "arduino_sample", "brightness"]
                if delay_s is not None:
                    head.insert(2, "time_exposure_s")
                pup = self.track_pupil and len(self.pupil_rows) >= n
                k = self.scale["px_per_mm"] if self.scale else None
                if pup:
                    head += ["pupil_found", "pupil_x", "pupil_y", "pupil_diameter", "pupil_quality"]
                    if k:
                        head += ["pupil_x_mm", "pupil_y_mm", "pupil_diameter_mm"]
                    head += ["pupil_minor", "eye_speed_px_s", "eye_moving", "pupil_change_pct_s", "pupil_state"]
                    if k:
                        head += ["pupil_minor_mm", "eye_speed_mm_s"]
                w.writerow(head)
                for i in range(n):
                    row = [i, f"{ft[i]:.6f}",
                           (wall0 + timedelta(seconds=float(ft[i]))).isoformat(timespec="milliseconds"),
                           "" if est is None else int(est[i]),
                           "" if not np.isfinite(lum[i]) else f"{lum[i]:.2f}"]
                    if delay_s is not None:
                        row.insert(2, f"{ft[i] - delay_s:.6f}")
                    if pup:
                        ok, x, y, d, q, mi, sp, mv, dd, st = self.pupil_rows[i]
                        row += [int(bool(ok))] + (["", "", ""] if not ok else
                                                  [f"{x:.2f}", f"{y:.2f}", f"{d:.2f}"]) + [f"{q:.2f}"]
                        if k:
                            row += ["", "", ""] if not ok else [f"{x / k:.4f}", f"{y / k:.4f}", f"{d / k:.4f}"]
                        fin = (lambda v, fmt: "" if not np.isfinite(v) else fmt % v)
                        row += ["" if not ok else f"{mi:.2f}", fin(sp, "%.1f"), mv, fin(dd, "%.2f"), st]
                        if k:
                            row += ["" if not ok else f"{mi / k:.4f}", fin(sp / k, "%.3f")]
                    w.writerow(row)
            if pup:
                found = np.array([r[0] for r in self.pupil_rows[:n]], bool)
                moving = [r[7] for r in self.pupil_rows[:n]]
                states = [r[9] for r in self.pupil_rows[:n]]
                diam = np.array([r[3] for r in self.pupil_rows[:n]], float)[found]
                info["pupil"] = {
                    "tracked": True, **(self.pupil_params_at_start or {}),
                    "settings_changed_during_recording": self.pupil_changed,
                    "found_in_percent_of_frames": round(100.0 * found.mean(), 1) if n else 0.0,
                    "diameter_px_median": round(float(np.median(diam)), 2) if len(diam) else None,
                    "diameter_mm_median": (round(float(np.median(diam)) / k, 4)
                                           if (k and len(diam)) else None),
                    "units": "image pixels; pupil_diameter = long axis of the fitted ellipse, pupil_minor = short axis",
                    "detector": "VisRig PupilDetector (auto threshold, opening, convex hull + ellipse, "
                                "confidence >= 0.6), confident blobs only",
                    "movement": {"eye_moving": "from the centre's velocity over 0.2 s, moving above 20 px/s",
                                 "pupil_state": "from the diameter slope over 1.0 s, dilating / constricting "
                                                "beyond 8 %/s",
                                 "percent_of_frames": {
                                     "eye_moving": round(100.0 * sum(1 for m in moving if m not in
                                                                    ("still", "", "lost")) / max(n, 1), 1),
                                     **{s_: round(100.0 * states.count(s_) / max(n, 1), 1)
                                        for s_ in ("dilating", "constricting", "stable")}}},
                    "columns": "frames.csv: pupil_found, pupil_x, pupil_y, pupil_diameter, pupil_quality, "
                               "pupil_minor, eye_speed_px_s, eye_moving, pupil_change_pct_s, pupil_state "
                               "(+ _mm columns with a calibration)",
                }
        # TTL events, each with the video frame at that moment
        if fit is not None:
            r = np.concatenate(self.ttl_rises) if self.ttl_rises else np.zeros(0, np.int64)
            f = np.concatenate(self.ttl_falls) if self.ttl_falls else np.zeros(0, np.int64)
            ev = sorted([(int(i), "rise") for i in r] + [(int(i), "fall") for i in f])
            times = self._sample_time(np.array([e[0] for e in ev])) if ev else np.zeros(0)
            with open(self.folder / "ttl_events.csv", "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                dly = getattr(self, "camera_delay_s", None)
                head = ["edge", "sample", "time_s", "video_frame", "frame_time_s"]
                if dly is not None:
                    head += ["video_frame_exposure", "frame_exposure_time_s"]
                w.writerow(head)
                for (smp, kind), t in zip(ev, times):
                    frame, ftime = self.frame_at(ft, t)
                    row = [kind, smp, f"{t:.6f}", frame, "" if frame == "" else f"{ftime:.6f}"]
                    if dly is not None:
                        fe, te = self.frame_exposed_at(ft - dly, t)
                        row += [fe, "" if fe == "" else f"{te:.6f}"]
                    w.writerow(row)
            info["ttl_rising_edges"] = int(len(r))
        with open(self.folder / "session.json", "w", encoding="utf-8") as fh:
            json.dump(info, fh, indent=2)
        self.summary = info
        return info

    def _camera_delay(self, ft: np.ndarray, lum: np.ndarray, fit) -> dict:
        """Camera delay from the IR sync blinks, using the Arduino-timed IR LED edges."""
        if not getattr(self, "ir_blinks", False):
            why = getattr(self, "blink_skipped", None) or ("IR sync blinks were off" if self.stream is not None
                                                           else "no Arduino connected")
            return {"measured": False, "reason": why}
        if fit is None or not len(ft):
            return {"measured": False, "reason": "no Arduino data or no frames"}
        falls = np.concatenate(self.led_falls) if self.led_falls else np.zeros(0, np.int64)
        rises = np.concatenate(self.led_rises) if self.led_rises else np.zeros(0, np.int64)
        tf, tr = self._sample_time(falls), self._sample_time(rises)
        blinks = []
        for a in tf:                                     # each off edge with the on edge that follows it
            nxt = tr[(tr > a) & (tr < a + 0.6)]
            if len(nxt):
                blinks.append((float(a), float(nxt[0])))
        if not blinks:
            return {"measured": False, "reason": "the Arduino recorded no IR LED off/on blink (" +
                    (getattr(self, "blink_error", "") or "commands sent but no LED change seen") + ")"}
        res = blink_delays(ft, lum, blinks)
        ds = [v for r in res for k, v in r.items() if k in ("off_delay_ms", "on_delay_ms")]
        iv = np.diff(ft)
        out = {"measured": bool(ds), "method": "IR LED off for 0.1 s at the start and end; first dark / first "
               "bright frame minus the Arduino-recorded LED edge", "blinks": res,
               "frame_interval_ms": round(float(np.median(iv) * 1000), 2) if iv.size else None}
        if ds:
            out.update(delay_ms=round(float(np.median(ds)), 2), delay_spread_ms=round(float(np.ptp(ds)), 2),
                       n_transitions=len(ds), per_transition_ms=ds,
                       note="time_exposure_s = time_s - delay. The delay includes up to one frame interval of "
                            "exposure and frame-time quantisation, so it is known to about +- half a frame.")
        else:
            out["reason"] = "; ".join(sorted({r.get("error", "") for r in res if r.get("error")}))
        return out

    @staticmethod
    def frame_exposed_at(t_exp: np.ndarray, t: float):
        """(frame, exposure time) of the frame whose exposure time is closest to t."""
        if len(t_exp) == 0 or t < t_exp[0] - 0.05 or t > t_exp[-1] + 0.05:
            return "", None
        i = int(np.argmin(np.abs(t_exp - t)))
        return i, float(t_exp[i])

    @staticmethod
    def frame_at(ft: np.ndarray, t: float):
        """(frame index, frame time) for the moment t: the first frame that reached the computer
        at or after t. A frame always arrives a little after its exposure, so this is the frame
        whose exposure is closest to t. ("", None) if t is outside the video."""
        if len(ft) == 0 or t < ft[0] or t > ft[-1]:
            return "", None
        i = int(np.searchsorted(ft, t, side="left"))
        return i, float(ft[i])
