# LCR Logger

Stream and log measurements from a [B\&K Precision 894 / 895 LCR meter](https://www.bkprecision.com/products/component-testers/894) over a serial connection. Supports continuous streaming at a fixed frequency and logarithmic frequency sweeps.

The meter is talked to over a serial port (USB Virtual COM, RS-232, or a USB-to-serial adapter), which uses only stock OS drivers — no admin rights or driver replacement (Zadig / libusb) required. The meter's USBTMC interface is also supported by the SCPI command set, but is not used by this script.

## Hardware

* B&K Precision **894** (20 Hz – 500 kHz) or **895** (20 Hz – 1 MHz)

* One of:

  * USB cable to the meter (meter's USB mode must be set to **USBCDC / Virtual COM** in the System / Setup menu — *not* USBTMC)

  * Null-modem RS-232 cable (pins 2/3 swapped — see manual p.6 for pinout)

  * USB-to-RS232 adapter + null-modem cable. Tested working: [FTDI USB-to-RS232 adapter (FT232R)](https://www.amazon.com/dp/B0BKJKYCJK).

## Setup

### 1. Python environment

**One-click setup (recommended).** From the project folder, run the installer for your OS. It creates a `.venv` and installs the pinned dependencies — re-run it any time; it reuses an existing `.venv`.

* **Windows:** double-click **`install.bat`** (or run it in a terminal).

* **Linux / macOS:** `install.sh` (or `bash install.sh`).

**Manual setup** (equivalent to what the scripts do):

```powershell
# Windows
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

```sh
# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> If `pip install` fails behind a corporate proxy that does SSL inspection, pip needs your organisation's root CA. See HANDOFF.md "Environment quirks".

### 2. Configure the meter

Front panel → System / Setup → Interface. The meter only listens on one interface at a time, so pick the one matching your cable:

* **Direct USB cable** → set Interface to **USBCDC** (a.k.a. "Virtual COM"). If you only see USBTMC, check the manual for the exact menu path — the option is there on stock firmware.

* **USB-to-RS232 adapter (or PC RS-232 port)** → set Interface to **RS-232C**.

Also note the **baud rate** (default 9600). Whatever the meter is set to, you'll pass the same value via `--baud`.

Before running the script, **exit any menus** so the meter is back on its main measurement screen showing live readings. `*TRG` and `FETCH?` only return data while the measurement loop is running on the front panel — if the meter is parked in System/Setup or any other menu, `FETCH?` comes back empty and the script prints blank lines.

### 3. Find your serial port

```sh
python LCR_logging.py --list-ports
```

`--list-ports` only shows ports backed by a real connected device (those with a USB hardware ID), so it hides the always-present legacy ports (`/dev/ttyS*` on Linux, `COM1`/`COM2` on Windows) that would otherwise clutter the list. **Note:** this also hides a genuine built-in RS-232 port (a native motherboard DB-9), since those carry no USB ID. If you connect the meter to one, skip `--list-ports` and pass it directly, e.g. `--port /dev/ttyS0` (Linux) or `--port COM1` (Windows).

Or look it up manually:

* **Windows:** Device Manager → Ports (COM & LPT) → look for the new COMx after plugging the meter in.

* **Linux:** `/dev/ttyACM0` for USB-CDC, `/dev/ttyUSB0` for USB-to-serial adapters.

* **macOS:** `/dev/cu.usbmodem*` or `/dev/cu.usbserial-*`.

## Usage

Use a `/dev/...` path on Linux/macOS and a `COMx` name on Windows. The examples below show Linux's `/dev/ttyACM0` (direct USB); substitute your own port from `--list-ports`.

```sh
python LCR_logging.py --port /dev/ttyACM0               # stream at the default 1 kHz (Linux direct USB)
python LCR_logging.py --port /dev/ttyUSB0               # Linux USB-to-RS232 adapter
python LCR_logging.py --port COM3                       # Windows
python LCR_logging.py --port /dev/ttyACM0 --freq 10000  # stream at 10 kHz
python LCR_logging.py --port /dev/ttyACM0 --baud 115200 # higher baud (must match meter)
python LCR_logging.py --port /dev/ttyACM0 --func RX     # force R-X mode, then stream
python LCR_logging.py --port /dev/ttyACM0 --sweep       # log-sweep 20 Hz -> 200 kHz, then prompt to save
python LCR_logging.py --port /dev/ttyACM0 --sweep --func LSRS  # set Ls-Rs, then sweep
python LCR_logging.py --list-ports                      # list available serial ports
```

> **Linux permissions:** serial ports are owned by the `dialout` group. If you get `Permission denied` opening the port, add yourself once with `sudo usermod -aG dialout $USER`, then log out and back in (or run `newgrp dialout` in the current terminal).

Press `Ctrl+C` to stop streaming. After a sweep finishes, the script asks for a filename, then an author and description; results are written to `data/` as three files sharing the same stem — a human-readable `.txt`, a `.csv` for analysis, and a `.json` metadata sidecar. Any extension you type is ignored. Press Enter without a name to skip saving.

The `.json` sidecar records the run's provenance:

```json
{
  "csv_file": "/abs/path/to/data/<name>.csv",
  "test_time": "2026-06-15T10:30:00",
  "author": "B. Tester",
  "description": "Cap bank unit 3, 1uF film cap",
  "measurement": "R (Ω)-X (Ω)"
}
```

All console output is also appended to `LCR_logging.log` in the working directory.

## GUI

For a point-and-click alternative to the sweep CLI, launch the GUI with the helper script — **`run_gui.bat`** (Windows, double-click) or **`run_gui.sh`** (Linux / macOS). These use the `.venv` created by the installer above; run the installer first.

Equivalently, with the environment activated:

```sh
python LCR_gui.py
```

It starts a tiny local web server (Python's built-in `http.server`, nothing to install) and opens `http://127.0.0.1:<port>/` in your default browser, where you:

1. Pick the **port** (Refresh rescans), **baud**, and optional measurement **function**.
2. Pre-fill the **filename**, **author**, and **description**. A live **JSON preview** shows exactly what the `.json` sidecar will contain before you commit.
3. Click **Run sweep & save** — the sweep runs on a background thread (the page stays responsive), each point streams into the progress pane, and the `.txt`/`.csv`/`.json` files are written to `data/`. **Cancel** stops after the current point without saving.
4. **Visualize** the result: the *Visualizer* pane plots frequency (X) against either measured column (Y).

   * **Folder** — the directory to scan for saved `.csv` datasets (defaults to `data`; type any path and click **Scan**, or press Enter). Since the UI runs on the same machine as the server, this is a path on that machine (e.g. `data`, `Example_data`, or an absolute path).

   * **Dataset** — what to plot: "Current sweep" (the most recent run, including a partial one if you cancelled) or any saved sweep found in the selected folder, parsed from its `.csv`.

   * Pick the **Column** and toggle **log/linear** on each axis. Drawn with a plain `<canvas>` — no plotting library to install.

The page is served on loopback only (`127.0.0.1`), so it is not reachable from other machines. Press **Ctrl+C** in the terminal to stop the server when you're done.

A browser UI (rather than Tkinter) is used because Tkinter is not installed on the target machine and there are no admin rights to add the `python3-tk` system package; `http.server` has no such dependency. The GUI drives the same instrument code as the CLI ([`LCR_logging.py`](LCR_logging.py)); it only replaces the interactive prompts with form fields. Same hardware setup applies (meter on USBCDC/RS-232C, on its live measurement screen, baud matching the front panel).

## Known limitations

See the docstring at the top of [`LCR_logging.py`](LCR_logging.py) for full details. Highlights:

* The measurement function (Cp-D, Ls-Q, R-X, etc.) can be set with `--func <MODE>` (e.g. `--func RX`, `--func LSRS`), which makes the measured parameter pair deterministic. If `--func` is omitted, the script uses whatever mode the meter was last left in. Valid modes are the FUNC:IMP codes listed in `--help`.

* `*TRG` only works when the meter's trigger source is `BUS`. The script sends `TRIG:SOUR BUS` during `open_instrument` so this is handled at startup; if you ever bypass that path, freshly triggered reads will silently fall back to the free-running result.

* The `FETCH?` response is parsed as `<primary>, <secondary>, <status>`. When the comparator is enabled the meter also returns a `<bin number>` field, which is silently dropped.

* Status byte values per the manual: `00` = normal, `-1` = no data in buffer, `+1` = analog unbalance, `+2` = A/D not working, `+3` = signal source overload, `+4` = constant voltage can't be adjusted. The script does not flag non-zero statuses — inspect the raw output.

* Neither RS-232 nor USB-CDC supports hardware flow control on this meter (manual p.7). The script adds a small post-write delay (`CMD_DELAY_S`) to keep the meter's input buffer from overrunning; if you see intermittent communication errors, raise it.

The full SCPI reference is in `894_895_programming_manual.pdf`.

## Files

| File                             | Purpose                                                              |
| -------------------------------- | -------------------------------------------------------------------- |
| `LCR_logging.py`                 | The CLI script + reusable instrument helpers                         |
| `LCR_gui.py`                     | Optional browser-based GUI (stdlib `http.server`) for sweep + save   |
| `install.bat` / `install.sh`     | One-click `.venv` setup + dependency install (Windows / Linux-macOS) |
| `run_gui.bat` / `run_gui.sh`     | One-click GUI launcher using `.venv` (Windows / Linux-macOS)         |
| `requirements.txt`               | Pinned Python dependencies                                           |
| `894_895_programming_manual.pdf` | Vendor SCPI command reference                                        |
| `data/`                          | Sweep results (created on first save)                                |
| `LCR_logging.log`                | Session log (created automatically)                                  |
