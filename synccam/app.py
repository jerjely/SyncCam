"""Command line entry point and small helpers shared by the GUI and the self-test."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import APP_NAME, __version__


def resource_path(name: str) -> Path:
    """Path of a bundled file in assets/ (works from source and when frozen)."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent.parent
    return base / "assets" / name


def _say(text: str) -> None:
    # windowed builds (Windows .exe / macOS .app) have no stdout
    if sys.stdout is not None:
        try:
            print(text)
        except Exception:
            pass


def _hard_exit(code: int) -> None:
    """Leave without Python/Qt interpreter teardown.

    Tearing down PySide6 at interpreter exit can crash in frozen builds (access violation on
    Windows, segmentation fault on macOS) *after* all work has finished, turning a pass into
    a non-zero exit code. Everything is already flushed and closed at this point.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:
            pass
    os._exit(int(code))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=APP_NAME, description="USB camera + Arduino sync recorder.")
    p.add_argument("--version", action="store_true", help="print the version and exit")
    p.add_argument("--selftest", action="store_true",
                   help="run the built-in self-test with a synthetic camera and a simulated Arduino, "
                        "then exit (exit code 0 = everything works)")
    p.add_argument("--selftest-log", metavar="FILE", help="where the self-test writes its report")
    p.add_argument("--expect-version", metavar="X.Y.Z",
                   help="self-test fails if the built-in version differs")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        _say(f"{APP_NAME} {__version__}")
        return 0
    if args.selftest:
        from .selftest import run_selftest
        rc = run_selftest(log_path=args.selftest_log, expect_version=args.expect_version)
        _hard_exit(rc)
    from .gui import run_gui
    rc = run_gui()
    if getattr(sys, "frozen", False):
        _hard_exit(rc)
    return rc
