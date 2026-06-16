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


def measurement_units(func: str) -> dict | str:
    """
    Parsed (primary, secondary) units for the preview. Mirrors what save_sweep
    records. If no function is selected (FUNC_KEEP), the units aren't known until
    the meter is read at run time, so a placeholder string is shown instead.
    """
    if func in lcr.IMP_FUNCTIONS:
        primary, secondary = lcr.IMP_FUNCTIONS[func]
        return {
            "primary": lcr.label_unit(primary),
            "secondary": lcr.label_unit(secondary),
        }
    return "(read from meter at run time)"


def build_preview(
    name: str, author: str, description: str, func: str,
    tags: dict[str, list[str]] | list[str] | None = None,
) -> dict:
    """The JSON sidecar that *will* be written, from the current field values."""
    stem = Path(name).with_suffix("") if name else Path("<filename>")
    csv_path = lcr.DATA_DIR / stem.with_suffix(".csv")
    return {
        "csv_file": str(csv_path.resolve()),
        "test_time": "(set when the sweep runs)",
        "author": author.strip(),
        "description": description.strip(),
        "measurement": measurement_label(func),
        "units": measurement_units(func),
        "tags": lcr.normalize_tag_selection(tags),
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


def dataset_tag_groups(name: str, folder: str = "") -> dict[str, list[str]]:
    """
    Tags recorded in a dataset's JSON sidecar (the `tags` field save_sweep
    writes), as the categorized {section: [tags]} mapping. Handles both the
    current dict shape and the legacy flat list (recorded under the first
    section). Returns empty sections if the sidecar is missing, unreadable, or
    carries no tags. Like load_dataset, only a bare filename within `folder` is
    accepted.
    """
    json_path = dataset_dir(folder) / f"{Path(name).name}.json"
    if not json_path.is_file():
        return {s: [] for s in lcr.TAG_SECTIONS}
    try:
        meta = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {s: [] for s in lcr.TAG_SECTIONS}
    tags = meta.get("tags") if isinstance(meta, dict) else None
    return lcr.normalize_tag_selection(tags)


def dataset_tags(name: str, folder: str = "") -> list[str]:
    """Flat list of a dataset's tags across all sections, for display/matching."""
    return [t for ts in dataset_tag_groups(name, folder).values() for t in ts]


def datasets_with_tag_groups(folder: str = "") -> dict[str, dict[str, list[str]]]:
    """Map each dataset stem in `folder` to its categorized sidecar tags."""
    return {name: dataset_tag_groups(name, folder) for name in list_datasets(folder)}


# Section the visualizer filter uses for dataset tags that aren't defined in
# tags.yaml (e.g. tags since removed from the YAML, or legacy uncategorized
# sidecars). Shown as its own box after the canonical sections.
OTHER_TAG_SECTION = "other"


def filter_tag_groups(folder: str = "") -> dict[str, list[str]]:
    """
    Tags present across a folder's datasets, grouped for the visualizer filter.
    tags.yaml is the source of truth for a tag's section: each dataset tag is
    placed in its YAML section, and any tag not in the YAML falls into the
    "other" section. Sections are ordered (canonical first, then "other") and
    empty sections are omitted, so the filter shows only what's filterable.
    """
    section_of = {
        tag: section
        for section, tags in lcr.load_tag_groups().items()
        for tag in tags
    }
    present = {
        tag
        for groups in datasets_with_tag_groups(folder).values()
        for tags in groups.values()
        for tag in tags
    }
    grouped: dict[str, list[str]] = {}
    for tag in sorted(present, key=str.lower):
        grouped.setdefault(section_of.get(tag, OTHER_TAG_SECTION), []).append(tag)

    order = list(lcr.TAG_SECTIONS) + [OTHER_TAG_SECTION]
    ordered = {s: grouped[s] for s in order if s in grouped}
    ordered.update({s: grouped[s] for s in grouped if s not in ordered})
    return ordered


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
            ser, start_hz=params["start"], stop_hz=params["stop"],
            points=params["points"], progress=progress,
            should_stop=STATE.stop_event.is_set,
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
            primary, secondary, test_time, tags=params["tags"],
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

    # Sweep span/points. 20 Hz is the meter's floor; the log sweep needs both
    # endpoints positive, stop above start, and at least two points.
    try:
        start = float(params.get("start"))   # pyright: ignore[reportArgumentType]
        stop = float(params.get("stop"))      # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return {"error": "Start and Stop frequencies must be numbers."}
    if start <= 0 or stop <= 0:
        return {"error": "Start and Stop frequencies must be positive."}
    if stop <= start:
        return {"error": "Stop frequency must be greater than Start."}
    try:
        points = int(params.get("points"))    # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return {"error": f"Points must be a whole number, got {params.get('points')!r}."}
    if points < 2:
        return {"error": "Points must be at least 2."}

    func = params.get("func") or FUNC_KEEP
    tags = lcr.normalize_tag_selection(params.get("tags"))
    run = {
        "port": port,
        "baud": baud,
        "func": func if func in lcr.IMP_FUNCTIONS else None,
        "name": name,
        "author": (params.get("author") or "").strip(),
        "description": (params.get("description") or "").strip(),
        "tags": tags,
        "start": start,
        "stop": stop,
        "points": points,
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
  .hint { font-size: 12px; opacity: .6; text-align: left; }
  .checklist { border: 1px solid #8886; border-radius: 6px; padding: 6px 8px;
               max-height: 120px; overflow: auto; background: Field; }
  .checklist label { display: block; text-align: left; cursor: pointer;
                     padding: 1px 0; }
  .checklist input { width: auto; margin: 0 6px 0 0; }
  .checklist .empty { opacity: .6; }
  .checklist .tagnote { opacity: .55; margin-left: 6px; font-size: 12px; }
  /* One scrollable checklist box per tag section, side by side; wraps to the
     next line when there isn't room (e.g. with an extra "other" section). */
  .taggrid { display: flex; flex-wrap: wrap; gap: 8px; }
  .taggrid .tagcol { flex: 1 1 140px; min-width: 0; text-align: left; }
  .taghead { font-size: 11px; font-weight: 600; text-transform: uppercase;
             letter-spacing: .04em; opacity: .55; margin: 0 0 2px; }
  .addtag { display: flex; gap: 8px; }
  .addtag input { flex: 1; }
  .addtag select { width: auto; }
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
  .plotwrap { position: relative; }
  #tip { position: absolute; display: none; pointer-events: none; z-index: 5;
         background: Canvas; color: CanvasText; border: 1px solid #8888;
         border-radius: 6px; padding: 4px 7px; white-space: pre;
         font: 12px/1.3 system-ui, sans-serif; box-shadow: 0 2px 8px #0004; }
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
    <label for="start">Start (Hz)</label>
    <input id="start" type="number" min="0" step="any" value="__START__">
    <span></span>
  </div>
  <div class="row">
    <label for="stop">Stop (Hz)</label>
    <input id="stop" type="number" min="0" step="any" value="__STOP__">
    <span></span>
  </div>
  <div class="row">
    <label for="points">Points</label>
    <input id="points" type="number" min="2" step="1" value="__POINTS__">
    <span class="hint">log-spaced from start to stop</span>
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
  <div class="row">
    <label>Tags</label>
    <div id="tags" class="taggrid"></div>
    <span class="hint">Tick tags to attach to this run</span>
  </div>
  <div class="row">
    <label for="newtag">Add tag</label>
    <div class="addtag">
      <input id="newtag" placeholder="new tag name, then Add">
      <select id="newtagsection"></select>
    </div>
    <button id="addtag" type="button">Add</button>
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
    <label>Filter tags</label>
    <div id="tagfilter" class="taggrid"></div>
    <span class="hint">Show only datasets with these tags</span>
  </div>
  <div class="row">
    <label for="tagmatch">Match</label>
    <select id="tagmatch">
      <option value="any" selected>any selected tag</option>
      <option value="all">all selected tags</option>
    </select>
    <span></span>
  </div>
  <div class="row">
    <label>Datasets</label>
    <div id="dataset" class="checklist"></div>
    <span class="hint">Tick runs to overlay them</span>
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
  <div class="plotwrap">
    <canvas id="plot" width="680" height="360"></canvas>
    <div id="tip"></div>
  </div>
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
    tags: checkedTags(),
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

// ── Tags ─────────────────────────────────────────────────────
// Tags are checkboxes built from the server's YAML, grouped into sections
// (test parameters / test configurations). New ones can be added at runtime to
// a chosen section, which persists them server-side and ticks them for this run.

// "test_parameters" -> "Test parameters" for section headings/dropdowns.
function sectionLabel(s) {
  const t = s.replace(/_/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
}

// Ticked run tags as the categorized {section: [tag, ...]} selection the
// server records in the sidecar. Sections with no ticks are omitted.
function checkedTags() {
  const groups = {};
  for (const cb of $("tags").querySelectorAll("input:checked")) {
    const s = cb.dataset.section;
    (groups[s] = groups[s] || []).push(cb.value);
  }
  return groups;
}

// Render {section: [tags]} into `container` as one labeled, scrollable checklist
// box per section, side by side. `isChecked(section, name)` sets each box's
// initial ticks; `emptyText` (optional) fills a section that has no tags.
// Returns the number of checkboxes rendered. Shared by the run picker and the
// visualizer's tag filter.
function renderTagBoxes(container, groups, isChecked, emptyText) {
  container.innerHTML = "";
  let count = 0;
  for (const section of Object.keys(groups)) {
    const tags = groups[section] || [];
    const col = document.createElement("div");
    col.className = "tagcol";
    const head = document.createElement("div");
    head.className = "taghead"; head.textContent = sectionLabel(section);
    col.appendChild(head);
    const list = document.createElement("div");
    list.className = "checklist";
    for (const name of tags) {
      count++;
      const lab = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox"; cb.value = name; cb.dataset.section = section;
      cb.checked = isChecked(section, name);
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(name));
      list.appendChild(lab);
    }
    if (!tags.length && emptyText) {
      const span = document.createElement("span");
      span.className = "empty"; span.textContent = emptyText;
      list.appendChild(span);
    }
    col.appendChild(list);
    container.appendChild(col);
  }
  return count;
}

// Render the run-tag picker (one box per section) and keep the add-tag section
// dropdown in sync with the sections the server knows about.
function renderTagPicker(groups) {
  const prev = checkedTags();  // keep ticks across a refresh, per section
  const sections = Object.keys(groups);
  renderTagBoxes($("tags"), groups,
    (section, name) => (prev[section] || []).includes(name), "No tags yet.");
  if (!sections.length) {
    const span = document.createElement("span");
    span.className = "empty"; span.textContent = "No tags yet - add one below.";
    $("tags").appendChild(span);
  }

  // Section dropdown for the "Add tag" control: one option per known section.
  const sel = $("newtagsection"), prevSection = sel.value;
  sel.innerHTML = "";
  for (const section of sections) {
    const o = document.createElement("option");
    o.value = section; o.textContent = sectionLabel(section);
    sel.appendChild(o);
  }
  if ([...sel.options].some(o => o.value === prevSection)) sel.value = prevSection;
}

async function refreshTags() {
  let groups = {};
  try { groups = (await (await fetch("api/tags")).json()).groups || {}; } catch (_) {}
  renderTagPicker(groups);
}

async function addTag() {
  const input = $("newtag"), tag = input.value.trim();
  const section = $("newtagsection").value;
  if (!tag) return;
  try {
    await fetch("api/tags", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tag, section }),
    });
  } catch (_) {}
  input.value = "";
  await refreshTags();
  for (const cb of $("tags").querySelectorAll("input"))
    if (cb.value === tag && cb.dataset.section === section) cb.checked = true;
  refreshPreview();
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
    tags: checkedTags(),
    start: $("start").value, stop: $("stop").value, points: $("points").value,
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
// One entry per selected run, so multiple runs can overlay on the same axes:
//   [{name, freq, primary, secondary, primary_label, secondary_label}, ...]
let plotSeries = [];

// Distinct line colours, cycled per overlaid run.
const PALETTE = ["#2f81f7", "#e0533d", "#3fb950", "#bf6bd1",
                 "#d29922", "#26a3a3", "#db61a2", "#8b949e"];

// Screen positions of every drawn marker, rebuilt each drawPlot, so a mouseover
// can map back to the exact data point: [{px, py, text}, ...].
let hoverPts = [];

function fmtNum(v) {
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a >= 1e4 || a < 1e-2) return v.toExponential(1);
  return (Math.round(v * 1000) / 1000).toString();
}

// Higher-precision formatter for the hover tooltip ("exact" readout), with
// trailing zeros trimmed off the fixed-notation form.
function fmtFull(v) {
  if (!isFinite(v)) return String(v);
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a >= 1e6 || a < 1e-3) return v.toExponential(4);
  let s = v.toPrecision(6);
  if (s.indexOf(".") >= 0) s = s.replace(/\\.?0+$/, "");
  return s;
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

// Format a value in its own SI-scaled unit, e.g. fmtSI(1234, "Hz") -> "1.234 kHz".
// Dimensionless quantities (unit === "") are returned unscaled.
function fmtSI(v, unit) {
  if (!unit) return fmtFull(v);
  const [factor, prefix] = siPrefix(Math.abs(v));
  return fmtFull(v / factor) + " " + prefix + unit;
}

// Fallback units for bare parameter names whose header carries no "(unit)" --
// e.g. older CSVs (and the bundled Example_data) written with plain "Ls"/"Rs"
// headers instead of "Ls (H)"/"Rs (Ω)". Keyed by parameter name; D and Q are
// dimensionless and intentionally absent.
const PARAM_UNITS = {
  Cp: "F", Cs: "F", Lp: "H", Ls: "H",
  Rp: "Ω", Rs: "Ω", R: "Ω", X: "Ω", G: "S",
};

// "Cp (F)" -> {name:"Cp", unit:"F"};  bare "Ls" -> {name:"Ls", unit:"H"} via
// the PARAM_UNITS fallback;  "D" -> {name:"D", unit:""}.
function splitUnit(label) {
  const m = /^(.*?)\\s*\\(([^)]*)\\)\\s*$/.exec(label);
  if (m) return { name: m[1], unit: m[2] };
  const name = (label || "").trim();
  return { name, unit: PARAM_UNITS[name] || "" };
}

function fillColumns() {
  const sel = $("column"), prev = sel.value;
  sel.innerHTML = "";
  if (!plotSeries.length) return;
  const base = plotSeries[0];  // column labels come from the first selected run
  for (const [val, lab] of [["primary", base.primary_label],
                            ["secondary", base.secondary_label]]) {
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

// Minor ticks (subticks) for a log axis: the 2..9 multipliers within each
// decade that fall inside the visible range.
function minorTicksLog(lo, hi) {
  const t = [];
  for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) {
    for (let m = 2; m <= 9; m++) {
      const v = m * Math.pow(10, e);
      if (v >= lo && v <= hi) t.push(v);
    }
  }
  return t;
}

// Minor ticks for a linear axis: `sub` subdivisions inside each of the `n`
// major intervals (the major boundaries themselves are excluded).
function minorTicksLin(lo, hi, n = 5, sub = 5) {
  if (lo === hi) return [];
  const t = [], step = (hi - lo) / n;
  for (let k = 0; k < n; k++)
    for (let s = 1; s < sub; s++) t.push(lo + step * (k + s / sub));
  return t;
}

// Value used in the dataset dropdown for the live, in-memory sweep.
const CURRENT = "__current__";

// Fetch one run's series by its dropdown value (CURRENT -> the live, in-memory
// sweep; otherwise a saved .csv in the scan folder). Returns the series tagged
// with a display name, or null if it didn't load.
async function fetchSeries(value) {
  const dir = $("folder").value.trim();
  try {
    const data = value === CURRENT
      ? (await (await fetch("api/plot")).json()).plot
      : (await (await fetch("api/dataset?name=" + encodeURIComponent(value) +
                            "&dir=" + encodeURIComponent(dir))).json()).plot;
    if (!data) return null;
    return { name: value === CURRENT ? "Current sweep" : value, ...data };
  } catch (_) {
    return null;
  }
}

// Latest /api/datasets response, kept so the tag filter can re-render the
// dataset list without re-fetching.
let datasetInfo = { datasets: [], tags_by_dataset: {}, all_tag_groups: {}, has_current: false };

// Values of the currently ticked dataset checkboxes.
function checkedDatasets() {
  return [...$("dataset").querySelectorAll("input:checked")].map(c => c.value);
}

// Values of the currently ticked filter-tag checkboxes.
function filterTags() {
  return [...$("tagfilter").querySelectorAll("input:checked")].map(c => c.value);
}

// Does a dataset's tags satisfy the current filter? Empty filter -> always.
// "all" requires every ticked tag; "any" requires at least one.
function passesFilter(tags) {
  const sel = filterTags();
  if (!sel.length) return true;
  return $("tagmatch").value === "all"
    ? sel.every(t => tags.includes(t))
    : sel.some(t => tags.includes(t));
}

async function refreshDatasets(announce = false) {
  const dir = $("folder").value.trim();
  let info = { datasets: [], tags_by_dataset: {}, all_tag_groups: {}, has_current: false };
  try {
    info = await (await fetch("api/datasets?dir=" + encodeURIComponent(dir))).json();
  } catch (_) {}
  datasetInfo = info;

  // Rebuild the filter-tag checklist: one box per section (tags not in the
  // YAML come back from the server under an "other" section), keeping any
  // filter tags already ticked.
  const tf = $("tagfilter");
  const prevFilter = new Set(filterTags());
  const count = renderTagBoxes(tf, info.all_tag_groups || {},
    (_section, name) => prevFilter.has(name));
  if (!count) {
    tf.innerHTML = "";
    const span = document.createElement("span");
    span.className = "empty"; span.textContent = "No tags on these datasets.";
    tf.appendChild(span);
  }

  renderDatasets();

  if (announce) {
    const n = (info.datasets || []).length;
    const where = info.dir || dir;
    writeLine(info.exists === false
      ? `Folder not found: ${where}`
      : `Scanned ${where}: ${n} dataset(s).`);
  }
}

// Build the dataset checklist from datasetInfo, applying the tag filter. Each
// dataset shows its tags after the name. Ticks are kept for datasets that stay
// visible; "Current sweep" is always shown (it has no saved sidecar to filter).
function renderDatasets() {
  const box = $("dataset");
  const prev = new Set(checkedDatasets());
  box.innerHTML = "";
  const add = (value, text, tags) => {
    const lab = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.value = value; cb.checked = prev.has(value);
    lab.appendChild(cb);
    lab.appendChild(document.createTextNode(text));
    if (tags && tags.length) {
      const note = document.createElement("span");
      note.className = "tagnote"; note.textContent = "(" + tags.join(", ") + ")";
      lab.appendChild(note);
    }
    box.appendChild(lab);
  };
  if (datasetInfo.has_current) add(CURRENT, "Current sweep");
  for (const name of (datasetInfo.datasets || [])) {
    const tags = datasetInfo.tags_by_dataset[name] || [];
    if (passesFilter(tags)) add(name, name, tags);
  }
  if (!box.children.length) {
    const span = document.createElement("span");
    span.className = "empty";
    span.textContent = (datasetInfo.datasets || []).length
      ? "No datasets match the tag filter."
      : "No datasets found.";
    box.appendChild(span);
  }
}

// Load every selected run in parallel and overlay them. Failed/empty loads are
// dropped so one bad file doesn't blank the whole plot.
async function selectDatasets() {
  const chosen = checkedDatasets();
  const loaded = await Promise.all(chosen.map(fetchSeries));
  plotSeries = loaded.filter(Boolean);
  fillColumns();
  drawPlot();
}

// After a run: rescan files, then tick the just-collected (current) sweep
// (keeping any runs already ticked so they stay overlaid).
async function showCurrent() {
  await refreshDatasets();
  const cur = $("dataset").querySelector("input[value='" + CURRENT + "']");
  if (cur) cur.checked = true;
  await selectDatasets();
}

function drawPlot() {
  const cv = $("plot"), ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height;
  const ink = getComputedStyle(cv).color || "#000";
  ctx.clearRect(0, 0, W, H);
  hoverPts = [];  // stale on every redraw; rebuilt as markers are drawn below
  ctx.font = "12px system-ui, sans-serif";
  ctx.textBaseline = "middle";

  const note = msg => {
    ctx.fillStyle = ink; ctx.globalAlpha = .6; ctx.textAlign = "left";
    ctx.fillText(msg, 16, 24); ctx.globalAlpha = 1;
  };
  if (!plotSeries.length)
    return note("No sweep data yet - run a sweep or pick a dataset to plot.");

  const col = $("column").value || "primary";
  const ylabel = col === "primary"
    ? plotSeries[0].primary_label : plotSeries[0].secondary_label;
  const xlog = $("xscale").value === "log";
  const ylog = $("yscale").value === "log";

  // Build the plottable points for each run, dropping non-numeric / out-of-scale
  // values. Runs that end up with no plottable points are omitted entirely.
  const series = plotSeries.map(s => {
    const ys = s[col] || [];
    const pts = [];
    for (let k = 0; k < (s.freq || []).length; k++) {
      const x = s.freq[k], y = ys[k];
      if (y === null || y === undefined || !isFinite(y)) continue;
      if (xlog && x <= 0) continue;
      if (ylog && y <= 0) continue;
      pts.push({ x, y });
    }
    return { name: s.name, pts };
  }).filter(s => s.pts.length);
  if (!series.length) return note("No plottable points for this column/scale.");

  // Axis ranges span every run so all overlaid curves fit.
  const L = 66, R = W - 16, T = 16, B = H - 38;
  const xs = [], yv = [];
  for (const s of series) for (const p of s.pts) { xs.push(p.x); yv.push(p.y); }
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
  let yFactor = 1, yPrefix = "", yUnitLabel = ylabel;
  if (SCALABLE.has(yUnit)) {
    const mags = yv.map(Math.abs).filter(v => isFinite(v) && v > 0);
    const [factor, prefix] = siPrefix(mags.length ? Math.max(...mags) : 0);
    yFactor = factor; yPrefix = prefix;
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

  // Subticks: short unlabeled marks reaching inward from each axis.
  ctx.globalAlpha = .4;
  for (const xt of (xlog ? minorTicksLog(xlo, xhi) : minorTicksLin(xlo, xhi))) {
    const px = sx(xt);
    ctx.beginPath(); ctx.moveTo(px, B); ctx.lineTo(px, B - 4); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(px, T); ctx.lineTo(px, T + 4); ctx.stroke();
  }
  for (const yt of (ylog ? minorTicksLog(ylo, yhi) : minorTicksLin(ylo, yhi))) {
    const py = sy(yt);
    ctx.beginPath(); ctx.moveTo(L, py); ctx.lineTo(L + 4, py); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(R, py); ctx.lineTo(R - 4, py); ctx.stroke();
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

  // Data line + point markers, one colour per overlaid run. Each marker also
  // records its screen position and an exact-value tooltip string for hover.
  series.forEach((s, i) => {
    const color = PALETTE[i % PALETTE.length];
    ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 2;
    ctx.beginPath();
    s.pts.forEach((p, k) => { const X = sx(p.x), Y = sy(p.y); k ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); });
    ctx.stroke();
    for (const p of s.pts) {
      const X = sx(p.x), Y = sy(p.y);
      ctx.beginPath(); ctx.arc(X, Y, 2.5, 0, Math.PI * 2); ctx.fill();
      // Y matches the axis's shared SI prefix; frequency gets its own per-point
      // prefix so each reads naturally (e.g. 20 Hz, 1.23 kHz, 200 kHz).
      const yStr = fmtFull(p.y / yFactor) + (yUnit ? " " + yPrefix + yUnit : "");
      const text = (series.length > 1 ? s.name + "\\n" : "") +
        fmtSI(p.x, "Hz") + "\\n" + yName + " = " + yStr;
      hoverPts.push({ px: X, py: Y, text });
    }
  });

  // Legend (only when overlaying more than one run): a coloured swatch and the
  // run name per series, top-right and right-aligned so long names grow inward.
  if (series.length > 1) {
    series.forEach((s, i) => {
      const color = PALETTE[i % PALETTE.length];
      const ly = T + 10 + i * 16;
      ctx.textAlign = "right"; ctx.fillStyle = ink; ctx.globalAlpha = .85;
      ctx.fillText(s.name, R - 8, ly);
      const tw = ctx.measureText(s.name).width;
      ctx.globalAlpha = 1; ctx.strokeStyle = color; ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(R - 8 - tw - 22, ly); ctx.lineTo(R - 8 - tw - 8, ly); ctx.stroke();
    });
  }
}

// Hover readout: find the marker nearest the cursor (within ~14 canvas px) and
// show its exact values in a tooltip. The canvas is drawn at its intrinsic
// 680x360 but displayed scaled to fit, so map mouse -> canvas coords first.
function showTip(evt) {
  const cv = $("plot"), tip = $("tip");
  if (!hoverPts.length) { tip.style.display = "none"; return; }
  const rect = cv.getBoundingClientRect();
  const scaleX = cv.width / rect.width, scaleY = cv.height / rect.height;
  const mx = (evt.clientX - rect.left) * scaleX;
  const my = (evt.clientY - rect.top) * scaleY;
  let best = null, bestD = 14 * 14;
  for (const p of hoverPts) {
    const d = (p.px - mx) ** 2 + (p.py - my) ** 2;
    if (d < bestD) { bestD = d; best = p; }
  }
  if (!best) { tip.style.display = "none"; return; }
  tip.textContent = best.text;
  tip.style.display = "block";
  // Position in CSS pixels (offset from the canvas within the wrap), flipping
  // left/up near the right/bottom edges so the tip stays on the canvas.
  let x = cv.offsetLeft + best.px / scaleX + 12;
  let y = cv.offsetTop + best.py / scaleY + 12;
  if (x + tip.offsetWidth > cv.offsetLeft + rect.width) x -= tip.offsetWidth + 24;
  if (y + tip.offsetHeight > cv.offsetTop + rect.height) y -= tip.offsetHeight + 24;
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}

$("plot").addEventListener("mousemove", showTip);
$("plot").addEventListener("mouseleave", () => { $("tip").style.display = "none"; });
$("refresh").onclick = refreshPorts;
$("run").onclick = run;
$("cancel").onclick = cancel;
for (const id of ["name", "author", "desc", "func"])
  $(id).addEventListener("input", schedulePreview);
$("tags").addEventListener("change", schedulePreview);
$("addtag").onclick = addTag;
$("newtag").addEventListener("keydown", e => { if (e.key === "Enter") addTag(); });
for (const id of ["column", "xscale", "yscale"])
  $(id).addEventListener("change", drawPlot);
$("dataset").onchange = selectDatasets;
// Changing the tag filter re-renders the (filtered) dataset list, then redraws
// so any datasets hidden by the filter drop out of the overlay.
for (const id of ["tagfilter", "tagmatch"])
  $(id).addEventListener("change", () => { renderDatasets(); selectDatasets(); });
$("scan").onclick = async () => { await refreshDatasets(true); await selectDatasets(); };
$("folder").addEventListener("keydown", e => { if (e.key === "Enter") $("scan").click(); });

refreshPorts();
refreshTags();
refreshPreview();
refreshDatasets().then(selectDatasets);
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
    html = (
        PAGE.replace("__BAUDS__", bauds)
            .replace("__FUNCS__", funcs)
            .replace("__START__", f"{lcr.SWEEP_START_HZ:g}")
            .replace("__STOP__", f"{lcr.SWEEP_STOP_HZ:g}")
            .replace("__POINTS__", str(lcr.SWEEP_POINTS))
    )
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
        elif path == "/api/tags":
            self._json({"groups": lcr.load_tag_groups()})
        elif path == "/api/plot":
            with STATE.lock:
                plot = STATE.plot
            self._json({"plot": plot})
        elif path == "/api/datasets":
            folder = (parse_qs(urlparse(self.path).query).get("dir") or [""])[0]
            with STATE.lock:
                has_current = STATE.plot is not None
            base = dataset_dir(folder)
            by_groups = datasets_with_tag_groups(folder)
            # Flat tags per dataset for the filter match + the per-row "(...)" note.
            tags_by_dataset = {
                n: [t for ts in g.values() for t in ts] for n, g in by_groups.items()
            }
            # Filter checklist grouped by the YAML's sections; tags not in the
            # YAML land in the "other" section.
            all_tag_groups = filter_tag_groups(folder)
            self._json({
                "datasets": list(by_groups),
                "tags_by_dataset": tags_by_dataset,
                "all_tag_groups": all_tag_groups,
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
                body.get("tags") or [],
            )
            self._send(json.dumps(preview, indent=2).encode("utf-8"))
        elif path == "/api/start":
            self._json(start_sweep(self._read_body()))
        elif path == "/api/tags":
            body = self._read_body()
            section = str(body.get("section", "") or lcr.TAG_SECTIONS[0])
            self._json({"groups": lcr.add_tag(str(body.get("tag", "")), section)})
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
