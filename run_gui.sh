#!/usr/bin/env bash
# ============================================================
#  LCR Logger - launch the GUI (Linux/macOS).
#  Run:  ./run_gui.sh   (or: bash run_gui.sh)
#  Uses the .venv created by install.sh.
# ============================================================

cd "$(dirname "$0")" || exit 1

PYEXE=".venv/bin/python"
if [ ! -x "$PYEXE" ]; then
    echo "Virtual environment not found at .venv"
    echo "Run ./install.sh first to create it and install dependencies."
    exit 1
fi

echo "Starting LCR Logger GUI ... (Ctrl+C to stop)"
exec "$PYEXE" LCR_gui.py
