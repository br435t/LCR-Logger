#!/usr/bin/env bash
# ============================================================
#  LCR Logger - one-click environment setup (Linux/macOS)
#  Run:  install.sh   (or: bash install.sh)
#  Creates a .venv next to this script and installs the
#  dependencies listed in requirements.txt.
# ============================================================

cd "$(dirname "$0")" || exit 1

echo "=============================================="
echo "  LCR Logger - environment setup"
echo "=============================================="
echo

# --- Locate a Python 3 interpreter --------------------------
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1 && \
     python -c 'import sys; raise SystemExit(0 if sys.version_info[0] >= 3 else 1)'; then
    PY=python
else
    echo "ERROR: Python 3 was not found on PATH."
    echo "Install it, e.g.:  sudo apt install python3 python3-venv"
    exit 1
fi
echo "Using Python: $("$PY" --version 2>&1)"
echo

# --- Create the virtual environment (reuse if present) ------
if [ ! -x ".venv/bin/python" ]; then
    echo "Creating virtual environment in .venv ..."
    if ! "$PY" -m venv .venv; then
        echo "ERROR: could not create the virtual environment."
        echo "On Debian/Ubuntu you may need:  sudo apt install python3-venv"
        exit 1
    fi
else
    echo "Reusing existing .venv"
fi

# --- Install dependencies -----------------------------------
echo
echo "Installing dependencies ..."
.venv/bin/python -m pip install --upgrade pip
if ! .venv/bin/python -m pip install -r requirements.txt; then
    echo
    echo "ERROR: dependency installation failed."
    echo "If you are behind a corporate proxy with SSL inspection, pip may need"
    echo "your organisation's root CA. See HANDOFF.md \"Environment quirks\"."
    exit 1
fi

echo
echo "=============================================="
echo "  Setup complete."
echo "  Run the GUI:  .venv/bin/python LCR_gui.py"
echo "  Or activate:  source .venv/bin/activate"
echo "=============================================="
