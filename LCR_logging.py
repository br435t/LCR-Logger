"""
LCR_logging.py -- Stream live LCR measurements from a BK Precision 894 over USBTMC.

HOW TO FIND YOUR VID/PID:
    Linux:   Run `lsusb` with the meter plugged in. Look for an entry like
             "ID 1ab1:0588 B&K Precision". Use those hex values below.
    Windows: Open Device Manager, find the meter (after the driver swap
             below it will appear under "libusb-win32 devices" or "Universal
             Serial Bus devices"), right-click -> Properties -> Details ->
             "Hardware Ids". The VID_xxxx and PID_yyyy fields are what you
             want.

INSTALL DEPENDENCIES:
    pip install python-usbtmc pyusb

DRIVER / PERMISSIONS (do this once, then replug the meter):
    Linux:
        echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="XXXX", MODE="0666"' \
            | sudo tee /etc/udev/rules.d/99-bk894.rules
        sudo udevadm control --reload-rules
        Replace XXXX with your actual VID (no 0x prefix).
    Windows:
        BK Precision's vendor driver claims the device by default, which
        blocks pyusb from seeing it. Install Zadig (https://zadig.akeo.ie/),
        select the BK894 from the dropdown, and replace its driver with
        WinUSB (or libusb-win32). Change the driver for the meter only --
        do not touch anything else in the dropdown.

USAGE:
    python LCR_logging.py              # stream at default freq
    python LCR_logging.py --freq 1000  # stream at 1 kHz
    python LCR_logging.py --sweep      # sweep 20 Hz to 200 kHz

LOGGING:
    All log messages (INFO and above) are written to LCR_logging.log
    in the working directory in addition to the console.

    After a sweep completes, you will be prompted to enter a filename.
    The sweep results are saved into the data/ subfolder of the working
    directory. The folder is created automatically if it does not exist.
    Press Enter without typing a name to skip saving.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

import numpy as np
import usb.core
import usbtmc

# ── Configuration ─────────────────────────────────────────────────────────────

# TODO: Replace with values from `lsusb` output.
VID = 0x0471
PID = 0x2827

# Sweep results are written here, relative to the working directory.
# The folder is created automatically if it does not exist.
DATA_DIR = Path("data")

# How long (seconds) to wait after changing frequency before measuring.
# The 894 switches internal relays; skipping this causes garbage reads.
FREQ_SETTLE_S = 0.8

# How long to wait after sending *TRG before fetching the result.
MEAS_SETTLE_S = 0.4

# Continuous-stream poll interval (seconds).
STREAM_INTERVAL_S = 0.5

# Log file written to the working directory.
LOG_FILE = "LCR_logging.log"

# ── Logging ───────────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    """
    Configure logging to write to both the console and a persistent log file.

    Why two handlers:
        The console handler gives immediate feedback during a run.
        The file handler preserves a record of every session -- useful for
        comparing runs, debugging intermittent errors, or auditing settings.

    Both handlers share the same format so log lines are identical in both
    destinations.
    """
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


_setup_logging()
log = logging.getLogger(__name__)


# ── Low-level helpers ─────────────────────────────────────────────────────────

def reset_usb_pipe(vid: int, pid: int) -> None:
    """
    Clear USB halt conditions on the bulk IN/OUT endpoints.

    Why this matters:
        If a previous session crashed mid-transfer, the USB endpoints can be
        left in a "halted" state. The host and device then disagree on state,
        causing every subsequent read/write to fail silently or raise an error.
        Clearing the halt is like pressing "reset" on the pipe -- both sides
        agree to start fresh.
    """
    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if not isinstance(dev, usb.core.Device):
        raise RuntimeError(
            f"USB device {vid:#06x}:{pid:#06x} not found. "
            "Check cable and VID/PID values."
        )
    try:
        # Kernel-driver detach is a Linux concept. On Windows the driver
        # (WinUSB/libusb-win32 installed via Zadig) already exposes the
        # device to libusb directly, so there is nothing to detach.
        if sys.platform.startswith("linux"):
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
                log.debug("Detached kernel driver from interface 0.")
        dev.clear_halt(0x81)  # Bulk IN  endpoint
        dev.clear_halt(0x02)  # Bulk OUT endpoint
        log.debug("USB endpoints cleared.")
    except Exception as exc:
        log.warning("USB pipe reset warning (may be safe to ignore): %s", exc)


def open_instrument(vid: int, pid: int, timeout_s: float = 10.0) -> usbtmc.Instrument:
    """
    Open the USBTMC instrument and verify communication with *IDN?.

    Returns:
        usbtmc.Instrument -- ready for SCPI commands.
    """
    reset_usb_pipe(vid, pid)
    instr = usbtmc.Instrument(vid, pid)
    instr.timeout = timeout_s
    instr.write("SYST:REM")
    time.sleep(0.2)
    instr.write("*CLS")
    time.sleep(0.5)
    idn = instr.ask("*IDN?").strip() # type: ignore
    log.info("Connected to: %s", idn)
    return instr


def fetch_measurement(instr: usbtmc.Instrument) -> str:
    """
    Trigger a single measurement and return the raw SCPI response string.

    Why split write/read instead of ask():
        ask() immediately reads after writing. If the meter is still
        integrating (especially at low frequencies), you get the previous
        result. The explicit *TRG + sleep + FETCH? pattern ensures you wait
        for the new measurement to complete.

    Returns:
        Raw comma-separated string, e.g. "1.234E+03,5.678E-02,0"
        Fields: [primary value, secondary value, status byte]
    """
    instr.write("*TRG")
    time.sleep(MEAS_SETTLE_S)
    instr.write("FETCH?")
    return instr.read().strip()


def set_frequency(instr: usbtmc.Instrument, freq_hz: float) -> None:
    """Set the test frequency and wait for relay settling."""
    instr.write(f":FREQ {freq_hz:.2f}")
    log.debug(
        "Frequency set to %.2f Hz -- waiting %.1fs for relay settle.",
        freq_hz,
        FREQ_SETTLE_S,
    )
    time.sleep(FREQ_SETTLE_S)


# ── Sweep result saving ───────────────────────────────────────────────────────

def prompt_and_save(rows: list[tuple[float, str]]) -> None:
    """
    Prompt the user for a filename and write sweep results to the data folder.

    Args:
        rows: List of (frequency_hz, raw_measurement_string) tuples collected
              during the sweep.

    Behaviour:
        - If the user enters a name, the file is written to DATA_DIR and the
          full path is logged.
        - If the user presses Enter without typing a name, saving is skipped.
        - A .txt extension is appended automatically if none is provided.
        - DATA_DIR is created automatically if it does not already exist.
    """
    print()
    name = input("Enter filename to save sweep results (or press Enter to skip): ").strip()

    if not name:
        log.info("Save skipped -- no filename entered.")
        return

    # Resolve path into the data folder, appending .txt if no extension given.
    stem = Path(name)
    if not stem.suffix:
        stem = stem.with_suffix(".txt")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / stem

    header  = f"{'Freq (Hz)':>14}  {'Primary':>16}  {'Secondary':>16}  {'Status':>8}\n"
    divider = "-" * 60 + "\n"

    with path.open("w", encoding="utf-8") as f:
        f.write("BK Precision 894 -- Frequency Sweep Results\n")
        f.write(divider)
        f.write(header)
        f.write(divider)

        for freq, data in rows:
            parts     = data.split(",")
            primary   = parts[0].strip() if len(parts) > 0 else "?"
            secondary = parts[1].strip() if len(parts) > 1 else "?"
            status    = parts[2].strip() if len(parts) > 2 else "?"
            f.write(f"  {freq:14.2f}  {primary:>16}  {secondary:>16}  {status:>8}\n")

    log.info("Sweep results saved to: %s", path.resolve())


# ── Modes ─────────────────────────────────────────────────────────────────────

def stream_continuous(instr: usbtmc.Instrument, freq_hz: float) -> None:
    """
    Poll the instrument continuously at a fixed frequency.
    Press Ctrl+C to stop.
    """
    set_frequency(instr, freq_hz)
    log.info("Streaming at %.2f Hz. Press Ctrl+C to stop.", freq_hz)

    while True:
        data = fetch_measurement(instr)
        print(f"  {data}")
        time.sleep(STREAM_INTERVAL_S)


def run_sweep(instr: usbtmc.Instrument) -> None:
    """
    Logarithmic frequency sweep from 20 Hz to 200 kHz (20 points).
    Prints each step to the console, then prompts to save results.
    """
    freqs = np.logspace(np.log10(20), np.log10(200_000), num=20)
    log.info("Starting sweep: %d points from 20 Hz to 200 kHz.", len(freqs))

    print(f"\n{'Freq (Hz)':>14}  {'Measurement'}")
    print("-" * 50)

    rows: list[tuple[float, str]] = []

    for freq in freqs:
        set_frequency(instr, freq)
        data = fetch_measurement(instr)
        rows.append((freq, data))
        print(f"  {freq:14.2f}  {data}")

    log.info("Sweep complete. %d points collected.", len(rows))
    prompt_and_save(rows)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="BK 894 LCR meter USBTMC streamer.")
    parser.add_argument(
        "--freq",
        type=float,
        default=1000.0,
        help="Test frequency in Hz for continuous stream (default: 1000).",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Run a logarithmic frequency sweep instead of streaming.",
    )
    args = parser.parse_args()

    if VID == 0 or PID == 0:
        log.error(
            "VID and PID are not set. Run `lsusb` to find your device IDs "
            "and update the VID/PID constants at the top of this file."
        )
        return

    instr = None
    try:
        instr = open_instrument(VID, PID)

        if args.sweep:
            run_sweep(instr)
        else:
            stream_continuous(instr, args.freq)

    except KeyboardInterrupt:
        log.info("Stopped by user.")
    except Exception as exc:
        log.error("Fatal error: %s", exc)
        log.info("TIP: Toggle the [LOCAL] button on the meter to reset remote state.")
    finally:
        if instr:
            try:
                instr.write("SYST:LOC")
                instr.close()
                log.debug("Instrument closed.")
            except Exception:
                pass


if __name__ == "__main__":
    main()