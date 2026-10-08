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

from . import rec, romdata
from .rec import STAT_KEYS, STAT_LABELS

# Sections the panel can draw, in draw order. --panel-info picks a subset.
# The three per-mon stat blocks (moves / evs / ivs) sit right after "teams";
# they are also the pages the optional time-cycling panel flips through.
PANEL_SECTIONS = ("header", "players", "trainer", "opponents", "teams",
                  "moves", "evs", "ivs", "export", "footer")

# The subset of PANEL_SECTIONS that show decoded per-mon battle stats. These
# are what --panel-cycle rotates through over time (one page each).
STAT_PAGE_SECTIONS = ("moves", "evs", "ivs")

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


def parse_cycle_pages(csv) -> tuple[str, ...]:
    """--panel-cycle-pages CSV -> ordered tuple of stat page names.

    'all', '' and None select all three stat pages (moves, evs, ivs). Unknown
    names raise ValueError (surfaced as a CLI/pipeline error before emulation).
    """
    if csv is None:
        return STAT_PAGE_SECTIONS
    if isinstance(csv, (tuple, list)):
        tokens = [str(t).strip().lower() for t in csv if str(t).strip()]
    else:
        text = str(csv).strip().lower()
        if text in ("", "all"):
            return STAT_PAGE_SECTIONS
        tokens = [t.strip() for t in text.split(",") if t.strip()]
    if not tokens:
        return STAT_PAGE_SECTIONS
    unknown = [t for t in tokens if t not in STAT_PAGE_SECTIONS]
    if unknown:
        raise ValueError(
            "unknown --panel-cycle-pages page(s): %s (valid: %s, or 'all')"
            % (", ".join(unknown), ", ".join(STAT_PAGE_SECTIONS)))
    return tuple(s for s in STAT_PAGE_SECTIONS if s in tokens)


def _require_pil():
    """Import Pillow lazily; raise a clear, actionable error when absent."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError(
            "the side panel needs Pillow, which this Python does not have. "
            "Install it into the Python you run rec2mp4 with:\n"
            "  python -m pip install pillow\n"
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


# --- the trainer's save state (PokeDNA's '<stem>.txt' 'state.*' block) -----
# docs/REC-SIDECAR.md. Every key is optional and a missing key is NEVER shown
# as a zero: "0 BP" and "this game has no Battle Points" are different facts.

_SYMBOL_COLORS = {"none": (58, 66, 78), "silver": (198, 206, 216),
                  "gold": (255, 203, 79)}


def trainer_lines(info: dict, extras: dict) -> list[tuple]:
    """Styled rows for the trainer-state section, or [] with no sidecar.

    The 'symbols' row carries the raw 7-character Frontier-Pass string; the
    renderers draw it as seven pips (Tower..Pyramid) rather than as text.
    """
    state = extras.get("state") or {}
    if not state:
        return []
    rows: list[tuple] = []
    who = (info.get("recorded_by") or "").strip()
    if who:
        rows.append((who, "title"))
    if state.get("playtime"):
        rows.append(("%s played" % state["playtime"], "body"))
    if "dex_seen" in state or "dex_caught" in state:
        parts = []
        if "dex_seen" in state:
            parts.append("%d seen" % state["dex_seen"])
        if "dex_caught" in state:
            parts.append("%d caught" % state["dex_caught"])
        rows.append(("Pokedex  " + " / ".join(parts), "body"))
    if "bp" in state:
        rows.append(("Battle Points  %d" % state["bp"], "accent"))
    if state.get("symbols"):
        rows.append((state["symbols"], "symbols"))
    return rows


def _draw_symbols(draw, text: str, x: int, y: int, width: int, px: int,
                  colors=None) -> None:
    """Seven Frontier symbols as pips, in Frontier Pass order, left aligned.

    '-' none (dark), 's' silver, 'G' gold — exactly the encoding in
    docs/REC-SIDECAR.md, drawn so silver/gold read at a glance.
    """
    colors = colors or _SYMBOL_COLORS
    n = max(1, len(text))
    d = max(3, int(px * 0.9))
    gap = max(2, int(d * 0.45))
    total = n * d + (n - 1) * gap
    if total > width and width > 0:                 # squeeze to fit the box
        d = max(3, int((width - (n - 1) * 2) / n))
        gap = 2
    for i, ch in enumerate(text):
        kind = {"s": "silver", "G": "gold"}.get(ch, "none")
        cx = x + i * (d + gap)
        draw.ellipse([cx, y, cx + d, y + d],
                     fill=colors.get(kind, colors["none"]),
                     outline=(90, 100, 115) if kind == "none" else None)


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


def _ev_value_str(ev: dict) -> str:
    """'HP 0  Atk 252  Def 0  SpA 0  SpD 4  Spe 252' from an EV dict."""
    return "  ".join("%s %d" % (STAT_LABELS[k], ev[k]) for k in STAT_KEYS)


def _iv_value_str(iv: dict) -> str:
    """Same layout as EVs, but a perfect (31) IV is flagged with a '*'."""
    return "  ".join("%s %d%s" % (STAT_LABELS[k], iv[k],
                                  "*" if iv[k] == 31 else "")
                     for k in STAT_KEYS)


def _move_labels(mon: dict, rom_bytes) -> list[str]:
    """Move names for one mon, read from the ROM; 'Move #id' when unknown."""
    labels = []
    for mv in mon.get("moves", []):
        name = romdata.move_name(rom_bytes, mv["id"]) if rom_bytes else None
        labels.append(name or ("Move #%d" % mv["id"]))
    return labels


def stat_page_lines(info: dict, extras: dict, kind: str) -> list[tuple]:
    """Styled text lines for one stat block (both teams), as (text, role).

    Pure logic — no Pillow. render_panel draws these with role-specific
    fonts/colors; tests use stat_manifest() (the plain-text projection) to
    assert content like the 'Sum NNN/510' / 'Sum NNN/186' totals and the
    ROM-resolved move names. Roles:
        title       -> dim section header ("<PLAYER> MOVES")
        name        -> a mon name line (moves page)
        moves       -> that mon's comma-joined move names
        namesum     -> "<name>  Sum NNN/510|186[ PERFECT]" (sum drawn bold)
        values      -> the six EV/IV values
        unavailable -> "<name>  (stats unavailable)" (checksum not ok)
    kind is one of STAT_PAGE_SECTIONS.
    """
    rom_bytes = extras.get("rom_bytes")
    titles = {"player": (info.get("recorded_by") or "Player").strip()
              or "Player", "opponent": "Opponent"}
    suffix = {"moves": "MOVES", "evs": "EVs", "ivs": "IVs"}[kind]
    out: list[tuple] = []
    for side in ("player", "opponent"):
        mons = (info.get("teams") or {}).get(side) or []
        if not mons:
            continue
        out.append((titles[side].upper() + " " + suffix, "title"))
        for m in mons:
            species, nick = _mon_display_name(m, rom_bytes)
            # Prefer the ROM species name; when the species is an unknown
            # "#id" placeholder, fall back to the mon's nickname so the row
            # still names something recognisable.
            if species.startswith("#") and nick:
                species = nick
            # The moves/evs/ivs keys are present ONLY when the mon's checksum
            # verified (see rec._decode_mon) — their absence means "untrusted".
            if kind not in m:
                out.append(("%s  (stats unavailable)" % species,
                            "unavailable"))
                continue
            if kind == "moves":
                names = _move_labels(m, rom_bytes)
                out.append(("%s:" % species, "name"))
                out.append(("  " + (", ".join(names) or "(no moves)"),
                            "moves"))
            elif kind == "evs":
                ev = m["evs"]
                out.append(("%s  Sum %d/510" % (species, ev["sum"]),
                            "namesum"))
                out.append(("  " + _ev_value_str(ev), "values"))
            else:                               # ivs
                iv = m["ivs"]
                perfect = " PERFECT" if iv["sum"] == 186 else ""
                out.append(("%s  Sum %d/186%s" % (species, iv["sum"], perfect),
                            "namesum"))
                out.append(("  " + _iv_value_str(iv), "values"))
    return out


def stat_manifest(info: dict, extras: dict, kind: str) -> list[str]:
    """Plain-text projection of stat_page_lines() — the exact strings the
    stat block draws, for tests / a debug manifest (no Pillow needed)."""
    return [text for text, _ in stat_page_lines(info, extras, kind)]


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
        pov              str         'player' | 'opponent' (header note)
        pov_faithful     bool        opponent POV faithful (link record)?
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
        if extras.get("pov") == "opponent":
            line("Opponent POV (experimental)", f_small, px_small, _RED)
            if not extras.get("pov_faithful"):
                line("what-if: replay diverges", f_small, px_small, _DIM)
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

    # ---------------------------------------------- trainer (save state)
    if "trainer" in sections:
        rows = trainer_lines(info, extras)
        for text, role in rows:
            if role == "symbols":
                step = int(px_body * 1.45)
                if y + step > y_limit:
                    break
                _draw_symbols(draw, text, margin, y, max_w, px_body)
                y += step
            elif role == "title":
                continue                    # the players line already names them
            else:
                colour = _GOLD if role == "accent" else _FG
                if not line(text, f_small, px_small, colour):
                    break
        if rows:
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

    # ----------------------------------------------- stats (moves/evs/ivs)
    def draw_stat_section(kind: str):
        """Draw one per-mon stat block, shrinking the font so both teams'
        actual mons fit before falling back to line()'s truncation."""
        nonlocal y
        rows = stat_page_lines(info, extras, kind)
        if not rows:
            return
        # Adaptive font: budget the remaining height across all rows so a
        # full 6v6 still fits; never smaller than px_small*0.62 (still legible)
        # and never larger than px_small (the panel's body-detail size).
        avail = max(0, y_limit - y)
        base_step = int(px_small * 1.45)
        needed = len(rows) * base_step
        spx = px_small
        if needed > avail and avail > 0:
            spx = max(int(px_small * 0.62),
                      int(px_small * avail / needed))
        sfont = _font(ImageFont, spx)
        step = int(spx * 1.45)

        for text, role in rows:
            if y + step > y_limit:
                break
            if role == "namesum":
                # "<name>  Sum NNN/510" — draw the name plain, the sum bold+gold
                # (drawn first-protected: the name is ellipsized to leave room).
                idx = text.find("  Sum ")
                name_part = text[:idx] if idx >= 0 else text
                sum_part = text[idx + 2:] if idx >= 0 else ""
                sum_w = draw.textlength(sum_part, font=sfont) if sum_part else 0
                gap = spx
                name_budget = max_w - int(sum_w) - gap
                shown = name_part
                while shown and draw.textlength(
                        shown + "...", font=sfont) > name_budget:
                    shown = shown[:-1]
                if shown != name_part and shown:
                    shown += "..."
                draw.text((margin, y), shown, font=sfont, fill=_FG)
                if sum_part:
                    sx = margin + max_w - int(sum_w)
                    perfect = "PERFECT" in sum_part
                    col = _GOLD if perfect else _GREEN
                    draw.text((sx, y), sum_part, font=sfont, fill=col)
                    draw.text((sx + 1, y), sum_part, font=sfont, fill=col)
                y += step
            else:
                color = _DIM if role in ("title", "unavailable") else _FG
                draw.text((margin, y), ellipsize(text, sfont),
                          font=sfont, fill=color)
                y += step
        y += px_small // 2

    stat_drawn = False
    for _kind in STAT_PAGE_SECTIONS:
        if _kind in sections:
            draw_stat_section(_kind)
            stat_drawn = True
    if stat_drawn:
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


# ===========================================================================
# Free-form block rendering (rec2mp4.layout) — the designer's renderer
# ===========================================================================
#
# render_panel() above is the classic fixed vertical flow. Everything below
# draws the SAME content into user-placed rectangles ("information windows")
# described by a rec2mp4.layout.PanelSpec: any side (left/right/top/bottom),
# per-block fonts/colours/backgrounds, an optional background image. The two
# renderers share the data (`stat_page_lines`) but not the geometry, so the
# classic panel's output is untouched by anything here.

# role -> (relative size, colour key). The colour keys resolve against a
# theme dict built per block (so a block can override body/title/accent).
_ROLE_STYLE = {
    "head":        (1.55, "fg"),
    "sub":         (1.00, "dim"),
    "accent":      (1.00, "accent"),
    "warn":        (0.85, "warn"),
    "body":        (1.00, "fg"),
    "small":       (0.80, "dim"),
    "title":       (0.80, "title"),
    "mon":         (1.00, "fg"),
    "mon_shiny":   (1.00, "fg"),
    "name":        (1.00, "fg"),
    "moves":       (0.92, "fg"),
    "namesum":     (1.00, "fg"),
    "values":      (0.92, "fg"),
    "unavailable": (0.92, "dim"),
    "outcome":     (1.20, "outcome"),
    "symbols":     (1.10, "fg"),
    "quote":       (1.15, "fg"),
    "blank":       (0.60, "dim"),
}

_MIN_BLOCK_PX = 7          # below this nothing is legible — clip instead
_MAX_BLOCK_PX = 96         # a one-line title block should not fill the screen


def _role_style(role: str) -> tuple[float, str]:
    return _ROLE_STYLE.get(role, (1.0, "fg"))


def section_lines(kind: str, info: dict, extras: dict) -> list[tuple]:
    """Styled rows (text, role) for one block kind — the block renderer's
    content source. Pure logic, no Pillow; tests assert on it directly.

    Mirrors what render_panel draws for the same section, minus the flow
    layout's rules/spacing (a block has its own box).
    """
    rom_bytes = extras.get("rom_bytes")
    rows: list[tuple] = []

    if kind in STAT_PAGE_SECTIONS:
        return stat_page_lines(info, extras, kind)

    if kind == "header":
        rows.append((info.get("facility", "?"), "head"))
        kinds = [label for label, key in
                 (("Double", "is_double"), ("Multi", "is_multi"),
                  ("Two opponents", "is_two_opponents"),
                  ("Link", "is_link_recorded")) if info.get(key)]
        sub = info.get("level_mode", "?")
        if kinds:
            sub += " - " + ", ".join(kinds)
        rows.append((sub, "sub"))
        streak = extras.get("streak")
        if streak is not None:
            rows.append(("Streak %d" % streak, "accent"))
        if extras.get("pov") == "opponent":
            rows.append(("Opponent POV (experimental)", "warn"))
            if not extras.get("pov_faithful"):
                rows.append(("what-if: replay diverges", "small"))
        return rows

    if kind == "players":
        by = info.get("recorded_by") or "?"
        gender = info.get("recorded_by_gender") or "?"
        text = "Recorded by %s" % by
        if gender in ("M", "F"):
            text += " (%s)" % gender
        langs = info.get("players_language") or []
        if langs and info.get("multiplayer_id", 0) == 0:
            text += ", %s" % langs[0]
        rows.append((text, "body"))
        others = [p for p in info.get("players", []) if p and p != by]
        if others:
            rows.append(("With " + ", ".join(others), "small"))
        return rows

    if kind == "trainer":
        return trainer_lines(info, extras)

    if kind == "opponents":
        opp_a = (extras.get("opponent_a_label")
                 or info.get("opponent_a_name")
                 or "#%s" % info.get("opponent_a", "?"))
        rows.append(("vs %s" % opp_a, "body"))
        opp_b = extras.get("opponent_b_label")
        if opp_b is None and info.get("opponent_b_kind"):
            opp_b = info.get("opponent_b_name")
        if opp_b:
            rows.append(("and %s" % opp_b, "body"))
        return rows

    if kind == "teams":
        titles = {"player": (info.get("recorded_by") or "Player").strip()
                  or "Player", "opponent": "Opponent"}
        for side in ("player", "opponent"):
            mons = (info.get("teams") or {}).get(side) or []
            if not mons:
                continue
            rows.append((titles[side].upper(), "title"))
            for m in mons:
                species, nick = _mon_display_name(m, rom_bytes)
                text = species
                if nick:
                    text += " (%s)" % nick
                text += "  Lv%s" % m.get("level", "?")
                rows.append((text, "mon_shiny" if m.get("shiny") else "mon"))
        return rows

    if kind == "export":
        shown = 0
        for raw in extras.get("export_lines") or []:
            text = str(raw).strip()
            if not text or len(text) > _MAX_EXPORT_LINE_CHARS:
                continue
            if shown >= _MAX_EXPORT_LINES:
                break
            rows.append((text, "small"))
            shown += 1
        return rows

    if kind == "footer":
        outcome = (extras.get("outcome_text") or "unknown").strip()
        text = outcome.upper()
        dur = extras.get("duration_seconds")
        if dur is not None:
            text += "  -  %s" % _mmss(float(dur))
        rows.append((text, "outcome"))
        rows.append(("seed %s" % info.get("rng_seed", "?"), "small"))
        return rows

    return rows


def _block_rows(block, info: dict, extras: dict) -> list[tuple]:
    """Rows for a block, honouring its own knobs (custom text, no caption)."""
    kind = getattr(block, "kind", "")
    if kind == "text":
        text = getattr(block, "text", "") or ""
        return [(ln, "body") if ln.strip() else ("", "blank")
                for ln in text.splitlines()] or [("", "blank")]
    if kind in ("rule", "frame"):
        return []
    rows = section_lines(kind, info, extras)
    if not getattr(block, "title", True):
        rows = [(t, r) for t, r in rows if r != "title"]
    return rows


def _block_theme(block, extras: dict) -> dict:
    """Colour table for one block: defaults, then the block's overrides."""
    from .layout import color_rgb            # local: layout imports panel
    outcome = (extras.get("outcome_text") or "unknown").strip()
    theme = {"fg": _FG, "dim": _DIM, "title": _DIM, "accent": _GOLD,
             "warn": _RED, "outcome": _OUTCOME_COLORS.get(outcome, _DIM)}
    if getattr(block, "color", None):
        theme["fg"] = color_rgb(block.color, _FG)
    if getattr(block, "title_color", None):
        theme["title"] = theme["dim"] = color_rgb(block.title_color, _DIM)
    if getattr(block, "accent_color", None):
        acc = color_rgb(block.accent_color, _GOLD)
        theme["accent"] = acc
        # A custom accent also recolours the footer outcome + EV/IV sums, so
        # one designer control retints every highlight in the block.
        theme["outcome"] = acc
        theme["sum"] = acc
    return theme


def _wrap_row(draw, text: str, font, max_w: int) -> list[str]:
    """Greedy word wrap; a single over-long word is split mid-word."""
    if max_w <= 0 or not text:
        return [text]
    if draw.textlength(text, font=font) <= max_w:
        return [text]
    out: list[str] = []
    line = ""
    for word in text.split(" "):
        cand = word if not line else line + " " + word
        if draw.textlength(cand, font=font) <= max_w or not line:
            line = cand
            # a lone word wider than the box: hard-split it
            while draw.textlength(line, font=font) > max_w and len(line) > 1:
                cut = len(line) - 1
                while cut > 1 and draw.textlength(line[:cut],
                                                  font=font) > max_w:
                    cut -= 1
                out.append(line[:cut])
                line = line[cut:]
        else:
            out.append(line)
            line = word
    if line:
        out.append(line)
    return out


def _ellipsize(draw, text: str, font, max_w: int) -> str:
    if max_w <= 0 or draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "...", font=font) > max_w:
        text = text[:-1]
    return text + "..."


def _fit_block(draw, ImageFont, rows: list[tuple], inner: tuple,
               block) -> tuple:
    """Choose the font size for a block and lay its rows out.

    Returns (font_px, laid_rows) where laid_rows is [(text, role, font)] —
    already wrapped when block.wrap is set. The size starts from "fill the
    box vertically" (so text grows with the rectangle, which is what a
    drag-to-resize designer should do), is then scaled by block.font_scale,
    and finally shrunk until it fits when block.fit == 'shrink'.
    """
    inner_w, inner_h = inner
    gap = float(getattr(block, "line_gap", 1.45)) or 1.45
    scale = float(getattr(block, "font_scale", 1.0) or 1.0)
    wrap = bool(getattr(block, "wrap", False))
    shrink = getattr(block, "fit", "shrink") != "clip"
    if not rows:
        return 0, []

    def weight(rs):
        return sum(_role_style(r)[0] for _t, r in rs) or 1.0

    px = int(inner_h / (gap * weight(rows)))
    px = max(_MIN_BLOCK_PX, min(_MAX_BLOCK_PX, px))
    px = max(_MIN_BLOCK_PX, min(_MAX_BLOCK_PX, int(round(px * scale))))

    laid: list[tuple] = []
    for _ in range(8):
        font_cache: dict[float, object] = {}

        def font_for(rel, _cache=font_cache, _px=px):
            key = round(rel, 3)
            if key not in _cache:
                _cache[key] = _font(ImageFont,
                                    max(_MIN_BLOCK_PX, int(round(_px * rel))))
            return _cache[key]

        laid = []
        for text, role in rows:
            rel = _role_style(role)[0]
            fnt = font_for(rel)
            if wrap and text:
                for piece in _wrap_row(draw, text, fnt, inner_w):
                    laid.append((piece, role, fnt))
            else:
                laid.append((text, role, fnt))
        need = sum(gap * px * _role_style(r)[0] for _t, r, _f in laid)
        too_wide = 0
        if not wrap:
            for text, role, fnt in laid:
                if text:
                    too_wide = max(too_wide,
                                   int(draw.textlength(text, font=fnt)))
        if not shrink:
            break
        over_h = need > inner_h
        over_w = too_wide > inner_w > 0
        if not over_h and not over_w:
            break
        if px <= _MIN_BLOCK_PX:
            break
        factor = 1.0
        if over_h:
            factor = min(factor, inner_h / need)
        if over_w:
            factor = min(factor, inner_w / too_wide)
        nxt = int(px * factor)
        px = max(_MIN_BLOCK_PX, min(px - 1, nxt))
    return px, laid


def _load_background(Image, spec, size: tuple, warn=None):
    """Panel background: solid colour, optionally an image on top of it."""
    from .layout import color_rgb
    w, h = size
    base = Image.new("RGB", (w, h), color_rgb(getattr(spec, "bg", None),
                                              _BG))
    path = getattr(spec, "bg_image", None)
    if not path:
        return base
    try:
        src = Image.open(path)
        src.load()
        src = src.convert("RGB")
    except Exception as exc:                       # missing / not an image
        if warn:
            warn("panel background image unusable (%s): %s"
                 % (path, type(exc).__name__))
        return base
    mode = getattr(spec, "bg_mode", "cover")
    layer = Image.new("RGB", (w, h), color_rgb(getattr(spec, "bg", None), _BG))
    sw, sh = src.size
    if sw <= 0 or sh <= 0:
        return base
    if mode == "stretch":
        layer = src.resize((w, h))
    elif mode == "tile":
        for oy in range(0, h, sh):
            for ox in range(0, w, sw):
                layer.paste(src, (ox, oy))
    elif mode == "center":
        layer.paste(src, ((w - sw) // 2, (h - sh) // 2))
    else:                                          # cover / contain
        ratio = max(w / sw, h / sh) if mode == "cover" else min(w / sw, h / sh)
        nw, nh = max(1, int(round(sw * ratio))), max(1, int(round(sh * ratio)))
        resized = src.resize((nw, nh))
        layer.paste(resized, ((w - nw) // 2, (h - nh) // 2))

    dim = float(getattr(spec, "bg_dim", 0.0) or 0.0)
    if dim > 0:
        black = Image.new("RGB", (w, h), (0, 0, 0))
        layer = Image.blend(layer, black, min(1.0, dim))
    opacity = float(getattr(spec, "bg_opacity", 1.0))
    if opacity >= 1.0:
        return layer
    if opacity <= 0.0:
        return base
    return Image.blend(base, layer, opacity)


def render_layout_panel_image(info: dict, extras: dict, spec,
                              size: tuple[int, int], warn=None):
    """Render one PanelSpec to a PIL Image (RGB). See render_layout_panel."""
    Image, ImageDraw, ImageFont = _require_pil()
    from .layout import PanelSpec, color_rgb
    if isinstance(spec, dict):
        spec = PanelSpec.from_dict(spec)

    w, h = int(size[0]), int(size[1])
    if w < 16 or h < 16:
        raise ValueError("panel size %dx%d too small to render" % (w, h))

    img = _load_background(Image, spec, (w, h), warn=warn)
    draw = ImageDraw.Draw(img)

    for block in getattr(spec, "blocks", []):
        if not getattr(block, "visible", True):
            continue
        left, top, right, bottom = block.rect_px((w, h))
        bw, bh = right - left, bottom - top
        if bw < 4 or bh < 4:
            continue

        # ---- block background / border
        radius = int(getattr(block, "radius", 0) or 0)
        bg = getattr(block, "bg", None)
        border = getattr(block, "border", None)
        bwidth = int(getattr(block, "border_width", 1) or 0)
        if bg:
            alpha = int(round(255 * float(getattr(block, "bg_opacity", 1.0))))
            if alpha > 0:
                overlay = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
                od = ImageDraw.Draw(overlay)
                fill = color_rgb(bg, _BG) + (alpha,)
                if radius > 0:
                    od.rounded_rectangle([0, 0, bw - 1, bh - 1],
                                         radius=min(radius, bw // 2, bh // 2),
                                         fill=fill)
                else:
                    od.rectangle([0, 0, bw - 1, bh - 1], fill=fill)
                img.paste(Image.alpha_composite(
                    img.crop((left, top, right, bottom)).convert("RGBA"),
                    overlay).convert("RGB"), (left, top))
        if border and bwidth > 0:
            box = [left, top, right - 1, bottom - 1]
            if radius > 0:
                draw.rounded_rectangle(
                    box, radius=min(radius, bw // 2, bh // 2),
                    outline=color_rgb(border, _RULE), width=bwidth)
            else:
                draw.rectangle(box, outline=color_rgb(border, _RULE),
                               width=bwidth)

        if block.kind == "frame":
            continue
        if block.kind == "rule":
            col = color_rgb(getattr(block, "color", None) or "", _RULE) \
                if getattr(block, "color", None) else _RULE
            ry = top + bh // 2
            pad = int(bw * float(getattr(block, "padding", 0.03)))
            draw.line([(left + pad, ry), (right - pad, ry)], fill=col,
                      width=max(1, bwidth))
            continue

        rows = _block_rows(block, info, extras)
        if not rows:
            continue
        pad_x = int(bw * float(getattr(block, "padding", 0.03)))
        pad_y = max(1, pad_x // 2)
        inner_w = max(1, bw - 2 * pad_x)
        inner_h = max(1, bh - 2 * pad_y)
        px, laid = _fit_block(draw, ImageFont, rows, (inner_w, inner_h), block)
        if not laid:
            continue

        theme = _block_theme(block, extras)
        gap = float(getattr(block, "line_gap", 1.45)) or 1.45
        total = sum(gap * px * _role_style(r)[0] for _t, r, _f in laid)
        valign = getattr(block, "valign", "top")
        if valign == "middle":
            y = top + pad_y + max(0, (inner_h - total) / 2.0)
        elif valign == "bottom":
            y = top + pad_y + max(0, inner_h - total)
        else:
            y = top + pad_y
        align = getattr(block, "align", "left")
        x_left, x_right = left + pad_x, right - pad_x

        drawn = 0
        for text, role, fnt in laid:
            rel = _role_style(role)[0]
            step = gap * px * rel
            if y + step > bottom - pad_y + 1:
                break                              # hard clip at the box edge
            drawn += 1
            if role == "symbols" and text:
                # Frontier symbols are pips, not characters (REC-SIDECAR.md).
                _draw_symbols(draw, text, x_left, int(y), inner_w,
                              int(px * rel))
                y += step
                continue
            if text:
                shown = text if getattr(block, "wrap", False) \
                    else _ellipsize(draw, text, fnt, inner_w)
                colour = theme.get(_role_style(role)[1], _FG)
                tw = int(draw.textlength(shown, font=fnt))
                if align == "center":
                    tx = x_left + max(0, (inner_w - tw) // 2)
                elif align == "right":
                    tx = max(x_left, x_right - tw)
                else:
                    tx = x_left
                if role == "namesum":
                    # "<name>  Sum NNN/510": name left, sum right in the
                    # accent colour (matches the classic panel's emphasis).
                    idx = text.find("  Sum ")
                    if idx >= 0:
                        name_part, sum_part = text[:idx], text[idx + 2:]
                        sum_w = int(draw.textlength(sum_part, font=fnt))
                        budget = max(0, inner_w - sum_w - int(px * 0.5))
                        draw.text((x_left, y),
                                  _ellipsize(draw, name_part, fnt, budget),
                                  font=fnt, fill=colour)
                        acc = theme.get("sum") or (
                            _GOLD if "PERFECT" in sum_part else _GREEN)
                        draw.text((x_right - sum_w, y), sum_part, font=fnt,
                                  fill=acc)
                        y += step
                        continue
                draw.text((tx, y), shown, font=fnt, fill=colour)
                if role == "mon_shiny":
                    draw.text((min(x_right - int(px * 0.6), tx + tw
                                   + int(px * 0.4)), y),
                              "*", font=fnt, fill=theme.get("accent", _GOLD))
            y += step

        # A block too small for its content must SAY so — silent truncation
        # is exactly how the old fixed-height panel hid half a team.
        if drawn < len(laid):
            mark = "+%d" % (len(laid) - drawn)
            mfont = _font(ImageFont, max(_MIN_BLOCK_PX, int(px * 0.8)))
            mw = int(draw.textlength(mark, font=mfont))
            draw.text((max(left, right - pad_x - mw),
                       bottom - pad_y - int(px * 0.9)), mark, font=mfont,
                      fill=theme.get("accent", _GOLD))

    return img


def render_layout_panel(info: dict, extras: dict, spec,
                        size: tuple[int, int], warn=None) -> bytes:
    """Render one PanelSpec (or its dict form) to PNG bytes.

    info/extras are exactly what render_panel takes; `spec` is a
    rec2mp4.layout.PanelSpec; `size` is the panel's pixel size from
    Layout.panel_size_px(). `warn(str)` (optional) receives non-fatal
    problems — currently an unusable background image, which degrades to
    the solid background colour instead of failing the conversion.
    """
    img = render_layout_panel_image(info, extras, spec, size, warn=warn)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def render_layout_pages(info: dict, extras: dict, specs, size,
                        warn=None) -> list[bytes]:
    """Render a panel's page list (layout.panel_page_specs) to PNG bytes."""
    return [render_layout_panel(info, extras, s, size, warn=warn)
            for s in specs]


# ---------------------------------------------------------------------------
# End card — the trainer's save state, held for the last seconds of the video
# ---------------------------------------------------------------------------

# One letter per facility, Frontier Pass order (matches state.symbols):
# Tower, Dome, Palace, Arena, Factory, Pike, Pyramid.
FACILITY_LABELS = ("T", "D", "P", "A", "F", "K", "Y")


def end_card_lines(info: dict, extras: dict) -> list[tuple]:
    """Rows for the end card: who this is, then their save state.

    Empty when there is no 'state.*' sidecar block — a video then simply ends
    where it always did (docs/REC-SIDECAR.md: a missing .txt is not an error).
    """
    state = extras.get("state") or {}
    if not state:
        return []
    rows: list[tuple] = []
    who = (info.get("recorded_by") or "").strip()
    gender = info.get("recorded_by_gender") or ""
    if who:
        rows.append((who + (" (%s)" % gender if gender in ("M", "F") else ""),
                     "head"))
    if state.get("playtime"):
        rows.append(("%s played" % state["playtime"], "body"))
    if "dex_seen" in state or "dex_caught" in state:
        parts = []
        if "dex_seen" in state:
            parts.append("%d seen" % state["dex_seen"])
        if "dex_caught" in state:
            parts.append("%d caught" % state["dex_caught"])
        rows.append(("Pokedex   " + " / ".join(parts), "body"))
    if "bp" in state:
        rows.append(("Battle Points   %d" % state["bp"], "accent"))
    if state.get("symbols"):
        rows.append(("Frontier Symbols", "small"))
        rows.append((state["symbols"], "symbols"))
    streak = extras.get("streak")
    if streak is not None:
        rows.append(("Streak %d" % streak, "accent"))
    return rows


def intro_card_lines(info: dict, extras: dict) -> list[tuple]:
    """Rows for the opening card: who you are about to fight, and their line.

    The pre-battle taunt is NOT part of the recorded battle (the record starts
    at the engine's "<TRAINER> would like to battle!"), but it IS in the ROM,
    keyed by the same opponent id the record carries — see
    romdata.frontier_trainer_speech. `extras['speech']` carries the decoded
    words; with none (a record-mix friend / apprentice, whose greeting lives
    in the SAVE, or a non-Emerald ROM) this returns [] and the caller skips
    the card entirely.
    """
    words = extras.get("speech") or []
    if not words:
        return []
    rows: list[tuple] = []
    opp = (extras.get("opponent_a_label") or info.get("opponent_a_name")
           or "").strip()
    if opp:
        rows.append(("VS " + opp, "head"))
    sub = "%s  -  %s" % (info.get("facility", "?"),
                         info.get("level_mode", "?"))
    rows.append((sub, "small"))
    # The game breaks its six easy-chat words after the third; keep that
    # rhythm so the line reads the way it does in-game.
    per_line = 3
    lines = [" ".join(words[i:i + per_line])
             for i in range(0, len(words), per_line)]
    for i, text in enumerate(lines):
        if len(lines) == 1:
            text = '"%s"' % text
        elif i == 0:
            text = '"%s' % text
        elif i == len(lines) - 1:
            text = '%s"' % text
        rows.append((text, "quote"))
    return rows


def render_intro_card(info: dict, extras: dict, size: tuple[int, int],
                      bg: str | None = None) -> bytes:
    """The opening card as PNG bytes, sized to the finished video's frame.

    Raises ValueError when there is no speech to show, so the caller can skip
    the whole stage (exactly like render_end_card).
    """
    rows = intro_card_lines(info, extras)
    if not rows:
        raise ValueError("no opponent speech to put on an intro card")
    return _render_card(rows, size, bg)


def render_end_card(info: dict, extras: dict, size: tuple[int, int],
                    bg: str | None = None) -> bytes:
    """The end card as PNG bytes, sized to the finished video's frame.

    Centred block: trainer, playtime, Pokedex, BP, the seven Frontier symbol
    pips (Tower..Pyramid) and the streak. Raises ValueError when there is
    nothing to show, so the caller can skip the whole stage.
    """
    rows = end_card_lines(info, extras)
    if not rows:
        raise ValueError("no trainer state to put on an end card")
    return _render_card(rows, size, bg)


def _render_card(rows: list[tuple], size: tuple[int, int],
                 bg: str | None = None) -> bytes:
    """Draw a full-frame card: centred rows, one shared look for every card."""
    Image, ImageDraw, ImageFont = _require_pil()
    from .layout import color_rgb

    w, h = int(size[0]), int(size[1])
    img = Image.new("RGB", (w, h), color_rgb(bg, _BG) if bg else _BG)
    draw = ImageDraw.Draw(img)

    px = max(10, min(h // 12, w // 26))
    gap = 1.6
    total = sum(px * gap * _role_style(r)[0] for _t, r in rows)
    y = (h - total) / 2.0
    x = w * 0.5
    label_font = _font(ImageFont, max(8, int(px * 0.55)))
    for text, role in rows:
        rel, key = _role_style(role)
        step = px * gap * rel
        font = _font(ImageFont, max(8, int(px * rel)))
        if role == "symbols":
            d = int(px * rel)
            n = len(text)
            span = n * d + (n - 1) * max(2, int(d * 0.45))
            _draw_symbols(draw, text, int(x - span / 2), int(y), span, d)
            # facility initials under the pips, so the row is self-explaining
            step_x = d + max(2, int(d * 0.45))
            for i, name in enumerate(FACILITY_LABELS[:n]):
                draw.text((x - span / 2 + i * step_x, y + d + 2), name,
                          font=label_font, fill=_DIM)
            y += step + px * 0.6
            continue
        colour = {"head": _FG, "accent": _GOLD, "small": _DIM,
                  "dim": _DIM, "quote": _FG}.get(role, _FG)
        tw = draw.textlength(text, font=font)
        draw.text((x - tw / 2, y), text, font=font, fill=colour)
        y += step

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def panel_pages(info: dict, extras: dict, size: tuple[int, int],
                cycle_sections) -> list[bytes]:
    """Render one PNG per requested stat page, sharing a static context.

    Each page keeps the same non-stat sections (header / players / opponents /
    teams / export / footer — whatever `extras['sections']` selected) and swaps
    in exactly ONE stat block (moves | evs | ivs), so a caller can flip through
    them over time. Returns a list of PNG byte strings in cycle order.

    cycle_sections — an iterable/CSV of STAT_PAGE_SECTIONS; empty/None means
    all three. Order follows STAT_PAGE_SECTIONS (moves, evs, ivs).
    """
    cycle = parse_cycle_pages(cycle_sections)
    base = tuple(s for s in (extras.get("sections") or PANEL_SECTIONS)
                 if s not in STAT_PAGE_SECTIONS)
    pages: list[bytes] = []
    for sec in cycle:
        page_sections = tuple(s for s in PANEL_SECTIONS
                              if s in base or s == sec)
        page_extras = dict(extras, sections=page_sections)
        pages.append(render_panel(info, page_extras, size))
    return pages
