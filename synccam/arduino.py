"""Arduino side of SyncCam: packet format, parser, devices and the streaming reader.

Nothing here imports Qt, so it can be tested and scripted without a display.
The packet format matches firmware/SyncCam_Arduino/SyncCam_Arduino.ino.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import numpy as np

BAUD = 1_000_000
HEADER = 0xA5
PACKET = 6
SIM_PORT = "sim://"
SAMPLE_RATES = [1000, 2000, 5000, 8000, 10000]

IR_OFF, IR_ON, IR_FOLLOW, IR_PULSE = 0, 1, 2, 3
IR_MODE_NAMES = {IR_OFF: "Off", IR_ON: "On", IR_FOLLOW: "Follow TTL", IR_PULSE: "Pulses"}


# --------------------------------------------------------------------------
# Packets
# --------------------------------------------------------------------------
def encode_packet(seq: int, a0: int, a1: int, led: bool, ttl: bool) -> bytes:
    bits = (a0 & 0x3FF) | ((a1 & 0x3FF) << 10) | (int(bool(led)) << 20) | (int(bool(ttl)) << 21)
    b0, b1, b2 = bits & 0xFF, (bits >> 8) & 0xFF, (bits >> 16) & 0xFF
    s = seq & 0xFF
    return bytes([HEADER, s, b0, b1, b2, 0x5A ^ s ^ b0 ^ b1 ^ b2])


def _valid_rows(rows: np.ndarray) -> np.ndarray:
    chk = 0x5A ^ rows[:, 1] ^ rows[:, 2] ^ rows[:, 3] ^ rows[:, 4]
    return (rows[:, 0] == HEADER) & (rows[:, 5] == chk) & ((rows[:, 4] & 0xC0) == 0)


class PacketParser:
    """Turns a byte stream into samples. Survives garbage and partial packets.

    feed() returns an (n, 5) int array with columns seq, a0, a1, led, ttl.
    """

    def __init__(self):
        self._buf = bytearray()
        self.bad_bytes = 0

    def feed(self, data: bytes) -> np.ndarray:
        self._buf += data
        out: List[np.ndarray] = []
        buf = self._buf
        pos = 0
        n = len(buf)
        arr = np.frombuffer(bytes(buf), dtype=np.uint8)
        while n - pos >= PACKET:
            if arr[pos] != HEADER:
                nxt = buf.find(bytes([HEADER]), pos + 1)
                skip = (nxt if nxt >= 0 else n) - pos
                self.bad_bytes += skip
                pos += skip
                continue
            k = (n - pos) // PACKET
            rows = arr[pos:pos + k * PACKET].reshape(k, PACKET).astype(np.int64)
            ok = _valid_rows(rows)
            good = k if ok.all() else int(np.argmin(ok))
            if good:
                out.append(rows[:good])
                pos += good * PACKET
            else:
                self.bad_bytes += 1
                pos += 1
        del self._buf[:pos]
        if not out:
            return np.zeros((0, 5), dtype=np.int64)
        rows = np.concatenate(out)
        bits = rows[:, 2] | (rows[:, 3] << 8) | (rows[:, 4] << 16)
        return np.column_stack([rows[:, 1], bits & 0x3FF, (bits >> 10) & 0x3FF,
                                (bits >> 20) & 1, (bits >> 21) & 1])


class SeqUnwrapper:
    """8-bit packet counter -> continuous sample index, counting lost samples."""

    def __init__(self):
        self.last: Optional[int] = None    # last absolute index
        self.dropped = 0

    def reset(self) -> None:
        self.last = None
        self.dropped = 0

    def unwrap(self, seq: np.ndarray) -> np.ndarray:
        if len(seq) == 0:
            return np.zeros(0, dtype=np.int64)
        seq = seq.astype(np.int64)
        if self.last is None:
            steps = np.diff(seq) % 256
            steps[steps == 0] = 256
            idx = seq[0] + np.concatenate([[0], np.cumsum(steps)])
        else:
            prev = np.concatenate([[self.last & 0xFF], seq[:-1]])
            steps = (seq - prev) % 256
            steps[steps == 0] = 256          # a full wrap is far likelier than a duplicate
            idx = self.last + np.cumsum(steps)
        self.dropped += int(np.sum(steps - 1))
        self.last = int(idx[-1])
        return idx


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------
class SerialArduino:
    """The real board on a serial port (or any pySerial URL such as loop://)."""

    def __init__(self, port: str, baud: int = BAUD):
        self.port = port
        self.baud = baud
        self.name = port
        self._ser = None

    def open(self) -> None:
        import serial
        self._ser = serial.serial_for_url(self.port, baudrate=self.baud, timeout=0.05, write_timeout=1.0)

    @property
    def needs_reset_wait(self) -> bool:
        return "://" not in self.port

    def write(self, data: bytes) -> None:
        self._ser.write(data)

    def read(self) -> bytes:
        ser = self._ser
        if ser is None:
            return b""
        n = ser.in_waiting
        return ser.read(n if n else 1)

    def reset_input(self) -> None:
        if self._ser is not None:
            self._ser.reset_input_buffer()

    def readline(self, timeout: float = 1.0) -> str:
        end = time.monotonic() + timeout
        buf = bytearray()
        while time.monotonic() < end:
            b = self._ser.read(1)
            if b:
                if b == b"\n":
                    return buf.decode("ascii", "replace").strip()
                buf += b
        return buf.decode("ascii", "replace").strip()

    def close(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            ser.close()


class SimulatedArduino:
    """Software copy of the firmware: TTL pulses, a 'microphone' and the IR LED.

    TTL: 100 ms pulses every 0.5 s (with a longer gap every 4th pulse so events are
    distinguishable). Mic: 300 Hz tone plus noise, with a louder 2 kHz burst during
    every third TTL pulse.
    """
    name = "Simulator"
    needs_reset_wait = False

    def __init__(self):
        self.fs = 5000
        self.streaming = False
        self.ir_mode = IR_OFF
        self.pulse_period_s = 1.0
        self.pulse_width_s = 0.05
        self.ttl_threshold = 512
        self.led = False
        self._t0 = 0.0
        self._n = 0
        self._pulse_t0 = time.perf_counter()
        self._out = bytearray()
        self._line = bytearray()
        self._rng = np.random.default_rng(1)
        self._lock = threading.Lock()
        self.is_open = False

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def reset_input(self) -> None:
        with self._lock:
            self._out.clear()

    def write(self, data: bytes) -> None:
        for ch in data:
            if ch in (10, 13):
                if self._line:
                    self._handle(self._line.decode("ascii", "replace"))
                self._line.clear()
            else:
                self._line.append(ch)

    def _handle(self, line: str) -> None:
        c, rest = line[0], line[1:]
        with self._lock:
            if c == "?" and not self.streaming:
                self._out += f"SYNCCAM 1 fs={self.fs}\n".encode()
            elif c == "S":
                self._generate(time.perf_counter())
                self.streaming = True
                self._t0 = time.perf_counter()
                self._n = 0
            elif c == "X":
                self._generate(time.perf_counter())
                self.streaming = False
            elif c == "F" and rest.isdigit() and 100 <= int(rest) <= 10000:
                self.fs = int(rest)
            elif c == "I" and rest.isdigit() and int(rest) <= 3:
                self._generate(time.perf_counter())
                self.ir_mode = int(rest)
                self._pulse_t0 = time.perf_counter()
            elif c == "P":
                try:
                    per, w = (int(x) for x in rest.split(","))
                except ValueError:
                    return
                if per >= 2 and 1 <= w < per:
                    self.pulse_period_s, self.pulse_width_s = per / 1000, w / 1000
                    self._pulse_t0 = time.perf_counter()
            elif c == "T" and rest.isdigit():
                self.ttl_threshold = int(rest)

    @staticmethod
    def ttl_at(t: np.ndarray) -> np.ndarray:
        """Ideal simulated TTL (0/1) at times t (seconds since streaming started)."""
        cycle = np.floor(t / 2.25)
        tc = t - cycle * 2.25
        k = np.floor(tc / 0.5)
        return ((k < 4) & (tc - k * 0.5 < 0.1)).astype(np.int64)

    def _generate(self, now: float) -> None:
        if not self.streaming:
            return
        n_due = int((now - self._t0) * self.fs) - self._n
        if n_due <= 0:
            return
        idx = self._n + np.arange(n_due)
        t = idx / self.fs
        ttl = self.ttl_at(t)
        a0 = np.where(ttl > 0, 1010, 8) + self._rng.integers(-4, 5, n_due)
        k = np.floor((t - np.floor(t / 2.25) * 2.25) / 0.5)
        burst = (ttl > 0) & (k % 3 == 0)
        mic = 512 + 60 * np.sin(2 * math.pi * 300 * t) + 220 * burst * np.sin(2 * math.pi * 2000 * t)
        mic = np.clip(mic + self._rng.normal(0, 12, n_due), 0, 1023).astype(np.int64)
        a0 = np.clip(a0, 0, 1023)
        host_t = self._t0 + t
        if self.ir_mode == IR_ON:
            led = np.ones(n_due, dtype=np.int64)
        elif self.ir_mode == IR_FOLLOW:
            led = ttl
        elif self.ir_mode == IR_PULSE:
            ph = np.mod(host_t - self._pulse_t0, self.pulse_period_s)
            led = (ph < self.pulse_width_s).astype(np.int64)
        else:
            led = np.zeros(n_due, dtype=np.int64)
        bits = (a0 & 0x3FF) | ((mic & 0x3FF) << 10) | (led << 20) | (ttl << 21)
        pk = np.empty((n_due, PACKET), dtype=np.uint8)
        pk[:, 0] = HEADER
        pk[:, 1] = idx & 0xFF
        pk[:, 2] = bits & 0xFF
        pk[:, 3] = (bits >> 8) & 0xFF
        pk[:, 4] = (bits >> 16) & 0xFF
        pk[:, 5] = 0x5A ^ pk[:, 1] ^ pk[:, 2] ^ pk[:, 3] ^ pk[:, 4]
        self._out += pk.tobytes()
        self._n += n_due
        self.led = bool(led[-1])

    def current_led(self) -> bool:
        """LED state right now (used by the test-pattern camera)."""
        now = time.perf_counter()
        if self.ir_mode == IR_ON:
            return True
        if self.ir_mode == IR_PULSE:
            return ((now - self._pulse_t0) % self.pulse_period_s) < self.pulse_width_s
        if self.ir_mode == IR_FOLLOW and self.streaming:
            return bool(self.ttl_at(np.array([now - self._t0]))[0])
        return False

    def read(self) -> bytes:
        time.sleep(0.004)
        with self._lock:
            self._generate(time.perf_counter())
            data = bytes(self._out)
            self._out.clear()
        return data

    def readline(self, timeout: float = 1.0) -> str:
        with self._lock:
            i = self._out.find(b"\n")
            if i < 0:
                return ""
            line = bytes(self._out[:i])
            del self._out[:i + 1]
        return line.decode("ascii", "replace").strip()


def make_arduino(port: str):
    return SimulatedArduino() if port == SIM_PORT else SerialArduino(port)


def list_serial_ports() -> List[Tuple[str, str]]:
    try:
        from serial.tools import list_ports
        return [(p.device, p.description or p.device) for p in sorted(list_ports.comports())]
    except Exception:
        return []


# --------------------------------------------------------------------------
# Clock alignment
# --------------------------------------------------------------------------
class ClockFit:
    """host_time = offset + sample_index * period, fitted on (index, arrival time) pairs.

    Arrival times are the true sample times plus a positive USB delay, so after a
    least-squares slope the offset is moved down to the lower envelope.
    """

    def __init__(self, fs: float, maxlen: int = 20000):
        self.fs = float(fs)
        self.pairs: deque = deque(maxlen=maxlen)

    def add(self, idx: int, t: float) -> None:
        self.pairs.append((idx, t))

    def solve(self) -> Tuple[float, float]:
        """(offset_s, period_s)."""
        if len(self.pairs) < 2:
            if self.pairs:
                i, t = self.pairs[0]
                return t - i / self.fs, 1.0 / self.fs
            return 0.0, 1.0 / self.fs
        a = np.array(self.pairs, dtype=np.float64)
        x, y = a[:, 0], a[:, 1]
        span = x.max() - x.min()
        if span < self.fs * 2:                  # too short for a slope: trust the nominal rate
            period = 1.0 / self.fs
        else:
            period = float(np.polyfit(x - x.mean(), y, 1)[0])
            if not (0.98 / self.fs < period < 1.02 / self.fs):
                period = 1.0 / self.fs
        resid = y - x * period
        offset = float(np.percentile(resid, 2))
        return offset, period


def detect_edges(x: np.ndarray, high: float, low: float,
                 start_state: bool = False) -> Tuple[np.ndarray, np.ndarray, bool]:
    """Schmitt-trigger edges. Returns (rise_indices, fall_indices, end_state).

    An index is the first sample of the new state.
    """
    x = np.asarray(x)
    if len(x) == 0:
        return np.zeros(0, np.int64), np.zeros(0, np.int64), start_state
    code = np.full(len(x), -1, dtype=np.int64)
    code[x > high] = 1
    code[x < low] = 0
    known = np.where(code >= 0, np.arange(len(x)), -1)
    last_known = np.maximum.accumulate(known)
    state = np.where(last_known >= 0, code[np.maximum(last_known, 0)], int(start_state))
    prev = np.concatenate([[int(start_state)], state[:-1]])
    rises = np.flatnonzero((state == 1) & (prev == 0))
    falls = np.flatnonzero((state == 0) & (prev == 1))
    return rises, falls, bool(state[-1])


# --------------------------------------------------------------------------
# Streaming reader
# --------------------------------------------------------------------------
@dataclass
class ArduinoInfo:
    fs: int
    firmware: str


class ArduinoStream:
    """Owns the device, keeps it streaming, and hands samples to listeners.

    Samples are kept in a ring buffer for the live plot; `sinks` (callables taking
    (indices, samples, host_time)) receive every chunk from the reader thread.
    """

    RING_S = 10

    def __init__(self, device, fs: int = 5000):
        self.device = device
        self.fs = int(fs)
        self.info: Optional[ArduinoInfo] = None
        self.parser = PacketParser()
        self.unwrap = SeqUnwrapper()
        self.clock = ClockFit(self.fs)
        self.error: Optional[str] = None
        self.sinks: List[Callable] = []
        self._lock = threading.Lock()
        self._ring = np.zeros((self.fs * self.RING_S, 3), dtype=np.int16)
        self._ring_pos = 0
        self._ring_n = 0
        self.total = 0
        self.last_led = False
        self.last_ttl = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sink_lock = threading.Lock()
        self._history: deque = deque(maxlen=200)   # recent chunks, replayed to a new sink

    # ---- setup -----------------------------------------------------------
    def connect(self, ir_mode: int = IR_OFF, pulse_ms: Tuple[int, int] = (1000, 50),
                ttl_threshold: int = 512) -> ArduinoInfo:
        dev = self.device
        dev.open()
        if dev.needs_reset_wait:
            time.sleep(2.0)                     # Uno/Nano reset when the port opens
        dev.write(b"X\n")
        time.sleep(0.05)
        dev.reset_input()
        line = ""
        for _ in range(3):
            dev.write(b"?\n")
            line = dev.readline(1.0)
            if line.startswith("SYNCCAM"):
                break
        if not line.startswith("SYNCCAM"):
            dev.close()
            raise RuntimeError("No answer from the SyncCam sketch. Is firmware/SyncCam_Arduino uploaded, "
                               f"and is this the right port? (got {line!r})")
        self.set_ir(ir_mode, pulse_ms)
        self.set_threshold(ttl_threshold)
        dev.write(f"F{self.fs}\n".encode())
        dev.write(b"S\n")
        self.info = ArduinoInfo(self.fs, line)
        self._thread = threading.Thread(target=self._main, name="synccam-arduino", daemon=True)
        self._thread.start()
        return self.info

    def set_ir(self, mode: int, pulse_ms: Tuple[int, int] = (1000, 50)) -> None:
        per, w = int(pulse_ms[0]), int(pulse_ms[1])
        self.device.write(f"P{per},{w}\nI{int(mode)}\n".encode())

    def set_threshold(self, adc: int) -> None:
        self.device.write(f"T{int(adc)}\n".encode())

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
        try:
            self.device.write(b"X\nI0\n")
        except Exception:
            pass
        try:
            self.device.close()
        except Exception:
            pass

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- worker ----------------------------------------------------------
    def _main(self) -> None:
        try:
            while not self._stop.is_set():
                data = self.device.read()
                t = time.perf_counter()
                if not data:
                    continue
                s = self.parser.feed(data)
                if len(s) == 0:
                    continue
                idx = self.unwrap.unwrap(s[:, 0])
                self.clock.add(int(idx[-1]), t)
                vals = s[:, 1:4].astype(np.int16)    # a0, a1, led
                self._push_ring(vals)
                self.total = int(idx[-1]) + 1
                self.last_led = bool(s[-1, 3])
                self.last_ttl = bool(s[-1, 4])
                with self._sink_lock:
                    self._history.append((idx, vals, t))
                    for sink in list(self.sinks):
                        sink(idx, vals, t)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def add_sink(self, sink: Callable, replay_from: Optional[int] = None) -> None:
        """Attach a sink. Recent chunks holding samples >= replay_from are delivered first,
        so nothing that arrived just before the call is missed."""
        with self._sink_lock:
            if replay_from is not None:
                for idx, vals, t in self._history:
                    if len(idx) and idx[-1] >= replay_from:
                        sink(idx, vals, t)
            self.sinks.append(sink)

    def remove_sink(self, sink: Callable) -> None:
        with self._sink_lock:
            if sink in self.sinks:
                self.sinks.remove(sink)

    def _push_ring(self, vals: np.ndarray) -> None:
        with self._lock:
            n = len(vals)
            size = len(self._ring)
            if n >= size:
                self._ring[:] = vals[-size:]
                self._ring_pos, self._ring_n = 0, size
                return
            end = self._ring_pos + n
            if end <= size:
                self._ring[self._ring_pos:end] = vals
            else:
                k = size - self._ring_pos
                self._ring[self._ring_pos:] = vals[:k]
                self._ring[:n - k] = vals[k:]
            self._ring_pos = end % size
            self._ring_n = min(size, self._ring_n + n)

    def recent(self, seconds: float) -> np.ndarray:
        """Last `seconds` of samples, oldest first, columns a0, a1, led."""
        with self._lock:
            n = min(self._ring_n, int(seconds * self.fs))
            if n == 0:
                return np.zeros((0, 3), dtype=np.int16)
            idx = (self._ring_pos - n + np.arange(n)) % len(self._ring)
            return self._ring[idx].copy()
