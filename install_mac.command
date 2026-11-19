#!/bin/bash
# Run SyncCam from source on macOS: creates .venv and installs the requirements.
cd "$(dirname "$0")" || exit 1
python3 -m venv .venv || { echo "Python 3 not found. Install it from python.org."; exit 1; }
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt || { echo "Installation failed."; exit 1; }
echo
echo "Installed. Start SyncCam with start_mac.command"
