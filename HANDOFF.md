# Handoff — LCR Logger

Snapshot of project state, intended for whoever picks this up next (future Claude session, teammate, or you in two weeks).

## What this is

A Python tool for streaming and logging measurements from a **B&K Precision 894** LCR meter. Two modes:

- **Streaming** — continuous polling at a fixed frequency (`--freq`).
- **Sweep** — logarithmic frequency sweep 20 Hz → 200 kHz (20 points), saved to `data/` as `.txt`/`.csv`/`.json`.

CLI: [`LCR_logging.py`](LCR_logging.py) — also exposes reusable helpers (`get_serial_ports`, `collect_sweep`, `save_sweep`, etc.). GUI: [`LCR_gui.py`](LCR_gui.py), a browser-based front end (stdlib `http.server`, served on `127.0.0.1`) for the sweep + save flow that drives those same helpers. See [`README.md`](README.md) for setup and [`894_895_programming_manual.pdf`](894_895_programming_manual.pdf) for the SCPI reference.

## Hardware status

**Confirmed working over serial on Windows (2026-06-11).** The script connects to the meter and streams measurements. The earlier "never been connected" risk is resolved.

To bring up on a fresh setup:

1. Connect the meter via USB.
2. Front panel: System / Setup → set USB interface mode to **USBCDC** (a.k.a. Virtual COM). The default may be USBTMC, which this script does *not* use.
3. `python LCR_logging.py --list-ports` to find the new COMx.
4. `python LCR_logging.py --port COMx` — expect `Connected to: B&K Precision,894,...` followed by streaming output.

If `*IDN?` returns nothing: re-check the meter's USB mode (must be USBCDC) and that `--baud` matches the meter's front-panel setting (default 9600).

## Recent history (working backwards)

1. **Refactored transport from USBTMC to serial (pyserial).** Reason: the host machine has no admin rights, so the Zadig driver swap that USBTMC needs on Windows isn't possible. The serial path uses only stock OS drivers. Both USB-CDC and RS-232 are supported. Same SCPI commands, just different transport.
2. **Cross-referenced the code against `894_895_programming_manual.pdf`.** Found several issues — see "Known limitations" below.
3. **Initial USBTMC implementation** worked on paper but was never tested against hardware.

## Environment quirks

- **Corporate SSL inspection (Zscaler + Helion Energy CA).** `pip install` fails with SSL cert errors on this machine unless the venv's CA bundle includes both root CAs. Already fixed for `.venv/`: `corp-ca-bundle.pem` lives in the venv root and is referenced by `.venv/pip.ini`. If you create a *new* venv on this machine, you'll need to repeat the setup. Pattern documented in this project's Claude memory at `~/.claude/projects/c--Code-LCR-Logger/memory/env_corporate_ssl_inspection.md`.
- **Windows machine, no admin rights.** Hence the move to serial. Don't suggest Zadig, NI-VISA installers, or anything that needs UAC unless you've confirmed the user can get admin.

## Known limitations (from manual analysis)

Documented in the docstring at the top of `LCR_logging.py` and in `README.md`. Recap:

| # | Issue | Severity |
|---|-------|----------|
| 1 | ~~`*TRG` requires `TRIG:SOUR BUS`. The script never sets this...~~ **Fixed:** `open_instrument` now sends `TRIG:SOUR BUS` at startup. | Resolved |
| 2 | ~~Measurement function (Cp-D, Ls-Q, R-X, etc.) is never set...~~ **Fixed:** `--func <MODE>` writes `FUNC:IMP <code>` at startup (see `set_measurement_function`). Still optional — omitting it keeps the front-panel mode, as before. | Resolved |
| 3 | `FETCH?` parsing assumes 3 fields. With comparator on, the meter appends a 4th `<bin number>` field that's silently dropped. | Minor |
| 4 | Status byte not decoded. Manual p.31: `00`=normal, `-1`=no data, `+1..+4`=various errors. Script prints the raw value without flagging non-zero. | Minor |

If/when the meter is connected and #1 turns out to be a real problem, the fix is one line in `open_instrument`: `scpi_write(ser, "TRIG:SOUR BUS")` after `*CLS`.

## Repo / branch state

GitHub: `https://github.com/bnt1002/LCR-Logger` (private).

Branches:

- `Front-End` — **current working branch.** Holds everything: the serial refactor, the `TRIG:SOUR BUS` fix, the RS-232C / USBCDC interface-selection docs, the CSV/JSON output, example data, this handoff doc, and the README. ~17 commits ahead of `main`.
- `main` — **behind.** Has not yet received the serial refactor (tip is "Add numpy to requirements.txt"). Bring it up to date by merging `Front-End` into it when the work is ready to land.
- `Windows`, `driver-change-windows` — superseded snapshots, both fully merged into `Front-End`. Safe to delete locally and on origin once you're confident nothing on them is needed.

If you continue this work, `git checkout Front-End` and go.

## Things explicitly *not* done

- ~~No automatic measurement-function setup (`FUNC:IMP`).~~ Done via `--func` (still opt-in; default behaviour is unchanged).
- No `TRIG:SOUR BUS` write (see issue #1).
- No `*RST` at startup — meter state carries over between runs.
- ~~No GUI~~ — `LCR_gui.py` is a browser-based front end for the sweep + save flow (pre-fill metadata, live JSON preview, threaded run). It serves a page from the stdlib `http.server` on `127.0.0.1` rather than using Tkinter, which is not installed on the target machine (no `python3-tk`, no admin rights). No plotting yet; sweep output is still text/CSV/JSON only, and the GUI covers sweep only (no live-stream view).
- No LAN/Ethernet transport (would need raw TCP to port 5025).
- The status byte and bin number are not surfaced to the user — they're either dropped or printed raw.

## Files

All tracked status below is for the `Front-End` branch.

| File | What it is | Tracked? |
|---|---|---|
| `LCR_logging.py` | The CLI script (serial version) + reusable instrument helpers | Yes |
| `LCR_gui.py` | Browser-based GUI front end (stdlib `http.server`) for the sweep + save flow | Yes |
| `requirements.txt` | Pinned deps: `numpy`, `pyserial` | Yes |
| `README.md` | User-facing setup + usage | Yes |
| `HANDOFF.md` | This file | Yes |
| `894_895_programming_manual.pdf` | Vendor SCPI reference (~1.2 MB) | Yes |
| `Example_data/` | Sample `.txt`/`.csv`/`.json` sweep outputs | Yes |
| `.gitignore` | `.venv/`, `__pycache__/`, `*.pyc`, `*.log` | Yes |
| `.venv/` | Local virtualenv + corp CA bundle + `pip.ini` | No (gitignored) |
| `data/` | Sweep results (created on first save) | No (gitignored) |
| `LCR_logging.log` | Session log (created on first run) | No (gitignored) |

## How to pick up

If you're a future Claude session: read this file, `README.md`, and the top docstring of `LCR_logging.py`. Then `git status` and `git log --oneline -10 --all` to see the branch situation. The Claude memory at `~/.claude/projects/c--Code-LCR-Logger/memory/` has the corporate SSL context.

If you're a human teammate: clone the repo, follow `README.md` setup, ask the project owner to add you as a collaborator on GitHub. The meter is on-site — first job is to actually run the script against it and resolve issue #1 if it bites.
