"""
LCR_logging.py -- Stream live LCR measurements from a BK Precision 894/895
over a serial port (USB Virtual COM, RS-232, or USB-to-serial adapter).

WHY SERIAL INSTEAD OF USBTMC:
    The meter's USBTMC interface needs the WinUSB / libusb-win32 driver
    swap (via Zadig) on Windows, which requires admin rights. The serial
    interfaces below use only stock OS drivers, so no admin is needed.

INTERFACE OPTIONS (the meter only listens on ONE interface at a time --
the front-panel System / Setup / Interface setting must match the
transport you're actually using):
    1. USB Virtual COM (USBCDC) -- on the meter, set Interface to
       "USBCDC" / "Virtual COM". Plug the meter in; Windows or Linux
       enumerates it as a standard COM port using the built-in CDC
       driver.
    2. RS-232 -- on the meter, set Interface to "RS-232C". Connect a
       null-modem cable (pins 2/3 swapped -- see programming manual
       p.6) to either a PC RS-232 port or a USB-to-RS232 adapter.
    3. LAN -- not supported by this script. If you need LAN, talk to
       the meter via TCP on port 5025; the SCPI commands are identical.

FRONT-PANEL STATE:
    Before running, exit any menus so the meter is back on its main
    measurement screen showing live readings. *TRG and FETCH? only
    return data while the measurement loop is running on the front
    panel -- if the meter is parked in System / Setup or any other
    menu, FETCH? comes back empty and streaming prints blank lines.

FIND YOUR PORT:
    Windows: Device Manager -> Ports (COM & LPT). Note the COMx number.
    Linux:   /dev/ttyACM0 (USB-CDC) or /dev/ttyUSB0 (USB-serial).
    macOS:   /dev/cu.usbmodem* or /dev/cu.usbserial-*.
    Or run:  python LCR_logging.py --list-ports

SERIAL SETTINGS (must match the meter's front-panel setting):
    Baud:   9600 (default), 19200, 28800, 38400, 48000, 57600, or 115200.
    Frame:  8 data bits, 1 stop bit, no parity, no flow control.

INSTALL DEPENDENCIES:
    pip install -r requirements.txt

USAGE:
    python LCR_logging.py --port COM3                # stream at default 1 kHz
    python LCR_logging.py --port COM3 --freq 1000    # stream at 1 kHz
    python LCR_logging.py --port COM3 --sweep        # log-sweep 20 Hz -> 200 kHz
    python LCR_logging.py --list-ports               # list available ports

LOGGING:
    All log messages (INFO and above) are written to LCR_logging.log
    in the working directory in addition to the console.

    After a sweep completes, you will be prompted to enter a filename.
    Two files are written into the data/ subfolder using that name as a
    stem: a human-readable .txt and a .csv for analysis. Any extension
    you type is ignored -- both formats are always produced. The folder
    is created automatically if it does not exist. Press Enter without
    typing a name to skip saving.
"""

import argparse
import csv
import logging
import time
from pathlib import Path

import numpy as np
import serial
import serial.tools.list_ports

# ── Configuration ─────────────────────────────────────────────────────────────

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

# Per-command delay. Manual p.7: the meter has no hardware flow control on
# either RS-232 or USB-CDC, so back-to-back commands can overrun its input
# buffer. A small sleep after each write prevents that.
CMD_DELAY_S = 0.05

# Default serial settings. The baud rate must match what is configured on
# the meter's front panel (System -> Interface).
DEFAULT_BAUD = 9600
SERIAL_TIMEOUT_S = 2.0

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


# ── Serial helpers ────────────────────────────────────────────────────────────

def scpi_write(ser: serial.Serial, cmd: str) -> None:
    """
    Send a SCPI command, terminated with a newline.

    Why the sleep:
        Manual p.7 -- neither RS-232 nor USB-CDC supports hardware flow
        control on this meter, so the host can outpace the meter's input
        parser. A short delay after every write prevents buffer overruns.
    """
    ser.write(cmd.encode("ascii") + b"\n")
    ser.flush()
    time.sleep(CMD_DELAY_S)


def scpi_query(ser: serial.Serial, cmd: str) -> str:
    """Send a SCPI query and return the response, stripped of whitespace."""
    scpi_write(ser, cmd)
    line = ser.read_until(b"\n")
    return line.decode("ascii", errors="replace").strip()


def list_serial_ports() -> None:
    """Print every serial port the OS currently knows about."""
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return
    for p in ports:
        line = f"{p.device}  --  {p.description}"
        if p.hwid and p.hwid != "n/a":
            line += f"  [{p.hwid}]"
        print(line)


# ── Instrument open ───────────────────────────────────────────────────────────

def open_instrument(
    port: str,
    baud: int = DEFAULT_BAUD,
    timeout_s: float = SERIAL_TIMEOUT_S,
) -> serial.Serial:
    """
    Open the serial port to the LCR meter and verify communication with *IDN?.
    """
    ser = serial.Serial(
        port=port,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=timeout_s,
        write_timeout=timeout_s,
    )
    # Drop any stale bytes from a previous session.
    time.sleep(0.2)
    ser.reset_input_buffer()
    ser.reset_output_buffer()

    scpi_write(ser, "*CLS")
    # *TRG (used by fetch_measurement) only fires when the trigger source
    # is BUS. The factory default is INTernal, in which case *TRG is
    # ignored and FETCH? returns the free-running result instead of a
    # freshly triggered one.
    scpi_write(ser, "TRIG:SOUR BUS")
    time.sleep(0.3)
    idn = scpi_query(ser, "*IDN?")
    if not idn:
        ser.close()
        raise RuntimeError(
            f"No response from meter on {port}. Check the baud rate, the "
            "cable, and that the meter's USB mode is set to USBCDC (not "
            "USBTMC) on the front panel."
        )
    log.info("Connected to: %s", idn)
    return ser


# ── Low-level helpers ─────────────────────────────────────────────────────────

def fetch_measurement(ser: serial.Serial) -> str:
    """
    Trigger a single measurement and return the raw SCPI response string.

    Pattern: *TRG triggers a measurement and pushes the result to the output
    buffer (manual p.9); MEAS_SETTLE_S lets the meter finish integrating --
    especially important at low frequencies -- and FETCH? then queries the
    most recent buffered result.

    Note: *TRG only fires when the meter's trigger source is BUS. The
    factory default is INTernal, so open_instrument sends TRIG:SOUR BUS
    at startup -- if that ever stops happening, *TRG will be silently
    ignored and FETCH? will return the free-running measurement instead.

    Returns:
        Raw comma-separated string, e.g. "1.234E+03,5.678E-02,0".
        Fields: <primary>, <secondary>, <status>.
        Manual p.31: <status> is "00" for a good reading; -1, +1..+4 flag
        various error conditions. If the comparator is enabled, a fourth
        <bin number> field is also appended; this script ignores it.
    """
    scpi_write(ser, "*TRG")
    time.sleep(MEAS_SETTLE_S)
    return scpi_query(ser, "FETCH?")


def set_frequency(ser: serial.Serial, freq_hz: float) -> None:
    """Set the test frequency and wait for relay settling."""
    scpi_write(ser, f":FREQ {freq_hz:.2f}")
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
        - If the user enters a name, two files are written under DATA_DIR
          sharing the same stem: a human-readable .txt and a .csv for
          downstream analysis. Any extension the user types is ignored --
          both formats are always produced.
        - If the user presses Enter without typing a name, saving is skipped.
        - DATA_DIR is created automatically if it does not already exist.
    """
    print()
    name = input("Enter filename to save sweep results (or press Enter to skip): ").strip()

    if not name:
        log.info("Save skipped -- no filename entered.")
        return

    # Strip whatever extension the user typed; we always write both .txt and .csv.
    bare = Path(name).with_suffix("")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    txt_path = DATA_DIR / bare.with_suffix(".txt")
    csv_path = DATA_DIR / bare.with_suffix(".csv")

    # Parse once so both writers see the same fields.
    parsed: list[tuple[float, str, str, str]] = []
    for freq, data in rows:
        parts     = data.split(",")
        primary   = parts[0].strip() if len(parts) > 0 else "?"
        secondary = parts[1].strip() if len(parts) > 1 else "?"
        status    = parts[2].strip() if len(parts) > 2 else "?"
        parsed.append((freq, primary, secondary, status))

    # Human-readable text file.
    header  = f"{'Freq (Hz)':>14}  {'Primary':>16}  {'Secondary':>16}  {'Status':>8}\n"
    divider = "-" * 60 + "\n"
    with txt_path.open("w", encoding="utf-8") as f:
        f.write("BK Precision 894 -- Frequency Sweep Results\n")
        f.write(divider)
        f.write(header)
        f.write(divider)
        for freq, primary, secondary, status in parsed:
            f.write(f"  {freq:14.2f}  {primary:>16}  {secondary:>16}  {status:>8}\n")

    # CSV for downstream analysis. newline="" lets the csv module pick its own
    # line ending and avoids the blank-line-between-rows quirk on Windows.
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Freq (Hz)", "Primary", "Secondary", "Status"])
        for freq, primary, secondary, status in parsed:
            writer.writerow([f"{freq:.2f}", primary, secondary, status])

    log.info("Sweep results saved to: %s", txt_path.resolve())
    log.info("Sweep results saved to: %s", csv_path.resolve())


# ── Modes ─────────────────────────────────────────────────────────────────────

def stream_continuous(ser: serial.Serial, freq_hz: float) -> None:
    """
    Poll the instrument continuously at a fixed frequency.
    Press Ctrl+C to stop.
    """
    set_frequency(ser, freq_hz)
    log.info("Streaming at %.2f Hz. Press Ctrl+C to stop.", freq_hz)

    while True:
        data = fetch_measurement(ser)
        print(f"  {data}")
        time.sleep(STREAM_INTERVAL_S)


def run_sweep(ser: serial.Serial) -> None:
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
        set_frequency(ser, freq)
        data = fetch_measurement(ser)
        rows.append((freq, data))
        print(f"  {freq:14.2f}  {data}")

    log.info("Sweep complete. %d points collected.", len(rows))
    prompt_and_save(rows)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="BK 894/895 LCR meter SCPI streamer.")
    parser.add_argument(
        "--port",
        help="Serial port (e.g. COM3 on Windows, /dev/ttyACM0 on Linux). "
             "Required unless --list-ports is given.",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=DEFAULT_BAUD,
        help=f"Baud rate (default: {DEFAULT_BAUD}). Must match the meter's "
             "front-panel setting.",
    )
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
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List available serial ports and exit.",
    )
    args = parser.parse_args()

    if args.list_ports:
        list_serial_ports()
        return

    if not args.port:
        parser.error("--port is required (use --list-ports to discover one)")

    if args.freq <= 0:
        parser.error(f"--freq must be positive, got {args.freq}")

    ser = None
    try:
        ser = open_instrument(args.port, baud=args.baud)

        if args.sweep:
            run_sweep(ser)
        else:
            stream_continuous(ser, args.freq)

    except KeyboardInterrupt:
        log.info("Stopped by user.")
    except Exception as exc:
        log.error("Fatal error: %s", exc)
    finally:
        if ser is not None and ser.is_open:
            try:
                ser.close()
                log.debug("Serial port closed.")
            except Exception:
                pass


if __name__ == "__main__":
    main()
