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
PANEL_SECTIONS = ("header", "players", "opponents", "teams",
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
