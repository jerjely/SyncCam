"""Entry point used by PyInstaller and by `python main.py`."""
from synccam.app import main

if __name__ == "__main__":
    raise SystemExit(main())
