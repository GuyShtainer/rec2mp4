# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""rec2mp4 desktop GUI — a stdlib-tkinter front-end over rec2mp4.pipeline.

    python -m rec2mp4.gui          (or the installed `rec2mp4-gui` script)

Zero dependencies beyond the Python that ships with python.org/conda
installers (tkinter is in the stdlib on macOS + Windows). The GUI is a
thin queue + settings form around the exact same engine the CLI uses:

  * Add .rec files / folders; every record is validated + summarized with
    rec.parse **without any emulator** — invalid records are flagged in
    the queue immediately.
  * The settings pane mirrors pipeline.ConvertSettings 1:1 (animations,
    text speed, scale, audio, side panel + sections, naming, sidecar,
    ROM/save/output paths).
  * Convert runs the batch on a worker thread; the tkinter widgets are
    only ever touched from the main thread — the worker posts messages
    to a queue.Queue that the UI drains via root.after() polling.
    Cancel finishes the current record, then stops.

Everything above the widgets (QueueModel, settings_from_form,
format_result_status, ...) is pure logic with no tkinter import so
tests/test_gui.py can exercise it on headless CI runners.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import rec
from .pipeline import (
    DEFAULT_OUTDIR, DEFAULT_ROM, DEFAULT_SAV,
    ConvertSettings, PipelineError, convert_one, load_context,
)
from .panel import PANEL_SECTIONS

# tkinter is stdlib but *can* be absent (some minimal Linux pythons).
# Import errors must not break `import rec2mp4.gui` for the pure-logic
# tests; main() reports the problem instead.
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:                                   # pragma: no cover
    tk = None


# ---------------------------------------------------------------------------
# Pure logic — no tkinter below this line until the "widgets" section
# ---------------------------------------------------------------------------

# Queue item statuses (the per-row "status" column).
ST_WAITING = "waiting"
ST_CONVERTING = "converting"
ST_OK = "OK"
ST_TRUNC = "TRUNC"
ST_FAILED = "FAILED"
ST_INVALID = "INVALID"

SCALE_MIN, SCALE_MAX = 1, 10


@dataclass
class QueueItem:
    """One queued .rec: its parse (done at add time, no emulator) + state."""
    path: Path
    info: dict | None = None          # rec.parse() result, None on read error
    read_error: str | None = None
    status: str = ST_WAITING
    detail: str = ""
    log_lines: list = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return bool(self.info and self.info.get("valid"))


def load_item(path) -> QueueItem:
    """Read + parse one .rec into a QueueItem (never raises)."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        return QueueItem(path=p, read_error=str(exc),
                         status=ST_FAILED, detail=f"read error: {exc}")
    info = rec.parse(data)
    item = QueueItem(path=p, info=info)
    if not info.get("valid"):
        errs = info.get("errors") or ["invalid record"]
        item.status = ST_INVALID
        item.detail = errs[0]
    return item


def item_kind(info: dict | None) -> str:
    """Battle kind column: multi / two-opponents / double / link / single."""
    if info is None:
        return "?"
    for token, key in (("multi", "is_multi"),
                       ("two-opponents", "is_two_opponents"),
                       ("double", "is_double"),
                       ("link", "is_link_recorded")):
        if info.get(key):
            return token
    return "single"


def item_opponent(info: dict | None) -> str:
    """Opponent column, straight from the parse (no ROM needed)."""
    if info is None:
        return "?"
    opp = info.get("opponent_a_name") or f"#{info.get('opponent_a', 0)}"
    if info.get("opponent_b_name"):
        opp += f" + {info['opponent_b_name']}"
    return opp


def item_row(item: QueueItem) -> tuple:
    """Tree row values: (file, facility, level, kind, opponent, valid,
    status, detail)."""
    info = item.info
    if item.read_error is not None:
        return (item.path.name, "?", "?", "?", "?", "unreadable",
                item.status, item.detail)
    return (item.path.name,
            info.get("facility", "?"),
            info.get("level_mode", "?"),
            item_kind(info),
            item_opponent(info),
            "ok" if item.valid else "INVALID",
            item.status,
            item.detail)


class QueueModel:
    """The conversion queue: ordered, deduplicated by resolved path."""

    def __init__(self):
        self.items: list[QueueItem] = []
        self._seen: set[str] = set()

    @staticmethod
    def _key(path) -> str:
        try:
            rp = Path(path).resolve()
        except OSError:
            rp = Path(path).absolute()
        return str(rp).lower()

    def add(self, paths) -> tuple[int, int]:
        """Add files; returns (added, duplicates_skipped)."""
        added = dupes = 0
        for p in paths:
            key = self._key(p)
            if key in self._seen:
                dupes += 1
                continue
            self._seen.add(key)
            self.items.append(load_item(p))
            added += 1
        return added, dupes

    def add_folder(self, folder) -> tuple[int, int]:
        """Add every *.rec directly inside folder (sorted, like the CLI)."""
        return self.add(sorted(Path(folder).glob("*.rec")))

    def remove(self, indices) -> int:
        """Remove items by index; returns how many were removed."""
        removed = 0
        for i in sorted(set(indices), reverse=True):
            if 0 <= i < len(self.items):
                self._seen.discard(self._key(self.items[i].path))
                del self.items[i]
                removed += 1
        return removed

    def clear(self) -> None:
        self.items.clear()
        self._seen.clear()

    def convertible_indices(self) -> list[int]:
        """Indices worth sending to the pipeline (readable records;
        convert_one re-validates and reports INVALID ones itself)."""
        return [i for i, it in enumerate(self.items)
                if it.read_error is None]


def default_form() -> dict:
    """The settings form's initial plain-value state.

    Mirrors the CLI defaults; ROM/save fields are prefilled only when the
    repo-default files actually exist (otherwise left empty for the user
    to browse). Empty path fields mean "use the pipeline default".
    """
    return {
        "rom": str(DEFAULT_ROM) if DEFAULT_ROM.is_file() else "",
        "sav": str(DEFAULT_SAV) if DEFAULT_SAV.is_file() else "",
        "outdir": str(DEFAULT_OUTDIR),
        "scale": 4,
        "audio": True,
        "anims": "on",
        "text_speed": "record",
        "plain_names": False,
        "sidecar": True,
        "panel": "right",
        "panel_sections": list(PANEL_SECTIONS),
    }


def settings_from_form(form: dict) -> ConvertSettings:
    """Marshal the plain form dict into a ConvertSettings.

    Raises ValueError on user-fixable input problems (bad scale). An empty
    panel-section selection with the panel on degrades to panel="off"
    (parse_panel_info would silently mean "all", which is not what an
    all-unchecked form says).
    """
    try:
        scale = int(form.get("scale", 4))
    except (TypeError, ValueError):
        raise ValueError(f"scale must be a whole number "
                         f"(got {form.get('scale')!r})") from None
    if not SCALE_MIN <= scale <= SCALE_MAX:
        raise ValueError(f"scale must be {SCALE_MIN}..{SCALE_MAX} "
                         f"(got {scale})")

    panel = form.get("panel", "right")
    sections = [s for s in form.get("panel_sections", [])
                if s in PANEL_SECTIONS]
    if panel in ("right", "left") and not sections:
        panel = "off"
    panel_info = ("all" if set(sections) == set(PANEL_SECTIONS)
                  else ",".join(sections)) or "all"

    return ConvertSettings(
        rom=form.get("rom") or None,
        sav=form.get("sav") or None,
        outdir=form.get("outdir") or None,
        scale=scale,
        audio=bool(form.get("audio", True)),
        anims=form.get("anims", "on"),
        text_speed=form.get("text_speed", "record"),
        plain_names=bool(form.get("plain_names", False)),
        sidecar=bool(form.get("sidecar", True)),
        panel=panel,
        panel_info=panel_info,
    )


def format_result_status(result: dict) -> tuple[str, str]:
    """(status, short detail) for a convert_one() result row."""
    status = result.get("status", ST_FAILED)
    if status == "OK" or status == "TRUNC":
        out = result.get("output")
        detail = (f"{result.get('frames', 0)}f "
                  f"{result.get('seconds', 0.0):.1f}s")
        if status == "TRUNC":
            detail += f" ({result.get('end_reason', 'truncated')}, partial)"
        if out:
            detail += f" -> {Path(out).name}"
        return status, detail
    # INVALID / FAILED: the pipeline's one-line detail says it best.
    return status, str(result.get("detail") or result.get("error") or "")


def open_in_file_manager(path) -> None:
    """Open a folder in Finder / Explorer / the Linux file manager."""
    p = str(path)
    if sys.platform == "darwin":
        subprocess.Popen(["open", p])
    elif os.name == "nt":                               # pragma: no cover
        os.startfile(p)                     # noqa  (Windows-only attribute)
    else:                                               # pragma: no cover
        subprocess.Popen(["xdg-open", p])


# ---------------------------------------------------------------------------
# Widgets — everything below needs a display
# ---------------------------------------------------------------------------

_COLUMNS = (("file", 250), ("facility", 105), ("level", 75), ("kind", 95),
            ("opponent", 170), ("valid", 65), ("status", 85),
            ("detail", 320))


class GuiApp:
    """Main window. All widget access happens on the tkinter main thread;
    the conversion worker communicates via self._msgq only."""

    POLL_MS = 100

    def __init__(self, root):
        self.root = root
        root.title("rec2mp4 — Battle Record to MP4")
        root.minsize(900, 560)

        self.model = QueueModel()
        self._msgq: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()

        self._build_queue_pane()
        self._build_settings_pane()
        self._build_action_bar()
        self._poll()

    # ---- layout ---------------------------------------------------------

    def _build_queue_pane(self):
        top = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        top.pack(side="top", fill="both", expand=True)

        bar = ttk.Frame(top)
        bar.pack(side="top", fill="x", pady=(0, 4))
        ttk.Button(bar, text="Add .rec files…",
                   command=self._on_add_files).pack(side="left")
        ttk.Button(bar, text="Add folder…",
                   command=self._on_add_folder).pack(side="left", padx=4)
        ttk.Button(bar, text="Remove selected",
                   command=self._on_remove).pack(side="left")
        ttk.Button(bar, text="Clear",
                   command=self._on_clear).pack(side="left", padx=4)
        self.queue_label = ttk.Label(bar, text="0 record(s) queued")
        self.queue_label.pack(side="right")

        cols = [c for c, _ in _COLUMNS]
        self.tree = ttk.Treeview(top, columns=cols, show="headings",
                                 selectmode="extended", height=12)
        for name, width in _COLUMNS:
            self.tree.heading(name, text=name)
            self.tree.column(name, width=width, stretch=(name == "detail"))
        ysb = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", self._on_details)
        self.tree.tag_configure("invalid", foreground="#b00020")
        self.tree.tag_configure("ok", foreground="#1a7f37")
        self.tree.tag_configure("trunc", foreground="#b26a00")

    def _build_settings_pane(self):
        f = ttk.LabelFrame(self.root, text="Settings", padding=8)
        f.pack(side="top", fill="x", padx=8, pady=4)
        d = default_form()

        self.var_anims = tk.StringVar(value=d["anims"])
        self.var_speed = tk.StringVar(value=d["text_speed"])
        self.var_scale = tk.IntVar(value=d["scale"])
        self.var_audio = tk.BooleanVar(value=d["audio"])
        self.var_panel = tk.StringVar(value=d["panel"])
        self.var_plain = tk.BooleanVar(value=d["plain_names"])
        self.var_sidecar = tk.BooleanVar(value=d["sidecar"])
        self.var_rom = tk.StringVar(value=d["rom"])
        self.var_sav = tk.StringVar(value=d["sav"])
        self.var_outdir = tk.StringVar(value=d["outdir"])
        self.var_sections = {s: tk.BooleanVar(value=True)
                             for s in PANEL_SECTIONS}

        row1 = ttk.Frame(f)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="Animations:").pack(side="left")
        ttk.Combobox(row1, textvariable=self.var_anims, state="readonly",
                     values=("on", "off", "record"), width=7
                     ).pack(side="left", padx=(2, 10))
        ttk.Label(row1, text="Text speed:").pack(side="left")
        ttk.Combobox(row1, textvariable=self.var_speed, state="readonly",
                     values=("slow", "mid", "fast", "record"), width=7
                     ).pack(side="left", padx=(2, 10))
        ttk.Label(row1, text="Scale:").pack(side="left")
        ttk.Spinbox(row1, textvariable=self.var_scale, from_=SCALE_MIN,
                    to=SCALE_MAX, width=3).pack(side="left", padx=(2, 10))
        ttk.Checkbutton(row1, text="Audio", variable=self.var_audio
                        ).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(row1, text="Rich names off (plain)",
                        variable=self.var_plain).pack(side="left",
                                                      padx=(0, 10))
        ttk.Checkbutton(row1, text="JSON sidecar",
                        variable=self.var_sidecar).pack(side="left")

        row2 = ttk.Frame(f)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="Side panel:").pack(side="left")
        ttk.Combobox(row2, textvariable=self.var_panel, state="readonly",
                     values=("right", "left", "off"), width=6
                     ).pack(side="left", padx=(2, 10))
        ttk.Label(row2, text="Sections:").pack(side="left")
        for s in PANEL_SECTIONS:
            ttk.Checkbutton(row2, text=s, variable=self.var_sections[s]
                            ).pack(side="left", padx=(0, 4))

        for label, var, patt in (
                ("ROM:", self.var_rom, [("GBA ROM", "*.gba"),
                                        ("All files", "*")]),
                ("Save:", self.var_sav, [("GBA save", "*.sav"),
                                         ("All files", "*")])):
            row = ttk.Frame(f)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, width=8).pack(side="left")
            ttk.Entry(row, textvariable=var).pack(side="left", fill="x",
                                                  expand=True, padx=2)
            ttk.Button(row, text="Browse…",
                       command=lambda v=var, p=patt: self._pick_file(v, p)
                       ).pack(side="left")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Output:", width=8).pack(side="left")
        ttk.Entry(row, textvariable=self.var_outdir).pack(
            side="left", fill="x", expand=True, padx=2)
        ttk.Button(row, text="Browse…",
                   command=self._pick_outdir).pack(side="left")

    def _build_action_bar(self):
        bar = ttk.Frame(self.root, padding=(8, 4, 8, 8))
        bar.pack(side="bottom", fill="x")
        self.btn_convert = ttk.Button(bar, text="Convert",
                                      command=self._on_convert)
        self.btn_convert.pack(side="left")
        self.btn_cancel = ttk.Button(bar, text="Cancel",
                                     command=self._on_cancel,
                                     state="disabled")
        self.btn_cancel.pack(side="left", padx=4)
        ttk.Button(bar, text="Open output folder",
                   command=self._on_open_out).pack(side="left")
        self.progress = ttk.Label(bar, text="idle", anchor="w")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

    # ---- form <-> settings ----------------------------------------------

    def read_form(self) -> dict:
        return {
            "rom": self.var_rom.get().strip(),
            "sav": self.var_sav.get().strip(),
            "outdir": self.var_outdir.get().strip(),
            "scale": self.var_scale.get(),
            "audio": self.var_audio.get(),
            "anims": self.var_anims.get(),
            "text_speed": self.var_speed.get(),
            "plain_names": self.var_plain.get(),
            "sidecar": self.var_sidecar.get(),
            "panel": self.var_panel.get(),
            "panel_sections": [s for s in PANEL_SECTIONS
                               if self.var_sections[s].get()],
        }

    def read_settings(self) -> ConvertSettings:
        """Current form -> ConvertSettings (raises ValueError on bad input)."""
        return settings_from_form(self.read_form())

    # ---- queue actions ----------------------------------------------------

    def _on_add_files(self):
        paths = filedialog.askopenfilenames(
            title="Add battle records",
            filetypes=[("Battle records", "*.rec"), ("All files", "*")])
        if paths:
            self._report_add(self.model.add(paths))

    def _on_add_folder(self):
        folder = filedialog.askdirectory(title="Add every *.rec in a folder")
        if folder:
            self._report_add(self.model.add_folder(folder))

    def _report_add(self, added_dupes):
        added, dupes = added_dupes
        self._refresh_tree()
        msg = f"added {added} record(s)"
        if dupes:
            msg += f", skipped {dupes} duplicate(s)"
        self.progress.configure(text=msg)

    def _on_remove(self):
        # Also refuse while worker messages are still queued: the thread may
        # have just exited with ('result', idx)/('done') pending, and those
        # indices refer to the pre-remove item list.
        if self._running() or not self._msgq.empty():
            return
        idxs = [self.tree.index(iid) for iid in self.tree.selection()]
        if idxs:
            self.model.remove(idxs)
            self._refresh_tree()

    def _on_clear(self):
        if self._running() or not self._msgq.empty():
            return
        self.model.clear()
        self._refresh_tree()

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for item in self.model.items:
            tags = ()
            if item.status == ST_INVALID or item.read_error:
                tags = ("invalid",)
            elif item.status == ST_OK:
                tags = ("ok",)
            elif item.status in (ST_TRUNC, ST_FAILED):
                tags = ("trunc",) if item.status == ST_TRUNC else ("invalid",)
            self.tree.insert("", "end", values=item_row(item), tags=tags)
        self.queue_label.configure(
            text=f"{len(self.model.items)} record(s) queued")

    def _update_row(self, idx: int):
        iids = self.tree.get_children()
        if 0 <= idx < len(iids):
            item = self.model.items[idx]
            self.tree.item(iids[idx], values=item_row(item))

    # ---- pickers -----------------------------------------------------------

    def _pick_file(self, var, filetypes):
        path = filedialog.askopenfilename(filetypes=filetypes)
        if path:
            var.set(path)

    def _pick_outdir(self):
        path = filedialog.askdirectory(title="Output folder")
        if path:
            self.var_outdir.set(path)

    def _on_open_out(self):
        out = Path(self.var_outdir.get().strip() or DEFAULT_OUTDIR)
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("rec2mp4", f"cannot open {out}: {exc}")
            return
        open_in_file_manager(out)

    # ---- conversion ---------------------------------------------------------

    def _running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _on_convert(self):
        if self._running():
            return
        jobs = self.model.convertible_indices()
        if not jobs:
            messagebox.showinfo("rec2mp4", "Queue is empty — add .rec "
                                "files first.")
            return
        try:
            settings = self.read_settings()
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("rec2mp4", f"Bad settings: {exc}")
            return
        for i in jobs:
            self.model.items[i].status = ST_WAITING
            self.model.items[i].detail = ""
            self.model.items[i].log_lines.clear()
        self._refresh_tree()
        self._cancel.clear()
        self.btn_convert.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.progress.configure(text="starting…")
        paths = [(i, self.model.items[i].path) for i in jobs]
        self._worker = threading.Thread(
            target=self._worker_main, args=(paths, settings), daemon=True)
        self._worker.start()

    def _on_cancel(self):
        if self._running():
            self._cancel.set()
            self.progress.configure(
                text="cancelling — finishing the current record…")

    def _worker_main(self, jobs, settings):
        """Worker thread: NEVER touches widgets — messages only."""
        put = self._msgq.put
        try:
            ctx = load_context(settings,
                               log=lambda m: put(("blog", str(m))),
                               err=lambda m: put(("blog", f"! {m}")))
        except PipelineError as exc:
            put(("fatal", str(exc)))
            return
        done = 0
        try:
            for idx, path in jobs:
                if self._cancel.is_set():
                    break
                put(("status", idx, ST_CONVERTING, ""))
                try:
                    res = convert_one(
                        path, settings, ctx=ctx,
                        log=lambda m, i=idx: put(("log", i, str(m))),
                        err=lambda m, i=idx: put(("log", i, f"! {m}")),
                        progress_cb=lambda p, i=idx: put(("progress", i, p)))
                except Exception as exc:   # convert_one shouldn't raise, but
                    res = {"status": "FAILED",   # a frozen GUI is worse
                           "detail": f"{type(exc).__name__}: {exc}"}
                put(("result", idx, res))
                done += 1
        finally:
            # Always posted, even if the loop itself blows up — 'done' is
            # what re-enables the Convert button.
            put(("done", done, len(jobs), self._cancel.is_set()))

    # ---- main-thread message pump ------------------------------------------

    def _poll(self):
        """Drain the worker queue. Must be unkillable: the after() reschedule
        runs no matter what a message handler does, and a bad message is
        logged + dropped instead of killing the pump for good."""
        try:
            while True:
                try:
                    msg = self._msgq.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(msg)
                except Exception:
                    traceback.print_exc()
        finally:
            self.root.after(self.POLL_MS, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "status":
            _, idx, status, detail = msg
            item = self.model.items[idx]
            item.status, item.detail = status, detail
            self._update_row(idx)
            if status == ST_CONVERTING:
                self.progress.configure(
                    text=f"converting {item.path.name}…")
        elif kind == "log":
            _, idx, text = msg
            self.model.items[idx].log_lines.append(text)
            first = text.strip().splitlines()[0] if text.strip() else ""
            if first:
                self.progress.configure(
                    text=f"{self.model.items[idx].path.name}: {first}")
        elif kind == "blog":                       # batch-level log line
            self.progress.configure(text=str(msg[1]).splitlines()[0])
        elif kind == "progress":
            _, idx, p = msg
            self.progress.configure(
                text=f"{self.model.items[idx].path.name}: replay "
                     f"{p.get('frames', 0)} frames "
                     f"({p.get('seconds', 0.0):.1f}s captured)")
        elif kind == "result":
            _, idx, res = msg
            item = self.model.items[idx]
            item.status, item.detail = format_result_status(res)
            self._update_row(idx)
        elif kind == "fatal":
            self._finish(f"error: {msg[1].splitlines()[0]}")
            messagebox.showerror("rec2mp4 — cannot convert", msg[1])
        elif kind == "done":
            _, done, total, cancelled = msg
            ok = sum(1 for it in self.model.items if it.status == ST_OK)
            text = f"{ok} OK of {done} converted"
            if cancelled and done < total:
                text += f" (cancelled, {total - done} left in queue)"
            self._finish(text)

    def _finish(self, text):
        self.btn_convert.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        self.progress.configure(text=text)

    # ---- details popup -------------------------------------------------------

    def _on_details(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        item = self.model.items[idx]
        win = tk.Toplevel(self.root)
        win.title(item.path.name)
        win.minsize(560, 360)
        txt = tk.Text(win, wrap="word")
        sb = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        parts = [f"{item.path}", ""]
        if item.read_error:
            parts.append(f"read error: {item.read_error}")
        elif item.info:
            parts.append(rec.summarize(item.info))
        if item.detail:
            parts += ["", f"status: {item.status} — {item.detail}"]
        if item.log_lines:
            parts += ["", "--- conversion log ---", *item.log_lines]
        txt.insert("1.0", "\n".join(parts))
        txt.configure(state="disabled")


def main(argv=None) -> int:                            # noqa: ARG001
    if tk is None:
        print("error: tkinter is not available in this Python — install "
              "the Tk support package for your Python (python.org and "
              "conda installers include it).", file=sys.stderr)
        return 2
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"error: cannot open a display ({exc})", file=sys.stderr)
        return 2
    GuiApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
