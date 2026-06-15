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

import json
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
        baud = int(params.get("baud"))
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

$("refresh").onclick = refreshPorts;
$("run").onclick = run;
$("cancel").onclick = cancel;
for (const id of ["name", "author", "desc", "func"])
  $(id).addEventListener("input", schedulePreview);

refreshPorts();
refreshPreview();
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
