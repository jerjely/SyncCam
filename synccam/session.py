"""One recording: video + frame times + Arduino channels, saved into one folder.

Files written (all times in seconds from the moment Record was pressed, on the
computer's clock; sample numbers count from the first saved Arduino sample):

    video.avi / video.mp4   the camera video
    frames.csv              per video frame: time, wall clock, matching Arduino sample
    analog.wav              3 channels, raw values: TTL (A0, 0-1023), microphone (A1, 0-1023), IR LED (0/1)
    microphone.wav          the microphone alone, centred and scaled so it can be listened to
    ttl_events.csv          TTL rising/falling edges: sample, time, and the video frame at that moment
    session.json            settings, clock fit, frame rate, drop counts

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
from .arduino import ArduinoStream, ClockFit, detect_edges
from .camera import CODECS, CameraThread, Frame, VideoWriterThread

TTL_HYST = 40


def safe_name(text: str) -> str:
    keep = "".join(c if (c.isalnum() or c in "-_") else "_" for c in text.strip())
    return keep.strip("_") or "session"


class Recording:
    def __init__(self, out_dir: str, name: str, camera: Optional[CameraThread],
                 stream: Optional[ArduinoStream], codec: str = "MJPG",
                 ttl_threshold: int = 512, extra: Optional[dict] = None):
        if camera is None and stream is None:
            raise ValueError("Nothing to record: open a camera or connect the Arduino.")
        self.started_wall = datetime.now().astimezone()
        self.folder = Path(out_dir) / f"{safe_name(name)}_{self.started_wall:%Y%m%d_%H%M%S}"
        self.camera = camera
        self.stream = stream
        self.codec = codec
        self.ttl_threshold = int(ttl_threshold)
        self.extra = extra or {}
        self.t0 = 0.0
        self.t_stop: Optional[float] = None
        self._lock = threading.Lock()
        # video
        self.writer: Optional[VideoWriterThread] = None
        self.frame_t: List[float] = []
        self.frames_seen = 0
        self.video_fps = 0.0
        # analog
        self.idx0: Optional[int] = None
        self.next_idx: Optional[int] = None
        self.n_samples = 0
        self.filled = 0
        self._last_vals = np.zeros(3, dtype=np.int16)
        self.clock: Optional[ClockFit] = None
        self._wav: Optional[wave.Wave_write] = None
        self._mic: Optional[wave.Wave_write] = None
        self._ttl_state = False
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
            self.writer = VideoWriterThread(str(self.folder / f"video{ext}"), self.video_fps,
                                            self.camera.frame_size, self.codec)
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
            self.stream.add_sink(self._on_samples, replay_from=self.idx0)

    # ---- callbacks (other threads) ------------------------------------------
    def _on_frame(self, f: Frame) -> None:
        if f.t < self.t0 or self.t_stop is not None:
            return
        self.frames_seen += 1
        if self.writer.put(f.image):
            self.frame_t.append(f.t)

    def _on_samples(self, idx: np.ndarray, vals: np.ndarray, t: float) -> None:
        with self._lock:
            if self._wav is None:
                return
            keep = idx >= self.idx0
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
        self.t_stop = time.perf_counter()
        if self.camera is not None:
            self.camera.on_frame = None
        if self.stream is not None:
            time.sleep(0.05)       # let the last chunk arrive
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
                "note": "Use frames.csv for frame times, not the file's nominal frame rate.",
            }
            wall0 = self.started_wall
            if fit is not None:
                off, per = fit
                est = np.round((ft + self.t0 - off) / per - self.idx0).astype(np.int64)
            else:
                est = None
            with open(self.folder / "frames.csv", "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["frame", "time_s", "wall_time", "arduino_sample"])
                for i in range(n):
                    w.writerow([i, f"{ft[i]:.6f}",
                                (wall0 + timedelta(seconds=float(ft[i]))).isoformat(timespec="milliseconds"),
                                "" if est is None else int(est[i])])
        # TTL events, each with the video frame at that moment
        if fit is not None:
            r = np.concatenate(self.ttl_rises) if self.ttl_rises else np.zeros(0, np.int64)
            f = np.concatenate(self.ttl_falls) if self.ttl_falls else np.zeros(0, np.int64)
            ev = sorted([(int(i), "rise") for i in r] + [(int(i), "fall") for i in f])
            times = self._sample_time(np.array([e[0] for e in ev])) if ev else np.zeros(0)
            with open(self.folder / "ttl_events.csv", "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["edge", "sample", "time_s", "video_frame", "frame_time_s"])
                for (smp, kind), t in zip(ev, times):
                    frame, ftime = self.frame_at(ft, t)
                    w.writerow([kind, smp, f"{t:.6f}", frame, "" if frame == "" else f"{ftime:.6f}"])
            info["ttl_rising_edges"] = int(len(r))
        with open(self.folder / "session.json", "w", encoding="utf-8") as fh:
            json.dump(info, fh, indent=2)
        self.summary = info
        return info

    @staticmethod
    def frame_at(ft: np.ndarray, t: float):
        """(frame index, frame time) for the moment t: the first frame that reached the computer
        at or after t. A frame always arrives a little after its exposure, so this is the frame
        whose exposure is closest to t. ("", None) if t is outside the video."""
        if len(ft) == 0 or t < ft[0] or t > ft[-1]:
            return "", None
        i = int(np.searchsorted(ft, t, side="left"))
        return i, float(ft[i])
