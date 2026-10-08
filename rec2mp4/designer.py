# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""Visual panel designer — drag the info windows where you want them.

    python -m rec2mp4.designer            (or the `rec2mp4-designer` script)
    …or the GUI's "Design panel…" button, which hands the current record and
    ROM over so the live preview shows YOUR battle, not placeholder text.

The window shows the composited video frame — game video in the middle, the
panels around it — exactly as `pipeline.compose_frame` will build it. Every
information block is a rectangle you can drag, resize from its bottom-right
corner, restyle, hide or delete. Panels can sit on all four sides at once
(left/right columns, top/bottom bands), each with its own thickness,
background colour and background image.

The result is a `rec2mp4.layout.Layout` — a plain JSON file — used by
`rec2mp4 --layout my.json` or the GUI's layout field.

Pure-logic helpers (grid snapping, hit testing, the demo record) live above
the widgets and are unit-tested headless in tests/test_designer.py.
"""

from __future__ import annotations

import io
import sys
from dataclasses import replace
from pathlib import Path

from . import layout as L
from . import panel as panel_mod
from . import pipeline

try:
    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox, ttk
except ImportError:                                   # pragma: no cover
    tk = None


# ---------------------------------------------------------------------------
# Pure logic — no tkinter below this line until the "widgets" section
# ---------------------------------------------------------------------------

# How close (in canvas pixels) to a block's bottom-right corner counts as
# "grab the resize handle" instead of "move the block".
HANDLE_PX = 12
MIN_FRACTION = 0.03          # a block may never be dragged smaller than this
ZOOM_STEP = 1.25             # one click of + / -
ZOOM_MIN, ZOOM_MAX = 0.25, 8.0
UNDO_DEPTH = 200             # snapshots kept; a Layout dict is tiny


def snap(value: float, step: float) -> float:
    """Round `value` to the nearest multiple of `step` (step<=0 = no snap)."""
    if not step or step <= 0:
        return value
    return round(value / step) * step


def panel_origin_units(lay: L.Layout, side: str) -> tuple:
    """(x, y) of a panel's top-left corner, in composite units."""
    left = lay.units_for("left")
    top = lay.units_for("top")
    if side == "left":
        return (0, top)
    if side == "right":
        return (left + L.GAME_UNITS[0], top)
    if side == "top":
        return (0, 0)
    return (0, top + L.GAME_UNITS[1])                 # bottom


def block_rect_canvas(lay: L.Layout, side: str, block, k: float) -> tuple:
    """A block's (x0, y0, x1, y1) in canvas pixels at units->pixels factor k."""
    ox, oy = panel_origin_units(lay, side)
    pw, ph = lay.panel_units(side)
    x0 = (ox + block.x * pw) * k
    y0 = (oy + block.y * ph) * k
    x1 = (ox + (block.x + block.w) * pw) * k
    y1 = (oy + (block.y + block.h) * ph) * k
    return (x0, y0, x1, y1)


def hit_test(lay: L.Layout, x: float, y: float, k: float,
             selected: tuple | None = None) -> tuple | None:
    """Which block is under the canvas point? -> (side, index, mode) or None.

    mode is 'resize' inside the selected block's corner handle, else 'move'.
    Later panels/blocks win, so the topmost drawn block is the one grabbed.
    """
    hit = None
    for spec in lay.ordered_panels():
        for i, block in enumerate(spec.blocks):
            x0, y0, x1, y1 = block_rect_canvas(lay, spec.side, block, k)
            if x0 <= x <= x1 and y0 <= y <= y1:
                mode = "move"
                if (selected == (spec.side, i)
                        and x >= x1 - HANDLE_PX and y >= y1 - HANDLE_PX):
                    mode = "resize"
                hit = (spec.side, i, mode)
    return hit


def move_block(block, dx_units: float, dy_units: float, panel_units: tuple,
               grid: float = 0.0):
    """Return a copy of `block` moved by a canvas delta expressed in units."""
    pw, ph = panel_units
    x = snap(block.x + dx_units / max(1, pw), grid)
    y = snap(block.y + dy_units / max(1, ph), grid)
    return replace(block, x=x, y=y).clamped()


def resize_block(block, dx_units: float, dy_units: float, panel_units: tuple,
                 grid: float = 0.0):
    """Return a copy of `block` resized from its bottom-right corner."""
    pw, ph = panel_units
    w = max(MIN_FRACTION, snap(block.w + dx_units / max(1, pw), grid))
    h = max(MIN_FRACTION, snap(block.h + dy_units / max(1, ph), grid))
    return replace(block, w=w, h=h).clamped()


def demo_info() -> dict:
    """A synthetic rec.parse()-shaped record so the designer has something to
    draw before any real .rec is loaded. Invented values only — no game data
    ships with rec2mp4; species/move names resolve from the user's ROM when
    one is available, and show as '#id' when it is not."""
    def mon(species, level, shiny=False):
        return {
            "species_internal": species, "nickname": "", "level": level,
            "shiny": shiny, "checksum_ok": True,
            "moves": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}],
            "evs": {"hp": 4, "atk": 252, "def": 0, "spa": 0, "spd": 0,
                    "spe": 252, "sum": 508},
            "ivs": {"hp": 31, "atk": 31, "def": 31, "spa": 31, "spd": 31,
                    "spe": 31, "sum": 186},
            "nature": "Adamant",
        }
    return {
        "valid": True, "facility": "Battle Tower", "facility_id": 0,
        "level_mode": "Open Level", "is_double": False, "is_multi": False,
        "is_two_opponents": False, "is_link_recorded": False,
        "battle_scene_off": False, "text_speed": "mid",
        "recorded_by": "PLAYER", "recorded_by_gender": "M",
        "players": ["PLAYER"], "players_language": ["ENG"],
        "multiplayer_id": 0,
        "opponent_a": 83, "opponent_a_kind": "frontier",
        "opponent_a_name": "frontier trainer 83",
        "opponent_b": 0, "opponent_b_kind": None, "opponent_b_name": None,
        "rng_seed": "0x1234abcd",
        "teams": {"player": [mon(376, 58, True), mon(248, 55), mon(373, 57)],
                  "opponent": [mon(363, 60), mon(236, 60), mon(130, 60)]},
    }


def demo_extras(rom_bytes=None, sections=None) -> dict:
    """`extras` for the demo record (see panel.render_panel)."""
    return {
        "rom_bytes": rom_bytes,
        "outcome_text": "won", "duration_seconds": 187.0, "streak": 21,
        "export_lines": ["Battle Tower - Open Level", "Streak 21"],
        "sections": tuple(sections or panel_mod.PANEL_SECTIONS),
        "opponent_a_label": "frontier trainer 83", "opponent_b_label": None,
        "pov": "player", "pov_faithful": False,
        "state": {"playtime": "116h 0m 10s", "playtime_hours": 116,
                  "dex_seen": 221, "dex_caught": 162, "bp": 18,
                  "bp_card": 15, "symbols": "sG--s--",
                  "symbols_silver": 2, "symbols_gold": 1},
    }


def layout_summary(lay: L.Layout) -> str:
    """One line for a status bar: sides, thickness, block counts."""
    if not lay.panels:
        return "no panels — the video would be the plain game frame"
    w, h = lay.composite_units()
    return "%s   |   output %dx%d units (%.2f:1)" % (
        lay.describe(), w, h, (w / h) if h else 0)


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

_BG_CANVAS = "#1b1f27"
_SEL = "#ffcb4f"
_UNSEL = "#5f6b7d"

# Property rows shown for the selected block, in editor order.
_ALIGN_LABELS = {"left": "left", "center": "center", "right": "right"}


class DesignerApp:
    """The designer window (a Toplevel when the GUI opens it, else the root)."""

    MAX_CANVAS = (980, 620)

    def __init__(self, master, lay: L.Layout | None = None, *,
                 info: dict | None = None, extras: dict | None = None,
                 rom_bytes: bytes | None = None, game_img=None,
                 on_apply=None, path=None, scale: int = 3):
        self.master = master
        self.on_apply = on_apply
        self.path = Path(path) if path else None
        self.scale = max(1, int(scale))
        self.lay = lay or L.default_layout("right")
        self.info = info or demo_info()
        self.extras = extras or demo_extras(rom_bytes)
        self.extras.setdefault("rom_bytes", rom_bytes)
        self.game_img = game_img
        self.page = 0
        self.selected: tuple | None = None            # (side, index)
        self._drag = None
        self._photo = None
        self._syncing = False
        self._k = 1.0
        self._dirty = False
        self._last_canvas = (0, 0)
        self._resize_job = None
        # Zoom is a multiplier ON TOP of the fit-to-window scale, so 100% is
        # always "the whole frame visible" and anything above scrolls.
        self.zoom = 1.0
        self._fit_k = 1.0
        # Undo/redo hold Layout.to_dict() snapshots — small, plain JSON, and
        # immune to the aliasing bugs a diff/command stack would invite.
        self._undo: list = []
        self._redo: list = []

        master.title("rec2mp4 — panel designer")
        master.minsize(1180, 700)

        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self._select_first()
        self.refresh(rerender=True)

    # ---- layout ---------------------------------------------------------

    def _build_toolbar(self):
        bar = ttk.Frame(self.master, padding=(8, 6, 8, 2))
        bar.pack(side="top", fill="x")
        ttk.Button(bar, text="New", command=self._on_new).pack(side="left")
        ttk.Button(bar, text="Open…", command=self._on_open
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="Save", command=self._on_save).pack(side="left")
        ttk.Button(bar, text="Save as…", command=self._on_save_as
                   ).pack(side="left", padx=4)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y",
                                                   padx=8)
        ttk.Label(bar, text="Preset:").pack(side="left")
        self.var_preset = tk.StringVar(value="right column")
        ttk.Combobox(bar, textvariable=self.var_preset, state="readonly",
                     width=16,
                     values=("right column", "left column", "top band",
                             "bottom band", "right + bands", "blank")
                     ).pack(side="left", padx=(2, 2))
        ttk.Button(bar, text="Apply preset",
                   command=self._on_preset).pack(side="left")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y",
                                                   padx=8)
        self.btn_undo = ttk.Button(bar, text="↶ Undo", width=8,
                                   command=self.undo)
        self.btn_undo.pack(side="left")
        self.btn_redo = ttk.Button(bar, text="↷ Redo", width=8,
                                   command=self.redo)
        self.btn_redo.pack(side="left", padx=4)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y",
                                                   padx=8)
        ttk.Button(bar, text="−", width=3,
                   command=lambda: self.zoom_by(1 / ZOOM_STEP)).pack(
                       side="left")
        self.lbl_zoom = ttk.Label(bar, text="100%", width=6, anchor="center")
        self.lbl_zoom.pack(side="left")
        ttk.Button(bar, text="+", width=3,
                   command=lambda: self.zoom_by(ZOOM_STEP)).pack(side="left")
        ttk.Button(bar, text="Fit", width=4,
                   command=self.zoom_fit).pack(side="left", padx=(4, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y",
                                                   padx=8)
        self.var_snap = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Snap to grid", variable=self.var_snap
                        ).pack(side="left")
        ttk.Button(bar, text="Preview page ▸",
                   command=self._on_next_page).pack(side="left", padx=8)
        ttk.Button(bar, text="Use in conversion",
                   command=self._on_apply).pack(side="right")
        ttk.Button(bar, text="Close", command=self._on_close
                   ).pack(side="right", padx=4)

    def _build_body(self):
        body = ttk.Frame(self.master, padding=(8, 2, 8, 2))
        body.pack(side="top", fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(left, bg=_BG_CANVAS, highlightthickness=0,
                                width=self.MAX_CANVAS[0],
                                height=self.MAX_CANVAS[1])
        self.canvas.grid(row=0, column=0, sticky="nsew")
        # Zoomed in, the frame is bigger than the window — it has to scroll.
        ysb = ttk.Scrollbar(left, orient="vertical", command=self.canvas.yview)
        xsb = ttk.Scrollbar(left, orient="horizontal",
                            command=self.canvas.xview)
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)

        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-1>", self._on_double)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        # Wheel: plain = scroll, Cmd/Ctrl = zoom (the platform conventions).
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_wheel_h)
        self.canvas.bind("<Command-MouseWheel>", self._on_wheel_zoom)
        self.canvas.bind("<Control-MouseWheel>", self._on_wheel_zoom)
        self.canvas.bind("<Button-4>", lambda e: self._on_wheel(e, 1))
        self.canvas.bind("<Button-5>", lambda e: self._on_wheel(e, -1))
        for key, dx, dy in (("Left", -1, 0), ("Right", 1, 0),
                            ("Up", 0, -1), ("Down", 0, 1)):
            self.canvas.bind(f"<{key}>",
                             lambda _e, a=dx, b=dy: self._nudge(a, b))
        self.canvas.bind("<BackSpace>", lambda _e: self._on_del_block())
        self.canvas.bind("<Delete>", lambda _e: self._on_del_block())
        self.canvas.focus_set()
        self._bind_shortcuts()

        right = ttk.Frame(body, width=380)
        right.pack(side="left", fill="y", padx=(8, 0))
        right.pack_propagate(False)
        self._build_panel_editor(right)
        self._build_block_editor(right)

    def _build_panel_editor(self, parent):
        f = ttk.LabelFrame(parent, text="Panels", padding=6)
        f.pack(side="top", fill="x")

        row = ttk.Frame(f)
        row.pack(fill="x")
        self.var_sides = {}
        for side in L.SIDES:
            var = tk.BooleanVar(value=self.lay.has_side(side))
            self.var_sides[side] = var
            ttk.Checkbutton(row, text=side, variable=var,
                            command=lambda s=side: self._on_toggle_side(s)
                            ).pack(side="left", padx=(0, 6))

        row2 = ttk.Frame(f)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="Editing:").pack(side="left")
        self.var_side = tk.StringVar(
            value=(self.lay.ordered_panels() or [L.PanelSpec()])[0].side)
        self.cmb_side = ttk.Combobox(row2, textvariable=self.var_side,
                                     state="readonly", width=8,
                                     values=[p.side for p in
                                             self.lay.ordered_panels()])
        self.cmb_side.pack(side="left", padx=4)
        self.cmb_side.bind("<<ComboboxSelected>>",
                           lambda _e: self._on_side_changed())
        ttk.Label(row2, text="thickness:").pack(side="left", padx=(8, 0))
        self.var_units = tk.StringVar(value="120")
        sp = ttk.Spinbox(row2, textvariable=self.var_units, from_=L.MIN_UNITS,
                         to=L.MAX_UNITS, increment=10, width=5,
                         command=self._on_units)
        sp.pack(side="left", padx=2)
        sp.bind("<Return>", lambda _e: self._on_units())
        sp.bind("<FocusOut>", lambda _e: self._on_units())

        row3 = ttk.Frame(f)
        row3.pack(fill="x", pady=(6, 0))
        ttk.Label(row3, text="Background:").pack(side="left")
        self.var_bg = tk.StringVar(value="#10141a")
        ttk.Entry(row3, textvariable=self.var_bg, width=10).pack(side="left",
                                                                 padx=2)
        ttk.Button(row3, text="…", width=3,
                   command=lambda: self._pick_color(self.var_bg)
                   ).pack(side="left")
        self.var_bg.trace_add("write", lambda *_: self._on_panel_style())

        row4 = ttk.Frame(f)
        row4.pack(fill="x", pady=(4, 0))
        ttk.Label(row4, text="Image:").pack(side="left")
        self.var_bgimg = tk.StringVar(value="")
        ttk.Entry(row4, textvariable=self.var_bgimg).pack(
            side="left", fill="x", expand=True, padx=2)
        ttk.Button(row4, text="…", width=3,
                   command=self._pick_bg_image).pack(side="left")
        ttk.Button(row4, text="x", width=2,
                   command=lambda: self.var_bgimg.set("")).pack(side="left")
        self.var_bgimg.trace_add("write", lambda *_: self._on_panel_style())

        row5 = ttk.Frame(f)
        row5.pack(fill="x", pady=(4, 0))
        ttk.Label(row5, text="fit:").pack(side="left")
        self.var_bgmode = tk.StringVar(value="cover")
        ttk.Combobox(row5, textvariable=self.var_bgmode, state="readonly",
                     width=8, values=L.BG_MODES).pack(side="left", padx=2)
        self.var_bgmode.trace_add("write", lambda *_: self._on_panel_style())
        ttk.Label(row5, text="dim:").pack(side="left", padx=(8, 0))
        self.var_bgdim = tk.DoubleVar(value=0.0)
        ttk.Scale(row5, from_=0.0, to=1.0, variable=self.var_bgdim,
                  command=lambda _v: self._on_panel_style(defer=True)
                  ).pack(side="left", fill="x", expand=True, padx=2)

    def _build_block_editor(self, parent):
        f = ttk.LabelFrame(parent, text="Information blocks", padding=6)
        f.pack(side="top", fill="both", expand=True, pady=(8, 0))

        add = ttk.Frame(f)
        add.pack(fill="x")
        self.var_kind = tk.StringVar(value="header")
        ttk.Combobox(add, textvariable=self.var_kind, state="readonly",
                     width=12, values=L.BLOCK_KINDS).pack(side="left")
        ttk.Button(add, text="Add", width=5,
                   command=self._on_add_block).pack(side="left", padx=2)
        ttk.Button(add, text="Copy", width=6,
                   command=self._on_dup_block).pack(side="left")
        ttk.Button(add, text="Delete", width=7,
                   command=self._on_del_block).pack(side="left", padx=2)
        ttk.Button(add, text="Reset panel", width=12,
                   command=self._on_reset_panel).pack(side="left")

        self.lst = tk.Listbox(f, height=8, exportselection=False,
                              activestyle="none")
        self.lst.pack(fill="x", pady=(6, 4))
        self.lst.bind("<<ListboxSelect>>", lambda _e: self._on_list_select())

        props = ttk.Frame(f)
        props.pack(fill="both", expand=True)
        self.prop_vars = {}

        def row(label):
            r = ttk.Frame(props)
            r.pack(fill="x", pady=1)
            ttk.Label(r, text=label, width=11).pack(side="left")
            return r

        r = row("visible")
        self.prop_vars["visible"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, variable=self.prop_vars["visible"],
                        command=self._on_prop).pack(side="left")
        ttk.Label(r, text="  caption").pack(side="left")
        self.prop_vars["title"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, variable=self.prop_vars["title"],
                        command=self._on_prop).pack(side="left")
        ttk.Label(r, text="  wrap").pack(side="left")
        self.prop_vars["wrap"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, variable=self.prop_vars["wrap"],
                        command=self._on_prop).pack(side="left")

        for key, label, lo, hi, step in (
                ("x", "x", 0.0, 1.0, 0.01), ("y", "y", 0.0, 1.0, 0.01),
                ("w", "w", 0.02, 1.0, 0.01), ("h", "h", 0.02, 1.0, 0.01),
                ("font_scale", "font x", 0.2, 4.0, 0.05),
                ("padding", "padding", 0.0, 0.4, 0.01),
                ("line_gap", "line gap", 0.8, 3.0, 0.05),
                ("bg_opacity", "bg alpha", 0.0, 1.0, 0.05),
                ("radius", "corner", 0, 60, 1),
                ("border_width", "border px", 0, 10, 1)):
            r = row(label)
            var = tk.StringVar()
            self.prop_vars[key] = var
            sp = ttk.Spinbox(r, textvariable=var, from_=lo, to=hi,
                             increment=step, width=7, command=self._on_prop)
            sp.pack(side="left")
            sp.bind("<Return>", lambda _e: self._on_prop())
            sp.bind("<FocusOut>", lambda _e: self._on_prop())

        for key, label, values in (("align", "align", L.ALIGNMENTS),
                                   ("valign", "v-align", L.VALIGNMENTS),
                                   ("fit", "overflow", L.FIT_MODES)):
            r = row(label)
            var = tk.StringVar()
            self.prop_vars[key] = var
            ttk.Combobox(r, textvariable=var, state="readonly", width=9,
                         values=values).pack(side="left")
            var.trace_add("write", lambda *_: self._on_prop())

        for key, label in (("color", "text"), ("title_color", "caption"),
                           ("accent_color", "accent"), ("bg", "block bg"),
                           ("border", "border")):
            r = row(label)
            var = tk.StringVar()
            self.prop_vars[key] = var
            ttk.Entry(r, textvariable=var, width=10).pack(side="left")
            ttk.Button(r, text="…", width=3,
                       command=lambda v=var: self._pick_color(v)
                       ).pack(side="left", padx=2)
            ttk.Button(r, text="x", width=2,
                       command=lambda v=var: v.set("")).pack(side="left")
            var.trace_add("write", lambda *_: self._on_prop())

        r = row("label")
        self.prop_vars["label"] = tk.StringVar()
        e = ttk.Entry(r, textvariable=self.prop_vars["label"])
        e.pack(side="left", fill="x", expand=True)
        e.bind("<FocusOut>", lambda _e: self._on_prop())
        e.bind("<Return>", lambda _e: self._on_prop())

        ttk.Label(props, text="custom text (kind 'text'):").pack(
            anchor="w", pady=(6, 0))
        self.txt_text = tk.Text(props, height=4, wrap="word")
        self.txt_text.pack(fill="x")
        self.txt_text.bind("<FocusOut>", lambda _e: self._on_prop())
        self.txt_text.bind("<Control-Return>", lambda _e: self._on_prop())

    def _build_statusbar(self):
        bar = ttk.Frame(self.master, padding=(8, 2, 8, 6))
        bar.pack(side="bottom", fill="x")
        self.status = ttk.Label(bar, text="", anchor="w")
        self.status.pack(side="left", fill="x", expand=True)

    # ---- model helpers ---------------------------------------------------

    def _panel(self, side=None):
        return self.lay.panel(side or self.var_side.get())

    def _sel_block(self):
        if not self.selected:
            return None
        spec = self.lay.panel(self.selected[0])
        if spec is None or not (0 <= self.selected[1] < len(spec.blocks)):
            return None
        return spec.blocks[self.selected[1]]

    def _select_first(self):
        for spec in self.lay.ordered_panels():
            if spec.blocks:
                self.selected = (spec.side, 0)
                self.var_side.set(spec.side)
                return
        self.selected = None

    def _grid(self) -> float:
        return 0.01 if self.var_snap.get() else 0.0

    # ---- rendering -------------------------------------------------------

    def refresh(self, rerender: bool = True):
        """Redraw everything: preview image, block list, property widgets."""
        self._sync_side_widgets()
        self._sync_list()
        self._sync_props()
        if rerender:
            self._render()
        self.status.configure(text=layout_summary(self.lay))

    # ---- zoom ------------------------------------------------------------

    def zoom_by(self, factor: float) -> None:
        """Multiply the zoom, keeping it inside the sane range."""
        self.set_zoom(self.zoom * factor)

    def set_zoom(self, value: float) -> None:
        value = max(ZOOM_MIN, min(ZOOM_MAX, float(value)))
        if abs(value - self.zoom) < 1e-6:
            return
        self.zoom = value
        self._render()
        self._sync_zoom_label()

    def zoom_fit(self) -> None:
        """Back to 'the whole composited frame is visible'."""
        self.set_zoom(1.0)
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def _sync_zoom_label(self) -> None:
        try:
            self.lbl_zoom.configure(text="%d%%" % round(self.zoom * 100))
        except Exception:                              # pragma: no cover
            pass

    def _on_wheel(self, event, direction=None):
        step = direction if direction is not None else (
            1 if getattr(event, "delta", 0) > 0 else -1)
        self.canvas.yview_scroll(-step, "units")
        return "break"

    def _on_wheel_h(self, event):
        self.canvas.xview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _on_wheel_zoom(self, event):
        self.zoom_by(ZOOM_STEP if getattr(event, "delta", 0) > 0
                     else 1 / ZOOM_STEP)
        return "break"

    # ---- undo / redo -----------------------------------------------------

    def _bind_shortcuts(self) -> None:
        """Cmd+Z/Cmd+Y on macOS, Ctrl+Z/Ctrl+Y elsewhere — bind both, since a
        Mac keyboard can still send Control and Windows never sends Command."""
        m = self.master
        for seq in ("<Command-z>", "<Control-z>"):
            m.bind(seq, lambda _e: (self.undo(), "break")[1])
        for seq in ("<Command-y>", "<Control-y>",
                    "<Command-Shift-Z>", "<Control-Shift-Z>",
                    "<Command-Shift-z>", "<Control-Shift-z>"):
            m.bind(seq, lambda _e: (self.redo(), "break")[1])
        for seq in ("<Command-plus>", "<Control-plus>",
                    "<Command-equal>", "<Control-equal>"):
            m.bind(seq, lambda _e: (self.zoom_by(ZOOM_STEP), "break")[1])
        for seq in ("<Command-minus>", "<Control-minus>"):
            m.bind(seq, lambda _e: (self.zoom_by(1 / ZOOM_STEP), "break")[1])
        for seq in ("<Command-0>", "<Control-0>"):
            m.bind(seq, lambda _e: (self.zoom_fit(), "break")[1])

    def snapshot(self) -> dict:
        return self.lay.to_dict()

    def push_undo(self) -> None:
        """Record the CURRENT layout before a change is applied.

        Consecutive identical snapshots are collapsed, so holding a spinbox or
        typing in a colour field does not bury the real edits under dozens of
        no-op steps.
        """
        snap = self.snapshot()
        if self._undo and self._undo[-1] == snap:
            return
        self._undo.append(snap)
        del self._undo[:-UNDO_DEPTH]
        self._redo.clear()
        self._sync_undo_buttons()

    def undo(self) -> None:
        if not self._undo:
            return
        self._redo.append(self.snapshot())
        self._restore(self._undo.pop())
        self.status.configure(text="undo — %d step(s) left" % len(self._undo))

    def redo(self) -> None:
        if not self._redo:
            return
        self._undo.append(self.snapshot())
        self._restore(self._redo.pop())
        self.status.configure(text="redo — %d step(s) left" % len(self._redo))

    def _restore(self, snap: dict) -> None:
        try:
            self.lay = L.Layout.from_dict(snap)
        except L.LayoutError:                          # pragma: no cover
            return
        sides = [p.side for p in self.lay.ordered_panels()]
        self.cmb_side.configure(values=sides)
        if self.var_side.get() not in sides and sides:
            self.var_side.set(sides[0])
        if not (self.selected and self.lay.panel(self.selected[0])
                and self.selected[1] < len(
                    self.lay.panel(self.selected[0]).blocks)):
            self._select_first()
        self._dirty = True
        self.refresh()
        self._sync_undo_buttons()

    def _sync_undo_buttons(self) -> None:
        try:
            self.btn_undo.configure(
                state="normal" if self._undo else "disabled")
            self.btn_redo.configure(
                state="normal" if self._redo else "disabled")
        except Exception:                              # pragma: no cover
            pass

    def _canvas_size(self) -> tuple:
        """Usable canvas size; falls back to MAX_CANVAS before the window is
        mapped (an unmapped tk widget reports width/height 1, and scaling the
        preview to 1 px would make every block rectangle unusable)."""
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw <= 1:
            cw = self.MAX_CANVAS[0]
        if ch <= 1:
            ch = self.MAX_CANVAS[1]
        return (max(200, cw), max(200, ch))

    def _on_canvas_configure(self, event):
        """Re-fit the preview when the window is resized (debounced)."""
        size = (event.width, event.height)
        if abs(size[0] - self._last_canvas[0]) < 8 \
                and abs(size[1] - self._last_canvas[1]) < 8:
            return
        self._last_canvas = size
        if self._resize_job is not None:
            try:
                self.master.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.master.after(120, self._render)

    def _render(self):
        self._resize_job = None
        self.canvas.delete("all")
        if not pipeline.pillow_available():
            self.canvas.create_text(
                20, 20, anchor="nw", fill="#ff8080",
                text="Pillow is not installed in this Python — the designer "
                     "cannot draw a preview.\n" + pipeline.pillow_hint())
            return
        try:
            img = self._compose()
        except Exception as exc:                       # never kill the window
            self.canvas.create_text(20, 20, anchor="nw", fill="#ff8080",
                                    text=f"preview failed: "
                                         f"{type(exc).__name__}: {exc}")
            return
        cw, ch = self._canvas_size()
        self._fit_k = min(cw / img.width, ch / img.height)
        k = self._fit_k * self.zoom
        disp_w, disp_h = max(1, int(img.width * k)), max(1, int(img.height * k))
        from PIL import Image
        # NEAREST when zoomed past 1:1 — at 400% the point is to see the exact
        # pixel edges of a block, not a smoothed guess at them.
        filt = Image.NEAREST if k > 1.0 else Image.LANCZOS
        shown = img.resize((disp_w, disp_h), filt)
        self._photo = to_photo(shown)
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self.canvas.configure(scrollregion=(0, 0, disp_w, disp_h))
        units_w = self.lay.composite_units()[0]
        self._k = disp_w / units_w if units_w else 1.0
        self._draw_handles()
        self._sync_zoom_label()

    def _compose(self):
        settings = pipeline.ConvertSettings(scale=self.scale, panel="right",
                                            layout=self.lay)
        # page > 0 means the user is stepping through the stat pages a
        # --panel-cycle conversion would show, so ask for the split then.
        ctx = pipeline.design_context(self.lay,
                                      rom_bytes=self.extras.get("rom_bytes"),
                                      panel_cycle=5.0 if self.page else 0.0)
        return pipeline.compose_frame(self.game_img, self.info, self.extras,
                                      ctx, settings, page=self.page)

    def _draw_handles(self):
        k = self._k
        for spec in self.lay.ordered_panels():
            for i, block in enumerate(spec.blocks):
                x0, y0, x1, y1 = block_rect_canvas(self.lay, spec.side,
                                                   block, k)
                sel = self.selected == (spec.side, i)
                colour = _SEL if sel else _UNSEL
                self.canvas.create_rectangle(
                    x0, y0, x1, y1, outline=colour, tags=("handle",),
                    width=2 if sel else 1,
                    dash=() if block.visible else (3, 3))
                self.canvas.create_text(
                    x0 + 4, y0 + 2, anchor="nw", fill=colour, tags=("handle",),
                    text=block.display_name(), font=("TkDefaultFont", 8))
                if sel:
                    self.canvas.create_rectangle(
                        x1 - HANDLE_PX, y1 - HANDLE_PX, x1, y1,
                        outline=_SEL, fill=_SEL, tags=("handle",))

    # ---- canvas interaction ---------------------------------------------

    def _cxy(self, event) -> tuple:
        """Widget coords -> CANVAS coords (they differ once it scrolls)."""
        return (self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))

    def _on_press(self, event):
        self.canvas.focus_set()
        cx, cy = self._cxy(event)
        hit = hit_test(self.lay, cx, cy, self._k, self.selected)
        if hit is None:
            self.selected = None
            self._drag = None
            self.refresh(rerender=False)
            self._draw_handles_only()
            return
        side, idx, mode = hit
        self.selected = (side, idx)
        self.var_side.set(side)
        # One undo step per gesture: pushed when the drag starts, not on
        # every motion event.
        self.push_undo()
        self._drag = {"mode": mode, "x": cx, "y": cy,
                      "block": self.lay.panel(side).blocks[idx]}
        self.refresh(rerender=False)
        self._draw_handles_only()

    def _draw_handles_only(self):
        """Redraw just the rectangles (cheap — no re-render of the panels)."""
        self.canvas.delete("handle")
        self._draw_handles()

    def _on_motion(self, event):
        if not self._drag or not self.selected:
            return
        side, idx = self.selected
        spec = self.lay.panel(side)
        if spec is None:
            return
        k = self._k or 1.0
        cx, cy = self._cxy(event)
        dx = (cx - self._drag["x"]) / k
        dy = (cy - self._drag["y"]) / k
        base = self._drag["block"]
        units = self.lay.panel_units(side)
        grid = self._grid()
        if self._drag["mode"] == "resize":
            spec.blocks[idx] = resize_block(base, dx, dy, units, grid)
        else:
            spec.blocks[idx] = move_block(base, dx, dy, units, grid)
        self._dirty = True
        self._draw_handles_only()
        self.status.configure(
            text="%s  x=%.2f y=%.2f w=%.2f h=%.2f"
                 % (spec.blocks[idx].display_name(), spec.blocks[idx].x,
                    spec.blocks[idx].y, spec.blocks[idx].w,
                    spec.blocks[idx].h))

    def _on_release(self, _event):
        if self._drag:
            self._drag = None
            self.refresh()

    def _on_double(self, _event):
        """Double-click toggles the block's visibility."""
        block = self._sel_block()
        if block is not None:
            self.push_undo()
            block.visible = not block.visible
            self._dirty = True
            self.refresh()

    def _nudge(self, dx, dy):
        if not self.selected:
            return
        side, idx = self.selected
        spec = self.lay.panel(side)
        self.push_undo()
        step = 0.01
        b = spec.blocks[idx]
        spec.blocks[idx] = replace(
            b, x=b.x + dx * step, y=b.y + dy * step).clamped()
        self._dirty = True
        self.refresh()

    # ---- panel actions ---------------------------------------------------

    def _on_toggle_side(self, side):
        self.push_undo()
        if self.var_sides[side].get():
            if not self.lay.has_side(side):
                self.lay.set_panel(L.default_panel(side))
        else:
            if len(self.lay.panels) <= 1:
                self.var_sides[side].set(True)
                messagebox.showinfo(
                    "rec2mp4 designer",
                    "A layout needs at least one panel. Convert with "
                    "--panel off for a plain game video.")
                return
            self.lay.remove_side(side)
            if self.selected and self.selected[0] == side:
                self._select_first()
        if self.lay.panels:
            sides = [p.side for p in self.lay.ordered_panels()]
            self.cmb_side.configure(values=sides)
            if self.var_side.get() not in sides:
                self.var_side.set(sides[0])
        self._dirty = True
        self.refresh()

    def _on_side_changed(self):
        spec = self._panel()
        if spec and spec.blocks:
            self.selected = (spec.side, 0)
        else:
            self.selected = None
        self.refresh()

    def _on_units(self):
        spec = self._panel()
        if spec is None or self._syncing:
            return
        try:
            units = int(float(self.var_units.get()))
        except (TypeError, ValueError):
            return
        units = L.even(max(L.MIN_UNITS, min(L.MAX_UNITS, units)))
        if units != spec.units:
            self.push_undo()
            spec.units = units
            self._dirty = True
            self.refresh()

    def _on_panel_style(self, defer: bool = False):
        spec = self._panel()
        if spec is None or self._syncing:
            return
        try:
            new_bg = L._color(self.var_bg.get() or None, "bg", "#10141a")
        except L.LayoutError:
            return                                    # mid-typing '#1e2'
        if (new_bg, self.var_bgimg.get().strip() or None,
                self.var_bgmode.get(), float(self.var_bgdim.get())) == (
                spec.bg, spec.bg_image, spec.bg_mode, spec.bg_dim):
            return                                    # nothing actually moved
        self.push_undo()
        spec.bg = new_bg
        spec.bg_image = self.var_bgimg.get().strip() or None
        spec.bg_mode = self.var_bgmode.get() or "cover"
        spec.bg_dim = float(self.var_bgdim.get())
        self._dirty = True
        self.refresh()

    def _on_reset_panel(self):
        spec = self._panel()
        if spec is None:
            return
        self.push_undo()
        fresh = L.default_panel(spec.side, units=spec.units)
        spec.blocks = fresh.blocks
        self.selected = (spec.side, 0) if spec.blocks else None
        self._dirty = True
        self.refresh()

    def _on_preset(self):
        self.push_undo()
        name = self.var_preset.get()
        if name == "blank":
            lay = L.blank_layout("right")
        elif name == "right + bands":
            lay = L.Layout(name="right + bands", panels=[
                L.default_panel("right"),
                L.default_panel("top", units=30,
                                sections=("header", "opponents")),
                L.default_panel("bottom", units=30,
                                sections=("players", "footer"))])
        else:
            side = {"right column": "right", "left column": "left",
                    "top band": "top", "bottom band": "bottom"}[name]
            lay = L.default_layout(side)
        self.lay = lay
        for side, var in self.var_sides.items():
            var.set(self.lay.has_side(side))
        self.cmb_side.configure(values=[p.side for p in
                                        self.lay.ordered_panels()])
        self.var_side.set(self.lay.ordered_panels()[0].side)
        self._select_first()
        self._dirty = True
        self.refresh()

    def _on_next_page(self):
        self.page += 1
        self.refresh()

    # ---- block actions ---------------------------------------------------

    def _on_add_block(self):
        spec = self._panel()
        if spec is None:
            return
        self.push_undo()
        kind = self.var_kind.get()
        block = L.Block(kind=kind, x=0.05, y=0.05, w=0.9, h=0.2)
        if kind == "text":
            block.text = "YOUR TEXT"
            block.align = "center"
            block.valign = "middle"
        spec.blocks.append(block)
        self.selected = (spec.side, len(spec.blocks) - 1)
        self._dirty = True
        self.refresh()

    def _on_dup_block(self):
        block = self._sel_block()
        if block is None:
            return
        self.push_undo()
        spec = self.lay.panel(self.selected[0])
        spec.blocks.append(replace(
            block, y=min(0.95, block.y + 0.03), x=min(0.95, block.x + 0.02)
        ).clamped())
        self.selected = (spec.side, len(spec.blocks) - 1)
        self._dirty = True
        self.refresh()

    def _on_del_block(self):
        if not self.selected:
            return
        side, idx = self.selected
        spec = self.lay.panel(side)
        if spec and 0 <= idx < len(spec.blocks):
            self.push_undo()
            del spec.blocks[idx]
            self.selected = (side, min(idx, len(spec.blocks) - 1)) \
                if spec.blocks else None
            self._dirty = True
            self.refresh()

    def _on_list_select(self):
        sel = self.lst.curselection()
        spec = self._panel()
        if sel and spec:
            self.selected = (spec.side, sel[0])
            self.refresh(rerender=False)
            self._draw_handles_only()
            self._sync_props()

    # ---- widget <-> model sync ------------------------------------------

    def _sync_side_widgets(self):
        self._syncing = True
        try:
            for side, var in self.var_sides.items():
                var.set(self.lay.has_side(side))
            spec = self._panel()
            if spec is not None:
                self.var_units.set(str(spec.units))
                self.var_bg.set(spec.bg or "")
                self.var_bgimg.set(spec.bg_image or "")
                self.var_bgmode.set(spec.bg_mode)
                self.var_bgdim.set(spec.bg_dim)
        finally:
            self._syncing = False

    def _sync_list(self):
        spec = self._panel()
        self.lst.delete(0, "end")
        if spec is None:
            return
        for i, b in enumerate(spec.blocks):
            mark = "" if b.visible else "(hidden) "
            self.lst.insert("end", f"{i + 1}. {mark}{b.display_name()}")
        if self.selected and self.selected[0] == spec.side \
                and 0 <= self.selected[1] < len(spec.blocks):
            self.lst.selection_clear(0, "end")
            self.lst.selection_set(self.selected[1])

    def _sync_props(self):
        block = self._sel_block()
        self._syncing = True
        try:
            for key, var in self.prop_vars.items():
                if block is None:
                    if isinstance(var, tk.BooleanVar):
                        var.set(False)
                    else:
                        var.set("")
                    continue
                value = getattr(block, key, "")
                if isinstance(var, tk.BooleanVar):
                    var.set(bool(value))
                elif isinstance(value, float):
                    var.set("%.3f" % value)
                else:
                    var.set("" if value is None else str(value))
            self.txt_text.delete("1.0", "end")
            if block is not None and block.kind == "text":
                self.txt_text.insert("1.0", block.text)
        finally:
            self._syncing = False

    def _on_prop(self):
        block = self._sel_block()
        if block is None or self._syncing:
            return
        before = self.snapshot()
        changed = False
        for key, var in self.prop_vars.items():
            raw = var.get()
            old = getattr(block, key, None)
            if isinstance(var, tk.BooleanVar):
                new = bool(raw)
            elif key in ("align", "valign", "fit", "label"):
                new = raw or old
            elif key in ("color", "title_color", "accent_color", "bg",
                         "border"):
                text = str(raw).strip()
                if not text:
                    new = None
                else:
                    try:
                        new = L._color(text, key)
                    except L.LayoutError:
                        continue                       # mid-typing
            elif key in ("radius", "border_width"):
                try:
                    new = int(float(raw))
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    new = float(raw)
                except (TypeError, ValueError):
                    continue
            if new != old:
                setattr(block, key, new)
                changed = True
        if block.kind == "text":
            text = self.txt_text.get("1.0", "end").rstrip("\n")
            if text != block.text:
                block.text = text
                changed = True
        if changed:
            # The edit is already on the block, so the snapshot to restore is
            # the one taken with the OLD values — hence push_undo() below
            # works on `before`, captured before the loop.
            self._undo.append(before)
            del self._undo[:-UNDO_DEPTH]
            self._redo.clear()
            self._sync_undo_buttons()
            clamped = block.clamped()
            spec = self.lay.panel(self.selected[0])
            spec.blocks[self.selected[1]] = clamped
            self._dirty = True
            self.refresh()

    # ---- pickers / files -------------------------------------------------

    def _pick_color(self, var):
        initial = var.get().strip() or "#10141a"
        try:
            _rgb, hexval = colorchooser.askcolor(initialcolor=initial,
                                                 parent=self.master)
        except tk.TclError:
            _rgb, hexval = colorchooser.askcolor(parent=self.master)
        if hexval:
            var.set(hexval)

    def _pick_bg_image(self):
        path = filedialog.askopenfilename(
            title="Panel background image", parent=self.master,
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"),
                       ("All files", "*")])
        if path:
            self.var_bgimg.set(path)

    def _on_new(self):
        self.lay = L.default_layout("right")
        self.path = None
        self._select_first()
        self._dirty = False
        self.refresh()

    def _on_open(self):
        path = filedialog.askopenfilename(
            title="Open a panel layout", parent=self.master,
            filetypes=[("rec2mp4 layout", "*.json"), ("All files", "*")])
        if not path:
            return
        try:
            self.lay = L.Layout.load(path)
        except L.LayoutError as exc:
            messagebox.showerror("rec2mp4 designer", str(exc))
            return
        self.path = Path(path)
        self._select_first()
        self.cmb_side.configure(values=[p.side for p in
                                        self.lay.ordered_panels()])
        if self.lay.panels:
            self.var_side.set(self.lay.ordered_panels()[0].side)
        self._dirty = False
        self.refresh()

    def _on_save(self):
        if self.path is None:
            return self._on_save_as()
        return self._save_to(self.path)

    def _on_save_as(self):
        path = filedialog.asksaveasfilename(
            title="Save panel layout", parent=self.master,
            defaultextension=".json", initialfile="panel-layout.json",
            filetypes=[("rec2mp4 layout", "*.json")])
        if not path:
            return None
        return self._save_to(Path(path))

    def _save_to(self, path: Path):
        try:
            self.lay.name = path.stem
            self.lay.save(path)
        except OSError as exc:
            messagebox.showerror("rec2mp4 designer", f"cannot save: {exc}")
            return None
        self.path = path
        self._dirty = False
        self.status.configure(text=f"saved {path}")
        return path

    def _on_apply(self):
        """Hand the layout back to the GUI (saving it first if needed)."""
        if self.path is None or self._dirty:
            if self._on_save() is None:
                return
        if self.on_apply is not None:
            self.on_apply(self.lay, self.path)
        self._on_close()

    def _on_close(self):
        try:
            self.master.destroy()
        except tk.TclError:
            pass


def to_photo(pil_image):
    """PIL Image -> a tk image, via ImageTk when present, else PNG bytes.

    Tk 8.6 reads PNG natively, so the fallback needs no Pillow-Tk binding —
    which some conda/pip Pillow builds ship without.
    """
    try:
        from PIL import ImageTk
        return ImageTk.PhotoImage(pil_image)
    except Exception:
        buf = io.BytesIO()
        pil_image.save(buf, "PNG")
        return tk.PhotoImage(data=buf.getvalue())


def open_designer(master, lay=None, **kwargs) -> "DesignerApp":
    """Open the designer as a Toplevel of an existing tkinter app."""
    win = tk.Toplevel(master)
    return DesignerApp(win, lay, **kwargs)


def main(argv=None) -> int:
    if tk is None:
        print("error: tkinter is not available in this Python — install the "
              "Tk support package for your Python.", file=sys.stderr)
        return 2
    if not pipeline.pillow_available():
        print("rec2mp4 designer: " + pipeline.pillow_hint(), file=sys.stderr)
    argv = list(sys.argv[1:] if argv is None else argv)
    lay = None
    path = None
    if argv:
        path = Path(argv[0])
        if path.is_file():
            try:
                lay = L.Layout.load(path)
            except L.LayoutError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        else:
            lay = L.default_layout("right")      # a name to save as later
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"error: cannot open a display ({exc})", file=sys.stderr)
        return 2
    DesignerApp(root, lay, path=path)
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
