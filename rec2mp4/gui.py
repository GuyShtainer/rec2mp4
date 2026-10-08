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

import io
import json
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
    CONDA_PYTHON, DEFAULT_OUTDIR, DEFAULT_ROM, DEFAULT_SAV, PANEL_SIDES,
    PREVIEW_COUNT, PREVIEW_SPACING_SECONDS,
    ConvertSettings, PipelineError, convert_batch, cpu_jobs, load_context,
    parse_export_stem, pillow_available, pillow_hint, preview_frames,
    resolve_jobs, stack_status,
)
from .panel import PANEL_SECTIONS, STAT_PAGE_SECTIONS

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
    # Own columns. The streak is knowable at ADD time (PokeDNA puts it in the
    # file name); the outcome only after the replay.
    streak: int | None = None
    outcome: str = ""
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
    parsed = parse_export_stem(p.stem)
    if parsed is not None:
        item.streak = parsed["streak"]
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
    """Tree row values, in _COLUMNS order."""
    info = item.info
    streak = "" if item.streak is None else str(item.streak)
    if item.read_error is not None:
        return (item.path.name, "?", "?", "?", "?", streak, item.outcome,
                "unreadable", item.status, item.detail)
    return (item.path.name,
            info.get("facility", "?"),
            info.get("level_mode", "?"),
            item_kind(info),
            item_opponent(info),
            streak,
            item.outcome,
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

    # Columns the queue can be sorted by, and how to read each one out of an
    # item. Kept next to the model so the widget layer stays dumb.
    SORT_KEYS = {
        "file": lambda it: it.path.name.lower(),
        "facility": lambda it: (it.info or {}).get("facility", ""),
        "level": lambda it: (it.info or {}).get("level_mode", ""),
        "kind": lambda it: item_kind(it.info),
        "opponent": lambda it: item_opponent(it.info).lower(),
        "streak": lambda it: (it.streak if it.streak is not None else -1),
        "outcome": lambda it: it.outcome,
        "valid": lambda it: ("unreadable" if it.read_error
                             else ("ok" if it.valid else "INVALID")),
        "status": lambda it: it.status,
        "detail": lambda it: it.detail.lower(),
    }

    def sort(self, column: str, descending: bool = False) -> bool:
        """Sort in place by a column; False if the column is not sortable.

        Mixed types never meet: each key returns one type for every row, so a
        blank streak sorts as -1 rather than blowing up against an int.
        """
        key = self.SORT_KEYS.get(column)
        if key is None:
            return False
        self.items.sort(key=key, reverse=bool(descending))
        return True

    def convertible_indices(self) -> list[int]:
        """Indices worth sending to the pipeline (readable records;
        convert_one re-validates and reports INVALID ones itself)."""
        return [i for i, it in enumerate(self.items)
                if it.read_error is None]


def settings_path() -> Path:
    """Where the GUI remembers your last settings.

    Per-user and outside the repo, so it survives a `git clean` and a move of
    the working copy: %APPDATA%\\rec2mp4 on Windows, ~/.config/rec2mp4 else.
    """
    if os.name == "nt":                                # pragma: no cover
        base = Path(os.environ.get("APPDATA")
                    or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME")
                    or Path.home() / ".config")
    return base / "rec2mp4" / "gui-settings.json"


def load_form(path=None) -> dict:
    """default_form() with whatever was remembered from last time on top.

    Every remembered value is checked against the default's type and the key
    must already exist, so an old or hand-edited file can never introduce a
    surprise setting or wedge the GUI. Missing/corrupt file = the defaults.
    """
    form = default_form()
    path = Path(path) if path else settings_path()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return form
    if not isinstance(saved, dict):
        return form
    for key, default in form.items():
        if key not in saved:
            continue
        value = saved[key]
        if isinstance(default, bool):
            if isinstance(value, bool):
                form[key] = value
        elif isinstance(default, list):
            if isinstance(value, list):
                form[key] = [v for v in value if isinstance(v, str)]
        elif isinstance(default, (int, float)):
            if isinstance(value, (int, float, str)):
                form[key] = value
        elif isinstance(value, str):
            form[key] = value
    return form


def save_form(form: dict, path=None):
    """Remember the current settings; never raises (it is a convenience)."""
    path = Path(path) if path else settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        keep = {k: v for k, v in form.items() if k in default_form()}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(keep, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path
    except (OSError, TypeError, ValueError):
        return None


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
        "panel_cycle": 0.0,
        "panel_cycle_pages": list(STAT_PAGE_SECTIONS),
        "pov": "player",
        "layout": "",
        # Multiprocessing is ON by default: one record per CPU. jobs 0 =
        # auto; the checkbox is what users actually toggle.
        "parallel": True,
        "jobs": 0,
        "end_card": 3.0,
        "intro_card": 3.0,
        "facility_folders": True,
        "outcome_in_name": True,
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

    pov = form.get("pov", "player")
    if pov not in ("player", "opponent"):
        pov = "player"

    # Stat-cycling: "cycle every N seconds" (0/blank = static panel) + which
    # of moves/evs/ivs to rotate. Kept simple + validated so a bad number
    # surfaces as a ValueError the GUI shows in a dialog (like scale).
    raw_cycle = form.get("panel_cycle", 0.0)
    try:
        panel_cycle = float(raw_cycle or 0.0)
    except (TypeError, ValueError):
        raise ValueError(f"cycle seconds must be a number "
                         f"(got {raw_cycle!r})") from None
    if panel_cycle < 0:
        raise ValueError(f"cycle seconds must be >= 0 (got {panel_cycle})")
    cycle_pages = tuple(s for s in STAT_PAGE_SECTIONS
                        if s in (form.get("panel_cycle_pages") or ()))
    if not cycle_pages:
        cycle_pages = STAT_PAGE_SECTIONS

    # Parallelism: the checkbox decides on/off, the spinbox how many (0 =
    # auto = one worker per CPU). jobs=1 is the classic sequential loop.
    raw_jobs = form.get("jobs", 0)
    try:
        jobs = int(raw_jobs or 0)
    except (TypeError, ValueError):
        raise ValueError(f"parallel jobs must be a whole number "
                         f"(got {raw_jobs!r})") from None
    if jobs < 0:
        raise ValueError(f"parallel jobs must be >= 0 (got {jobs})")
    if not form.get("parallel", True):
        jobs = 1

    raw_card = form.get("end_card", 3.0)
    try:
        end_card = float(raw_card or 0.0)
    except (TypeError, ValueError):
        raise ValueError(f"end-card seconds must be a number "
                         f"(got {raw_card!r})") from None
    if end_card < 0:
        raise ValueError(f"end-card seconds must be >= 0 (got {end_card})")

    raw_intro = form.get("intro_card", 3.0)
    try:
        intro_card = float(raw_intro or 0.0)
    except (TypeError, ValueError):
        raise ValueError(f"intro-card seconds must be a number "
                         f"(got {raw_intro!r})") from None
    if intro_card < 0:
        raise ValueError(f"intro-card seconds must be >= 0 (got {intro_card})")

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
        pov=pov,
        panel_cycle=panel_cycle,
        panel_cycle_pages=cycle_pages,
        layout=(form.get("layout") or None),
        jobs=jobs,
        end_card=end_card,
        intro_card=intro_card,
        facility_folders=bool(form.get("facility_folders", True)),
        outcome_in_name=bool(form.get("outcome_in_name", True)),
    )


def compact_detail(text: str, limit: int = 78) -> str:
    """One short line for the queue's Details cell.

    The driver's log is indented and prefixed ("    [driver] [f1234] battle
    running"); the frame counter in it is noise next to the progress line, so
    strip the scaffolding and keep the sentence.
    """
    line = (str(text) or "").strip().splitlines()[0] if str(text).strip() else ""
    for prefix in ("! ", "[driver] "):
        while line.startswith(prefix):
            line = line[len(prefix):]
    if line.startswith("[f") and "] " in line:         # drop the frame stamp
        line = line.split("] ", 1)[1]
    line = line.strip()
    return line if len(line) <= limit else line[:limit - 1] + "…"


def batch_status(total: int, done: int, running: int,
                 cancelling: bool = False) -> str:
    """The bottom bar's line: the BATCH, not whichever record shouted last.

    With several records converting at once, per-record chatter down there was
    unreadable — it belongs on each record's own row (Details).
    """
    if cancelling:
        return f"cancelling… {done}/{total} done, {running} finishing"
    if running:
        return f"{done}/{total} done · {running} converting"
    return f"{done}/{total} done"


def format_result_status(result: dict) -> tuple[str, str]:
    """(status, short detail) for a convert_one() result row.

    When the result carries the pipeline's `panel_applied` flag (True/False;
    None = panel not requested) a '[panel on]'/'[panel off]' tag is appended
    so the user can SEE whether the side panel actually made it onto the
    video — a silently-dropped panel is otherwise invisible in the row.
    """
    status = result.get("status", ST_FAILED)
    if status == "OK" or status == "TRUNC":
        out = result.get("output")
        detail = (f"{result.get('frames', 0)}f "
                  f"{result.get('seconds', 0.0):.1f}s")
        if status == "TRUNC":
            detail += f" ({result.get('end_reason', 'truncated')}, partial)"
        if out:
            detail += f" -> {Path(out).name}"
        pa = result.get("panel_applied")
        if pa is not None:
            detail += "  [panel on]" if pa else "  [panel off]"
        return status, detail
    # INVALID / FAILED: the pipeline's one-line detail says it best.
    return status, str(result.get("detail") or result.get("error") or "")


def panel_precheck(settings: ConvertSettings) -> str | None:
    """Warning text when a conversion would SILENTLY lose its panel.

    Returns an actionable message (naming this interpreter + how to fix it)
    when `settings` asks for a side panel but Pillow is missing in the Python
    running the GUI; None when the panel is off or Pillow is present. Pure
    logic — the GUI shows the result in a red status line + a dialog, and
    tests exercise it headless.
    """
    if settings.panel in ("right", "left") and not pillow_available():
        return "info panel requested but unavailable — " + pillow_hint()
    return None


def launch_hint(status: dict) -> str | None:
    """stderr/dialog message when THIS interpreter can't do a full conversion.

    `status` is a pipeline.stack_status() dict. Returns None when the stack is
    complete; otherwise a message that lists what is missing and gives the
    exact rec2mp4-conda-env command to relaunch the GUI where it all works.
    """
    missing = []
    if not status.get("emulator"):
        missing.append("the mGBA emulator bindings")
    if not status.get("ffmpeg"):
        missing.append("ffmpeg")
    if not status.get("pillow"):
        missing.append("Pillow (the info panel)")
    if not missing:
        return None
    return ("This Python ({py}) is missing {what}.\n"
            "Conversions here will {fail}. Launch the GUI with the rec2mp4 "
            "conda env, where the whole stack is installed:\n"
            "  {conda} -m rec2mp4.gui".format(
                py=status.get("interpreter") or "the current interpreter",
                what=", ".join(missing),
                fail=("fail outright" if not status.get("emulator")
                      or not status.get("ffmpeg")
                      else "drop the info panel"),
                conda=CONDA_PYTHON))


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

_COLUMNS = (("file", 230), ("facility", 100), ("level", 70), ("kind", 90),
            ("opponent", 160), ("streak", 55), ("outcome", 70),
            ("valid", 60), ("status", 85), ("detail", 300))


class PreviewWindow:
    """Flip through the composited preview frames of one record.

    Each frame is the REAL output frame — game video plus the panel/layout
    the current settings ask for — so this is what the .mp4 will look like.
    """

    MAX_SIZE = (1180, 720)

    def __init__(self, master, frames, title="preview", outdir=None):
        self.frames = list(frames)
        self.outdir = outdir
        self.i = 0
        self._photo = None
        self.win = tk.Toplevel(master)
        self.win.title(f"rec2mp4 — preview: {title}")

        bar = ttk.Frame(self.win, padding=(8, 6, 8, 2))
        bar.pack(side="top", fill="x")
        ttk.Button(bar, text="◀ Prev",
                   command=lambda: self.step(-1)).pack(side="left")
        ttk.Button(bar, text="Next ▶",
                   command=lambda: self.step(1)).pack(side="left", padx=4)
        self.label = ttk.Label(bar, text="")
        self.label.pack(side="left", padx=10)
        ttk.Button(bar, text="Save this frame…",
                   command=self.save_one).pack(side="right")
        ttk.Button(bar, text="Save all…",
                   command=self.save_all).pack(side="right", padx=4)

        self.canvas = tk.Canvas(self.win, bg="#11141a", highlightthickness=0)
        self.canvas.pack(side="top", fill="both", expand=True)
        self.win.bind("<Left>", lambda _e: self.step(-1))
        self.win.bind("<Right>", lambda _e: self.step(1))
        self.win.bind("<Escape>", lambda _e: self.win.destroy())
        self.show()

    def step(self, delta):
        if self.frames:
            self.i = (self.i + delta) % len(self.frames)
            self.show()

    def show(self):
        if not self.frames:
            return
        frame = self.frames[self.i]
        where = frame.get("label") or ("%.1f s into the battle"
                                       % frame["seconds"])
        self.label.configure(
            text="frame %d/%d — %s — %dx%d"
                 % (self.i + 1, len(self.frames), where,
                    frame["size"][0], frame["size"][1]))
        try:
            from PIL import Image

            from .designer import to_photo
            img = Image.open(io.BytesIO(frame["png"]))
            img.load()
            k = min(self.MAX_SIZE[0] / img.width,
                    self.MAX_SIZE[1] / img.height, 1.0)
            if k < 1.0:
                img = img.resize((max(1, int(img.width * k)),
                                  max(1, int(img.height * k))), Image.LANCZOS)
            self._photo = to_photo(img)
        except Exception as exc:                       # never kill the window
            self.canvas.delete("all")
            self.canvas.create_text(20, 20, anchor="nw", fill="#ff8080",
                                    text=f"cannot show frame: {exc}")
            return
        self.canvas.delete("all")
        self.canvas.configure(width=self._photo.width(),
                              height=self._photo.height())
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

    def save_one(self):
        if not self.frames:
            return
        path = filedialog.asksaveasfilename(
            parent=self.win, title="Save preview frame",
            defaultextension=".png", initialdir=self.outdir or None,
            initialfile=f"preview-{self.i + 1}.png",
            filetypes=[("PNG image", "*.png")])
        if path:
            Path(path).write_bytes(self.frames[self.i]["png"])

    def save_all(self):
        if not self.frames:
            return
        folder = filedialog.askdirectory(parent=self.win,
                                         title="Save every preview frame",
                                         initialdir=self.outdir or None)
        if not folder:
            return
        for n, frame in enumerate(self.frames, start=1):
            (Path(folder) / f"preview-{n}.png").write_bytes(frame["png"])
        self.label.configure(text=f"saved {len(self.frames)} PNG(s) "
                                  f"to {folder}")


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
        self._abort = threading.Event()
        self._batch_warnings: list[str] = []
        self._batch_total = 0
        self._batch_done = 0
        self._batch_running: set = set()

        self._build_queue_pane()
        self._build_settings_pane()
        self._build_status_bar()
        self._build_action_bar()
        # Re-evaluate the panel/Pillow warning whenever the panel choice
        # changes, and probe the stack loudly at startup.
        self.var_panel.trace_add("write",
                                 lambda *_: self._refresh_stack_warning())
        self._probe_stack_at_startup()
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
        self._sort_key = None
        self._sort_desc = False
        for name, width in _COLUMNS:
            self.tree.heading(name, text=name,
                              command=lambda c=name: self._on_sort(c))
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
        d = load_form()

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
        self.var_pov = tk.StringVar(value=d["pov"])
        self.var_sections = {s: tk.BooleanVar(value=True)
                             for s in PANEL_SECTIONS}
        self.var_cycle = tk.StringVar(value=str(d["panel_cycle"]))
        self.var_cycle_pages = {s: tk.BooleanVar(value=True)
                                for s in STAT_PAGE_SECTIONS}
        self.var_layout = tk.StringVar(value=d["layout"])
        self.var_parallel = tk.BooleanVar(value=d["parallel"])
        self.var_jobs = tk.StringVar(value=str(d["jobs"]))
        self.var_end_card = tk.StringVar(value=str(d["end_card"]))
        self.var_intro_card = tk.StringVar(value=str(d["intro_card"]))
        self.var_facility_folders = tk.BooleanVar(
            value=d["facility_folders"])
        self.var_outcome_name = tk.BooleanVar(value=d["outcome_in_name"])
        # Preview-only knobs (not part of ConvertSettings).
        self.var_prev_count = tk.StringVar(value=str(PREVIEW_COUNT))
        self.var_prev_every = tk.StringVar(value=str(PREVIEW_SPACING_SECONDS))

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
        ttk.Checkbutton(row1, text="Folder per facility",
                        variable=self.var_facility_folders
                        ).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(row1, text="Outcome in name",
                        variable=self.var_outcome_name
                        ).pack(side="left", padx=(10, 0))

        row2 = ttk.Frame(f)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="Panel:").pack(side="left")
        ttk.Combobox(row2, textvariable=self.var_panel, state="readonly",
                     values=PANEL_SIDES + ("off",), width=7
                     ).pack(side="left", padx=(2, 10))
        ttk.Label(row2, text="Sections:").pack(side="left")
        for s in PANEL_SECTIONS:
            ttk.Checkbutton(row2, text=s, variable=self.var_sections[s]
                            ).pack(side="left", padx=(0, 4))

        row_layout = ttk.Frame(f)
        row_layout.pack(fill="x", pady=2)
        ttk.Label(row_layout, text="Layout:", width=8).pack(side="left")
        ttk.Entry(row_layout, textvariable=self.var_layout).pack(
            side="left", fill="x", expand=True, padx=2)
        ttk.Button(row_layout, text="Browse…",
                   command=lambda: self._pick_file(
                       self.var_layout, [("rec2mp4 layout", "*.json"),
                                         ("All files", "*")])
                   ).pack(side="left")
        ttk.Button(row_layout, text="Design panel…",
                   command=self._on_design).pack(side="left", padx=2)
        ttk.Button(row_layout, text="Clear",
                   command=lambda: self.var_layout.set("")).pack(side="left")

        row_par = ttk.Frame(f)
        row_par.pack(fill="x", pady=2)
        ttk.Checkbutton(row_par, text="Convert in parallel",
                        variable=self.var_parallel).pack(side="left")
        ttk.Label(row_par, text="workers:").pack(side="left", padx=(8, 0))
        ttk.Spinbox(row_par, textvariable=self.var_jobs, from_=0, to=64,
                    width=4).pack(side="left", padx=2)
        ttk.Label(row_par,
                  text=f"(0 = auto: one per CPU — {cpu_jobs()} here)"
                  ).pack(side="left")
        ttk.Label(row_par, text="Cards:").pack(side="left", padx=(12, 0))
        ttk.Spinbox(row_par, textvariable=self.var_intro_card, from_=0, to=30,
                    increment=1, width=4).pack(side="left", padx=2)
        ttk.Label(row_par, text="s intro (opponent's line) +").pack(side="left")
        ttk.Spinbox(row_par, textvariable=self.var_end_card, from_=0, to=30,
                    increment=1, width=4).pack(side="left", padx=2)
        ttk.Label(row_par,
                  text="s end (trainer state; needs PokeDNA's .txt). "
                       "0 = off").pack(side="left")

        row_prev = ttk.Frame(f)
        row_prev.pack(fill="x", pady=2)
        ttk.Label(row_prev, text="Preview:").pack(side="left")
        ttk.Spinbox(row_prev, textvariable=self.var_prev_count, from_=1, to=20,
                    width=3).pack(side="left", padx=2)
        ttk.Label(row_prev, text="frame(s), one every").pack(side="left")
        ttk.Spinbox(row_prev, textvariable=self.var_prev_every, from_=0.5,
                    to=60, increment=0.5, width=4).pack(side="left", padx=2)
        ttk.Label(row_prev,
                  text="s of battle (the \u201cPreview frames\u2026\u201d "
                       "button; no video is encoded)").pack(side="left")

        row_cycle = ttk.Frame(f)
        row_cycle.pack(fill="x", pady=2)
        ttk.Label(row_cycle, text="Cycle stats every").pack(side="left")
        ttk.Spinbox(row_cycle, textvariable=self.var_cycle, from_=0, to=60,
                    increment=1, width=4).pack(side="left", padx=(2, 2))
        ttk.Label(row_cycle, text="s (0 = static) — pages:").pack(side="left")
        for s in STAT_PAGE_SECTIONS:
            ttk.Checkbutton(row_cycle, text=s,
                            variable=self.var_cycle_pages[s]
                            ).pack(side="left", padx=(0, 4))

        row_pov = ttk.Frame(f)
        row_pov.pack(fill="x", pady=2)
        ttk.Label(row_pov, text="Camera:").pack(side="left")
        ttk.Radiobutton(row_pov, text="Player side", value="player",
                        variable=self.var_pov).pack(side="left", padx=(2, 6))
        ttk.Radiobutton(row_pov, text="Opponent side", value="opponent",
                        variable=self.var_pov).pack(side="left", padx=(0, 8))
        ttk.Label(row_pov,
                  text="(opponent side is experimental — a 'what-if' for "
                       "Frontier battles; may end early)",
                  foreground="#8a6d00").pack(side="left")

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
        self.btn_preview = ttk.Button(bar, text="Preview frames…",
                                      command=self._on_preview)
        self.btn_preview.pack(side="left", padx=4)
        self.btn_cancel = ttk.Button(bar, text="Cancel",
                                     command=self._on_cancel,
                                     state="disabled")
        self.btn_cancel.pack(side="left", padx=4)
        self.btn_cancel_all = ttk.Button(bar, text="Cancel all",
                                         command=self._on_cancel_all,
                                         state="disabled")
        self.btn_cancel_all.pack(side="left", padx=(0, 4))
        ttk.Button(bar, text="Open output folder",
                   command=self._on_open_out).pack(side="left")
        self.progress = ttk.Label(bar, text="idle", anchor="w")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

    def _build_status_bar(self):
        """A red, wrapping warning line above the action bar. Empty = hidden.

        Plain tk.Label (not ttk) so a red foreground works without a custom
        ttk style; wraplength lets the multi-line install/relaunch hints show
        in full instead of being clipped to one row."""
        frame = ttk.Frame(self.root, padding=(8, 0, 8, 0))
        frame.pack(side="bottom", fill="x")
        self.warn_label = tk.Label(frame, text="", anchor="w",
                                   justify="left", fg="#b00020",
                                   wraplength=860)
        self.warn_label.pack(side="left", fill="x", expand=True)
        # keep the wrap width in step with the window so nothing is clipped
        frame.bind("<Configure>",
                   lambda e: self.warn_label.configure(
                       wraplength=max(200, e.width - 16)))

    def _set_warning(self, text: str) -> None:
        """Show (red) or clear the warning line. Also mirrored to stderr so a
        terminal-launched GUI logs it too."""
        text = (text or "").strip()
        self.warn_label.configure(text=("⚠ " + text) if text else "")
        if text:
            print("rec2mp4 GUI: " + text.replace("\n", " "), file=sys.stderr)

    def _refresh_stack_warning(self) -> dict:
        """Probe this interpreter and surface any missing-stack warning.

        Emulator/ffmpeg missing => conversions fail (loud, always shown);
        Pillow missing + panel requested => the panel would be dropped. The
        message names THIS interpreter and the exact conda relaunch command."""
        st = stack_status()
        msgs = []
        hint = launch_hint(st)
        try:
            panel_on = self.var_panel.get() in ("right", "left")
        except Exception:
            panel_on = True
        # Only nag about Pillow when the panel is actually requested.
        if st.get("emulator") and st.get("ffmpeg") and st.get("pillow"):
            hint = None
        elif st.get("emulator") and st.get("ffmpeg") and not panel_on:
            hint = None                     # only Pillow missing, panel off
        if hint:
            msgs.append(hint)
        self._set_warning("\n".join(msgs))
        return st

    def _probe_stack_at_startup(self) -> None:
        """Loud, un-missable check when the GUI opens in a Python that cannot
        do a full conversion: a red status line always, plus a modal dialog
        when the emulator/ffmpeg (i.e. any video at all) is missing."""
        st = self._refresh_stack_warning()
        if not (st.get("emulator") and st.get("ffmpeg")):
            hint = launch_hint(st)
            if hint:
                messagebox.showwarning(
                    "rec2mp4 — conversions unavailable here", hint)

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
            "panel_cycle": self.var_cycle.get(),
            "panel_cycle_pages": [s for s in STAT_PAGE_SECTIONS
                                  if self.var_cycle_pages[s].get()],
            "pov": self.var_pov.get(),
            "layout": self.var_layout.get().strip(),
            "parallel": self.var_parallel.get(),
            "jobs": self.var_jobs.get(),
            "end_card": self.var_end_card.get(),
            "intro_card": self.var_intro_card.get(),
            "facility_folders": self.var_facility_folders.get(),
            "outcome_in_name": self.var_outcome_name.get(),
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

    def _on_sort(self, column: str) -> None:
        """Click a heading to sort by it; click it again to reverse."""
        if self._running() or not self._msgq.empty():
            return          # indices are in flight; re-ordering would misfile
        if self._sort_key == column:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key, self._sort_desc = column, False
        if self.model.sort(column, self._sort_desc):
            self._refresh_tree()

    def _sync_headings(self) -> None:
        for name, _w in _COLUMNS:
            arrow = ""
            if name == self._sort_key:
                arrow = " \u25bc" if self._sort_desc else " \u25b2"
            self.tree.heading(name, text=name + arrow)

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
        self._sync_headings()
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
        # Never let a requested panel silently vanish: if Pillow is missing in
        # this Python, say so BEFORE converting and let the user decide.
        warn = panel_precheck(settings)
        if warn:
            self._set_warning(warn)
            if not messagebox.askyesno(
                    "rec2mp4 — info panel unavailable",
                    warn + "\n\nConvert anyway? The video(s) will be produced "
                    "WITHOUT the info panel."):
                self.progress.configure(text="cancelled — panel unavailable")
                return
        # Remember what was chosen — above all the SAVE, whose default
        # (local/template.sav) is not the user's own save.
        save_form(self.read_form())
        self._batch_warnings.clear()
        for i in jobs:
            self.model.items[i].status = ST_WAITING
            self.model.items[i].detail = ""
            self.model.items[i].log_lines.clear()
        self._refresh_tree()
        self._cancel.clear()
        self._abort.clear()
        self._batch_total = len(jobs)
        self._batch_done = 0
        self._batch_running.clear()
        self.btn_convert.configure(state="disabled")
        self.btn_preview.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.btn_cancel_all.configure(state="normal")
        self._refresh_batch_line()
        paths = [(i, self.model.items[i].path) for i in jobs]
        self._worker = threading.Thread(
            target=self._worker_main, args=(paths, settings), daemon=True)
        self._worker.start()

    def _on_cancel(self):
        """Stop dispatching; let the records in flight finish normally."""
        if self._running():
            self._cancel.set()
            self._refresh_batch_line()

    def _on_cancel_all(self):
        """Stop everything NOW, including the conversions already running.

        The replays check the abort flag every few emulated frames, so this
        takes effect in well under a second even mid-battle; each aborted
        record drops its partial video and comes back CANCELLED.
        """
        if self._running():
            self._cancel.set()
            self._abort.set()
            self.progress.configure(text="cancelling everything…")

    # ---- preview -----------------------------------------------------------

    def _preview_target(self) -> int | None:
        """Which queued row to preview: the selection, else the first
        convertible record."""
        sel = self.tree.selection()
        if sel:
            idx = self.tree.index(sel[0])
            if 0 <= idx < len(self.model.items) \
                    and self.model.items[idx].read_error is None:
                return idx
        conv = self.model.convertible_indices()
        return conv[0] if conv else None

    def _on_preview(self):
        """Replay a few seconds of ONE record and show the composited frames.

        Same engine, same panel/layout, no encoding — so you can check how the
        video will look (and that the record replays at all) before spending a
        full conversion on a queue."""
        if self._running():
            return
        idx = self._preview_target()
        if idx is None:
            messagebox.showinfo("rec2mp4", "Add a .rec file first, then "
                                "select it to preview.")
            return
        try:
            settings = self.read_settings()
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("rec2mp4", f"Bad settings: {exc}")
            return
        if not pillow_available():
            messagebox.showerror("rec2mp4 — preview unavailable",
                                 "The frame preview draws with Pillow — "
                                 + pillow_hint())
            return
        item = self.model.items[idx]
        self._cancel.clear()
        self.btn_convert.configure(state="disabled")
        self.btn_preview.configure(state="disabled")
        self.progress.configure(
            text=f"previewing {item.path.name} — booting the emulator…")
        try:
            count = max(1, int(float(self.var_prev_count.get() or
                                     PREVIEW_COUNT)))
            every = max(0.1, float(self.var_prev_every.get() or
                                   PREVIEW_SPACING_SECONDS))
        except (TypeError, ValueError):
            count, every = PREVIEW_COUNT, PREVIEW_SPACING_SECONDS
        self._worker = threading.Thread(
            target=self._preview_main,
            args=(idx, item.path, settings, count, every), daemon=True)
        self._worker.start()

    def _preview_main(self, idx, path, settings, count=PREVIEW_COUNT,
                      every=PREVIEW_SPACING_SECONDS):
        """Worker thread for the preview (widgets are off-limits here)."""
        put = self._msgq.put
        try:
            ctx = load_context(settings,
                               log=lambda m: put(("blog", str(m))),
                               err=lambda m: put(("blog", f"! {m}")))
            res = preview_frames(
                path, settings, ctx=ctx, count=count, spacing_seconds=every,
                log=lambda m: put(("log", idx, str(m))),
                err=lambda m: put(("log", idx, f"! {m}")),
                progress_cb=lambda p: put(("progress", idx, p)))
        except PipelineError as exc:
            res = {"status": "FAILED", "frames": [], "detail": str(exc),
                   "error": str(exc), "name": Path(path).name}
        except Exception as exc:
            res = {"status": "FAILED", "frames": [],
                   "detail": f"{type(exc).__name__}: {exc}",
                   "error": str(exc), "name": Path(path).name}
        put(("preview", idx, res))

    def _show_preview(self, idx, res):
        self.btn_convert.configure(state="normal")
        self.btn_preview.configure(state="normal")
        if res.get("status") != "OK":
            self.progress.configure(text=f"preview failed: {res['detail']}")
            messagebox.showerror("rec2mp4 — preview failed",
                                 str(res.get("detail") or "unknown error"))
            return
        frames = res["frames"]
        self._preview_frames = frames          # the designer reuses frame 0
        name = self.model.items[idx].path.name if 0 <= idx < len(
            self.model.items) else res.get("name", "preview")
        self.progress.configure(
            text=f"preview: {len(frames)} frame(s) from {name}")
        PreviewWindow(self.root, frames, name, outdir=self.var_outdir.get())

    # ---- panel designer ----------------------------------------------------

    def _design_sample(self):
        """(info, extras, game_img) for the designer's live preview.

        Prefers the selected/first queued record + the ROM from the form, so
        what you design against is your own battle; falls back to the
        designer's synthetic demo record.
        """
        from . import designer as designer_mod
        rom_bytes = None
        rom_path = Path(self.var_rom.get().strip() or DEFAULT_ROM)
        try:
            if rom_path.is_file():
                rom_bytes = rom_path.read_bytes()
        except OSError:
            rom_bytes = None
        sections = [s for s in PANEL_SECTIONS if self.var_sections[s].get()]
        extras = designer_mod.demo_extras(rom_bytes, sections=sections)
        info = None
        idx = self._preview_target()
        if idx is not None and self.model.items[idx].valid:
            from .pipeline import opponent_label
            info = self.model.items[idx].info
            extras["opponent_a_label"] = opponent_label(info, "a", rom_bytes)
            extras["opponent_b_label"] = opponent_label(info, "b", rom_bytes)
            extras["outcome_text"] = "unknown"
            extras["duration_seconds"] = None
            extras["streak"] = None
        game_img = None
        frames = getattr(self, "_preview_frames", None)
        if frames and pillow_available():
            # The intro-card frame carries no game_png — design over a real
            # battle frame, never over the card.
            real = next((f for f in frames if f.get("game_png")), None)
            try:
                import io as _io

                from PIL import Image
                if real is not None:
                    game_img = Image.open(_io.BytesIO(real["game_png"]))
                    game_img.load()
            except Exception:
                game_img = None
        return info or designer_mod.demo_info(), extras, game_img

    def _on_design(self):
        """Open the visual panel designer on the current layout."""
        from . import designer as designer_mod
        from . import layout as layout_mod
        current = self.var_layout.get().strip()
        lay = None
        if current:
            try:
                lay = layout_mod.Layout.load(current)
            except layout_mod.LayoutError as exc:
                if not messagebox.askyesno(
                        "rec2mp4 designer",
                        f"{current} could not be loaded:\n{exc}\n\n"
                        "Start from a fresh default layout?"):
                    return
        if lay is None:
            side = self.var_panel.get()
            lay = layout_mod.default_layout(
                side if side in PANEL_SIDES else "right",
                sections=[s for s in PANEL_SECTIONS
                          if self.var_sections[s].get()])
        info, extras, game_img = self._design_sample()

        def on_apply(new_layout, path):
            if path:
                self.var_layout.set(str(path))
                self.progress.configure(text=f"layout applied: {path}")

        try:                            # a half-typed scale must not crash it
            scale = max(1, min(4, int(self.var_scale.get() or 3)))
        except (ValueError, tk.TclError):
            scale = 3
        designer_mod.open_designer(
            self.root, lay, info=info, extras=extras, game_img=game_img,
            rom_bytes=extras.get("rom_bytes"), on_apply=on_apply,
            path=(current or None), scale=scale)

    def _worker_main(self, jobs, settings):
        """Worker thread: NEVER touches widgets — messages only.

        Hands the queue to pipeline.convert_batch, which runs the records
        either in this process (workers = 1) or one per CPU in separate
        processes. Every callback just posts a message; the indices are
        translated from batch position to the model's row index here.
        """
        put = self._msgq.put
        rows = [idx for idx, _p in jobs]
        done = [0]
        try:
            ctx = load_context(settings,
                               log=lambda m: put(("blog", str(m))),
                               err=lambda m: put(("blog", f"! {m}")))
        except PipelineError as exc:
            put(("fatal", str(exc)))
            put(("done", 0, len(jobs), self._cancel.is_set()))
            return
        try:
            n = resolve_jobs(settings.jobs, len(jobs))
            if n > 1:
                put(("blog", f"converting {len(jobs)} record(s) "
                             f"{n} at a time, one process each"))

            def on_result(k, res):
                done[0] += 1
                put(("result", rows[k], res))

            convert_batch(
                [p for _i, p in jobs], settings, ctx=ctx,
                log=lambda m: put(("blog", str(m))),
                err=lambda m: put(("blog", f"! {m}")),
                on_start=lambda k, _p: put(("status", rows[k],
                                            ST_CONVERTING, "")),
                on_log=lambda k, text: put(("log", rows[k], text)),
                on_progress=lambda k, p: put(("progress", rows[k], p)),
                on_result=on_result,
                cancelled=self._cancel.is_set,
                aborted=self._abort.is_set)
        except PipelineError as exc:
            put(("fatal", str(exc)))
        except Exception as exc:              # a frozen GUI is worse
            put(("blog", f"! batch failed: {type(exc).__name__}: {exc}"))
        finally:
            # Always posted, even if the batch itself blows up — 'done' is
            # what re-enables the Convert button.
            put(("done", done[0], len(jobs), self._cancel.is_set()))

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
            item.status = status
            item.detail = detail or ("starting…" if status == ST_CONVERTING
                                     else "")
            self._update_row(idx)
            if status == ST_CONVERTING:
                self._batch_running.add(idx)
                self._refresh_batch_line()
        elif kind == "log":
            _, idx, text = msg
            item = self.model.items[idx]
            item.log_lines.append(text)
            line = compact_detail(text)
            if line:
                # Live progress belongs on the record's OWN row; the final
                # result overwrites it when the conversion finishes.
                item.detail = line
                self._update_row(idx)
        elif kind == "blog":                       # batch-level log line
            text = str(msg[1])
            # err() prefixes batch warnings with "! " (see _worker_main):
            # surface those in the red warning line, not just the (scrolling)
            # progress label, so a Pillow-missing degrade can't hide.
            if text.startswith("! "):
                warn = text[2:]
                self._batch_warnings.append(warn)
                self._set_warning(warn)
            self.progress.configure(text=text.lstrip("! ").splitlines()[0])
        elif kind == "progress":
            _, idx, p = msg
            item = self.model.items[idx]
            if p.get("phase") == "preview":
                item.detail = (f"preview {p.get('grabbed', 0)}"
                               f"/{p.get('wanted', 0)} frames "
                               f"({p.get('seconds', 0.0):.1f}s in)")
            else:
                item.detail = (f"replay {p.get('frames', 0)}f "
                               f"{p.get('seconds', 0.0):.1f}s captured")
            self._update_row(idx)
        elif kind == "preview":
            _, idx, res = msg
            self._show_preview(idx, res)
        elif kind == "result":
            _, idx, res = msg
            item = self.model.items[idx]
            item.status, item.detail = format_result_status(res)
            item.outcome = (res.get("outcome_text") or "").upper() \
                if res.get("outcome_text") not in (None, "unknown") else ""
            if res.get("streak") is not None:
                item.streak = res["streak"]
            self._update_row(idx)
            self._batch_running.discard(idx)
            self._batch_done += 1
            self._refresh_batch_line()
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

    def _refresh_batch_line(self) -> None:
        self.progress.configure(text=batch_status(
            self._batch_total, self._batch_done, len(self._batch_running),
            cancelling=self._cancel.is_set()))

    def _finish(self, text):
        self.btn_convert.configure(state="normal")
        self.btn_preview.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        self.btn_cancel_all.configure(state="disabled")
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
    # A frozen build (PyInstaller/py2app) re-runs the entry module in every
    # spawned worker; without this the parallel batch would start whole new
    # GUIs instead of conversion workers. A no-op when not frozen.
    import multiprocessing
    multiprocessing.freeze_support()
    if tk is None:
        print("error: tkinter is not available in this Python — install "
              "the Tk support package for your Python (python.org and "
              "conda installers include it).", file=sys.stderr)
        return 2
    # Detect a launch under the wrong interpreter (no mGBA / ffmpeg / Pillow)
    # and print the exact conda-env relaunch command to the terminal. The GUI
    # itself repeats this in a red status line + dialog once it is up.
    hint = launch_hint(stack_status())
    if hint:
        print("rec2mp4: " + hint, file=sys.stderr)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"error: cannot open a display ({exc})", file=sys.stderr)
        return 2
    app = GuiApp(root)
    root.protocol("WM_DELETE_WINDOW",
                  lambda: (save_form(app.read_form()), root.destroy()))
    # Launched by double-clicking an icon, Tk often comes up BEHIND whatever
    # was in front. Raise once, then drop topmost so it behaves normally.
    try:
        root.lift()
        root.attributes("-topmost", True)
        root.after(300, lambda: root.attributes("-topmost", False))
    except tk.TclError:                                # pragma: no cover
        pass
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
