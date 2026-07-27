# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""Render the battle-info side panel as a PNG (text only, no artwork).

render_panel(info, extras, size) draws everything rec2mp4 knows about a
conversion — facility/level-mode header, streak, recorder and players,
opponent display names, both teams (species names read from the USER'S ROM
at runtime via romdata.species_name), PokeDNA export-sidecar lines, and an
outcome/duration/seed footer — onto a dark background sized to sit beside
the game video (width = half the game width, same height).

Pillow is imported lazily: `--panel off` (and every non-panel code path)
works without it. When Pillow is missing, render_panel raises RuntimeError
with install instructions naming the rec2mp4 conda env. Only the
PIL-bundled default font is used (no OS font paths), so output is portable
across macOS / Windows / Linux. NO Game Freak artwork or shipped game text
— every game-derived string on the panel comes from the user's ROM or from
the user's own .rec file.
"""

from __future__ import annotations

import io

from . import romdata

# Sections the panel can draw, in draw order. --panel-info picks a subset.
PANEL_SECTIONS = ("header", "players", "opponents", "teams", "export",
                  "footer")

# Colors (RGB) — dark, readable at 640 px height.
_BG = (16, 20, 26)
_FG = (232, 236, 240)
_DIM = (146, 156, 168)
_GOLD = (255, 203, 79)
_GREEN = (97, 211, 140)
_RED = (235, 110, 110)
_RULE = (44, 52, 62)

_OUTCOME_COLORS = {"won": _GREEN, "lost": _RED, "draw": _GOLD}

_MAX_EXPORT_LINES = 6          # "first ~6 short lines" of the .txt sidecar
_MAX_EXPORT_LINE_CHARS = 60    # a longer line is not "short" — skipped


def parse_panel_info(csv: str | None) -> tuple[str, ...]:
    """--panel-info CSV -> ordered tuple of section names.

    'all', '' and None select every section. Unknown names raise ValueError
    (callers surface this as a CLI/pipeline error before any emulation).
    """
    if csv is None:
        return PANEL_SECTIONS
    text = str(csv).strip().lower()
    if text in ("", "all"):
        return PANEL_SECTIONS
    tokens = [t.strip() for t in text.split(",") if t.strip()]
    unknown = [t for t in tokens if t not in PANEL_SECTIONS]
    if unknown:
        raise ValueError(
            "unknown --panel-info section(s): %s (valid: %s, or 'all')"
            % (", ".join(unknown), ", ".join(PANEL_SECTIONS)))
    if not tokens:
        return PANEL_SECTIONS
    # keep PANEL_SECTIONS draw order, drop duplicates
    return tuple(s for s in PANEL_SECTIONS if s in tokens)


def _require_pil():
    """Import Pillow lazily; raise a clear, actionable error when absent."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError(
            "the side panel needs Pillow, which this Python does not have. "
            "Install it in the rec2mp4 conda env:\n"
            "  ~/miniconda3/envs/rec2mp4/bin/python "
            "-m pip install pillow\n"
            "(or into whatever Python you run rec2mp4 with), "
            "or convert with --panel off.") from exc
    return Image, ImageDraw, ImageFont


def _font(image_font_mod, px: int):
    """PIL-bundled default font at ~px pixels; portable (no OS font paths).

    Pillow >= 10.1 scales its bundled font via load_default(size=...);
    older Pillows fall back to the tiny fixed bitmap font.
    """
    try:
        return image_font_mod.load_default(size=px)
    except TypeError:                       # Pillow < 10.1
        return image_font_mod.load_default()


def _mmss(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return "%d:%02d" % (s // 60, s % 60)


def _mon_display_name(mon: dict, rom_bytes) -> tuple[str, str | None]:
    """(species display name, nickname-if-different) for one team mon."""
    sid = mon.get("species_internal", 0)
    species = romdata.species_name(rom_bytes, sid) if rom_bytes else None
    if not species or not species.strip("?"):
        species = "#%s" % sid               # unknown / OLD_UNOWN hole
    nick = (mon.get("nickname") or "").strip()
    if nick and nick.upper() != species.upper():
        return species, nick
    return species, None


def render_panel(info: dict, extras: dict, size: tuple[int, int]) -> bytes:
    """Render the side panel -> PNG bytes.

    info   — a rec.parse() dict (teams, facility, players, flags, seed ...).
    extras — everything only known outside the record:
        rom_bytes        bytes|None  user's ROM (species/opponent names)
        outcome_text     str         'won' / 'lost' / ... / 'unknown'
        duration_seconds float       replay length
        streak           int|None    streak parsed from the export filename
        export_lines     list[str]   lines of PokeDNA's <stem>.txt sidecar
        sections         tuple[str]  parse_panel_info() result (default all)
        opponent_a_label str|None    resolved display name (pipeline)
        opponent_b_label str|None
    size   — (width, height) in pixels; the pipeline passes
             (scale*120, scale*160) so the panel is half the game's width
             at the same height.
    """
    Image, ImageDraw, ImageFont = _require_pil()

    w, h = int(size[0]), int(size[1])
    if w < 60 or h < 80:
        raise ValueError("panel size %dx%d too small to render" % (w, h))

    sections = tuple(extras.get("sections") or PANEL_SECTIONS)
    rom_bytes = extras.get("rom_bytes")

    img = Image.new("RGB", (w, h), _BG)
    draw = ImageDraw.Draw(img)

    # Type scale relative to the panel height (readable at h=640).
    px_head = max(10, h // 23)              # 27 at 640
    px_body = max(8, h // 34)               # 18 at 640
    px_small = max(7, h // 46)              # 13 at 640
    f_head = _font(ImageFont, px_head)
    f_body = _font(ImageFont, px_body)
    f_small = _font(ImageFont, px_small)

    margin = max(6, w // 24)
    max_w = w - 2 * margin
    y = margin

    footer_h = (int(px_body * 1.5) + int(px_small * 1.6) + margin) \
        if "footer" in sections else 0
    y_limit = h - footer_h - margin

    def ellipsize(text: str, font) -> str:
        if draw.textlength(text, font=font) <= max_w:
            return text
        while text and draw.textlength(text + "...", font=font) > max_w:
            text = text[:-1]
        return text + "..."

    def line(text: str, font, px: int, color=_FG, extra_gap=0.0) -> bool:
        """Draw one text line at the cursor; False when out of room."""
        nonlocal y
        step = int(px * 1.45)
        if y + step > y_limit:
            return False
        draw.text((margin, y), ellipsize(text, font), font=font, fill=color)
        y += step + int(px * extra_gap)
        return True

    def rule():
        nonlocal y
        gap = max(4, px_small // 2)
        if y + gap * 2 > y_limit:
            return
        draw.line([(margin, y + gap), (w - margin, y + gap)],
                  fill=_RULE, width=1)
        y += gap * 2 + 1

    # ----------------------------------------------------------- header
    if "header" in sections:
        line(info.get("facility", "?"), f_head, px_head, _FG)
        kinds = [label for label, key in
                 (("Double", "is_double"), ("Multi", "is_multi"),
                  ("Two opponents", "is_two_opponents"),
                  ("Link", "is_link_recorded")) if info.get(key)]
        sub = info.get("level_mode", "?")
        if kinds:
            sub += " - " + ", ".join(kinds)
        line(sub, f_body, px_body, _DIM)
        streak = extras.get("streak")
        if streak is not None:
            line("Streak %d" % streak, f_body, px_body, _GOLD)
        rule()

    # ---------------------------------------------------------- players
    if "players" in sections:
        by = info.get("recorded_by") or "?"
        gender = info.get("recorded_by_gender") or "?"
        rec_line = "Recorded by %s" % by
        if gender in ("M", "F"):
            rec_line += " (%s)" % gender
        langs = info.get("players_language") or []
        if langs and info.get("multiplayer_id", 0) == 0:
            rec_line += ", %s" % langs[0]
        line(rec_line, f_body, px_body, _FG)
        players = [p for p in info.get("players", []) if p and p != by]
        if players:
            line("With " + ", ".join(players), f_small, px_small, _DIM)
        rule()

    # -------------------------------------------------------- opponents
    if "opponents" in sections:
        opp_a = (extras.get("opponent_a_label")
                 or info.get("opponent_a_name")
                 or "#%s" % info.get("opponent_a", "?"))
        line("vs %s" % opp_a, f_body, px_body, _FG)
        opp_b = extras.get("opponent_b_label")
        if opp_b is None and info.get("opponent_b_kind"):
            opp_b = info.get("opponent_b_name")
        if opp_b:
            line("and %s" % opp_b, f_body, px_body, _FG)
        rule()

    # ------------------------------------------------------------ teams
    if "teams" in sections:
        titles = {"player": (info.get("recorded_by") or "Player").strip()
                  or "Player", "opponent": "Opponent"}
        for side in ("player", "opponent"):
            mons = (info.get("teams") or {}).get(side) or []
            if not line(titles[side].upper(), f_small, px_small, _DIM):
                break
            for m in mons:
                species, nick = _mon_display_name(m, rom_bytes)
                text = species
                if nick:
                    text += " (%s)" % nick
                text += "  Lv%s" % m.get("level", "?")
                shown = ellipsize(text, f_body)
                step = int(px_body * 1.45)
                if y + step > y_limit:
                    break
                draw.text((margin, y), shown, font=f_body, fill=_FG)
                if m.get("shiny"):
                    x_star = margin + draw.textlength(shown, font=f_body) \
                        + px_body // 2
                    draw.text((x_star, y), "*", font=f_body, fill=_GOLD)
                y += step
            y += px_small // 2
        rule()

    # ----------------------------------------------------------- export
    if "export" in sections:
        shown = 0
        for raw in extras.get("export_lines") or []:
            text = str(raw).strip()
            if not text or len(text) > _MAX_EXPORT_LINE_CHARS:
                continue
            if shown >= _MAX_EXPORT_LINES:
                break
            if not line(text, f_small, px_small, _DIM):
                break
            shown += 1
        if shown:
            rule()

    # ----------------------------------------------------------- footer
    if "footer" in sections:
        outcome = (extras.get("outcome_text") or "unknown").strip()
        color = _OUTCOME_COLORS.get(outcome, _DIM)
        fy = h - margin - int(px_small * 1.6) - int(px_body * 1.5)
        text = outcome.upper()
        dur = extras.get("duration_seconds")
        if dur is not None:
            text += "  -  %s" % _mmss(float(dur))
        draw.text((margin, fy), ellipsize(text, f_body),
                  font=f_body, fill=color)
        fy += int(px_body * 1.5)
        draw.text((margin, fy),
                  ellipsize("seed %s" % info.get("rng_seed", "?"), f_small),
                  font=f_small, fill=_DIM)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
