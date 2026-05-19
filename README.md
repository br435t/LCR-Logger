# LCR Logger

Stream and log measurements from a [B&K Precision 894 / 895 LCR meter](https://www.bkprecision.com/products/component-testers/894) over USBTMC. Supports continuous streaming at a fixed frequency and logarithmic frequency sweeps.

## Hardware

- B&K Precision **894** (20 Hz – 500 kHz) or **895** (20 Hz – 1 MHz)
- USB cable from the meter to the host PC

## Setup

### 1. Driver / permissions

**Windows.** The vendor driver claims the device by default and blocks `pyusb` from seeing it. Install [Zadig](https://zadig.akeo.ie/), select the BK 894 from the dropdown, and replace its driver with **WinUSB** (or libusb-win32). Change the driver for the meter only — do not touch anything else in the dropdown.

**Linux.** Add a udev rule once, then replug the meter:

```sh
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="XXXX", MODE="0666"' \
    | sudo tee /etc/udev/rules.d/99-bk894.rules
sudo udevadm control --reload-rules
```

Replace `XXXX` with your VID (no `0x` prefix).

### 2. Find your VID/PID and set them in the script

- **Windows:** Device Manager → meter → Properties → Details → "Hardware Ids". The `VID_xxxx` and `PID_yyyy` fields are what you want.
- **Linux:** `lsusb` — look for an entry like `ID 1ab1:0588 B&K Precision`.

Edit the `VID` and `PID` constants at the top of [`LCR_logging.py`](LCR_logging.py).

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
python LCR_logging.py                  # stream at the default 1 kHz
python LCR_logging.py --freq 10000     # stream at 10 kHz
python LCR_logging.py --sweep          # log-sweep 20 Hz → 200 kHz, then prompt to save
```

Press `Ctrl+C` to stop streaming. After a sweep finishes, the script asks for a filename; results are written to `data/` (a `.txt` extension is appended if you don't provide one). Press Enter without a name to skip saving.

All console output is also appended to `LCR_logging.log` in the working directory.

## Known limitations

See the docstring at the top of [`LCR_logging.py`](LCR_logging.py) for full details. Highlights:

- The measurement function (Cp-D, Ls-Q, R-X, etc.) is **not** set by the script — it uses whatever mode the meter was last in. Set it on the front panel before starting, or add a `FUNC:IMP <mode>` write to `open_instrument`.
- `*TRG` only works when the meter's trigger source is `BUS`. The script does not currently send `TRIG:SOUR BUS`; if the meter is in `INTernal` trigger mode (the factory default), `*TRG` may be ignored and `FETCH?` will return the most recent free-running result.
- The `FETCH?` response is parsed as `<primary>, <secondary>, <status>`. When the comparator is enabled the meter also returns a `<bin number>` field, which is silently dropped.
- Status byte values per the manual: `00` = normal, `-1` = no data in buffer, `+1` = analog unbalance, `+2` = A/D not working, `+3` = signal source overload, `+4` = constant voltage can't be adjusted. The script does not flag non-zero statuses — inspect the raw output.

The full SCPI reference is in `894_895_programming_manual.pdf`.

## Files

| File | Purpose |
|---|---|
| `LCR_logging.py` | The script (entry point + helpers) |
| `requirements.txt` | Pinned Python dependencies |
| `894_895_programming_manual.pdf` | Vendor SCPI command reference |
| `data/` | Sweep results (created on first save) |
| `LCR_logging.log` | Session log (created automatically) |
