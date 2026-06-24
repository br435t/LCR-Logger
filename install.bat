@echo off
REM ============================================================
REM  LCR Logger - one-click environment setup (Windows)
REM  Double-click this file. It creates a .venv next to it and
REM  installs the dependencies listed in requirements.txt.
REM ============================================================
setlocal
cd /d "%~dp0"

echo ==============================================
echo   LCR Logger - environment setup (Windows)
echo ==============================================
echo.

REM --- Locate a Python 3 interpreter ---------------------------
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY (
    python --version >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo ERROR: Python 3 was not found on your PATH.
    echo Install it from https://www.python.org/downloads/
    echo ^(tick "Add python.exe to PATH" during setup^), then re-run this file.
    goto :end
)

echo Using Python:
%PY% --version
echo.

REM --- Create the virtual environment (reuse if present) -------
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo ERROR: could not create the virtual environment.
        goto :end
    )
) else (
    echo Reusing existing .venv
)

REM --- Install dependencies ------------------------------------
echo.
echo Installing dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: dependency installation failed.
    echo If you are on a corporate network that does SSL inspection, pip may
    echo need your organisation's root CA. See HANDOFF.md "Environment quirks".
    goto :end
)

echo.
echo ==============================================
echo   Setup complete.
echo   Run the GUI:  .venv\Scripts\python.exe LCR_gui.py
echo   Or activate:  .venv\Scripts\activate
echo ==============================================

:end
echo.
pause
