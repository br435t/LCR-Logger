"""
LCR_gui.py -- A small browser-based front end for LCR_logging.py.

Lets you pre-fill the run's metadata (filename, author, description) and pick
the port / baud / measurement function up front, then run a frequency sweep and
save the .txt/.csv/.json files -- without the interactive console prompts the
CLI uses. The JSON preview pane shows exactly what will be written to the
sidecar before you commit.

WHY A LOCAL WEB UI (NOT TKINTER):
    Tkinter is not available on this machine -- `import tkinter` fails (there is
    no python3-tk system package and no admin rights to add one). Python's
    stdlib http.server has no such dependency, so this serves a tiny page on
    127.0.0.1 and uses the browser as the display. The only third-party import
    is pyserial, which LCR_logging.py already requires. Nothing to pip install
    beyond what the CLI already needs -- which matters here, where corporate SSL
    inspection makes pip painful. See HANDOFF.md "Environment quirks".

ALL THE INSTRUMENT LOGIC LIVES IN LCR_logging.py. This file only serves the
page and drives those functions; it adds no new SCPI behaviour. The sweep runs
on a background thread so the server stays responsive, and the browser polls
/api/status to follow progress.

RUN:
    python LCR_gui.py
    (opens http://127.0.0.1:<port>/ in your default browser; Ctrl+C to stop)

Same hardware setup as the CLI applies: meter on USBCDC (or RS-232C), back on
its live measurement screen, baud matching the front panel. See README.md.
"""

import csv
import json
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import LCR_logging as lcr

# Standard baud rates the meter supports (manual p.7 / front-panel options).
BAUD_CHOICES = ["9600", "19200", "28800", "38400", "48000", "57600", "115200"]

# Sentinel meaning "don't send FUNC:IMP; keep the meter's current mode".
FUNC_KEEP = "(leave as-is)"

# Loopback only -- this control surface should never be reachable off the host.
HOST = "127.0.0.1"


class SweepState:
    """
    Shared state between the sweep worker thread and the HTTP handlers.

    The worker only ever appends log lines and updates counters here under the
    lock; the browser reads a snapshot via /api/status. Nothing in here touches
    the network, so a single instance is shared by every request.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.reset()

    def reset(self) -> None:
        # Caller may hold the lock; keep this allocation-only and cheap.
        self.log_lines: list[str] = []
        self.i = 0
        self.total = 0
        self.error: str | None = None
        self.saved: list[str] | None = None
        # Numeric sweep series for the visualizer (set when a sweep collects).
        self.plot: dict | None = None

    def is_running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def log(self, line: str) -> None:
        with self.lock:
            self.log_lines.append(line)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.is_running(),
                "i": self.i,
                "total": self.total,
                "log": list(self.log_lines),
                "error": self.error,
                "saved": self.saved,
            }


STATE = SweepState()


# ── Preview / labels (mirror the old _refresh_preview / _measurement_label) ──

def measurement_label(func: str) -> str:
    """Measurement string for the preview, given the function selection."""
    if func in lcr.IMP_FUNCTIONS:
        primary, secondary = lcr.IMP_FUNCTIONS[func]
        return f"{primary}-{secondary}"
    return "(read from meter at run time)"


def build_preview(name: str, author: str, description: str, func: str) -> dict:
    """The JSON sidecar that *will* be written, from the current field values."""
    stem = Path(name).with_suffix("") if name else Path("<filename>")
    csv_path = lcr.DATA_DIR / stem.with_suffix(".csv")
    return {
        "csv_file": str(csv_path.resolve()),
        "test_time": "(set when the sweep runs)",
        "author": author.strip(),
        "description": description.strip(),
        "measurement": measurement_label(func),
    }


def _to_float(text: str) -> float | None:
    """Parse one measurement field to float, or None if it isn't numeric."""
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def build_plot(
    rows: list[tuple[float, str]], primary_label: str, secondary_label: str
) -> dict:
    """
    Turn collected sweep rows into numeric series the browser can plot:
    frequency (X) against either measured column (Y). Non-numeric fields become
    null and are skipped client-side. Frequencies are cast to plain float so
    json.dumps accepts them (collect_sweep yields numpy floats).
    """
    freq, primary, secondary = [], [], []
    for f, data in rows:
        parts = data.split(",")
        freq.append(float(f))
        primary.append(_to_float(parts[0].strip() if len(parts) > 0 else ""))
        secondary.append(_to_float(parts[1].strip() if len(parts) > 1 else ""))
    return {
        "freq": freq,
        "primary": primary,
        "secondary": secondary,
        "primary_label": primary_label,
        "secondary_label": secondary_label,
    }


# ── Saved datasets (for the visualizer's dataset dropdown) ────────────────────

def dataset_dir(folder: str = "") -> Path:
    """The folder to scan for datasets: the user-supplied one, else DATA_DIR."""
    folder = (folder or "").strip()
    return Path(folder).expanduser() if folder else lcr.DATA_DIR


def list_datasets(folder: str = "") -> list[str]:
    """Names (stems) of sweeps with a .csv in `folder` (defaults to DATA_DIR)."""
    base = dataset_dir(folder)
    if not base.is_dir():
        return []
    return sorted(p.stem for p in base.glob("*.csv"))


def load_dataset(name: str, folder: str = "") -> dict | None:
    """
    Parse a saved sweep CSV into the same structure build_plot produces.

    The CSV header is [Freq (Hz), <primary_label>, <secondary_label>, Status]
    as written by save_sweep, so the column labels come straight from the file.
    `folder` is the user-selected scan folder (defaults to DATA_DIR); only a
    bare filename within it is accepted (Path(name).name strips any directory
    components) so a crafted name can't escape the chosen folder. Returns None
    if the file is missing or unreadable.
    """
    csv_path = dataset_dir(folder) / f"{Path(name).name}.csv"
    if not csv_path.is_file():
        return None
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                return None
            primary_label = header[1] if len(header) > 1 else "Primary"
            secondary_label = header[2] if len(header) > 2 else "Secondary"
            freq, primary, secondary = [], [], []
            for row in reader:
                if not row:
                    continue
                f_hz = _to_float(row[0]) if len(row) > 0 else None
                if f_hz is None:  # frequency is the X axis -- skip if it didn't parse
                    continue
                freq.append(f_hz)
                primary.append(_to_float(row[1]) if len(row) > 1 else None)
                secondary.append(_to_float(row[2]) if len(row) > 2 else None)
    except (OSError, UnicodeDecodeError, csv.Error):
        return None
    return {
        "freq": freq,
        "primary": primary,
        "secondary": secondary,
        "primary_label": primary_label,
        "secondary_label": secondary_label,
    }


# ── Sweep worker (mirrors the old _run_worker) ───────────────────────────────

def run_sweep_worker(params: dict) -> None:
    """
    Runs off the request threads. Pushes progress into STATE; never writes to a
    socket. The browser follows along by polling /api/status.
    """
    ser = None
    try:
        STATE.log(f"Opening {params['port']} @ {params['baud']} baud...")
        ser = lcr.open_instrument(params["port"], baud=params["baud"])
        STATE.log("Connected.")

        if params["func"]:
            lcr.set_measurement_function(ser, params["func"])
            STATE.log(f"Measurement function set to {params['func']}.")

        def progress(i: int, total: int, freq: float, data: str) -> None:
            with STATE.lock:
                STATE.i, STATE.total = i, total
            STATE.log(f"  [{i}/{total}] {freq:10.2f} Hz  ->  {data}")

        rows, primary, secondary, test_time = lcr.collect_sweep(
            ser, progress=progress, should_stop=STATE.stop_event.is_set
        )

        # Expose the data to the visualizer even if the sweep was cancelled
        # partway -- a partial curve is still worth seeing.
        with STATE.lock:
            STATE.plot = build_plot(rows, primary, secondary)

        if STATE.stop_event.is_set():
            STATE.log("Sweep cancelled -- nothing saved.")
            return

        paths = lcr.save_sweep(
            rows, params["name"], params["author"], params["description"],
            primary, secondary, test_time,
        )
        resolved = [str(p.resolve()) for p in paths]
        with STATE.lock:
            STATE.saved = resolved
        for path in resolved:
            STATE.log(f"Saved: {path}")
    except Exception as exc:  # surface any failure to the UI
        with STATE.lock:
            STATE.error = str(exc)
        STATE.log(f"ERROR: {exc}")
    finally:
        if ser is not None and ser.is_open:
            try:
                lcr.return_to_local(ser)
                ser.close()
            except Exception:
                pass


def start_sweep(params: dict) -> dict:
    """
    Validate the form and kick off the sweep on a worker thread. Returns
    {"ok": True} or {"error": "..."} -- the same checks the Tk version did.
    """
    if STATE.is_running():
        return {"error": "A sweep is already running."}

    port = (params.get("port") or "").strip()
    if not port:
        return {"error": "Select a serial port first (Refresh to rescan)."}
    name = (params.get("name") or "").strip()
    if not name:
        return {"error": "Enter a filename for the saved results."}
    try:
        baud = int(params.get("baud")) # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return {"error": f"Baud must be a number, got {params.get('baud')!r}."}

    func = params.get("func") or FUNC_KEEP
    run = {
        "port": port,
        "baud": baud,
        "func": func if func in lcr.IMP_FUNCTIONS else None,
        "name": name,
        "author": (params.get("author") or "").strip(),
        "description": (params.get("description") or "").strip(),
    }

    with STATE.lock:
        STATE.reset()
    STATE.stop_event.clear()
    STATE.log("-" * 40)
    STATE.worker = threading.Thread(target=run_sweep_worker, args=(run,), daemon=True)
    STATE.worker.start()
    return {"ok": True}


# ── Page ─────────────────────────────────────────────────────────────────────

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LCR Logger</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.4 system-ui, sans-serif; margin: 0; padding: 16px;
         max-width: 720px; }
  h1 { font-size: 18px; margin: 0 0 12px; }
  fieldset { border: 1px solid #8884; border-radius: 8px; margin: 0 0 14px;
             padding: 10px 12px 12px; }
  legend { font-weight: 600; padding: 0 6px; }
  .row { display: grid; grid-template-columns: 110px 1fr auto; gap: 8px;
         align-items: center; margin: 6px 0; }
  label { text-align: right; }
  input, select, button { font: inherit; padding: 5px 7px; border-radius: 6px;
          border: 1px solid #8886; background: Field; color: FieldText; }
  input, select { width: 100%; box-sizing: border-box; }
  button { cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
  .controls { display: flex; gap: 8px; align-items: center; margin-bottom: 14px; }
  progress { flex: 1; height: 16px; }
  pre { background: #8881; border-radius: 6px; padding: 8px; margin: 0;
        white-space: pre-wrap; word-break: break-word; }
  #log { height: 220px; overflow: auto; }
  canvas { width: 100%; max-width: 680px; height: auto; display: block;
           margin-top: 8px; border: 1px solid #8884; border-radius: 6px;
           background: #8881; }
</style>
</head>
<body>
<h1>LCR Logger</h1>

<fieldset>
  <legend>Run setup</legend>
  <div class="row">
    <label for="port">Port</label>
    <select id="port"></select>
    <button id="refresh" type="button">Refresh</button>
  </div>
  <div class="row">
    <label for="baud">Baud</label>
    <input id="baud" list="bauds" value="9600">
    <datalist id="bauds">__BAUDS__</datalist>
    <span></span>
  </div>
  <div class="row">
    <label for="func">Function</label>
    <select id="func">__FUNCS__</select>
    <span></span>
  </div>
  <div class="row">
    <label for="name">Filename</label>
    <input id="name"><span></span>
  </div>
  <div class="row">
    <label for="author">Author</label>
    <input id="author"><span></span>
  </div>
  <div class="row">
    <label for="desc">Description</label>
    <input id="desc"><span></span>
  </div>
</fieldset>

<fieldset>
  <legend>JSON metadata preview</legend>
  <pre id="preview"></pre>
</fieldset>

<div class="controls">
  <button id="run" type="button">Run sweep &amp; save</button>
  <button id="cancel" type="button" disabled>Cancel</button>
  <progress id="bar" value="0" max="1"></progress>
</div>

<fieldset>
  <legend>Progress</legend>
  <pre id="log"></pre>
</fieldset>

<fieldset>
  <legend>Visualizer</legend>
  <div class="row">
    <label for="folder">Folder</label>
    <input id="folder" value="data" placeholder="folder to scan for .csv datasets">
    <button id="scan" type="button">Scan</button>
  </div>
  <div class="row">
    <label for="dataset">Dataset</label>
    <select id="dataset"></select>
    <span></span>
  </div>
  <div class="row">
    <label for="column">Column (Y)</label>
    <select id="column"></select>
    <span></span>
  </div>
  <div class="row">
    <label for="xscale">X scale</label>
    <select id="xscale">
      <option value="log" selected>log</option>
      <option value="linear">linear</option>
    </select>
    <span></span>
  </div>
  <div class="row">
    <label for="yscale">Y scale</label>
    <select id="yscale">
      <option value="linear" selected>linear</option>
      <option value="log">log</option>
    </select>
    <span></span>
  </div>
  <canvas id="plot" width="680" height="360"></canvas>
</fieldset>

<script>
const $ = id => document.getElementById(id);
let polling = false;
let notified = false;
// How many of the server's cumulative log lines we have already rendered.
let serverLogShown = 0;

function writeLine(line) {
  $("log").textContent += line + "\\n";
  $("log").scrollTop = $("log").scrollHeight;
}

async function refreshPorts() {
  const r = await fetch("api/ports");
  const { ports } = await r.json();
  const sel = $("port"), prev = sel.value;
  sel.innerHTML = "";
  for (const p of ports) {
    const o = document.createElement("option");
    o.value = p.dev; o.textContent = p.display;
    sel.appendChild(o);
  }
  if (ports.some(p => p.dev === prev)) sel.value = prev;
  writeLine(`Found ${ports.length} connected serial device(s).`);
}

async function refreshPreview() {
  const body = {
    name: $("name").value, author: $("author").value,
    description: $("desc").value, func: $("func").value,
  };
  const r = await fetch("api/preview", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  $("preview").textContent = await r.text();
}

let previewTimer = null;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(refreshPreview, 150);
}

function setRunning(running) {
  $("run").disabled = running;
  $("cancel").disabled = !running;
}

async function poll() {
  const r = await fetch("api/status");
  const s = await r.json();
  // Replay only the server log lines we have not shown yet.
  for (let k = serverLogShown; k < s.log.length; k++) writeLine(s.log[k]);
  serverLogShown = s.log.length;
  $("bar").max = s.total || 1;
  $("bar").value = s.i;
  setRunning(s.running);
  if (!s.running && !notified) {
    showCurrent();  // rescan saved datasets, then show the fresh sweep
    if (s.error) alert("Error: " + s.error);
    else if (s.saved) alert("Wrote:\\n" + s.saved.join("\\n"));
    notified = true;
    polling = false;
    return;
  }
  if (s.running) setTimeout(poll, 200);
  else polling = false;
}

async function run() {
  const body = {
    port: $("port").value, baud: $("baud").value, func: $("func").value,
    name: $("name").value, author: $("author").value, description: $("desc").value,
  };
  const r = await fetch("api/start", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const res = await r.json();
  if (res.error) { alert(res.error); return; }
  notified = false;
  serverLogShown = 0;  // server cleared its log on start; replay from the top
  setRunning(true);
  if (!polling) { polling = true; poll(); }
}

async function cancel() {
  await fetch("api/cancel", { method: "POST" });
}

// ── Visualizer ───────────────────────────────────────────────
// Plots frequency (X) against the selected measured column (Y) on a canvas.
// Vanilla JS so there is nothing to load from a CDN -- keeps it offline.
let plotData = null;

function fmtNum(v) {
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a >= 1e4 || a < 1e-2) return v.toExponential(1);
  return (Math.round(v * 1000) / 1000).toString();
}

// SI prefixes for Y-axis scaling (engineering powers of 1000).
const SI_PREFIXES = [
  [1e9, "G"], [1e6, "M"], [1e3, "k"], [1, ""],
  [1e-3, "m"], [1e-6, "µ"], [1e-9, "n"], [1e-12, "p"],
];
// Units we SI-scale; phase (deg/rad) and dimensionless D/Q are left alone.
const SCALABLE = new Set(["F", "H", "Ω", "S", "V", "A", "W"]);

// Pick the prefix that puts the largest value in the [1, 1000) range.
function siPrefix(maxAbs) {
  if (!isFinite(maxAbs) || maxAbs <= 0) return [1, ""];
  for (const [factor, p] of SI_PREFIXES) if (maxAbs >= factor) return [factor, p];
  return [1e-12, "p"];  // smaller than pico -> still express in pico
}

// "Cp (F)" -> {name:"Cp", unit:"F"};  "D" -> {name:"D", unit:""}
function splitUnit(label) {
  const m = /^(.*?)\\s*\\(([^)]*)\\)\\s*$/.exec(label);
  return m ? { name: m[1], unit: m[2] } : { name: label, unit: "" };
}

function fillColumns() {
  const sel = $("column"), prev = sel.value;
  sel.innerHTML = "";
  if (!plotData) return;
  for (const [val, lab] of [["primary", plotData.primary_label],
                            ["secondary", plotData.secondary_label]]) {
    const o = document.createElement("option");
    o.value = val; o.textContent = lab;
    sel.appendChild(o);
  }
  if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
}

function ticksLog(lo, hi) {
  const t = [];
  for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) {
    const v = Math.pow(10, e);
    if (v >= lo * 0.999 && v <= hi * 1.001) t.push(v);
  }
  return t.length < 2 ? [lo, hi] : t;
}

function ticksLin(lo, hi, n = 5) {
  if (lo === hi) return [lo];
  const t = [];
  for (let k = 0; k <= n; k++) t.push(lo + (hi - lo) * k / n);
  return t;
}

async function loadPlot() {
  try {
    const r = await fetch("api/plot");
    plotData = (await r.json()).plot;
  } catch (_) { plotData = null; }
  fillColumns();
  drawPlot();
}

// Value used in the dataset dropdown for the live, in-memory sweep.
const CURRENT = "__current__";

async function refreshDatasets(announce = false) {
  const dir = $("folder").value.trim();
  let info = { datasets: [], has_current: false };
  try {
    info = await (await fetch("api/datasets?dir=" + encodeURIComponent(dir))).json();
  } catch (_) {}
  const sel = $("dataset"), prev = sel.value;
  sel.innerHTML = "";
  if (info.has_current) {
    const o = document.createElement("option");
    o.value = CURRENT; o.textContent = "Current sweep";
    sel.appendChild(o);
  }
  for (const name of (info.datasets || [])) {
    const o = document.createElement("option");
    o.value = name; o.textContent = name;
    sel.appendChild(o);
  }
  if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
  if (announce) {
    const n = (info.datasets || []).length;
    const where = info.dir || dir;
    writeLine(info.exists === false
      ? `Folder not found: ${where}`
      : `Scanned ${where}: ${n} dataset(s).`);
  }
}

async function selectDataset() {
  const v = $("dataset").value;
  if (v === CURRENT) { await loadPlot(); return; }
  if (!v) { plotData = null; fillColumns(); drawPlot(); return; }
  const dir = $("folder").value.trim();
  try {
    const r = await fetch("api/dataset?name=" + encodeURIComponent(v) +
                          "&dir=" + encodeURIComponent(dir));
    plotData = (await r.json()).plot;
  } catch (_) { plotData = null; }
  fillColumns();
  drawPlot();
}

// After a run: rescan files, then show the just-collected (current) sweep.
async function showCurrent() {
  await refreshDatasets();
  if ([...$("dataset").options].some(o => o.value === CURRENT))
    $("dataset").value = CURRENT;
  await selectDataset();
}

function drawPlot() {
  const cv = $("plot"), ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height;
  const ink = getComputedStyle(cv).color || "#000";
  const accent = "#2f81f7";
  ctx.clearRect(0, 0, W, H);
  ctx.font = "12px system-ui, sans-serif";
  ctx.textBaseline = "middle";

  const note = msg => {
    ctx.fillStyle = ink; ctx.globalAlpha = .6; ctx.textAlign = "left";
    ctx.fillText(msg, 16, 24); ctx.globalAlpha = 1;
  };
  if (!plotData || !plotData.freq || !plotData.freq.length)
    return note("No sweep data yet - run a sweep to plot.");

  const col = $("column").value || "primary";
  const ys = plotData[col] || [];
  const ylabel = col === "primary" ? plotData.primary_label : plotData.secondary_label;
  const xlog = $("xscale").value === "log";
  const ylog = $("yscale").value === "log";

  const pts = [];
  for (let k = 0; k < plotData.freq.length; k++) {
    const x = plotData.freq[k], y = ys[k];
    if (y === null || y === undefined || !isFinite(y)) continue;
    if (xlog && x <= 0) continue;
    if (ylog && y <= 0) continue;
    pts.push({ x, y });
  }
  if (!pts.length) return note("No plottable points for this column/scale.");

  const L = 66, R = W - 16, T = 16, B = H - 38;
  const xs = pts.map(p => p.x), yv = pts.map(p => p.y);
  const xlo = Math.min(...xs), xhi = Math.max(...xs);
  const ylo = Math.min(...yv), yhi = Math.max(...yv);
  const fx = v => xlog ? Math.log10(v) : v;
  const fy = v => ylog ? Math.log10(v) : v;

  let xmn = fx(xlo), xmx = fx(xhi), ymn = fy(ylo), ymx = fy(yhi);
  if (xmn === xmx) { xmn -= 1; xmx += 1; }
  if (ymn === ymx) { ymn -= 1; ymx += 1; }
  else if (!ylog) { const pad = (ymx - ymn) * 0.06; ymn -= pad; ymx += pad; }

  const sx = v => L + (fx(v) - xmn) / (xmx - xmn) * (R - L);
  const sy = v => B - (fy(v) - ymn) / (ymx - ymn) * (B - T);

  // SI-prefix the Y axis: one prefix for the whole axis from the data's
  // magnitude (e.g. ~1e-7 F -> "n"), so ticks read "123" on a "Cp (nF)" axis
  // rather than "1.2e-7". Only for units with an SI base (F/H/Ω/S/...).
  const { name: yName, unit: yUnit } = splitUnit(ylabel);
  let yFactor = 1, yUnitLabel = ylabel;
  if (SCALABLE.has(yUnit)) {
    const mags = pts.map(p => Math.abs(p.y)).filter(v => isFinite(v) && v > 0);
    const [factor, prefix] = siPrefix(mags.length ? Math.max(...mags) : 0);
    yFactor = factor;
    yUnitLabel = `${yName} (${prefix}${yUnit})`;
  }

  // Gridlines + tick labels.
  ctx.strokeStyle = ink; ctx.fillStyle = ink; ctx.lineWidth = 1;
  ctx.textAlign = "center";
  for (const xt of (xlog ? ticksLog(xlo, xhi) : ticksLin(xlo, xhi))) {
    const px = sx(xt);
    ctx.globalAlpha = .12; ctx.beginPath(); ctx.moveTo(px, T); ctx.lineTo(px, B); ctx.stroke();
    ctx.globalAlpha = .8; ctx.fillText(fmtNum(xt), px, B + 14);
  }
  ctx.textAlign = "right";
  for (const yt of (ylog ? ticksLog(ylo, yhi) : ticksLin(ylo, yhi))) {
    const py = sy(yt);
    ctx.globalAlpha = .12; ctx.beginPath(); ctx.moveTo(L, py); ctx.lineTo(R, py); ctx.stroke();
    ctx.globalAlpha = .8; ctx.fillText(fmtNum(yt / yFactor), L - 6, py);
  }
  ctx.globalAlpha = 1;

  // Frame + axis titles.
  ctx.globalAlpha = .5; ctx.strokeRect(L, T, R - L, B - T); ctx.globalAlpha = 1;
  // "log " prefix (not a " (log)" suffix) so it doesn't collide with the
  // unit already in the label, e.g. "log Cp (F)" rather than "Cp (F) (log)".
  ctx.textAlign = "center";
  ctx.fillText((xlog ? "log " : "") + "Frequency (Hz)", (L + R) / 2, H - 8);
  ctx.save();
  ctx.translate(14, (T + B) / 2); ctx.rotate(-Math.PI / 2);
  ctx.fillText((ylog ? "log " : "") + yUnitLabel, 0, 0);
  ctx.restore();

  // Data line + point markers.
  ctx.strokeStyle = accent; ctx.fillStyle = accent; ctx.lineWidth = 2;
  ctx.beginPath();
  pts.forEach((p, k) => { const X = sx(p.x), Y = sy(p.y); k ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); });
  ctx.stroke();
  for (const p of pts) { ctx.beginPath(); ctx.arc(sx(p.x), sy(p.y), 2.5, 0, Math.PI * 2); ctx.fill(); }
}

$("refresh").onclick = refreshPorts;
$("run").onclick = run;
$("cancel").onclick = cancel;
for (const id of ["name", "author", "desc", "func"])
  $(id).addEventListener("input", schedulePreview);
for (const id of ["column", "xscale", "yscale"])
  $(id).addEventListener("change", drawPlot);
$("dataset").onchange = selectDataset;
$("scan").onclick = async () => { await refreshDatasets(true); await selectDataset(); };
$("folder").addEventListener("keydown", e => { if (e.key === "Enter") $("scan").click(); });

refreshPorts();
refreshPreview();
refreshDatasets().then(selectDataset);
</script>
</body>
</html>
"""


def render_page() -> bytes:
    bauds = "".join(f"<option value='{b}'>" for b in BAUD_CHOICES)
    funcs = "".join(
        f"<option value='{f}'>{f}</option>"
        for f in [FUNC_KEEP] + sorted(lcr.IMP_FUNCTIONS)
    )
    html = PAGE.replace("__BAUDS__", bauds).replace("__FUNCS__", funcs)
    return html.encode("utf-8")


# ── HTTP handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    # Quiet the default one-line-per-request stderr spam; the meter log is
    # what matters here, not every poll.
    def log_message(self, *args) -> None:  # noqa: D401
        pass

    def _send(self, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj) -> None:
        self._send(json.dumps(obj).encode("utf-8"))

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(render_page(), "text/html; charset=utf-8")
        elif path == "/api/ports":
            ports = [
                {"dev": dev, "display": f"{dev}  --  {desc}"}
                for dev, desc, _hwid in lcr.get_serial_ports()
            ]
            self._json({"ports": ports})
        elif path == "/api/status":
            self._json(STATE.snapshot())
        elif path == "/api/plot":
            with STATE.lock:
                plot = STATE.plot
            self._json({"plot": plot})
        elif path == "/api/datasets":
            folder = (parse_qs(urlparse(self.path).query).get("dir") or [""])[0]
            with STATE.lock:
                has_current = STATE.plot is not None
            base = dataset_dir(folder)
            self._json({
                "datasets": list_datasets(folder),
                "has_current": has_current,
                "dir": str(base),
                "exists": base.is_dir(),
            })
        elif path == "/api/dataset":
            qs = parse_qs(urlparse(self.path).query)
            name = (qs.get("name") or [""])[0]
            folder = (qs.get("dir") or [""])[0]
            self._json({"plot": load_dataset(name, folder)})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/preview":
            body = self._read_body()
            preview = build_preview(
                body.get("name", ""), body.get("author", ""),
                body.get("description", ""), body.get("func", FUNC_KEEP),
            )
            self._send(json.dumps(preview, indent=2).encode("utf-8"))
        elif path == "/api/start":
            self._json(start_sweep(self._read_body()))
        elif path == "/api/cancel":
            if STATE.is_running():
                STATE.stop_event.set()
                STATE.log("Cancel requested -- stopping after the current point...")
            self._json({"ok": True})
        else:
            self.send_error(404)


def main() -> None:
    server = ThreadingHTTPServer((HOST, 0), Handler)
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"LCR Logger UI running at {url}")
    print("Open it in your browser if it didn't open automatically. Ctrl+C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
