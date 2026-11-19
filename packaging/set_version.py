"""Write the release version into synccam/_version.py.

    python packaging/set_version.py 1.2.3        (a leading "v" is removed)

Used by the build workflow so that Help > About, --version and the installers
all show the version of the git tag.
"""
import re
import sys
from pathlib import Path

PATTERN = r"\d+\.\d+\.\d+(?:[.+-][0-9A-Za-z.+-]+)?"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    version = sys.argv[1].strip().lstrip("v")
    if not re.fullmatch(PATTERN, version):
        print(f"error: '{version}' is not a version like 1.2.3 or 1.2.3-rc1", file=sys.stderr)
        return 1
    target = Path(__file__).resolve().parent.parent / "synccam" / "_version.py"
    target.write_text(
        "# This file is rewritten by packaging/set_version.py during a release build\n"
        "# (the version comes from the git tag). Do not edit by hand.\n"
        f'__version__ = "{version}"\n',
        encoding="utf-8",
    )
    print(f"{target} -> {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
