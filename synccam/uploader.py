"""Put the SyncCam sketch on an Arduino Uno / Nano from inside the app (no Arduino IDE).

Uno and Nano boards (ATmega328P) come with a bootloader that accepts a new program
over USB serial with the STK500 v1 protocol (optiboot on the Uno and newer Nanos at
115200 baud, ATmegaBOOT on older Nanos at 57600 baud). This module speaks that
protocol with pySerial, so nothing else needs to be installed.

Other boards (Nano Every, Uno R4, ...) use different bootloaders: use the Arduino IDE
with the .ino file for those.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .app import resource_path

HEX_NAME = "SyncCam_Arduino_328p.hex"
INO_NAME = "SyncCam_Arduino.ino"
PAGE = 128                         # ATmega328P flash page, bytes
APP_LIMIT = 0x7800                 # 30 kB: keep clear of the largest (2 kB) bootloader
SIGNATURES = {b"\x1e\x95\x0f": "ATmega328P", b"\x1e\x95\x14": "ATmega328"}
BAUDS = (115200, 57600)            # optiboot (Uno, new Nano), ATmegaBOOT (old Nano)

STK_OK, STK_INSYNC, CRC_EOP = 0x10, 0x14, 0x20
GET_SYNC, ENTER_PROG, LEAVE_PROG = 0x30, 0x50, 0x51
LOAD_ADDRESS, PROG_PAGE, READ_PAGE, READ_SIGN = 0x55, 0x64, 0x74, 0x75


class UploadError(Exception):
    pass


def firmware_path(name: str = HEX_NAME) -> Path:
    """Bundled firmware file: the .hex, the .ino or SOURCE_SHA256.txt (source and installed app)."""
    here = Path(__file__).resolve().parent
    candidates = [resource_path(name).parent.parent / "synccam" / "firmware" / name,   # installed app
                  here / "firmware" / name,
                  here.parent / "firmware" / "SyncCam_Arduino" / name]               # running from source
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


def firmware_matches_sketch() -> bool:
    """True if the bundled .hex was built from the bundled .ino (see tools/build_firmware.py)."""
    import hashlib
    want = firmware_path("SOURCE_SHA256.txt").read_text(encoding="utf-8").split()[0]
    return sketch_digest(firmware_path(INO_NAME).read_bytes()) == want


def sketch_digest(data: bytes) -> str:
    """Checksum of the sketch text, the same whatever line endings git gave the file."""
    import hashlib
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()[:16]


def parse_ihex(text: str) -> bytes:
    """Intel HEX -> flash image starting at address 0 (gaps filled with 0xFF)."""
    mem = {}
    base = 0
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise UploadError(f"line {n} of the firmware file is not Intel HEX")
        raw = bytes.fromhex(line[1:])
        if sum(raw) & 0xFF:
            raise UploadError(f"checksum error on line {n} of the firmware file")
        count, addr, rtype, data = raw[0], (raw[1] << 8) | raw[2], raw[3], raw[4:4 + raw[0]]
        if len(data) != count:
            raise UploadError(f"line {n} of the firmware file is cut short")
        if rtype == 0:
            for i, b in enumerate(data):
                mem[base + addr + i] = b
        elif rtype == 1:
            break
        elif rtype == 2:
            base = ((data[0] << 8) | data[1]) << 4
        elif rtype == 4:
            base = ((data[0] << 8) | data[1]) << 16
    if not mem:
        raise UploadError("the firmware file is empty")
    size = max(mem) + 1
    img = bytearray(b"\xff" * size)
    for a, b in mem.items():
        img[a] = b
    return bytes(img)


# --------------------------------------------------------------------------
class SerialLink:
    """pySerial port with the reset pulse an Uno/Nano needs to enter its bootloader."""

    def __init__(self, port: str, baud: int):
        import serial
        self.ser = serial.serial_for_url(port, baudrate=baud, timeout=0.3, write_timeout=2.0)

    def reset_board(self) -> None:
        # Same pulse as the Arduino IDE: DTR/RTS low, then high. The board's auto-reset
        # capacitor turns the edge into a short reset, and the bootloader waits ~1 s.
        s = self.ser
        s.dtr = False
        s.rts = False
        time.sleep(0.25)
        s.dtr = True
        s.rts = True
        time.sleep(0.05)
        s.reset_input_buffer()

    def write(self, data: bytes) -> None:
        self.ser.write(data)

    def read(self, n: int, timeout: float) -> bytes:
        self.ser.timeout = timeout
        return self.ser.read(n)

    def flush_input(self) -> None:
        self.ser.reset_input_buffer()

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass


class STK500:
    def __init__(self, link):
        self.link = link

    def _cmd(self, body: bytes, n_reply: int = 0, timeout: float = 0.5) -> bytes:
        self.link.write(body + bytes([CRC_EOP]))
        r = self.link.read(n_reply + 2, timeout)
        if len(r) < n_reply + 2 or r[0] != STK_INSYNC or r[-1] != STK_OK:
            raise UploadError(f"unexpected answer from the bootloader to command 0x{body[0]:02x}: {r.hex() or 'nothing'}")
        return r[1:-1]

    def sync(self, tries: int = 8) -> bool:
        for _ in range(tries):
            self.link.write(bytes([GET_SYNC, CRC_EOP]))
            r = self.link.read(2, 0.15)
            if r == bytes([STK_INSYNC, STK_OK]):
                self.link.flush_input()
                return True
            self.link.flush_input()
        return False

    def signature(self) -> bytes:
        return self._cmd(bytes([READ_SIGN]), 3)

    def enter(self) -> None:
        self._cmd(bytes([ENTER_PROG]))

    def leave(self) -> None:
        self._cmd(bytes([LEAVE_PROG]))

    def _address(self, byte_addr: int) -> None:
        w = byte_addr >> 1                     # the bootloader counts 16-bit words
        self._cmd(bytes([LOAD_ADDRESS, w & 0xFF, (w >> 8) & 0xFF]))

    def write_page(self, addr: int, data: bytes) -> None:
        self._address(addr)
        self._cmd(bytes([PROG_PAGE, len(data) >> 8, len(data) & 0xFF, ord("F")]) + data, 0, timeout=1.0)

    def read_page(self, addr: int, n: int) -> bytes:
        self._address(addr)
        return self._cmd(bytes([READ_PAGE, n >> 8, n & 0xFF, ord("F")]), n, timeout=1.0)


def upload_image(link_factory: Callable[[int], object], image: bytes,
                 progress: Optional[Callable[[float, str], None]] = None,
                 bauds: Tuple[int, ...] = BAUDS) -> dict:
    """Write and verify `image`. link_factory(baud) returns an open link (SerialLink or a test double)."""
    say = progress or (lambda f, msg: None)
    if len(image) > APP_LIMIT:
        raise UploadError(f"firmware is {len(image)} bytes, more than the {APP_LIMIT} bytes available")
    pad = (-len(image)) % PAGE
    image = image + b"\xff" * pad
    pages = [(a, image[a:a + PAGE]) for a in range(0, len(image), PAGE)]
    tried: List[str] = []
    for baud in bauds:
        say(0.0, f"Resetting the board ({baud} baud)...")
        link = link_factory(baud)
        try:
            link.reset_board()
            stk = STK500(link)
            if not stk.sync():
                tried.append(str(baud))
                continue
            sig = stk.signature()
            chip = SIGNATURES.get(bytes(sig))
            if chip is None:
                raise UploadError(f"this board's chip (signature {bytes(sig).hex()}) is not an ATmega328P. "
                                  "Only the Uno and Nano can be programmed from SyncCam; use the Arduino "
                                  "IDE with the .ino file for other boards.")
            stk.enter()
            total = 2 * len(pages)
            for i, (addr, data) in enumerate(pages):
                stk.write_page(addr, data)
                say((i + 1) / total, f"Writing {addr + len(data)} of {len(image)} bytes...")
            for i, (addr, data) in enumerate(pages):
                got = stk.read_page(addr, len(data))
                if got != data:
                    bad = next(k for k in range(len(data)) if got[k] != data[k])
                    raise UploadError(f"verification failed at address 0x{addr + bad:04x}: the board did not "
                                      "store the sketch correctly. Try again, or use a different USB cable.")
                say((len(pages) + i + 1) / total, f"Checking {addr + len(data)} of {len(image)} bytes...")
            stk.leave()
            say(1.0, "Done.")
            return {"baud": baud, "chip": chip, "bytes": len(image) - pad, "pages": len(pages)}
        finally:
            link.close()
    raise UploadError("The board did not answer its bootloader (tried " + " and ".join(tried) + " baud). "
                      "Check that this is the Arduino's port, that no other program (Bonsai, Arduino IDE "
                      "Serial Monitor) has it open, and that the board is an Uno or Nano. For a Nano with "
                      "the old bootloader, unplug and replug it and try again.")


def upload(port: str, hex_file: Optional[Path] = None,
           progress: Optional[Callable[[float, str], None]] = None) -> dict:
    image = parse_ihex(Path(hex_file or firmware_path()).read_text(encoding="ascii"))
    return upload_image(lambda baud: SerialLink(port, baud), image, progress)


# --------------------------------------------------------------------------
class FakeBootloader:
    """Software optiboot for tests: answers STK500 like a real Uno and keeps a flash image."""

    def __init__(self, baud_ok: int = 115200, signature: bytes = b"\x1e\x95\x0f"):
        self.baud_ok = baud_ok
        self.baud = None
        self.sig = signature
        self.flash = bytearray(b"\xff" * 0x8000)
        self._in = bytearray()
        self._out = bytearray()
        self._addr = 0
        self.active = False
        self.reset_count = 0
        self.left_progmode = False

    def factory(self, baud: int):
        self.baud = baud
        return self

    def reset_board(self) -> None:
        self.reset_count += 1
        self.active = self.baud == self.baud_ok
        self._in.clear()
        self._out.clear()

    def write(self, data: bytes) -> None:
        if not self.active:
            return                           # wrong baud: the board sees garbage
        self._in += data
        self._process()

    def _reply(self, payload: bytes = b"") -> None:
        self._out += bytes([STK_INSYNC]) + payload + bytes([STK_OK])

    def _process(self) -> None:
        b = self._in
        while b:
            c = b[0]
            need = {GET_SYNC: 2, READ_SIGN: 2, ENTER_PROG: 2, LEAVE_PROG: 2, LOAD_ADDRESS: 4}.get(c)
            if c in (PROG_PAGE, READ_PAGE):
                if len(b) < 4:
                    return
                n = (b[1] << 8) | b[2]
                need = 5 + (n if c == PROG_PAGE else 0)
            if need is None:
                del b[0]
                continue
            if len(b) < need:
                return
            frame = bytes(b[:need])
            del b[:need]
            if frame[-1] != CRC_EOP:
                continue
            if c == GET_SYNC or c == ENTER_PROG:
                self._reply()
            elif c == LEAVE_PROG:
                self.left_progmode = True
                self._reply()
            elif c == READ_SIGN:
                self._reply(self.sig)
            elif c == LOAD_ADDRESS:
                self._addr = (frame[1] | (frame[2] << 8)) * 2
                self._reply()
            elif c == PROG_PAGE:
                n = (frame[1] << 8) | frame[2]
                self.flash[self._addr:self._addr + n] = frame[4:4 + n]
                self._reply()
            elif c == READ_PAGE:
                n = (frame[1] << 8) | frame[2]
                self._reply(bytes(self.flash[self._addr:self._addr + n]))

    def read(self, n: int, timeout: float) -> bytes:
        out = bytes(self._out[:n])
        del self._out[:n]
        return out

    def flush_input(self) -> None:
        self._out.clear()

    def close(self) -> None:
        pass
