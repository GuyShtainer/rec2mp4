#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""Tests for the panel LAYOUT engine, the designer's pure logic, the
parallel batch plumbing and PokeDNA's 'state.*' sidecar block.

Plain python, no pytest — same style as tests/test_rec.py / test_panel.py.
Exits non-zero on failure. Everything here is asset-free and runs on every
CI runner: no ROM, no save, no emulator, no ffmpeg. The Pillow sections
(block rendering, the end card) skip cleanly where Pillow is absent; the
tkinter designer section only uses its pure-logic helpers, so no display is
needed. Exit 2 marks a vacuous run (zero checks).

Run:  python3 tests/test_layout.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from rec2mp4 import designer, layout as L, panel, pipeline    # noqa: E402
from rec2mp4.pipeline import ConvertContext, ConvertSettings  # noqa: E402
from test_rec import build_synthetic_record                   # noqa: E402

_checks = 0


def ok(cond, msg):
    # No bare `assert`: it would be stripped under `python3 -O`.
    global _checks
    _checks += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def _pil_available() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def _raises(fn, exc=L.LayoutError):
    try:
        fn()
    except exc:
        return True
    except Exception as other:
        print(f"  (raised {type(other).__name__} instead of {exc.__name__})")
        return False
    return False


# ---------------------------------------------------------------------------
# Block / PanelSpec / Layout model
# ---------------------------------------------------------------------------

def test_block_validation():
    print("-- Block.from_dict validation + clamping")
    b = L.Block.from_dict({"kind": "header", "x": 0.1, "y": 0.2,
                           "w": 0.5, "h": 0.3})
    ok(b.kind == "header" and abs(b.x - 0.1) < 1e-9, "round-trip lost values")
    ok(_raises(lambda: L.Block.from_dict({"kind": "nope"})),
       "unknown block kind must raise LayoutError")
    ok(_raises(lambda: L.Block.from_dict({"kind": "header", "color": "red"})),
       "a non-hex colour must raise LayoutError")
    ok(L.Block.from_dict({"kind": "header", "color": "#abc"}).color
       == "#aabbcc", "#rgb must expand to #rrggbb")
    # a drag that overshoots the edge is clamped back inside the panel
    over = L.Block(kind="teams", x=0.9, y=0.9, w=0.5, h=0.5).clamped()
    ok(over.x + over.w <= 1.0 + 1e-9 and over.y + over.h <= 1.0 + 1e-9,
       f"clamped block still leaves the panel: {over}")
    tiny = L.Block(kind="teams", x=0.5, y=0.5, w=0.0001, h=0.0001).clamped()
    ok(tiny.w >= 0.02 and tiny.h >= 0.02, "a block must keep a usable size")

    rect = L.Block(kind="teams", x=0.0, y=0.5, w=1.0, h=0.5).rect_px((100, 200))
    ok(rect == (0, 100, 100, 200), f"rect_px wrong: {rect}")


def test_panel_and_layout():
    print("-- PanelSpec / Layout validation, ordering, even units")
    spec = L.PanelSpec.from_dict({"side": "right", "units": 121,
                                  "blocks": [{"kind": "header"}]})
    ok(spec.units == 122, "panel units must be rounded up to even "
                          "(odd sizes break yuv420p)")
    ok(_raises(lambda: L.PanelSpec.from_dict({"side": "middle"})),
       "an unknown side must raise")
    ok(_raises(lambda: L.PanelSpec.from_dict({"side": "top", "units": 4})),
       "an absurdly thin panel must raise")

    lay = L.Layout(name="t", panels=[L.default_panel("bottom"),
                                     L.default_panel("right"),
                                     L.default_panel("top")])
    ok([p.side for p in lay.ordered_panels()] == ["top", "right", "bottom"],
       "panels must come out in SIDES order for a stable filter graph")
    ok(_raises(lambda: L.Layout.from_dict(
        {"panels": [{"side": "right"}, {"side": "right"}]})),
       "two panels on one side must raise")
    ok(_raises(lambda: L.Layout.from_dict({"rec2mp4_layout": 99})),
       "a newer layout version must raise, not silently mis-render")


def test_geometry():
    print("-- composite / panel geometry in units and pixels")
    lay = L.Layout(panels=[L.PanelSpec(side="right", units=120),
                           L.PanelSpec(side="left", units=60),
                           L.PanelSpec(side="top", units=40),
                           L.PanelSpec(side="bottom", units=30)])
    ok(lay.composite_units() == (60 + 240 + 120, 40 + 160 + 30),
       f"composite units wrong: {lay.composite_units()}")
    ok(lay.panel_units("right") == (120, 160), "a column spans the game height")
    ok(lay.panel_units("top") == (420, 40),
       "a band spans the FULL composited width")
    for scale in (1, 2, 3, 4, 5):
        cw, ch = lay.composite_size_px(scale)
        ok(cw % 2 == 0 and ch % 2 == 0,
           f"composite {cw}x{ch} at scale {scale} must be even (yuv420p)")
        # the stacks only line up if the parts add up exactly
        parts_w = (lay.panel_size_px("left", scale)[0] + 240 * scale
                   + lay.panel_size_px("right", scale)[0])
        ok(parts_w == cw, f"hstack width mismatch at scale {scale}: "
                          f"{parts_w} != {cw}")
        ok(lay.panel_size_px("top", scale)[0] == cw,
           "a band must be exactly as wide as the row it stacks onto")
        ok(lay.panel_size_px("right", scale)[1] == 160 * scale,
           "a column must be exactly as tall as the game")

    # designer origins must match that same geometry
    ok(designer.panel_origin_units(lay, "top") == (0, 0), "top origin")
    ok(designer.panel_origin_units(lay, "left") == (0, 40), "left origin")
    ok(designer.panel_origin_units(lay, "right") == (60 + 240, 40),
       "right origin")
    ok(designer.panel_origin_units(lay, "bottom") == (0, 40 + 160),
       "bottom origin")


def test_json_round_trip():
    print("-- Layout JSON round trip (save -> load -> identical)")
    lay = L.default_layout("right")
    lay.set_panel(L.default_panel("top", units=40))
    lay.panel("right").bg = "#123456"
    lay.panel("right").blocks[0].text = ""
    lay.panel("right").blocks[1].color = "#ff8800"
    lay.panels[0].blocks.append(L.Block(kind="text", text="HELLO",
                                        align="center"))
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "mylayout.json"
        lay.save(path)
        back = L.Layout.load(path)
        ok(back.to_dict() == lay.to_dict(),
           "a saved layout must load back byte-identical")
        ok(back.name == lay.name, "a named layout keeps its own name")
        ok(json.loads(path.read_text())["rec2mp4_layout"] == L.LAYOUT_VERSION,
           "the version stamp must be written")
        anon = Path(td) / "from-a-friend.json"
        L.Layout(name="custom", panels=[L.default_panel("top")]).save(anon)
        ok(L.Layout.load(anon).name == "from-a-friend",
           "an unnamed layout is named after its file")
    ok(_raises(lambda: L.Layout.from_json("{oops")),
       "invalid JSON must raise LayoutError, not ValueError/JSONDecodeError")


def test_default_layout_covers_sections():
    print("-- default layouts: every section gets its own box")
    for side in L.SIDES:
        lay = L.default_layout(side)
        kinds = [b.kind for b in lay.panels[0].blocks]
        for must in ("header", "teams", "moves", "evs", "ivs", "footer"):
            ok(must in kinds, f"{side} default layout is missing {must}")
        # boxes must not overlap or leave the panel
        for b in lay.panels[0].blocks:
            ok(0 <= b.x and b.x + b.w <= 1.0 + 1e-6, f"{side}/{b.kind} x")
            ok(0 <= b.y and b.y + b.h <= 1.0 + 1e-6, f"{side}/{b.kind} y")
    # a section outside the default stack is added only when asked for
    ok("export" not in [b.kind for b in L.default_panel("right").blocks],
       "export must not take a slice by default (it is usually empty)")
    ok("export" in [b.kind for b in
                    L.default_panel("right",
                                    sections=("header", "export")).blocks],
       "an explicitly requested section must appear")


def test_page_specs():
    print("-- stat cycling: one stat block per page")
    spec = L.default_panel("right")
    pages = L.panel_page_specs(spec, ())
    ok(len(pages) == 3, f"3 stat blocks -> 3 pages, got {len(pages)}")
    for page, kind in zip(pages, L.STAT_PAGE_SECTIONS):
        visible = [b.kind for b in page.blocks if b.visible and b.is_stat]
        ok(visible == [kind], f"page for {kind} shows {visible}")
        ok([b.kind for b in page.blocks if b.visible and not b.is_stat],
           "non-stat blocks must stay on every page")
    # A cycling page inherits the whole stat area: the other stat blocks are
    # hidden, so leaving its own thin slice in place is what made the text
    # tiny AND made it overflow with a third of the panel blank.
    stats = [b for b in spec.blocks if b.is_stat]
    own = max(b.h for b in stats)
    whole = max(b.y + b.h for b in stats) - min(b.y for b in stats)
    for page in pages:
        shown = [b for b in page.blocks if b.is_stat and b.visible]
        ok(len(shown) == 1, "a page must show exactly one stat block")
        ok(abs(shown[0].h - whole) < 1e-6,
           f"page block kept its slice ({shown[0].h:.3f}) instead of the "
           f"whole stat area ({whole:.3f})")
    ok(whole > own * 2, "the union of the stat blocks should dwarf one slice")

    ok(len(L.panel_page_specs(spec, ("evs",))) == 1,
       "one requested page -> a still panel")
    plain = L.PanelSpec(side="top", blocks=[L.Block(kind="header")])
    ok(len(L.panel_page_specs(plain, ())) == 1,
       "a panel with no stat blocks has exactly one page")


# ---------------------------------------------------------------------------
# Section content (pure, no Pillow)
# ---------------------------------------------------------------------------

def _synthetic_info():
    from rec2mp4 import rec
    return rec.parse(build_synthetic_record())


def _extras(state=None, **over):
    ex = {"rom_bytes": None, "outcome_text": "won", "duration_seconds": 61.0,
          "streak": 7, "export_lines": [], "sections": panel.PANEL_SECTIONS,
          "opponent_a_label": "frontier trainer 83", "opponent_b_label": None,
          "pov": "player", "pov_faithful": False, "state": state or {},
          "speech": None}
    ex.update(over)
    return ex


def test_section_lines():
    print("-- panel.section_lines for every block kind")
    info = _synthetic_info()
    ex = _extras()
    for kind in panel.PANEL_SECTIONS:
        rows = panel.section_lines(kind, info, ex)
        ok(isinstance(rows, list), f"{kind} must return a list")
        for row in rows:
            ok(isinstance(row, tuple) and len(row) == 2,
               f"{kind} row is not (text, role): {row!r}")
            ok(isinstance(row[0], str), f"{kind} row text must be str")
    head = [t for t, _r in panel.section_lines("header", info, ex)]
    ok(head and head[0] == info["facility"], "header must lead with the facility")
    ok(any("Streak 7" == t for t in head), "the streak belongs in the header")
    teams = panel.section_lines("teams", info, ex)
    ok(sum(1 for _t, r in teams if r in ("mon", "mon_shiny")) ==
       len(info["teams"]["player"]) + len(info["teams"]["opponent"]),
       "every mon on BOTH teams must produce a row")
    ok(panel.section_lines("trainer", info, ex) == [],
       "no state sidecar -> no trainer rows at all")


def test_trainer_section():
    print("-- trainer section + end card from a 'state.*' block")
    info = _synthetic_info()
    state = pipeline.parse_state_sidecar([
        "prose line, not parsed",
        "state.playtime: 116h 0m 10s", "state.dex_seen: 221",
        "state.dex_caught: 162", "state.bp: 18", "state.bp_card: 15",
        "state.symbols: sG--s--", "state.symbols_silver: 2",
        "state.symbols_gold: 1"])
    rows = panel.trainer_lines(info, _extras(state))
    texts = [t for t, _r in rows]
    ok(any("116h 0m 10s" in t for t in texts), "playtime missing")
    ok(any("221 seen" in t and "162 caught" in t for t in texts),
       "Pokedex line missing")
    ok(any("Battle Points  18" == t for t in texts), "BP line missing")
    ok(("sG--s--", "symbols") in rows,
       "the raw symbol string must reach the renderer as a 'symbols' row")
    card = panel.end_card_lines(info, _extras(state))
    ok(card and card[0][1] == "head", "the end card must lead with the trainer")
    ok(("sG--s--", "symbols") in card, "the end card must carry the symbols")
    ok(panel.end_card_lines(info, _extras({})) == [],
       "no state -> no end card at all")


# ---------------------------------------------------------------------------
# PokeDNA 'state.*' sidecar (docs/REC-SIDECAR.md)
# ---------------------------------------------------------------------------

def test_state_sidecar_parser():
    print("-- pipeline.parse_state_sidecar (docs/REC-SIDECAR.md rules)")
    ok(pipeline.parse_state_sidecar([]) == {}, "no lines -> {}")
    ok(pipeline.parse_state_sidecar(None) == {}, "None -> {}")
    ok(pipeline.parse_state_sidecar(["Battle Arena - Open", "GUYA vs X"])
       == {}, "prose alone must produce nothing")

    st = pipeline.parse_state_sidecar([
        "state.playtime: 116h 0m 10s", "state.dex_seen: 221",
        "state.dex_caught: 162", "state.bp: 18", "state.bp_card: 15",
        "state.symbols: sG--s--", "state.symbols_silver: 2",
        "state.symbols_gold: 1", "state.brand_new_key: whatever"])
    ok(st["playtime"] == "116h 0m 10s", "playtime string")
    ok(st["playtime_hours"] == 116 and st["playtime_seconds"] == 116 * 3600 + 10,
       f"playtime not decomposed: {st}")
    ok(st["dex_seen"] == 221 and st["dex_caught"] == 162, "dex counts")
    ok(st["bp"] == 18 and st["bp_card"] == 15, "bp / bp_card kept separate")
    ok(st["symbols"] == "sG--s--", "symbol string")
    ok(st["symbols_list"][0] == ("Tower", "silver")
       and st["symbols_list"][1] == ("Dome", "gold")
       and st["symbols_list"][2] == ("Palace", "none"),
       f"symbols not mapped in Frontier Pass order: {st['symbols_list']}")
    ok("brand_new_key" not in st, "unknown keys are ignored, never fatal")

    # Rule 1: every key is optional; a Ruby record has no symbols at all.
    ruby = pipeline.parse_state_sidecar(["state.playtime: 3h 2m 1s",
                                         "state.dex_seen: 40"])
    ok(ruby == {"playtime": "3h 2m 1s", "playtime_hours": 3,
                "playtime_seconds": 3 * 3600 + 2 * 60 + 1, "dex_seen": 40},
       f"partial block must not invent keys: {ruby}")
    ok("bp" not in ruby, "a missing key must NEVER become a zero")

    # Malformed values are dropped, not guessed.
    bad = pipeline.parse_state_sidecar([
        "state.bp: lots", "state.symbols: sG", "state.symbols: sGxxxxx",
        "state.dex_seen:", "state.playtime: whenever"])
    ok("bp" not in bad and "symbols" not in bad and "dex_seen" not in bad,
       f"malformed values must be dropped: {bad}")
    ok(bad.get("playtime") == "whenever" and "playtime_hours" not in bad,
       "an unparseable playtime keeps the raw string but derives nothing")


def test_state_lines_are_not_prose():
    print("-- the 'state.' block never leaks into the export section")
    lines = ["Battle Arena - Open Level", "Streak 20",
             "state.bp: 18", "state.symbols: -------"]
    ctx = pipeline.design_context()
    settings = ConvertSettings()

    class _R:
        outcome_text = "won"
        seconds = 12.0
    ex = pipeline._panel_extras(_synthetic_info(), settings, ctx, _R(),
                                None, lines)
    ok(ex["export_lines"] == ["Battle Arena - Open Level", "Streak 20"],
       f"export lines must exclude state.*: {ex['export_lines']}")
    ok(ex["state"]["bp"] == 18, "state must be parsed out of the same lines")


# ---------------------------------------------------------------------------
# ffmpeg filter graph (string only — no ffmpeg needed)
# ---------------------------------------------------------------------------

def _inputs(*sides):
    return [{"side": s, "size": (10, 20), "pages": [b""]} for s in sides]


def test_filter_graph():
    print("-- pipeline.build_filter_graph for every side combination")
    graph, out = pipeline.build_filter_graph(_inputs("right"))
    ok(out == "[row]" and "hstack=inputs=2:shortest=1" in graph,
       f"single right panel: {graph} -> {out}")
    ok(graph.index("[0:v]") < graph.index("[p1]hstack")
       if "[p1]hstack" in graph else True, "right panel must follow the game")

    graph, out = pipeline.build_filter_graph(_inputs("left"))
    ok("[p1][0:v]hstack" in graph, f"left panel must precede the game: {graph}")

    graph, out = pipeline.build_filter_graph(_inputs("top"))
    ok(out == "[v]" and "[p1][0:v]vstack=inputs=2:shortest=1[v]" in graph,
       f"top band: {graph}")

    graph, out = pipeline.build_filter_graph(_inputs("bottom"))
    ok("[0:v][p1]vstack=inputs=2" in graph, f"bottom band: {graph}")

    graph, out = pipeline.build_filter_graph(
        _inputs("top", "left", "right", "bottom"))
    ok("[p2][0:v][p3]hstack=inputs=3:shortest=1[row]" in graph,
       f"columns must hstack around the game: {graph}")
    ok("[p1][row][p4]vstack=inputs=3:shortest=1[v]" in graph,
       f"bands must vstack around the row: {graph}")
    ok(out == "[v]", "four panels must end on [v]")
    ok(graph.count("[0:v]") == 1,
       "the game may be consumed by exactly one filter")

    graph, _out = pipeline.build_filter_graph(_inputs("right"),
                                              fps="16777216/280896")
    ok("fps=16777216/280896" in graph,
       "the panel track must be normalised to the game frame rate")


# ---------------------------------------------------------------------------
# Layout resolution + settings
# ---------------------------------------------------------------------------

def test_resolve_layout():
    print("-- pipeline.resolve_layout: when the block renderer takes over")
    # Since 2026-08-08 the BLOCK layout is the default: the flow renderer
    # squeezed every section into one column and shrank the stat rows until
    # they were unreadable. 'classic' brings it back.
    auto = pipeline.resolve_layout(ConvertSettings(panel="right"))
    ok(auto is not None and auto.panels[0].side == "right",
       "--panel right must now generate a block layout")
    ok(pipeline.resolve_layout(ConvertSettings(panel="left")).panels[0].side
       == "left", "the generated layout must follow --panel")
    ok(pipeline.resolve_layout(ConvertSettings(layout="classic")) is None,
       "--layout classic must restore the flow renderer")
    ok(pipeline.resolve_layout(ConvertSettings(panel="off",
                                               layout="x.json")) is None,
       "--panel off must beat --layout")
    lay = pipeline.resolve_layout(ConvertSettings(panel="top"))
    ok(lay is not None and [p.side for p in lay.panels] == ["top"],
       "--panel top has no flow equivalent -> a generated layout")
    lay = pipeline.resolve_layout(ConvertSettings(panel="left",
                                                  layout="default"))
    ok(lay is not None and lay.panels[0].side == "left",
       "--layout default must follow --panel")
    made = L.default_layout("right")
    ok(pipeline.resolve_layout(ConvertSettings(layout=made)) is made,
       "a Layout object must pass straight through")
    ok(pipeline.resolve_layout(
        ConvertSettings(layout=made.to_dict())).describe() == made.describe(),
        "a layout dict must be accepted")
    try:
        pipeline.resolve_layout(ConvertSettings(
            layout={"panels": []}))
        ok(False, "an empty layout must raise")
    except L.LayoutError:
        ok(True, "an empty layout raises")


def test_composite_size():
    print("-- pipeline.composite_size matches the layout geometry")
    ctx = pipeline.design_context()
    ctx.panel_enabled = False
    ok(pipeline.composite_size(ConvertSettings(scale=4, panel="off"), ctx)
       == (960, 640), "panel off -> the plain game frame")
    ctx = pipeline.design_context()
    ok(pipeline.composite_size(ConvertSettings(scale=4, panel="right"), ctx)
       == (960 + 480, 640), "classic panel -> game + half width")
    lay = L.Layout(panels=[L.PanelSpec(side="top", units=40),
                           L.PanelSpec(side="right", units=120)])
    ctx = pipeline.design_context(lay)
    ok(pipeline.composite_size(ConvertSettings(scale=2, panel="right"), ctx)
       == lay.composite_size_px(2), "layout -> the layout's own size")


# ---------------------------------------------------------------------------
# Parallel batch plumbing (no emulator: reservation + job maths only)
# ---------------------------------------------------------------------------

def test_resolve_jobs():
    print("-- pipeline.resolve_jobs")
    cpus = pipeline.cpu_jobs()
    ok(cpus >= 1, "cpu_jobs must be at least 1")
    ok(pipeline.resolve_jobs(0, 1) == 1, "one record never needs a pool")
    ok(pipeline.resolve_jobs(0, 99) == cpus, "auto = one per CPU")
    ok(pipeline.resolve_jobs(1, 99) == 1, "--jobs 1 = sequential")
    ok(pipeline.resolve_jobs(3, 99) == 3, "an explicit job count is honoured")
    ok(pipeline.resolve_jobs(99, 4) == 4, "never more workers than records")
    ok(pipeline.resolve_jobs(-5, 99) == cpus, "a nonsense count falls back")
    ok(pipeline.resolve_jobs("x", 99) == cpus, "a non-number falls back")
    ok(pipeline.resolve_jobs(0, 0) == 1, "an empty batch is 1 worker")


def _fake_ctx(outdir: Path, rom_bytes=b"") -> ConvertContext:
    """A ConvertContext with just the fields naming/reservation touches."""
    return ConvertContext(
        rom_path=Path("rom"), sav_path=Path("sav"), outdir=outdir,
        rom_bytes=rom_bytes, rom_crc32=0, sav_bytes=b"",
        driver_mod=None, video_mod=None, writer_kwargs={},
        writer_accepts_log=False, panel_enabled=False,
        panel_sections=panel.PANEL_SECTIONS)


def test_output_reservation():
    print("-- reserve_output_paths: no two parallel workers share a name")
    data = build_synthetic_record()
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in"
        out = Path(td) / "out"
        src.mkdir()
        out.mkdir()
        paths = []
        for name in ("A_27-07-2026_10-40", "B_27-07-2026_10-41",
                     "C_27-07-2026_10-42"):
            p = src / (name + ".rec")
            p.write_bytes(data)
            paths.append(p)
        bad = src / "broken.rec"
        bad.write_bytes(b"\x00" * 64)
        paths.append(bad)

        ctx = _fake_ctx(out)
        settings = ConvertSettings(outdir=out)
        reserved = pipeline.reserve_output_paths(paths, settings, ctx)
        ok(len(reserved) == len(paths), "one entry per input, in order")
        ok(reserved[-1] is None, "an invalid record reserves no output")
        names = [r for r in reserved if r]
        ok(len(set(names)) == len(names),
           f"two records were given the SAME output file: {names}")
        for r in names:
            ok(r.endswith(".mp4"), f"reserved a non-mp4: {r}")
        # the records differ only by stem, so the stems must survive naming
        ok(any("A_27-07-2026_10-40" in r for r in names), "stem A missing")
        ok(any("C_27-07-2026_10-42" in r for r in names), "stem C missing")

        # a second reservation pass on the same context must not re-hand out
        # the same paths (that is exactly the parallel collision)
        again = [r for r in pipeline.reserve_output_paths(paths, settings, ctx)
                 if r]
        ok(not (set(again) & set(names)),
           f"reservations collided on a second pass: {again}")


def test_batch_falls_back_when_processes_cannot_start():
    print("-- convert_batch: no worker processes -> sequential, never a hang")
    import multiprocessing as mp
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out"
        out.mkdir()
        paths = []
        for i in range(2):                      # invalid: never reach the
            p = Path(td) / f"bad{i}.rec"        # emulator, so no ROM needed
            p.write_bytes(b"\x00" * 4096)
            paths.append(p)
        ctx = _fake_ctx(out)
        settings = ConvertSettings(outdir=out, jobs=2)
        errors = []
        real_manager = mp.Manager

        def boom(*_a, **_k):                    # what an unguarded __main__
            raise RuntimeError("bootstrapping phase")   # looks like on spawn
        mp.Manager = boom
        try:
            results = pipeline.convert_batch(
                paths, settings, ctx=ctx, log=lambda *_a: None,
                err=errors.append)
        finally:
            mp.Manager = real_manager
        ok(len(results) == 2 and all(r and r["status"] == "INVALID"
                                     for r in results),
           f"the batch must still finish every record: {results}")
        ok(any("sequentially" in e for e in errors),
           f"the fallback must say what happened: {errors}")
        ok(any("__main__" in e for e in errors),
           "the fallback must name the actual fix (a main guard)")


def test_prepare_record_agrees_with_convert():
    print("-- prepare_record: the names the workers are handed")
    data = build_synthetic_record()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "GUYA_Dome-O-12_27-07-2026_10-40.rec"
        p.write_bytes(data)
        ctx = _fake_ctx(Path(td))
        prep = pipeline.prepare_record(p, ConvertSettings(), ctx,
                                       log=lambda *_a: None,
                                       err=lambda *_a: None)
        ok("result" not in prep, "a valid record must prepare cleanly")
        ok(prep["streak"] == 12, f"streak not read from the stem: {prep}")
        ok("(streak 12)" in prep["base"], f"streak missing from the name: "
                                          f"{prep['base']}")
        ok("Battle Dome" in prep["base"], "facility missing from the name")

        broken = Path(td) / "broken.rec"
        broken.write_bytes(b"\xff" * 4096)
        prep = pipeline.prepare_record(broken, ConvertSettings(), ctx,
                                       log=lambda *_a: None,
                                       err=lambda *_a: None)
        ok("result" in prep and prep["result"]["status"] == "INVALID",
           "an invalid record must come back as a ready-made result")


# ---------------------------------------------------------------------------
# Designer pure logic (no display needed)
# ---------------------------------------------------------------------------

def test_designer_logic():
    print("-- designer: snapping, hit testing, drag maths")
    ok(designer.snap(0.123, 0.01) == 0.12, "snap to 1%")
    ok(designer.snap(0.123, 0) == 0.123, "no grid -> no snap")

    lay = L.default_layout("right")
    k = 2.0                                    # 2 canvas px per unit
    block = lay.panels[0].blocks[0]            # header, at the panel top
    x0, y0, x1, y1 = designer.block_rect_canvas(lay, "right", block, k)
    ok(x0 == 240 * k, f"the right panel starts after the game: {x0}")
    ok(y0 == 0, "the header sits at the top of the panel")

    hit = designer.hit_test(lay, (x0 + x1) / 2, (y0 + y1) / 2, k)
    ok(hit == ("right", 0, "move"), f"hit test missed the header: {hit}")
    ok(designer.hit_test(lay, 10, 10, k) is None,
       "clicking the game area selects nothing")
    corner = designer.hit_test(lay, x1 - 2, y1 - 2, k, selected=("right", 0))
    ok(corner == ("right", 0, "resize"),
       f"the selected block's corner must resize: {corner}")
    ok(designer.hit_test(lay, x1 - 2, y1 - 2, k)[2] == "move",
       "an unselected block's corner still moves")

    units = lay.panel_units("right")
    moved = designer.move_block(block, 0, 16, units, 0.0)   # +16 units of 160
    ok(abs(moved.y - (block.y + 0.1)) < 1e-6,
       f"a 16-unit drag on a 160-unit panel is 10%: {moved.y}")
    snapped = designer.move_block(block, 0, 1.2, units, 0.01)
    ok(abs(snapped.y * 100 - round(snapped.y * 100)) < 1e-6,
       "a snapped drag must land on the grid")
    grown = designer.resize_block(block, 0, 16, units, 0.0)
    ok(abs(grown.h - (block.h + 0.1)) < 1e-6, "resize grows by the delta")
    shrunk = designer.resize_block(block, 0, -10000, units, 0.0)
    ok(shrunk.h >= designer.MIN_FRACTION,
       "a block can never be dragged to nothing")

    info = designer.demo_info()
    ok(info["valid"] and len(info["teams"]["player"]) == 3,
       "the demo record must look like a parsed 3v3")
    for kind in panel.PANEL_SECTIONS:           # every section must render
        panel.section_lines(kind, info, designer.demo_extras())
    ok(True, "demo record feeds every section")
    ok("no panels" in designer.layout_summary(L.Layout()),
       "an empty layout must say so")


# ---------------------------------------------------------------------------
# Rendering (needs Pillow)
# ---------------------------------------------------------------------------

def test_render_blocks():
    print("-- render_layout_panel / render_end_card (Pillow)")
    if not _pil_available():
        print("   SKIP: Pillow not importable in this interpreter")
        return
    import io

    from PIL import Image
    info = _synthetic_info()
    state = pipeline.parse_state_sidecar(["state.bp: 18",
                                          "state.symbols: sG--s--",
                                          "state.playtime: 5h 4m 3s"])
    ex = _extras(state)

    for side in L.SIDES:
        lay = L.default_layout(side)
        size = lay.panel_size_px(side, 3)
        png = panel.render_layout_panel(info, ex, lay.panels[0], size)
        ok(png[:8] == b"\x89PNG\r\n\x1a\n", f"{side}: not a PNG")
        img = Image.open(io.BytesIO(png))
        ok(img.size == size, f"{side}: rendered {img.size}, wanted {size}")

    # a dict spec must work exactly like the dataclass
    spec = L.default_panel("right")
    a = panel.render_layout_panel(info, ex, spec, (240, 320))
    b = panel.render_layout_panel(info, ex, spec.to_dict(), (240, 320))
    ok(a == b, "a layout dict must render identically to the dataclass")

    # a missing background image degrades to the colour + one warning
    warned = []
    spec2 = L.PanelSpec(side="right", units=120, bg_image="/nope/none.png",
                        blocks=[L.Block(kind="header")])
    png = panel.render_layout_panel(info, ex, spec2, (240, 320),
                                    warn=warned.append)
    ok(png[:8] == b"\x89PNG\r\n\x1a\n", "a bad background must still render")
    ok(warned and "background image" in warned[0],
       f"a bad background must warn loudly: {warned}")

    # visible vs hidden blocks actually change the pixels
    shown = L.PanelSpec(side="right", units=120,
                        blocks=[L.Block(kind="teams", h=1.0)])
    hidden = L.PanelSpec(side="right", units=120,
                         blocks=[L.Block(kind="teams", h=1.0, visible=False)])
    ok(panel.render_layout_panel(info, ex, shown, (240, 320))
       != panel.render_layout_panel(info, ex, hidden, (240, 320)),
       "hiding a block must change the render")

    card = panel.render_end_card(info, ex, (480, 320))
    ok(card[:8] == b"\x89PNG\r\n\x1a\n", "end card is not a PNG")
    ok(Image.open(io.BytesIO(card)).size == (480, 320), "end card size")
    try:
        panel.render_end_card(info, _extras({}), (480, 320))
        ok(False, "an end card with no state must raise, not draw a blank")
    except ValueError:
        ok(True, "no state -> ValueError, so the caller skips the stage")


def test_compose_frame():
    print("-- pipeline.compose_frame geometry (Pillow)")
    if not _pil_available():
        print("   SKIP: Pillow not importable in this interpreter")
        return
    info = _synthetic_info()
    ex = _extras()
    for lay, scale in ((None, 4), (L.default_layout("right"), 2),
                       (L.Layout(panels=[L.default_panel("top", units=40),
                                         L.default_panel("bottom", units=30),
                                         L.default_panel("left", units=60)]),
                        3)):
        ctx = pipeline.design_context(lay)
        settings = ConvertSettings(scale=scale, panel="right", layout=lay)
        img = pipeline.compose_frame(None, info, ex, ctx, settings)
        want = pipeline.composite_size(settings, ctx)
        ok(img.size == want,
           f"composed {img.size}, composite_size says {want}")

    ctx = pipeline.design_context()
    ctx.panel_enabled = False
    img = pipeline.compose_frame(None, info, ex, ctx,
                                 ConvertSettings(scale=2, panel="off"))
    ok(img.size == (480, 320), f"panel off -> plain game frame, got {img.size}")


# ---------------------------------------------------------------------------
# The opponent's pre-battle line (Easy Chat) and the opening card
# ---------------------------------------------------------------------------

ROM_PATH = os.path.join(ROOT, "local", "rom.gba")


def test_easy_chat_word_ids():
    print("-- romdata.easy_chat_word: id shape + doubt paths (no ROM needed)")
    from rec2mp4 import romdata
    ok(romdata.easy_chat_word(b"", 0x0A01) is None, "empty ROM -> None")
    ok(romdata.easy_chat_word(None, 0x0A01) is None, "no ROM -> None")
    ok(romdata.easy_chat_word(b"\x00" * 16, romdata.EC_EMPTY_WORD) is None,
       "the empty-slot word must never render")
    ok(romdata.easy_chat_word(b"\x00" * 16, -1) is None, "negative id -> None")
    ok(romdata.easy_chat_word(b"\x00" * 16, (30 << 9) | 1) is None,
       "a group past EC_NUM_GROUPS -> None")
    ok(romdata.easy_chat_word(b"\x00" * 16, True) is None,
       "a bool is not a word id")
    try:
        romdata.frontier_trainer_speech(b"", 0, "sideways")
        ok(False, "an unknown speech kind must raise")
    except ValueError:
        ok(True, "an unknown speech kind raises ValueError")
    ok(romdata.frontier_trainer_speech(b"", 0) is None, "empty ROM -> None")
    ok(romdata.frontier_trainer_speech(b"\x00" * 16, 9999) is None,
       "a trainer id past the table -> None")
    ok(set(romdata.SPEECH_OFFSETS) == {"before", "win", "lose"},
       "three speeches per trainer")
    ok(romdata.SPEECH_OFFSETS["before"] == 12,
       "speechBefore sits right after facilityClass + filler + name")


def test_easy_chat_against_rom():
    print("-- easy chat + speeches decoded from the real ROM")
    from rec2mp4 import romdata
    if not os.path.isfile(ROM_PATH):
        print("   SKIP: local/rom.gba not present")
        return
    rom = Path(ROM_PATH).read_bytes()

    # The whole corpus must decode: a '?' in a card is a shipped bug.
    total = undecodable = 0
    for tid in range(romdata.FRONTIER_TRAINERS_COUNT):
        for which in ("before", "win", "lose"):
            base = (romdata.GBATTLE_FRONTIER_TRAINERS_ADDR
                    + tid * romdata.BFT_ENTRY_SIZE
                    + romdata.SPEECH_OFFSETS[which])
            raw = romdata._rom_slice(rom, base, 12)
            for i in range(romdata.EASY_CHAT_BATTLE_WORDS_COUNT):
                wid = int.from_bytes(raw[i * 2:i * 2 + 2], "little")
                if wid == romdata.EC_EMPTY_WORD:
                    continue
                total += 1
                if romdata.easy_chat_word(rom, wid) is None:
                    undecodable += 1
    ok(total > 4000, f"only {total} words swept — table not being read")
    ok(undecodable == 0,
       f"{undecodable}/{total} easy-chat words do not decode")

    # A value-group word: the index IS the move id, NOT an index into the
    # group's own list (154 entries) — the exact bug this guards.
    sweet = romdata.easy_chat_word(rom, (18 << 9) | 230)
    ok(sweet == "SWEET SCENT",
       f"EC_WORD(MOVE_1, 230) must be SWEET SCENT, got {sweet!r}")
    ok(romdata.easy_chat_word(rom, (0 << 9) | 1) == "BULBASAUR",
       "EC_WORD(POKEMON, 1) must resolve through gSpeciesNames")

    ok(romdata.frontier_trainer_speech(rom, 120) == ["I", "KNOW", "ONLY",
                                                     "YOU"],
       "PSYCHIC NORTON's before-line")
    ok(" ".join(romdata.frontier_trainer_speech(rom, 72, "win"))
       == "IT'S THE SWEET SCENT OF TASTY WATER",
       "AROMA LADY JILLIAN's win line (the value-group case end to end)")
    ok(romdata.frontier_trainer_speech(rom, 83) != romdata
       .frontier_trainer_speech(rom, 83, "lose"),
       "before/lose must read different slots")


def test_intro_card():
    print("-- the opening card: rows, quoting, and 'no speech -> no card'")
    info = _synthetic_info()
    ex = _extras(speech=["SHOW", "ME", "THAT", "YOU'RE", "SERIOUS", "!"],
                 opponent_a_label="BLACK BELT SHIZUKA")
    rows = panel.intro_card_lines(info, ex)
    texts = [t for t, _r in rows]
    ok(texts[0] == "VS BLACK BELT SHIZUKA", f"card must open with VS: {texts}")
    quotes = [t for t, r in rows if r == "quote"]
    ok(len(quotes) == 2, f"6 words must break 3+3 like the game: {quotes}")
    ok(quotes[0] == '"SHOW ME THAT' and quotes[1] == 'YOU\'RE SERIOUS !"',
       f"the quote must open and close exactly once: {quotes}")
    one = panel.intro_card_lines(info, _extras(speech=["HI", "THERE"]))
    ok([t for t, r in one if r == "quote"] == ['"HI THERE"'],
       "a single line is quoted on both ends")

    ok(panel.intro_card_lines(info, _extras()) == [],
       "no speech -> no rows at all")
    if _pil_available():
        png = panel.render_intro_card(info, ex, (480, 320))
        ok(png[:8] == b"\x89PNG\r\n\x1a\n", "intro card is not a PNG")
        try:
            panel.render_intro_card(info, _extras(), (480, 320))
            ok(False, "an intro card with no speech must raise")
        except ValueError:
            ok(True, "no speech -> ValueError, so the caller skips the stage")


def test_opponent_speech_gating():
    print("-- pipeline.opponent_speech only speaks for ROM trainers")
    info = _synthetic_info()
    ok(pipeline.opponent_speech(info, None) is None, "no ROM -> None")
    ok(pipeline.opponent_speech(info, b"") is None, "empty ROM -> None")
    for kind in ("record_mix_friend", "apprentice", "frontier_brain", None):
        ok(pipeline.opponent_speech(dict(info, opponent_a_kind=kind),
                                    b"\x00" * 64) is None,
           f"{kind} keeps its greeting outside the ROM -> None")


def test_card_splicing():
    print("-- card plumbing: order, and the audio delay a prepend needs")
    ok(pipeline._card_specs(None, 3, None, 3) == [], "no PNGs -> no cards")
    ok(pipeline._card_specs(b"i", 0, b"e", 0) == [], "0 seconds -> no cards")
    specs = pipeline._card_specs(b"i", 3.0, b"e", 2.0)
    ok([w for w, _p, _s in specs] == ["intro", "end"],
       f"intro must come first: {specs}")

    graph, label = pipeline.build_filter_graph(_inputs("right"))
    cmd, g2, out, tmps = pipeline._add_cards(
        graph, label, specs, (100, 50), "60/1", 2,
        tempfile.gettempdir(), "t.mp4")
    try:
        ok(out == "[vout]", "cards must produce a new output label")
        ok("[introcard]%s[endcard]concat=n=3:v=1:a=0[vout]" % label in g2,
           f"concat order must be intro, video, end: {g2}")
        ok(g2.count("scale=100:50") == 2, "both cards scale to the frame")
        ok(cmd.count("-loop") == 2 and cmd.count("-t") == 2,
           f"each card needs a bounded looped input: {cmd}")
        ok(len(tmps) == 2, "one temp PNG per card")
    finally:
        for t in tmps:
            try:
                os.unlink(t)
            except OSError:
                pass

    # audio: only a PREPENDED card shifts the game, and only then is the
    # audio re-encoded rather than stream-copied.
    g, args = pipeline._audio_args(specs, has_audio=True)
    ok("adelay=3000:all=1" in g,
       f"a 3 s intro must delay the audio by 3000 ms: {g}")
    ok("[aout]" in args and "aac" in args, f"delayed audio is re-encoded: {args}")
    g, args = pipeline._audio_args([("end", b"e", 3.0)], has_audio=True)
    ok(g == "" and "copy" in args,
       f"an end card alone must still stream-copy the audio: {args}")
    g, args = pipeline._audio_args(specs, has_audio=False)
    ok(g == "" and args == ["-map", "0:a?"],
       f"a silent video must not grow an audio filter: {args}")


def test_designer_zoom_and_undo():
    print("-- designer: zoom range, and undo/redo snapshots")
    ok(designer.ZOOM_MIN < 1.0 < designer.ZOOM_MAX, "1.0 (fit) must be inside "
                                                    "the zoom range")
    ok(designer.ZOOM_STEP > 1.0, "a zoom step must magnify")
    ok(designer.UNDO_DEPTH >= 20, "an undo stack this shallow is useless")

    # The snapshot/restore contract undo is built on: a Layout survives a
    # to_dict -> from_dict round trip with every field intact.
    lay = L.default_layout("right")
    lay.panel("right").blocks[0].y = 0.42
    lay.set_panel(L.default_panel("top", units=40))
    snap = lay.to_dict()
    lay.panel("right").blocks[0].y = 0.99
    lay.remove_side("top")
    back = L.Layout.from_dict(snap)
    ok(abs(back.panel("right").blocks[0].y - 0.42) < 1e-9,
       "restoring a snapshot did not bring the block back")
    ok(back.has_side("top"), "restoring a snapshot lost a whole panel")
    ok(back.to_dict() == snap, "snapshot -> restore -> snapshot must be stable")


def test_outcome_and_facility_naming():
    print("-- outcome tag, facility folder, and the renamed output")
    ok(pipeline.outcome_tag("won") == "WON", "won -> WON")
    ok(pipeline.outcome_tag("lost") == "LOST", "lost -> LOST")
    ok(pipeline.outcome_tag("draw") == "DRAW", "draw -> DRAW")
    ok(pipeline.outcome_tag("player teleported") == "PLAYER-TELEPORTED",
       "an odd outcome must still be file-name safe")
    for nothing in (None, "", "unknown", "  "):
        ok(pipeline.outcome_tag(nothing) is None,
           f"{nothing!r} must NOT put a tag in the name")

    info = _synthetic_info()
    named = pipeline.build_output_basename(info, "GUYA_x", None, streak=12,
                                           outcome="won")
    ok(named.endswith("[WON]"), f"outcome tag missing: {named}")
    ok("(streak 12)" in named, f"streak lost when the outcome was added: "
                               f"{named}")
    plain = pipeline.build_output_basename(info, "GUYA_x", None, plain=True,
                                           outcome="lost")
    ok(plain == "GUYA_x [LOST]", f"plain naming + outcome: {plain}")
    ok(pipeline.build_output_basename(info, "GUYA_x", None, outcome="won",
                                      pov="opponent")
       .endswith("[WON] [opponent POV]"),
       "both tags must survive together")
    ok(pipeline.outcome_tag("won") not in
       pipeline.build_output_basename(info, "GUYA_x", None),
       "no outcome passed -> no tag")

    ok(pipeline.facility_folder(info) == info["facility"],
       f"facility folder: {pipeline.facility_folder(info)}")
    ok(pipeline.facility_folder({}) == "Unknown facility",
       "a record with no facility still needs somewhere to go")
    ok("/" not in pipeline.facility_folder({"facility": "a/b"}),
       "a facility name must never become a path separator")

    out = Path("/tmp/x")
    on = ConvertSettings(facility_folders=True)
    off = ConvertSettings(facility_folders=False)
    ok(pipeline.output_dir_for(out, info, on) == out / info["facility"],
       "facility folders on -> a subfolder")
    ok(pipeline.output_dir_for(out, info, off) == out,
       "facility folders off -> the plain output folder")


def test_ffmpeg_is_interruptible():
    print("-- run_ffmpeg: abort tears the child down instead of waiting")
    import time
    # Nothing ffmpeg-specific about the mechanism; a sleep stands in for a
    # long encode so the test needs no ffmpeg at all.
    t0 = time.time()
    try:
        pipeline.run_ffmpeg([sys.executable, "-c", "import time;time.sleep(30)"],
                            should_abort=lambda: True, poll=0.05)
        ok(False, "an aborted run must raise ConversionCancelled")
    except pipeline.ConversionCancelled:
        dt = time.time() - t0
        ok(dt < 5, f"abort took {dt:.1f}s — it is not actually interrupting")
    code, _err = pipeline.run_ffmpeg([sys.executable, "-c", "pass"],
                                     should_abort=lambda: False)
    ok(code == 0, "a normal run must still return its exit code")
    code, err = pipeline.run_ffmpeg(
        [sys.executable, "-c", "import sys;sys.stderr.write('boom');"
                               "sys.exit(3)"])
    ok(code == 3 and b"boom" in err,
       f"a failing run must return code + stderr: {code} {err!r}")


def main() -> int:
    test_block_validation()
    test_panel_and_layout()
    test_geometry()
    test_json_round_trip()
    test_default_layout_covers_sections()
    test_page_specs()
    test_section_lines()
    test_trainer_section()
    test_state_sidecar_parser()
    test_state_lines_are_not_prose()
    test_filter_graph()
    test_resolve_layout()
    test_composite_size()
    test_resolve_jobs()
    test_output_reservation()
    test_batch_falls_back_when_processes_cannot_start()
    test_prepare_record_agrees_with_convert()
    test_designer_logic()
    test_designer_zoom_and_undo()
    test_render_blocks()
    test_compose_frame()
    test_easy_chat_word_ids()
    test_easy_chat_against_rom()
    test_intro_card()
    test_opponent_speech_gating()
    test_card_splicing()
    test_outcome_and_facility_naming()
    test_ffmpeg_is_interruptible()
    if _checks == 0:
        print("FAIL: vacuous run (no checks executed)")
        return 2
    print(f"\nPASS: {_checks} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
