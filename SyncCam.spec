# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build file, used unchanged on Windows, macOS and Linux:
#     pyinstaller --noconfirm --clean SyncCam.spec
# Windows -> dist/SyncCam/SyncCam.exe (folder, packed by Inno Setup)
# macOS   -> dist/SyncCam.app          (packed by packaging/macos/build_pkg.sh)
import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH)

ns = {}
exec((ROOT / "synccam" / "_version.py").read_text(encoding="utf-8"), ns)
VERSION = ns["__version__"]
m = re.match(r"\d+\.\d+\.\d+", VERSION)
NUMERIC_VERSION = m.group(0) if m else "0.0.0"

if sys.platform == "win32":
    ICON = str(ROOT / "assets" / "SyncCam.ico")
elif sys.platform == "darwin":
    ICON = str(ROOT / "assets" / "SyncCam.icns")
else:
    ICON = None

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / "assets" / "icon_512.png"), "assets"),
        (str(ROOT / "assets" / "icon_256.png"), "assets"),
        # Arduino sketch, its compiled firmware (for Arduino > Upload sketch) and the source checksum
        (str(ROOT / "synccam" / "firmware" / "SyncCam_Arduino_328p.hex"), "synccam/firmware"),
        (str(ROOT / "synccam" / "firmware" / "SOURCE_SHA256.txt"), "synccam/firmware"),
        (str(ROOT / "firmware" / "SyncCam_Arduino" / "SyncCam_Arduino.ino"), "synccam/firmware"),
    ],
    # pySerial loads its URL handlers by name at run time, so list the one the self-test uses
    hiddenimports=["serial.urlhandler.protocol_loop", "serial.tools.list_ports"],
    excludes=["tkinter", "matplotlib", "scipy", "PIL", "yaml", "unittest", "pydoc",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtWebEngineCore", "PySide6.QtNetwork",
              "PySide6.QtSql", "PySide6.QtSvg", "PySide6.QtPdf", "PySide6.Qt3DCore"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SyncCam",
    console=False,          # windowed app; --selftest writes its report to a file
    icon=ICON,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="SyncCam")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="SyncCam.app",
        icon=ICON,
        bundle_identifier="io.github.jerjely.synccam",
        version=NUMERIC_VERSION,
        info_plist={
            "CFBundleName": "SyncCam",
            "CFBundleDisplayName": "SyncCam",
            "CFBundleShortVersionString": NUMERIC_VERSION,
            "CFBundleVersion": NUMERIC_VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            # without this key macOS kills the app the moment it touches the camera
            "NSCameraUsageDescription": "SyncCam records video from your USB camera.",
            "NSHumanReadableCopyright": "Copyright (c) 2026 Gergely Szarka. MIT License.",
        },
    )
