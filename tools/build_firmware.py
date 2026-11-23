"""Compile firmware/SyncCam_Arduino/SyncCam_Arduino.ino into synccam/firmware/SyncCam_Arduino_328p.hex.

The .hex is bundled with the app, so SyncCam can put the sketch on an Arduino Uno or
Nano by itself (Arduino menu > Upload sketch to board). Run this after changing the sketch:

    sudo apt install gcc-avr avr-libc binutils-avr          (or: brew install avr-gcc)
    git clone --depth 1 https://github.com/arduino/ArduinoCore-avr /tmp/ArduinoCore-avr
    python tools/build_firmware.py /tmp/ArduinoCore-avr

One image serves the Uno and both Nano bootloaders (all ATmega328P at 16 MHz). It is
built with the Nano's 'eightanaloginputs' variant so A6/A7 can be chosen on a Nano.
"""
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKETCH = ROOT / "firmware" / "SyncCam_Arduino" / "SyncCam_Arduino.ino"
OUT = ROOT / "synccam" / "firmware" / "SyncCam_Arduino_328p.hex"
MCU = "atmega328p"
DEFS = ["-DF_CPU=16000000L", "-DARDUINO=10819", "-DARDUINO_AVR_NANO", "-DARDUINO_ARCH_AVR"]
COMMON = ["-g", "-Os", "-Wall", "-ffunction-sections", "-fdata-sections", f"-mmcu={MCU}"] + DEFS
CFLAGS = COMMON + ["-std=gnu11"]
CXXFLAGS = COMMON + ["-std=gnu++11", "-fpermissive", "-fno-exceptions", "-fno-threadsafe-statics"]
FLASH_LIMIT = 30720          # bytes left for the sketch with the Nano's 2 kB bootloader


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(" ".join(map(str, cmd)))
        print(r.stdout + r.stderr)
        raise SystemExit(1)
    return r.stdout + r.stderr


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    core_root = Path(sys.argv[1])
    core = core_root / "cores" / "arduino"
    variant = core_root / "variants" / "eightanaloginputs"
    inc = [f"-I{core}", f"-I{variant}", f"-I{core_root / 'variants' / 'standard'}"]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        objs = []
        for src in sorted(core.glob("*.c")) + sorted(core.glob("*.cpp")) + sorted(core.glob("*.S")):
            obj = tmp / (src.name + ".o")
            if src.suffix == ".c":
                run(["avr-gcc", "-c", *CFLAGS, *inc, str(src), "-o", str(obj)])
            elif src.suffix == ".S":
                run(["avr-gcc", "-c", "-x", "assembler-with-cpp", *COMMON, *inc, str(src), "-o", str(obj)])
            else:
                run(["avr-g++", "-c", *CXXFLAGS, *inc, str(src), "-o", str(obj)])
            objs.append(obj)
        sketch_cpp = tmp / "SyncCam_Arduino.ino.cpp"
        sketch_cpp.write_text("#include <Arduino.h>\n#line 1 \"SyncCam_Arduino.ino\"\n"
                              + SKETCH.read_text(encoding="utf-8"), encoding="utf-8")
        warnings = run(["avr-g++", "-c", *CXXFLAGS, *inc, str(sketch_cpp), "-o", str(tmp / "sketch.o")])
        if warnings.strip():
            print(warnings)
        elf = tmp / "sketch.elf"
        run(["avr-gcc", "-Os", "-Wl,--gc-sections", f"-mmcu={MCU}", "-o", str(elf),
             str(tmp / "sketch.o"), *map(str, objs), "-lm"])
        size = run(["avr-size", "-A", str(elf)])
        text = sum(int(l.split()[1]) for l in size.splitlines() if l.startswith((".text", ".data")))
        if text > FLASH_LIMIT:
            print(f"error: sketch is {text} bytes, more than the {FLASH_LIMIT} bytes available")
            return 1
        OUT.parent.mkdir(parents=True, exist_ok=True)
        run(["avr-objcopy", "-O", "ihex", "-R", ".eeprom", str(elf), str(OUT)])
        shutil.copy(elf, ROOT / "synccam" / "firmware" / ".last_build.elf") if "--keep-elf" in sys.argv else None
    digest = hashlib.sha256(SKETCH.read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:16]   # line-ending independent
    (OUT.parent / "SOURCE_SHA256.txt").write_text(f"{digest}  SyncCam_Arduino.ino\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}  ({text} bytes of flash), sketch sha256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
