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

USAGE (use a /dev/... path on Linux/macOS, COMx on Windows):
    python LCR_logging.py --port /dev/ttyACM0        # stream at default 1 kHz (Linux direct USB)
    python LCR_logging.py --port /dev/ttyUSB0        # Linux USB-to-RS232 adapter
    python LCR_logging.py --port COM3                # Windows
    python LCR_logging.py --port /dev/ttyACM0 --freq 1000    # stream at 1 kHz
    python LCR_logging.py --port /dev/ttyACM0 --func RX      # force R-X mode, then stream
    python LCR_logging.py --port /dev/ttyACM0 --sweep        # log-sweep 20 Hz -> 200 kHz
    python LCR_logging.py --port /dev/ttyACM0 --sweep --func LSRS  # set Ls-Rs, then sweep
    python LCR_logging.py --list-ports               # list available ports

LINUX PERMISSIONS:
    Serial ports belong to the "dialout" group. If opening the port fails
    with "Permission denied", add yourself once with
        sudo usermod -aG dialout $USER
    then log out and back in (or run "newgrp dialout" in the current shell).

LOGGING:
    All log messages (INFO and above) are written to LCR_logging.log
    in the working directory in addition to the console.

    After a sweep completes, you will be prompted to enter a filename, then
    an author and description. Three files are written into the data/ subfolder
    using that name as a stem: a human-readable .txt, a .csv for analysis, and
    a .json metadata sidecar (csv location, test time, author, description,
    measurement type). Any extension you type is ignored -- all three are
    always produced. The folder is created automatically if it does not exist.
    Press Enter without typing a name to skip saving.

    The primary/secondary column headers in both files reflect the meter's
    current measurement function (e.g. R/X, Cp/D), read via FUNC:IMP? at the
    start of the sweep. If the function is unrecognised, generic
    "Primary"/"Secondary" headers are used.
"""

import argparse
import csv
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import serial
import serial.tools.list_ports

# ── Configuration ─────────────────────────────────────────────────────────────

# Sweep results are written here, relative to the working directory.
# The folder is created automatically if it does not exist.
DATA_DIR = Path("data")

# Default frequency sweep span and point count (log-spaced). Shared by the CLI
# and GUI. 20 Hz is the meter's floor; 200 kHz stays within both the 894
# (500 kHz) and 895 (1 MHz) ranges.
SWEEP_START_HZ = 20.0
SWEEP_STOP_HZ = 200_000.0
SWEEP_POINTS = 20

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

# User-selectable measurement tags live here (working directory) as a small
# YAML mapping of section -> list of tag strings. Kept dependency-free (no
# PyYAML): the load/save helpers below parse and emit the format themselves,
# because adding a pip dependency is painful on this machine (corporate SSL
# inspection -- see HANDOFF.md "Environment quirks"). The file is created on
# first add.
TAGS_FILE = Path("tags.yaml")

# The two tag sections, in display order. Keys are the YAML/sidecar field names;
# the GUI/CLI show them title-cased ("test_parameters" -> "Test parameters").
# A tag selection is recorded per-section, so a saved sweep's sidecar knows
# which kind of tag each one was.
TAG_SECTIONS: tuple[str, ...] = ("test_parameters", "test_configurations")

# Map each FUNCtion:IMPedance code to its (primary, secondary) parameter
# labels, with units in parentheses, so output headers and plot axes read
# "R (Ω)"/"X (Ω)" or "Cp (F)"/"D" instead of generic "Primary"/"Secondary".
# Units by parameter: capacitance Cp/Cs -> F (farad); inductance Lp/Ls -> H
# (henry); resistance/reactance/impedance R/Rp/Rs/X/Z -> Ω (ohm); conductance/
# susceptance/admittance G/B/Y -> S (siemens); phase theta -> deg or rad. The
# dissipation factor D and quality factor Q are dimensionless, so they carry no
# unit. Source: 894/895 programming manual, the FUNCtion:IMPedance table.
IMP_FUNCTIONS: dict[str, tuple[str, str]] = {
    "CPD":  ("Cp (F)", "D"),
    "CPQ":  ("Cp (F)", "Q"),
    "CPG":  ("Cp (F)", "G (S)"),
    "CPRP": ("Cp (F)", "Rp (Ω)"),
    "CSD":  ("Cs (F)", "D"),
    "CSQ":  ("Cs (F)", "Q"),
    "CSRS": ("Cs (F)", "Rs (Ω)"),
    "LPD":  ("Lp (H)", "D"),
    "LPQ":  ("Lp (H)", "Q"),
    "LPG":  ("Lp (H)", "G (S)"),
    "LPRP": ("Lp (H)", "Rp (Ω)"),
    "LSD":  ("Ls (H)", "D"),
    "LSQ":  ("Ls (H)", "Q"),
    "LSRS": ("Ls (H)", "Rs (Ω)"),
    "RX":   ("R (Ω)", "X (Ω)"),
    "ZTD":  ("Z (Ω)", "theta (deg)"),
    "ZTR":  ("Z (Ω)", "theta (rad)"),
    "GB":   ("G (S)", "B (S)"),
    "YTD":  ("Y (S)", "theta (deg)"),
    "YTR":  ("Y (S)", "theta (rad)"),
}

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

    Measurement labels can contain non-ASCII unit symbols (e.g. the Ω in
    "Rs (Ω)"). A default Windows console is cp1252 and would raise
    UnicodeEncodeError on those, so the console stream is reconfigured to UTF-8
    (replacing any glyph the terminal still can't draw). The file handler is
    already UTF-8.
    """
    console = logging.StreamHandler()
    reconfigure = getattr(console.stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            console,
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


def get_serial_ports() -> list[tuple[str, str, str]]:
    """
    Return serial ports backed by a real connected device, as
    (device, description, hwid) tuples sorted by device name.

    comports() also reports legacy/built-in ports that always exist whether or
    not anything is plugged in (e.g. /dev/ttyS* on Linux, COM1 on Windows).
    Those carry no hardware ID, so we skip them and show only ports that
    advertise USB/device info -- i.e. things actually connected, such as the
    meter's USB-CDC port or a USB-to-serial adapter.

    Note: this also hides a genuine built-in RS-232 port (a native DB-9), since
    those carry no USB ID -- pass such a port explicitly rather than discovering
    it here. Shared by the CLI's --list-ports and the GUI's port dropdown.
    """
    ports = sorted(
        (p for p in serial.tools.list_ports.comports() if p.hwid and p.hwid != "n/a"),
        key=lambda p: p.device,
    )
    return [(p.device, p.description, p.hwid) for p in ports]


def list_serial_ports() -> None:
    """Print the connected serial ports from get_serial_ports()."""
    ports = get_serial_ports()
    if not ports:
        print("No connected serial devices found.")
        return
    for device, description, hwid in ports:
        print(f"{device}  --  {description}  [{hwid}]")


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


def return_to_local(ser: serial.Serial) -> None:
    """
    Undo the remote-control state from open_instrument so the front panel is
    usable again after the script exits ("reset keylock").

    open_instrument switches the meter to bus triggering (TRIG:SOUR BUS) so
    *TRG works. Left that way, the meter sits waiting for bus triggers and the
    front panel stops free-running measurements -- which reads as a locked /
    frozen panel. This restores internal triggering so live measurement
    resumes, returns the display to the measurement page, and issues the
    standard SCPI go-to-local request.

    The 894/895 programming manual documents no keylock/local command, so
    SYSTem:LOCal is best-effort: if the firmware ignores it, the trigger and
    display restores above are what actually free the panel. Failures here are
    logged, not raised -- cleanup must never mask the real exit path.
    """
    try:
        scpi_write(ser, "TRIG:SOUR INT")   # resume front-panel free-running
        scpi_write(ser, "DISP:PAGE MEAS")  # show the live measurement page
        scpi_write(ser, "SYSTem:LOCal")    # standard return-to-local (best-effort)
        log.debug("Meter returned to local front-panel control.")
    except Exception as exc:
        log.warning("Could not fully return meter to local control: %s", exc)


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


def set_measurement_function(ser: serial.Serial, code: str) -> None:
    """
    Set the meter's measurement function (e.g. RX, CPD, LSRS) so each run
    measures a known parameter pair instead of inheriting whatever mode the
    front panel was last left in -- which makes results non-deterministic
    across runs.

    `code` must be one of the FUNCtion:IMPedance selections in IMP_FUNCTIONS.
    Validation happens at argument-parse time (--func choices), so this only
    ever sees codes the meter understands.

    A relay-settle wait follows the write: switching function can re-range the
    meter's internal relays, the same way changing frequency does, and querying
    or measuring too soon can return a stale or garbage reading.
    """
    scpi_write(ser, f"FUNC:IMP {code}")
    primary, secondary = IMP_FUNCTIONS[code]
    log.info("Measurement function set to %s (%s-%s).", code, primary, secondary)
    time.sleep(FREQ_SETTLE_S)


def get_measurement_labels(ser: serial.Serial) -> tuple[str, str]:
    """
    Query the meter's measurement function and return its (primary, secondary)
    parameter labels -- e.g. ("R", "X") for R-X mode or ("Cp", "D") for Cp-D.

    This reads back whatever function the meter is currently in (FUNC:IMP?) --
    either the mode set this run via --func / set_measurement_function, or, if
    --func was not given, whatever the front panel was last left in -- so the
    output headers always reflect the actual measured parameters.

    Falls back to ("Primary", "Secondary") if the meter returns an empty or
    unrecognised code, so saving never fails just because the mode is unknown.
    """
    code = scpi_query(ser, "FUNC:IMP?").strip().upper()
    if code not in IMP_FUNCTIONS:
        log.warning(
            "Measurement function %r not recognised; using generic headers.",
            code,
        )
        return ("Primary", "Secondary")
    primary, secondary = IMP_FUNCTIONS[code]
    log.info("Measurement function: %s-%s", primary, secondary)
    return primary, secondary


def label_unit(label: str) -> str:
    """
    Pull the unit out of a measurement label, e.g. "Cp (F)" -> "F", "Rs (Ω)" ->
    "Ω", "theta (deg)" -> "deg". Dimensionless labels like "D" or "Q" (and the
    generic "Primary"/"Secondary" fallback) have no parenthesised unit and
    return "". This is the parsed unit recorded in the JSON sidecar.
    """
    m = re.match(r"^.*?\(([^)]*)\)\s*$", label)
    return m.group(1).strip() if m else ""


# ── Tags ──────────────────────────────────────────────────────────────────────

def _parse_yaml_tag(text: str) -> str:
    """
    Turn one YAML list item's value into a plain string, handling the three
    forms our writer (and a human editing the file) might produce: a bare
    scalar, a "double-quoted" scalar (JSON-style escaping), or a 'single-quoted'
    one ('' is an escaped quote). Anything we can't parse is taken literally.
    """
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        if text[0] == '"':
            try:
                return json.loads(text)
            except ValueError:
                pass
        else:
            return text[1:-1].replace("''", "'")
    return text


def _section_header(line: str) -> str | None:
    """
    If `line` is a bare YAML mapping key ("test_parameters:") return the key,
    else None. A key has a trailing colon, is not a "- value" list item, and
    carries no inline value after the colon (that would be a scalar, not a
    section we collect items under).
    """
    if line.startswith("-") or not line.endswith(":"):
        return None
    return line[:-1].strip() or None


def load_tag_groups(path: Path = TAGS_FILE) -> dict[str, list[str]]:
    """
    Read the YAML tag file, returning {section: [unique tags in file order]}.
    The two canonical sections (TAG_SECTIONS) are always present (possibly
    empty); any extra sections found in the file are preserved after them.

    Blank lines and "# comments" are skipped. A "key:" line starts a section;
    each following "- value" line adds a tag to it. For backward compatibility
    with the old flat list, "- value" lines that appear before any section
    header land in the first section. Missing/unreadable file -> empty sections.
    """
    groups: dict[str, list[str]] = {s: [] for s in TAG_SECTIONS}
    if not path.is_file():
        return groups
    current = TAG_SECTIONS[0]  # legacy bare items fall into the first section
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            section = _section_header(line)
            if section is not None:
                current = section
                groups.setdefault(section, [])
                continue
            if line.startswith("-"):
                line = line[1:]
            tag = _parse_yaml_tag(line)
            # Tags are unique across the whole file -- a name belongs to one
            # section, so a duplicate (in this or another section) is ignored.
            if tag and not any(tag in v for v in groups.values()):
                groups[current].append(tag)
    except OSError as exc:
        log.warning("Could not read tags from %s: %s", path, exc)
        return {s: [] for s in TAG_SECTIONS}
    return groups


def load_tags(path: Path = TAGS_FILE) -> list[str]:
    """
    Flat list of every tag across all sections, in file order, for callers that
    just need the set of known names regardless of section.
    """
    flat: list[str] = []
    for tags in load_tag_groups(path).values():
        flat.extend(tags)
    return flat


def save_tag_groups(groups: dict[str, list[str]], path: Path = TAGS_FILE) -> None:
    """
    Write the section -> tags mapping back as YAML. Canonical sections come
    first (always emitted, even if empty), then any extra sections. Each tag is
    emitted with json.dumps -- a double-quoted scalar that is valid YAML and
    round-trips punctuation/Unicode safely. A header comment documents the
    format for anyone editing by hand.
    """
    lines = [
        "# LCR Logger measurement tags, grouped into sections.",
        "# Add tags under either section as \"- value\" lines (GUI/CLI or by hand).",
        "",
    ]
    keys = list(TAG_SECTIONS) + [k for k in groups if k not in TAG_SECTIONS]
    for key in keys:
        lines.append(f"{key}:")
        for tag in groups.get(key, []):
            lines.append(f"  - {json.dumps(tag, ensure_ascii=False)}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def add_tag(
    tag: str, section: str = TAG_SECTIONS[0], path: Path = TAGS_FILE
) -> dict[str, list[str]]:
    """
    Add a tag to the given section (creating the file if needed) and return the
    updated section -> tags mapping, each section case-insensitively sorted. A
    blank tag, an unknown section, or a name already present in any section is a
    no-op. This is how new tags get persisted "at runtime" from either front end.
    """
    tag = tag.strip()
    groups = load_tag_groups(path)
    if section not in groups:
        section = TAG_SECTIONS[0]
    if tag and not any(tag in v for v in groups.values()):
        groups[section].append(tag)
        groups[section].sort(key=str.lower)
        save_tag_groups(groups, path)
    return groups


def normalize_tag_selection(tags: object) -> dict[str, list[str]]:
    """
    Coerce a tag selection into the {section: [tags]} shape recorded in a
    sweep's JSON sidecar. Accepts the categorized dict form (as the GUI/CLI now
    produce), or a flat list/tuple (legacy or uncategorized callers), which is
    recorded under the first section. The canonical sections are always present.
    """
    result: dict[str, list[str]] = {s: [] for s in TAG_SECTIONS}
    if isinstance(tags, dict):
        for section, vals in tags.items():
            bucket = result.setdefault(str(section), [])
            for t in (vals or []):
                t = str(t).strip()
                if t and t not in bucket:
                    bucket.append(t)
    elif isinstance(tags, (list, tuple)):
        bucket = result[TAG_SECTIONS[0]]
        for t in tags:
            t = str(t).strip()
            if t and t not in bucket:
                bucket.append(t)
    return result


# ── Sweep result saving ───────────────────────────────────────────────────────

def save_sweep(
    rows: list[tuple[float, str]],
    name: str,
    author: str = "",
    description: str = "",
    primary_label: str = "Primary",
    secondary_label: str = "Secondary",
    test_time: datetime | None = None,
    tags: dict[str, list[str]] | list[str] | None = None,
) -> tuple[Path, Path, Path]:
    """
    Write sweep results to the data folder. No prompting -- all metadata is
    passed in, so this is shared by the CLI (which gathers it via input()) and
    the GUI (which gathers it from form fields).

    Args:
        rows: List of (frequency_hz, raw_measurement_string) tuples collected
              during the sweep.
        name: Filename stem. Any extension is stripped; .txt, .csv, and .json
              are always produced from this stem.
        author: Free-text author, recorded in the JSON sidecar. May be blank.
        description: Free-text description for the JSON sidecar. May be blank.
        primary_label: Header for the primary parameter column (e.g. "R", "Cp"),
              from the meter's current measurement function.
        secondary_label: Header for the secondary parameter column (e.g. "X", "D").
        test_time: When the sweep was run, recorded in the JSON sidecar. Defaults
              to the current time if not supplied.
        tags: User-selected tags recorded in the JSON sidecar for
              cataloguing/filtering. Either the categorized {section: [tags]}
              mapping or a flat list (taken as uncategorized); normalized to the
              {section: [tags]} shape on write. Defaults to no tags.

    Returns:
        (txt_path, csv_path, json_path) -- the three files written. DATA_DIR is
        created automatically if it does not already exist.
    """
    # Strip whatever extension the caller passed; we always write .txt/.csv/.json.
    bare = Path(name).with_suffix("")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    txt_path  = DATA_DIR / bare.with_suffix(".txt")
    csv_path  = DATA_DIR / bare.with_suffix(".csv")
    json_path = DATA_DIR / bare.with_suffix(".json")

    # Parse once so both writers see the same fields.
    parsed: list[tuple[float, str, str, str]] = []
    for freq, data in rows:
        parts     = data.split(",")
        primary   = parts[0].strip() if len(parts) > 0 else "?"
        secondary = parts[1].strip() if len(parts) > 1 else "?"
        status    = parts[2].strip() if len(parts) > 2 else "?"
        parsed.append((freq, primary, secondary, status))

    # Human-readable text file.
    header  = f"{'Freq (Hz)':>14}  {primary_label:>16}  {secondary_label:>16}  {'Status':>8}\n"
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
        writer.writerow(["Freq (Hz)", primary_label, secondary_label, "Status"])
        for freq, primary, secondary, status in parsed:
            writer.writerow([f"{freq:.2f}", primary, secondary, status])

    # JSON metadata sidecar describing this run, for cataloguing/analysis.
    metadata = {
        "csv_file": str(csv_path.resolve()),
        "test_time": (test_time or datetime.now()).isoformat(timespec="seconds"),
        "author": author,
        "description": description,
        "measurement": f"{primary_label}-{secondary_label}",
        "units": {
            "primary": label_unit(primary_label),
            "secondary": label_unit(secondary_label),
        },
        "tags": normalize_tag_selection(tags),
    }
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
        f.write("\n")

    log.info("Sweep results saved to: %s", txt_path.resolve())
    log.info("Sweep results saved to: %s", csv_path.resolve())
    log.info("Sweep metadata saved to: %s", json_path.resolve())
    return txt_path, csv_path, json_path


def prompt_and_save(
    rows: list[tuple[float, str]],
    primary_label: str = "Primary",
    secondary_label: str = "Secondary",
    test_time: datetime | None = None,
) -> None:
    """
    CLI wrapper around save_sweep: prompt for filename, author, and description,
    then write the files. Press Enter at the filename prompt to skip saving.
    """
    print()
    name = input("Enter filename to save sweep results (or press Enter to skip): ").strip()

    if not name:
        log.info("Save skipped -- no filename entered.")
        return

    author      = input("Data author (optional): ").strip()
    description = input("Description (optional): ").strip()
    tags        = prompt_for_tags()

    save_sweep(
        rows, name, author, description, primary_label, secondary_label,
        test_time, tags=tags,
    )


def prompt_for_tags() -> dict[str, list[str]]:
    """
    Prompt for tags one section at a time, showing any already defined in
    TAGS_FILE for that section. Any tag the user types that isn't already known
    is persisted to its section in the YAML so it's offered next time -- this is
    the CLI's "add new tags at runtime" path. Press Enter to skip a section.
    Returns the categorized {section: [tags]} selection.
    """
    groups = load_tag_groups()
    chosen: dict[str, list[str]] = {s: [] for s in TAG_SECTIONS}
    for section in TAG_SECTIONS:
        label = section.replace("_", " ").capitalize()
        available = groups.get(section, [])
        if available:
            print(f"Available {label.lower()}: " + ", ".join(available))
        entry = input(f"{label} (comma-separated, optional): ").strip()
        if not entry:
            continue
        for tag in (t.strip() for t in entry.split(",") if t.strip()):
            add_tag(tag, section)
            if tag not in chosen[section]:
                chosen[section].append(tag)
    return chosen


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


def collect_sweep(
    ser: serial.Serial,
    start_hz: float = SWEEP_START_HZ,
    stop_hz: float = SWEEP_STOP_HZ,
    points: int = SWEEP_POINTS,
    progress=None,
    should_stop=None,
) -> tuple[list[tuple[float, str]], str, str, datetime]:
    """
    Run a logarithmic frequency sweep and return the collected rows plus the
    metadata needed to save them. No console printing and no saving -- those
    are the caller's job -- so this is shared by the CLI and the GUI.

    Args:
        start_hz, stop_hz, points: Sweep span and point count (log-spaced).
        progress: Optional callback(i, total, freq_hz, raw_data) invoked after
                  each point, e.g. to print to the console or update a GUI.
        should_stop: Optional callable returning True to abort the sweep early
                  (used by the GUI's Cancel button). Checked before each point.

    Returns:
        (rows, primary_label, secondary_label, test_time), where rows is a list
        of (frequency_hz, raw_measurement_string) tuples ready for save_sweep().
    """
    primary_label, secondary_label = get_measurement_labels(ser)
    test_time = datetime.now()

    freqs = np.logspace(np.log10(start_hz), np.log10(stop_hz), num=points)
    log.info(
        "Starting sweep: %d points from %.0f Hz to %.0f Hz.",
        len(freqs), start_hz, stop_hz,
    )

    rows: list[tuple[float, str]] = []
    for i, freq in enumerate(freqs, start=1):
        if should_stop is not None and should_stop():
            log.info("Sweep cancelled after %d/%d points.", i - 1, len(freqs))
            break
        set_frequency(ser, freq)
        data = fetch_measurement(ser)
        rows.append((freq, data))
        if progress is not None:
            progress(i, len(freqs), freq, data)

    log.info("Sweep complete. %d points collected.", len(rows))
    return rows, primary_label, secondary_label, test_time


def run_sweep(ser: serial.Serial) -> None:
    """
    CLI sweep: print each step to the console, then prompt to save results.
    """
    print(f"\n{'Freq (Hz)':>14}  {'Measurement'}")
    print("-" * 50)

    def _print(i: int, total: int, freq: float, data: str) -> None:
        print(f"  {freq:14.2f}  {data}")

    rows, primary_label, secondary_label, test_time = collect_sweep(ser, progress=_print)
    prompt_and_save(rows, primary_label, secondary_label, test_time)


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
        "--func",
        type=str.upper,
        choices=sorted(IMP_FUNCTIONS),
        metavar="MODE",
        help="Set the meter's measurement function before measuring, so the "
             "measured parameter pair is deterministic instead of inheriting "
             "the front panel's last mode. If omitted, the meter keeps its "
             "current mode. Choices: " + ", ".join(sorted(IMP_FUNCTIONS)),
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

        if args.func:
            set_measurement_function(ser, args.func)

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
                return_to_local(ser)
                ser.close()
                log.debug("Serial port closed.")
            except Exception:
                pass


if __name__ == "__main__":
    main()
