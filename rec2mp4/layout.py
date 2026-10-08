# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""Panel layout model — where the info blocks live, and on which side.

The classic side panel (`rec2mp4/panel.py:render_panel`) is a fixed vertical
flow: sections are drawn top-to-bottom in a hard-coded order, on the right or
the left of the game video. A *layout* replaces that with free-form geometry:

    Layout
      └── PanelSpec        one per side (left / right / top / bottom)
            ├── background (colour, image + fit mode, dim)
            └── Block[]    an "information window": a section drawn inside a
                           rectangle you can move and resize

Everything is pure data — no Pillow, no tkinter, no game logic — so the
designer (`rec2mp4/designer.py`), the renderer (`panel.render_layout_panel`),
the pipeline compositor and the tests all share one model, and a layout is a
plain JSON file the user can keep, diff and hand around.

Geometry conventions
--------------------
* Block rects are **fractions of their panel** (0..1), so one layout renders
  correctly at any `--scale`.
* Panel thickness is in **GBA units** (the game screen is 240x160 units):
  `units` is the width of a left/right panel and the height of a top/bottom
  band. Kept even so every composited dimension stays even for yuv420p.
* Top/bottom bands span the **full composited width** (game + left + right),
  which is why they read as a title bar / footer.

    +-----------------------------------------+
    |                  top                    |
    +--------+-----------------------+--------+
    |  left  |      game video       | right  |
    +--------+-----------------------+--------+
    |                 bottom                  |
    +-----------------------------------------+
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .panel import PANEL_SECTIONS, STAT_PAGE_SECTIONS

LAYOUT_VERSION = 1

# Sides a panel can occupy. Order matters for stable serialization/compositing.
SIDES = ("top", "left", "right", "bottom")
VERTICAL_SIDES = ("top", "bottom")      # thickness = height, spans full width
HORIZONTAL_SIDES = ("left", "right")    # thickness = width, spans game height

# Block kinds: every panel section, plus the layout-only extras.
#   text  — free text the user types in the designer (a title, a watermark,
#           a channel handle …). No game data, so it is always safe to ship.
#   rule  — a horizontal divider line.
#   frame — an empty box (just its background/border) for visual grouping.
EXTRA_BLOCK_KINDS = ("text", "rule", "frame")
BLOCK_KINDS = tuple(PANEL_SECTIONS) + EXTRA_BLOCK_KINDS

ALIGNMENTS = ("left", "center", "right")
VALIGNMENTS = ("top", "middle", "bottom")
FIT_MODES = ("shrink", "clip")
BG_MODES = ("cover", "contain", "stretch", "tile", "center")

GAME_UNITS = (240, 160)

# Default thickness per side (GBA units, even).
DEFAULT_UNITS = {"left": 120, "right": 120, "top": 40, "bottom": 40}

# Guard rails: a panel thinner than this cannot render legible text; one
# thicker than this dwarfs the game. Both are in GBA units.
MIN_UNITS, MAX_UNITS = 20, 480

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class LayoutError(ValueError):
    """A layout file/dict is malformed (message is user-facing)."""


# ---------------------------------------------------------------------------
# small validators — every one raises LayoutError with an actionable message
# ---------------------------------------------------------------------------

def _num(value, name: str, lo: float, hi: float, default=None) -> float:
    if value is None and default is not None:
        return float(default)
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise LayoutError(f"{name} must be a number (got {value!r})") from None
    if not (lo <= out <= hi):
        raise LayoutError(f"{name} must be {lo}..{hi} (got {out})")
    return out


def _choice(value, name: str, allowed, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text not in allowed:
        raise LayoutError("%s must be one of %s (got %r)"
                          % (name, ", ".join(allowed), value))
    return text


def _color(value, name: str, default=None):
    """'#rgb'/'#rrggbb' -> normalized '#rrggbb'; None allowed (= inherit)."""
    if value is None or value == "":
        return default
    text = str(value).strip()
    if not _HEX_RE.match(text):
        raise LayoutError(f"{name} must be a hex colour like '#1e2430' "
                          f"(got {value!r})")
    if len(text) == 4:                       # #abc -> #aabbcc
        text = "#" + "".join(ch * 2 for ch in text[1:])
    return text.lower()


def color_rgb(text, default=(0, 0, 0)) -> tuple:
    """'#rrggbb' -> (r, g, b). Anything unparseable falls back to `default`."""
    if not text:
        return tuple(default)
    s = str(text).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return tuple(default)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return tuple(default)


def even(n: int) -> int:
    """Round up to an even number — h264/yuv420p refuses odd dimensions."""
    n = int(n)
    return n + (n & 1)


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------

@dataclass
class Block:
    """One draggable, resizable "information window" inside a panel.

    x/y/w/h are fractions of the panel (0..1). Everything else is presentation
    the designer exposes; all of it has a sane default, so a hand-written
    layout only needs `kind` + a rect.
    """
    kind: str
    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 0.2
    visible: bool = True
    font_scale: float = 1.0
    align: str = "left"
    valign: str = "top"
    color: str | None = None          # body text colour override
    title_color: str | None = None    # section-caption colour override
    accent_color: str | None = None   # highlights (sums, streak, outcome)
    bg: str | None = None             # block background colour
    bg_opacity: float = 1.0           # 0 = fully transparent block background
    border: str | None = None         # border colour ('' / None = no border)
    border_width: int = 1
    radius: int = 0                   # rounded-corner radius, in panel px/1000
    padding: float = 0.03             # fraction of the block's width
    line_gap: float = 1.45            # line height as a multiple of font px
    wrap: bool = False                # wrap long lines instead of ellipsizing
    fit: str = "shrink"               # shrink the font to fit, or hard-clip
    title: bool = True                # draw the section caption
    text: str = ""                    # kind == "text": the literal text
    label: str = ""                   # designer-only friendly name

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "x": round(float(self.x), 5), "y": round(float(self.y), 5),
            "w": round(float(self.w), 5), "h": round(float(self.h), 5),
            "visible": bool(self.visible),
            "font_scale": round(float(self.font_scale), 4),
            "align": self.align, "valign": self.valign,
            "color": self.color, "title_color": self.title_color,
            "accent_color": self.accent_color,
            "bg": self.bg, "bg_opacity": round(float(self.bg_opacity), 4),
            "border": self.border, "border_width": int(self.border_width),
            "radius": int(self.radius),
            "padding": round(float(self.padding), 5),
            "line_gap": round(float(self.line_gap), 4),
            "wrap": bool(self.wrap), "fit": self.fit,
            "title": bool(self.title), "text": self.text,
            "label": self.label,
        }

    @staticmethod
    def from_dict(d: dict) -> "Block":
        if not isinstance(d, dict):
            raise LayoutError(f"block must be an object (got {type(d).__name__})")
        kind = str(d.get("kind", "")).strip().lower()
        if kind not in BLOCK_KINDS:
            raise LayoutError("unknown block kind %r (valid: %s)"
                              % (d.get("kind"), ", ".join(BLOCK_KINDS)))
        blk = Block(
            kind=kind,
            x=_num(d.get("x", 0.0), "block.x", -1.0, 2.0),
            y=_num(d.get("y", 0.0), "block.y", -1.0, 2.0),
            w=_num(d.get("w", 1.0), "block.w", 0.01, 2.0),
            h=_num(d.get("h", 0.2), "block.h", 0.01, 2.0),
            visible=bool(d.get("visible", True)),
            font_scale=_num(d.get("font_scale", 1.0), "block.font_scale",
                            0.2, 6.0),
            align=_choice(d.get("align"), "block.align", ALIGNMENTS, "left"),
            valign=_choice(d.get("valign"), "block.valign", VALIGNMENTS,
                           "top"),
            color=_color(d.get("color"), "block.color"),
            title_color=_color(d.get("title_color"), "block.title_color"),
            accent_color=_color(d.get("accent_color"), "block.accent_color"),
            bg=_color(d.get("bg"), "block.bg"),
            bg_opacity=_num(d.get("bg_opacity", 1.0), "block.bg_opacity",
                            0.0, 1.0),
            border=_color(d.get("border"), "block.border"),
            border_width=int(_num(d.get("border_width", 1),
                                  "block.border_width", 0, 20)),
            radius=int(_num(d.get("radius", 0), "block.radius", 0, 200)),
            padding=_num(d.get("padding", 0.03), "block.padding", 0.0, 0.45),
            line_gap=_num(d.get("line_gap", 1.45), "block.line_gap", 0.8, 3.0),
            wrap=bool(d.get("wrap", False)),
            fit=_choice(d.get("fit"), "block.fit", FIT_MODES, "shrink"),
            title=bool(d.get("title", True)),
            text=str(d.get("text", ""))[:2000],
            label=str(d.get("label", ""))[:80],
        )
        return blk.clamped()

    def clamped(self) -> "Block":
        """Keep the rect inside the panel (a drag can overshoot the edge)."""
        w = min(max(float(self.w), 0.02), 1.0)
        h = min(max(float(self.h), 0.02), 1.0)
        x = min(max(float(self.x), 0.0), 1.0 - w)
        y = min(max(float(self.y), 0.0), 1.0 - h)
        return replace(self, x=x, y=y, w=w, h=h)

    def rect_px(self, size: tuple) -> tuple:
        """(left, top, right, bottom) in pixels for a panel of `size`."""
        pw, ph = int(size[0]), int(size[1])
        left = int(round(self.x * pw))
        top = int(round(self.y * ph))
        right = max(left + 1, int(round((self.x + self.w) * pw)))
        bottom = max(top + 1, int(round((self.y + self.h) * ph)))
        return (left, top, min(right, pw), min(bottom, ph))

    @property
    def is_stat(self) -> bool:
        return self.kind in STAT_PAGE_SECTIONS

    def display_name(self) -> str:
        if self.label:
            return self.label
        if self.kind == "text":
            head = " ".join(self.text.split())[:24]
            return f"text: {head}" if head else "text"
        return self.kind


# ---------------------------------------------------------------------------
# PanelSpec
# ---------------------------------------------------------------------------

@dataclass
class PanelSpec:
    """One panel: which side it sits on, how thick, its background, blocks."""
    side: str = "right"
    units: int = 120
    bg: str = "#10141a"
    bg_image: str | None = None
    bg_mode: str = "cover"
    bg_dim: float = 0.0               # 0 = image as-is, 1 = fully black
    bg_opacity: float = 1.0           # image blended over the colour
    blocks: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "side": self.side, "units": int(self.units), "bg": self.bg,
            "bg_image": self.bg_image, "bg_mode": self.bg_mode,
            "bg_dim": round(float(self.bg_dim), 4),
            "bg_opacity": round(float(self.bg_opacity), 4),
            "blocks": [b.to_dict() for b in self.blocks],
        }

    @staticmethod
    def from_dict(d: dict) -> "PanelSpec":
        if not isinstance(d, dict):
            raise LayoutError(f"panel must be an object "
                              f"(got {type(d).__name__})")
        side = _choice(d.get("side"), "panel.side", SIDES, "right")
        units = int(_num(d.get("units", DEFAULT_UNITS[side]), "panel.units",
                         MIN_UNITS, MAX_UNITS))
        blocks = [Block.from_dict(b) for b in (d.get("blocks") or [])]
        return PanelSpec(
            side=side, units=even(units),
            bg=_color(d.get("bg"), "panel.bg", "#10141a"),
            bg_image=(str(d["bg_image"]) if d.get("bg_image") else None),
            bg_mode=_choice(d.get("bg_mode"), "panel.bg_mode", BG_MODES,
                            "cover"),
            bg_dim=_num(d.get("bg_dim", 0.0), "panel.bg_dim", 0.0, 1.0),
            bg_opacity=_num(d.get("bg_opacity", 1.0), "panel.bg_opacity",
                            0.0, 1.0),
            blocks=blocks)

    @property
    def stat_kinds(self) -> tuple:
        """Stat sections present as visible blocks, in cycle order."""
        have = {b.kind for b in self.blocks if b.visible and b.is_stat}
        return tuple(k for k in STAT_PAGE_SECTIONS if k in have)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

@dataclass
class Layout:
    """A named set of panels around the game video."""
    name: str = "custom"
    version: int = LAYOUT_VERSION
    panels: list = field(default_factory=list)

    # ---- serialization ---------------------------------------------------

    def to_dict(self) -> dict:
        return {"rec2mp4_layout": self.version, "name": self.name,
                "panels": [p.to_dict() for p in self.ordered_panels()]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2) + "\n"

    @staticmethod
    def from_dict(d: dict) -> "Layout":
        if not isinstance(d, dict):
            raise LayoutError("a layout must be a JSON object "
                              f"(got {type(d).__name__})")
        version = d.get("rec2mp4_layout", d.get("version", LAYOUT_VERSION))
        try:
            version = int(version)
        except (TypeError, ValueError):
            raise LayoutError(f"bad layout version {version!r}") from None
        if version > LAYOUT_VERSION:
            raise LayoutError(
                f"this layout is version {version}; this rec2mp4 understands "
                f"up to {LAYOUT_VERSION} — update rec2mp4 or re-save the "
                "layout in the designer")
        panels = [PanelSpec.from_dict(p) for p in (d.get("panels") or [])]
        seen = set()
        for p in panels:
            if p.side in seen:
                raise LayoutError(f"two panels on the same side ({p.side}) — "
                                  "one panel per side")
            seen.add(p.side)
        return Layout(name=str(d.get("name") or "custom")[:80],
                      version=LAYOUT_VERSION, panels=panels)

    @staticmethod
    def from_json(text: str) -> "Layout":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise LayoutError(f"not valid JSON: {exc}") from exc
        return Layout.from_dict(data)

    @staticmethod
    def load(path) -> "Layout":
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise LayoutError(f"cannot read layout {p}: {exc}") from exc
        lay = Layout.from_json(text)
        if lay.name in ("", "custom"):
            lay.name = p.stem
        return lay

    def save(self, path) -> Path:
        """Write atomically — a half-written layout would fail to load."""
        p = Path(path)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        tmp.replace(p)
        return p

    # ---- queries ---------------------------------------------------------

    def ordered_panels(self) -> list:
        """Panels in SIDES order (stable output + stable ffmpeg input order)."""
        by_side = {p.side: p for p in self.panels}
        return [by_side[s] for s in SIDES if s in by_side]

    def panel(self, side: str):
        for p in self.panels:
            if p.side == side:
                return p
        return None

    def has_side(self, side: str) -> bool:
        return self.panel(side) is not None

    def set_panel(self, spec: PanelSpec) -> None:
        """Add or replace the panel on `spec.side`."""
        self.panels = [p for p in self.panels if p.side != spec.side]
        self.panels.append(spec)

    def remove_side(self, side: str) -> bool:
        before = len(self.panels)
        self.panels = [p for p in self.panels if p.side != side]
        return len(self.panels) != before

    @property
    def is_empty(self) -> bool:
        return not any(p.blocks for p in self.panels) and not self.panels

    def sections_used(self) -> tuple:
        """Panel sections drawn by any visible block (for warnings/summaries)."""
        used = {b.kind for p in self.panels for b in p.blocks if b.visible}
        return tuple(s for s in PANEL_SECTIONS if s in used)

    def describe(self) -> str:
        if not self.panels:
            return "no panels"
        return ", ".join(
            "%s %du (%d block%s)"
            % (p.side, p.units, len(p.blocks), "" if len(p.blocks) == 1 else "s")
            for p in self.ordered_panels())

    # ---- geometry --------------------------------------------------------

    def units_for(self, side: str) -> int:
        p = self.panel(side)
        return int(p.units) if p else 0

    def composite_units(self) -> tuple:
        """(total_width_units, total_height_units) of the finished frame."""
        w = GAME_UNITS[0] + self.units_for("left") + self.units_for("right")
        h = GAME_UNITS[1] + self.units_for("top") + self.units_for("bottom")
        return (w, h)

    def panel_units(self, side: str) -> tuple:
        """(width_units, height_units) of one panel."""
        if side in HORIZONTAL_SIDES:
            return (self.units_for(side), GAME_UNITS[1])
        total_w = (GAME_UNITS[0] + self.units_for("left")
                   + self.units_for("right"))
        return (total_w, self.units_for(side))

    def panel_size_px(self, side: str, scale: int) -> tuple:
        w, h = self.panel_units(side)
        return (even(w * int(scale)), even(h * int(scale)))

    def composite_size_px(self, scale: int) -> tuple:
        w, h = self.composite_units()
        return (even(w * int(scale)), even(h * int(scale)))


# ---------------------------------------------------------------------------
# Default layouts
# ---------------------------------------------------------------------------

# The classic flow panel, re-expressed as blocks. Fractions were chosen so a
# 3v3 record fills the column without the old "shrink until unreadable" pass:
# each section owns its slice and shrinks only inside it.
#
# Text inside a block fills its box (resize the box, resize the text — what a
# drag-and-drop designer should do), so these weights ARE the type hierarchy:
# the header gets enough room for its three rows to out-size the one-line
# players/opponents rows.
_CLASSIC_ROWS = (
    # (kind, height weight)
    ("header", 0.16),
    ("players", 0.05),
    ("opponents", 0.05),
    ("teams", 0.18),
    ("moves", 0.18),
    ("evs", 0.13),
    ("ivs", 0.13),
    ("footer", 0.12),
)

# Sections with no place in the default stack (usually empty: the PokeDNA
# '<stem>.txt' export sidecar). Added only when explicitly requested.
_EXTRA_ROW_WEIGHT = 0.06


def default_panel(side: str = "right", units: int | None = None,
                  sections=None) -> PanelSpec:
    """A sensible starting panel for `side` — the classic stack, as blocks."""
    side = _choice(side, "side", SIDES, "right")
    spec = PanelSpec(side=side,
                     units=even(units if units else DEFAULT_UNITS[side]))
    rows = [(k, h) for k, h in _CLASSIC_ROWS
            if sections is None or k in sections]
    # Sections outside the default stack (export, trainer) only appear when
    # asked for — an empty export block would otherwise waste a slice.
    known = {k for k, _h in _CLASSIC_ROWS}
    for extra in (sections or ()):
        if extra in PANEL_SECTIONS and extra not in known:
            rows.append((extra, _EXTRA_ROW_WEIGHT))
    rows.sort(key=lambda kv: PANEL_SECTIONS.index(kv[0]))
    if not rows:
        return spec
    if side in HORIZONTAL_SIDES:
        total = sum(h for _, h in rows)
        y = 0.0
        for kind, h in rows:
            share = h / total
            spec.blocks.append(Block(kind=kind, x=0.0, y=round(y, 4),
                                     w=1.0, h=round(share, 4)))
            y += share
    else:
        # A band is wide and short: lay the sections out in a row instead.
        n = len(rows)
        share = 1.0 / n
        for i, (kind, _h) in enumerate(rows):
            spec.blocks.append(Block(kind=kind, x=round(i * share, 4), y=0.0,
                                     w=round(share, 4), h=1.0,
                                     font_scale=0.9))
    return spec


def default_layout(side: str = "right", units: int | None = None,
                   sections=None) -> Layout:
    """One panel on `side`, laid out like the classic flow panel."""
    return Layout(name=f"default-{side}",
                  panels=[default_panel(side, units, sections)])


def layout_from_classic(panel_side: str, sections=None,
                        units: int | None = None) -> Layout:
    """Bridge for `--panel right|left|top|bottom` when no layout file is set.

    Returns None for 'off'. Used so the block renderer and the flow renderer
    can produce the same *content* from the same settings.
    """
    if panel_side in (None, "", "off"):
        return None
    return default_layout(panel_side, units=units, sections=sections)


def blank_layout(side: str = "right") -> Layout:
    """An empty panel to start designing from scratch."""
    return Layout(name="blank", panels=[PanelSpec(side=side,
                                                  units=DEFAULT_UNITS[side])])


# ---------------------------------------------------------------------------
# Stat-cycling support
# ---------------------------------------------------------------------------

def panel_page_specs(spec: PanelSpec, cycle_pages=()) -> list:
    """Split one panel into the pages a time-cycling panel flips through.

    A panel with several stat blocks (moves/evs/ivs) shows ONE of them per
    page; every other block stays put. A panel with no stat blocks — or with
    only one — has a single page, so it composites as a still image.

    Returns a list of PanelSpec (each a shallow copy with some blocks hidden).
    """
    kinds = spec.stat_kinds
    if cycle_pages:
        wanted = tuple(k for k in STAT_PAGE_SECTIONS if k in cycle_pages)
        kinds = tuple(k for k in kinds if k in wanted)
    if len(kinds) <= 1:
        return [spec]

    # The stat blocks are mutually exclusive per page, so the one being shown
    # inherits the space of ALL of them. Without this each page kept its own
    # thin slice and left the hidden blocks' rows empty — the text stayed tiny
    # and still overflowed (a '+N' marker above a third of blank panel).
    stat_rects = [(b.y, b.y + b.h) for b in spec.blocks
                  if b.visible and b.is_stat and b.kind in kinds]
    top = min(r[0] for r in stat_rects)
    bottom = max(r[1] for r in stat_rects)

    pages = []
    for kind in kinds:
        blocks = []
        for b in spec.blocks:
            if b.is_stat and b.kind != kind:
                blocks.append(replace(b, visible=False))
            elif b.is_stat and b.kind == kind:
                blocks.append(replace(b, y=top, h=max(0.02, bottom - top)))
            else:
                blocks.append(b)
        pages.append(replace(spec, blocks=blocks))
    return pages


def layout_page_specs(lay: Layout, cycle_pages=()) -> dict:
    """{side: [PanelSpec, ...]} — the page list for every panel in `lay`."""
    return {p.side: panel_page_specs(p, cycle_pages)
            for p in lay.ordered_panels()}


def max_pages(lay: Layout, cycle_pages=()) -> int:
    pages = layout_page_specs(lay, cycle_pages)
    return max([len(v) for v in pages.values()] or [1])
