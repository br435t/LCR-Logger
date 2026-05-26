# LCR Logger

Stream and log measurements from a [B&K Precision 894 / 895 LCR meter](https://www.bkprecision.com/products/component-testers/894) over a serial connection. Supports continuous streaming at a fixed frequency and logarithmic frequency sweeps.

The meter is talked to over a serial port (USB Virtual COM, RS-232, or a USB-to-serial adapter), which uses only stock OS drivers — no admin rights or driver replacement (Zadig / libusb) required. The meter's USBTMC interface is also supported by the SCPI command set, but is not used by this script.

## Hardware

- B&K Precision **894** (20 Hz – 500 kHz) or **895** (20 Hz – 1 MHz)
- One of:
  - USB cable to the meter (meter's USB mode must be set to **USBCDC / Virtual COM** in the System / Setup menu — *not* USBTMC)
  - Null-modem RS-232 cable (pins 2/3 swapped — see manual p.6 for pinout)
  - USB-to-RS232 adapter + null-modem cable. Tested working: [FTDI USB-to-RS232 adapter (FT232R)](https://www.amazon.com/dp/B0BKJKYCJK).

## Setup

### 1. Configure the meter

Front panel → System / Setup → Interface. The meter only listens on one interface at a time, so pick the one matching your cable:

- **Direct USB cable** → set Interface to **USBCDC** (a.k.a. "Virtual COM"). If you only see USBTMC, check the manual for the exact menu path — the option is there on stock firmware.
- **USB-to-RS232 adapter (or PC RS-232 port)** → set Interface to **RS-232C**.

Also note the **baud rate** (default 9600). Whatever the meter is set to, you'll pass the same value via `--baud`.

Before running the script, **exit any menus** so the meter is back on its main measurement screen showing live readings. `*TRG` and `FETCH?` only return data while the measurement loop is running on the front panel — if the meter is parked in System/Setup or any other menu, `FETCH?` comes back empty and the script prints blank lines.

### 2. Find your serial port

```sh
python LCR_logging.py --list-ports
```

Or look it up manually:

- **Windows:** Device Manager → Ports (COM & LPT) → look for the new COMx after plugging the meter in.
- **Linux:** `/dev/ttyACM0` for USB-CDC, `/dev/ttyUSB0` for USB-to-serial adapters.
- **macOS:** `/dev/cu.usbmodem*` or `/dev/cu.usbserial-*`.

### 3. Python environment

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

## Usage

```sh
python LCR_logging.py --port COM3                       # stream at the default 1 kHz
python LCR_logging.py --port COM3 --freq 10000          # stream at 10 kHz
python LCR_logging.py --port COM3 --baud 115200         # higher baud (must match meter)
python LCR_logging.py --port COM3 --sweep               # log-sweep 20 Hz -> 200 kHz, then prompt to save
python LCR_logging.py --list-ports                      # list available serial ports
```

Press `Ctrl+C` to stop streaming. After a sweep finishes, the script asks for a filename; results are written to `data/` as two files sharing the same stem — a human-readable `.txt` and a `.csv` for analysis. Any extension you type is ignored. Press Enter without a name to skip saving.

All console output is also appended to `LCR_logging.log` in the working directory.

## Known limitations

See the docstring at the top of [`LCR_logging.py`](LCR_logging.py) for full details. Highlights:

- The measurement function (Cp-D, Ls-Q, R-X, etc.) is **not** set by the script — it uses whatever mode the meter was last in. Set it on the front panel before starting, or add a `FUNC:IMP <mode>` write to `open_instrument`.
- `*TRG` only works when the meter's trigger source is `BUS`. The script sends `TRIG:SOUR BUS` during `open_instrument` so this is handled at startup; if you ever bypass that path, freshly triggered reads will silently fall back to the free-running result.
- The `FETCH?` response is parsed as `<primary>, <secondary>, <status>`. When the comparator is enabled the meter also returns a `<bin number>` field, which is silently dropped.
- Status byte values per the manual: `00` = normal, `-1` = no data in buffer, `+1` = analog unbalance, `+2` = A/D not working, `+3` = signal source overload, `+4` = constant voltage can't be adjusted. The script does not flag non-zero statuses — inspect the raw output.
- Neither RS-232 nor USB-CDC supports hardware flow control on this meter (manual p.7). The script adds a small post-write delay (`CMD_DELAY_S`) to keep the meter's input buffer from overrunning; if you see intermittent communication errors, raise it.

The full SCPI reference is in `894_895_programming_manual.pdf`.

## Files

| File | Purpose |
|---|---|
| `LCR_logging.py` | The script (entry point + helpers) |
| `requirements.txt` | Pinned Python dependencies |
| `894_895_programming_manual.pdf` | Vendor SCPI command reference |
| `data/` | Sweep results (created on first save) |
| `LCR_logging.log` | Session log (created automatically) |
