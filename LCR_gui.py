"""
LCR_gui.py -- A small Tkinter front end for LCR_logging.py.

Lets you pre-fill the run's metadata (filename, author, description) and pick
the port / baud / measurement function up front, then run a frequency sweep and
save the .txt/.csv/.json files -- without the interactive console prompts the
CLI uses. The JSON preview pane shows exactly what will be written to the
sidecar before you commit.

WHY TKINTER:
    It ships with CPython, so there is nothing to pip install -- which matters
    on this machine, where corporate SSL inspection makes pip painful and there
    are no admin rights. See HANDOFF.md "Environment quirks".

ALL THE INSTRUMENT LOGIC LIVES IN LCR_logging.py. This file only builds the
window and drives those functions; it adds no new SCPI behaviour. The sweep
runs on a background thread so the window stays responsive, and the worker
talks back to the UI through a queue polled on the Tk main loop.

RUN:
    python LCR_gui.py

Same hardware setup as the CLI applies: meter on USBCDC (or RS-232C), back on
its live measurement screen, baud matching the front panel. See README.md.
"""

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import LCR_logging as lcr

# Standard baud rates the meter supports (manual p.7 / front-panel options).
BAUD_CHOICES = ["9600", "19200", "28800", "38400", "48000", "57600", "115200"]

# Combobox sentinel meaning "don't send FUNC:IMP; keep the meter's current mode".
FUNC_KEEP = "(leave as-is)"

# How often (ms) the Tk main loop drains the worker->UI message queue.
POLL_MS = 100


class LcrGui:
    """The application window and its worker-thread plumbing."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("LCR Logger")
        root.minsize(560, 560)

        # worker -> UI messages; drained by _poll on the main thread.
        self.queue: queue.Queue = queue.Queue()
        # Set by Cancel to ask collect_sweep to stop early.
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        # display string -> device name, for the port combobox.
        self.port_map: dict[str, str] = {}

        # Form field variables.
        self.var_port = tk.StringVar()
        self.var_baud = tk.StringVar(value=str(lcr.DEFAULT_BAUD))
        self.var_func = tk.StringVar(value=FUNC_KEEP)
        self.var_name = tk.StringVar()
        self.var_author = tk.StringVar()
        self.var_desc = tk.StringVar()

        self._build_form()
        self._build_controls()
        self._build_output()

        # Keep the JSON preview in sync with the metadata fields.
        for var in (self.var_name, self.var_author, self.var_desc, self.var_func):
            var.trace_add("write", lambda *_: self._refresh_preview())

        self.refresh_ports()
        self._refresh_preview()
        self.root.after(POLL_MS, self._poll)

    # ── UI construction ────────────────────────────────────────────────────

    def _build_form(self) -> None:
        frm = ttk.LabelFrame(self.root, text="Run setup")
        frm.pack(fill="x", padx=10, pady=(10, 6))
        frm.columnconfigure(1, weight=1)

        # Port + Refresh.
        ttk.Label(frm, text="Port").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.cmb_port = ttk.Combobox(frm, textvariable=self.var_port, state="readonly")
        self.cmb_port.grid(row=0, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(frm, text="Refresh", command=self.refresh_ports).grid(
            row=0, column=2, padx=6, pady=4
        )

        # Baud (editable, with common presets).
        ttk.Label(frm, text="Baud").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Combobox(
            frm, textvariable=self.var_baud, values=BAUD_CHOICES, width=12
        ).grid(row=1, column=1, sticky="w", padx=6, pady=4)

        # Measurement function (optional; sets FUNC:IMP for determinism).
        ttk.Label(frm, text="Function").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        ttk.Combobox(
            frm,
            textvariable=self.var_func,
            values=[FUNC_KEEP] + sorted(lcr.IMP_FUNCTIONS),
            state="readonly",
            width=12,
        ).grid(row=2, column=1, sticky="w", padx=6, pady=4)

        # Metadata fields.
        ttk.Label(frm, text="Filename").grid(row=3, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(frm, textvariable=self.var_name).grid(
            row=3, column=1, columnspan=2, sticky="ew", padx=6, pady=4
        )
        ttk.Label(frm, text="Author").grid(row=4, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(frm, textvariable=self.var_author).grid(
            row=4, column=1, columnspan=2, sticky="ew", padx=6, pady=4
        )
        ttk.Label(frm, text="Description").grid(row=5, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(frm, textvariable=self.var_desc).grid(
            row=5, column=1, columnspan=2, sticky="ew", padx=6, pady=4
        )

        # Live preview of the JSON sidecar that will be written.
        prev = ttk.LabelFrame(self.root, text="JSON metadata preview")
        prev.pack(fill="x", padx=10, pady=6)
        self.txt_preview = tk.Text(prev, height=7, wrap="none", state="disabled")
        self.txt_preview.pack(fill="x", padx=6, pady=6)

    def _build_controls(self) -> None:
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=10, pady=4)
        self.btn_run = ttk.Button(bar, text="Run sweep & save", command=self.start_sweep)
        self.btn_run.pack(side="left")
        self.btn_cancel = ttk.Button(
            bar, text="Cancel", command=self.cancel_sweep, state="disabled"
        )
        self.btn_cancel.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(bar, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=6)

    def _build_output(self) -> None:
        out = ttk.LabelFrame(self.root, text="Progress")
        out.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        self.txt_log = tk.Text(out, height=10, wrap="word", state="disabled")
        scroll = ttk.Scrollbar(out, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.txt_log.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def refresh_ports(self) -> None:
        """Repopulate the port dropdown from get_serial_ports()."""
        self.port_map = {
            f"{dev}  --  {desc}": dev for dev, desc, _hwid in lcr.get_serial_ports()
        }
        displays = list(self.port_map)
        self.cmb_port["values"] = displays
        if displays:
            # Keep the current selection if it still exists, else pick the first.
            if self.var_port.get() not in displays:
                self.var_port.set(displays[0])
        else:
            self.var_port.set("")
        self._log(f"Found {len(displays)} connected serial device(s).")

    def _selected_port(self) -> str:
        """The device name (e.g. COM3) for the current dropdown selection."""
        return self.port_map.get(self.var_port.get(), "")

    def _measurement_label(self) -> str:
        """Measurement string for the preview, given the function selection."""
        code = self.var_func.get()
        if code in lcr.IMP_FUNCTIONS:
            primary, secondary = lcr.IMP_FUNCTIONS[code]
            return f"{primary}-{secondary}"
        return "(read from meter at run time)"

    def _refresh_preview(self) -> None:
        """Rebuild the JSON preview from the current field values."""
        name = self.var_name.get().strip()
        stem = Path(name).with_suffix("") if name else Path("<filename>")
        csv_path = lcr.DATA_DIR / stem.with_suffix(".csv")
        preview = {
            "csv_file": str(csv_path.resolve()),
            "test_time": "(set when the sweep runs)",
            "author": self.var_author.get().strip(),
            "description": self.var_desc.get().strip(),
            "measurement": self._measurement_label(),
        }
        text = json.dumps(preview, indent=2)
        self.txt_preview.configure(state="normal")
        self.txt_preview.delete("1.0", "end")
        self.txt_preview.insert("1.0", text)
        self.txt_preview.configure(state="disabled")

    def _log(self, line: str) -> None:
        """Append a line to the progress pane (main thread only)."""
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", line + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        self.btn_run.configure(state="disabled" if running else "normal")
        self.btn_cancel.configure(state="normal" if running else "disabled")

    # ── Run / cancel ───────────────────────────────────────────────────────

    def start_sweep(self) -> None:
        """Validate the form and kick off the sweep on a worker thread."""
        if self.worker is not None and self.worker.is_alive():
            return  # already running

        port = self._selected_port()
        if not port:
            messagebox.showwarning("No port", "Select a serial port first (Refresh to rescan).")
            return
        name = self.var_name.get().strip()
        if not name:
            messagebox.showwarning("No filename", "Enter a filename for the saved results.")
            return
        try:
            baud = int(self.var_baud.get())
        except ValueError:
            messagebox.showwarning("Bad baud", f"Baud must be a number, got {self.var_baud.get()!r}.")
            return

        func = self.var_func.get()
        params = {
            "port": port,
            "baud": baud,
            "func": func if func in lcr.IMP_FUNCTIONS else None,
            "name": name,
            "author": self.var_author.get().strip(),
            "description": self.var_desc.get().strip(),
        }

        self.stop_event.clear()
        self.progress.configure(value=0)
        self._set_running(True)
        self._log("─" * 40)
        self.worker = threading.Thread(target=self._run_worker, args=(params,), daemon=True)
        self.worker.start()

    def cancel_sweep(self) -> None:
        """Ask the running sweep to stop after the current point."""
        if self.worker is not None and self.worker.is_alive():
            self.stop_event.set()
            self._log("Cancel requested -- stopping after the current point...")

    # ── Worker thread ────────────────────────────────────────────────────────

    def _run_worker(self, params: dict) -> None:
        """
        Runs off the main thread. MUST NOT touch Tk widgets directly -- it only
        pushes (kind, ...) tuples onto self.queue, which _poll drains on the
        main thread.
        """
        q = self.queue
        ser = None
        try:
            q.put(("log", f"Opening {params['port']} @ {params['baud']} baud..."))
            ser = lcr.open_instrument(params["port"], baud=params["baud"])
            q.put(("log", "Connected."))

            if params["func"]:
                lcr.set_measurement_function(ser, params["func"])
                q.put(("log", f"Measurement function set to {params['func']}."))

            def progress(i: int, total: int, freq: float, data: str) -> None:
                q.put(("progress", i, total, freq, data))

            rows, primary, secondary, test_time = lcr.collect_sweep(
                ser, progress=progress, should_stop=self.stop_event.is_set
            )

            if self.stop_event.is_set():
                q.put(("log", "Sweep cancelled -- nothing saved."))
                return

            paths = lcr.save_sweep(
                rows, params["name"], params["author"], params["description"],
                primary, secondary, test_time,
            )
            q.put(("saved", [str(p.resolve()) for p in paths]))
        except Exception as exc:  # surface any failure to the UI
            q.put(("error", str(exc)))
        finally:
            if ser is not None and ser.is_open:
                try:
                    lcr.return_to_local(ser)
                    ser.close()
                except Exception:
                    pass
            q.put(("done", None))

    # ── Queue polling (main thread) ────────────────────────────────────────

    def _poll(self) -> None:
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1])
                elif kind == "progress":
                    _, i, total, freq, data = msg
                    self.progress.configure(maximum=total, value=i)
                    self._log(f"  [{i}/{total}] {freq:10.2f} Hz  ->  {data}")
                elif kind == "saved":
                    for path in msg[1]:
                        self._log(f"Saved: {path}")
                    messagebox.showinfo("Saved", "Wrote:\n" + "\n".join(msg[1]))
                elif kind == "error":
                    self._log(f"ERROR: {msg[1]}")
                    messagebox.showerror("Error", msg[1])
                elif kind == "done":
                    self._set_running(False)
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll)


def main() -> None:
    root = tk.Tk()
    LcrGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
