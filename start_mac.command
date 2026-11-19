#!/bin/bash
cd "$(dirname "$0")" || exit 1
[ -x .venv/bin/python ] || { echo "Run install_mac.command first."; exit 1; }
exec .venv/bin/python main.py
