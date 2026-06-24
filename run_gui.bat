@echo off
REM ============================================================
REM  LCR Logger - launch the GUI (Windows). Double-click to run.
REM  Uses the .venv created by install.bat.
REM ============================================================
setlocal
cd /d "%~dp0"

set "PYEXE=.venv\Scripts\python.exe"
if not exist "%PYEXE%" (
    echo Virtual environment not found at .venv
    echo Run install.bat first to create it and install dependencies.
    echo.
    pause
    exit /b 1
)

echo Starting LCR Logger GUI ...
echo Your browser should open automatically. Close this window or press
echo Ctrl+C here to stop the server.
echo.
"%PYEXE%" LCR_gui.py

echo.
echo GUI stopped.
pause
