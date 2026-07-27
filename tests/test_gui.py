#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""Tests for the tkinter GUI's pure-logic layer (rec2mp4/gui.py).

Plain python, no pytest — same style as tests/test_rec.py. Exits non-zero
on failure. Every section except the last is display-free: settings
marshalling to pipeline.ConvertSettings, the queue model (add / folder /
dedupe / remove / clear), row + status formatting. The final widget smoke
test constructs the real window ONLY when a display is available —
tkinter missing or tk.Tk() raising TclError (headless CI runners) skips
it cleanly and the suite still exits 0. No emulation happens here.

Run:  python3 tests/test_gui.py
"""

import sys
import os
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from rec2mp4 import gui, rec                            # noqa: E402
from rec2mp4.pipeline import ConvertSettings            # noqa: E402
from rec2mp4.panel import PANEL_SECTIONS                # noqa: E402
from test_rec import build_synthetic_record             # noqa: E402

_checks = 0


def ok(cond, msg):
    # No bare `assert`: it would be stripped under `python3 -O`.
    global _checks
    _checks += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Settings marshalling
# ---------------------------------------------------------------------------

def test_settings_defaults():
    print("-- settings_from_form(default_form()) == CLI defaults")
    s = gui.settings_from_form(gui.default_form())
    ok(isinstance(s, ConvertSettings), "not a ConvertSettings")
    ref = ConvertSettings()
    ok(s.scale == 4 and s.audio is True, f"scale/audio wrong: {s}")
    ok(s.anims == "on" and s.text_speed == "record",
       f"anims/text_speed wrong: {s}")
    ok(s.panel == "right" and s.panel_info == "all",
       f"panel wrong: {s.panel}/{s.panel_info}")
    ok(s.plain_names is False and s.sidecar is True,
       f"naming/sidecar wrong: {s}")
    ok(s.pix_fmt == ref.pix_fmt and s.max_seconds == ref.max_seconds
       and s.headed == ref.headed,
       "GUI must not diverge from ConvertSettings defaults it doesn't expose")
    # outdir is prefilled with the repo default (a real string)
    ok(s.outdir, "outdir must be prefilled")
    # ROM/save prefill: a path only when the default file exists, else None
    from rec2mp4.pipeline import DEFAULT_ROM, DEFAULT_SAV
    ok((s.rom is not None) == DEFAULT_ROM.is_file(),
       f"rom prefill wrong: {s.rom!r}")
    ok((s.sav is not None) == DEFAULT_SAV.is_file(),
       f"sav prefill wrong: {s.sav!r}")
    print("   defaults mirror the CLI (panel right/all, scale 4, audio on)")


def test_settings_marshalling():
    print("-- settings_from_form() overrides + validation")
    form = gui.default_form()
    form.update({"rom": "", "sav": "", "outdir": "",
                 "scale": "2", "audio": False, "anims": "record",
                 "text_speed": "fast", "plain_names": True,
                 "sidecar": False, "panel": "left",
                 "panel_sections": ["teams", "header"]})
    s = gui.settings_from_form(form)
    ok(s.rom is None and s.sav is None and s.outdir is None,
       "empty path fields must map to None (pipeline defaults)")
    ok(s.scale == 2, f"string scale not coerced: {s.scale!r}")
    ok(s.audio is False and s.plain_names is True and s.sidecar is False,
       f"bool overrides lost: {s}")
    ok(s.anims == "record" and s.text_speed == "fast",
       f"anims/text_speed overrides lost: {s}")
    ok(s.panel == "left", f"panel override lost: {s.panel}")
    # subset of sections -> CSV in PANEL_SECTIONS order (parse re-orders,
    # but the CSV must round-trip through parse_panel_info)
    from rec2mp4.panel import parse_panel_info
    ok(parse_panel_info(s.panel_info) == ("header", "teams"),
       f"section subset CSV wrong: {s.panel_info!r}")
    # all sections -> literally "all"
    form["panel_sections"] = list(PANEL_SECTIONS)
    ok(gui.settings_from_form(form).panel_info == "all",
       "full section set must marshal to 'all'")
    # no sections + panel on -> panel degrades to off (NOT silently 'all')
    form["panel_sections"] = []
    form["panel"] = "right"
    s = gui.settings_from_form(form)
    ok(s.panel == "off", f"empty sections must turn the panel off: {s.panel}")
    # panel off keeps sections irrelevant
    form["panel"] = "off"
    form["panel_sections"] = ["teams"]
    ok(gui.settings_from_form(form).panel == "off", "panel off lost")
    # bad scales raise ValueError (the GUI shows these in a dialog)
    for bad in ("x", "", None, 0, 11, -3):
        form2 = gui.default_form()
        form2["scale"] = bad
        try:
            gui.settings_from_form(form2)
            ok(False, f"scale {bad!r} accepted")
        except ValueError:
            ok(True, "")
    print("   overrides, CSV sections, empty->off degrade, bad scale raises")


# ---------------------------------------------------------------------------
# Queue model
# ---------------------------------------------------------------------------

def test_queue_model():
    print("-- QueueModel add / add_folder / dedupe / remove / clear")
    good = build_synthetic_record()
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        a = d / "a.rec"
        b = d / "b.rec"
        junk = d / "junk.rec"
        other = d / "not-a-record.txt"
        a.write_bytes(good)
        b.write_bytes(good)
        junk.write_bytes(b"\x00" * 100)         # wrong size -> invalid
        other.write_text("nope")

        m = gui.QueueModel()
        added, dupes = m.add([a, junk])
        ok((added, dupes) == (2, 0), f"add: {(added, dupes)}")
        ok(len(m.items) == 2, f"items: {len(m.items)}")
        # duplicates (same file, different spelling) are skipped
        added, dupes = m.add([a, Path(td) / "." / "a.rec"])
        ok((added, dupes) == (0, 2), f"dedupe failed: {(added, dupes)}")
        # add_folder picks up only *.rec, sorted, deduped against the queue
        added, dupes = m.add_folder(d)
        ok((added, dupes) == (1, 2),
           f"add_folder must add only b.rec: {(added, dupes)}")
        ok([it.path.name for it in m.items] == ["a.rec", "junk.rec", "b.rec"],
           f"order wrong: {[it.path.name for it in m.items]}")

        # parse-at-add: valid vs invalid marking, no emulator involved
        ok(m.items[0].valid and m.items[0].status == gui.ST_WAITING,
           "valid record not marked waiting")
        ok(not m.items[1].valid and m.items[1].status == gui.ST_INVALID,
           "junk.rec not marked INVALID")
        ok("wrong size" in m.items[1].detail,
           f"invalid detail missing: {m.items[1].detail!r}")

        # only readable records go to the pipeline
        ok(m.convertible_indices() == [0, 1, 2],
           f"convertible: {m.convertible_indices()}")

        # remove (reverse-safe, dedupe set released so re-add works)
        ok(m.remove([1]) == 1, "remove count wrong")
        ok([it.path.name for it in m.items] == ["a.rec", "b.rec"],
           "wrong item removed")
        added, _ = m.add([junk])
        ok(added == 1, "removed path not re-addable")
        ok(m.remove([99, -1]) == 0, "out-of-range remove must be a no-op")
        m.clear()
        ok(not m.items and m.add([a])[0] == 1, "clear must reset the dedupe")
    print("   add/dedupe/folder/remove/clear + parse-at-add markers")


def test_row_formatting():
    print("-- item_row() / item_kind() / load_item()")
    good = build_synthetic_record()
    info = rec.parse(good)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "x.rec"
        p.write_bytes(good)
        item = gui.load_item(p)
        row = gui.item_row(item)
        ok(row[0] == "x.rec", f"file col: {row[0]!r}")
        ok(row[1] == info["facility"] and row[2] == info["level_mode"],
           f"facility/level cols: {row[1:3]}")
        ok(row[5] == "ok" and row[6] == gui.ST_WAITING,
           f"valid/status cols: {row[5:7]}")
        # opponent column shows the parse's name without any ROM
        ok(row[4] == gui.item_opponent(info) and row[4] not in ("", "?"),
           f"opponent col: {row[4]!r}")
        # missing file -> read error row, never an exception
        gone = gui.load_item(Path(td) / "missing.rec")
        ok(gone.read_error and gone.status == gui.ST_FAILED,
           "missing file must be a FAILED read-error item")
        ok(gui.item_row(gone)[5] == "unreadable", "unreadable marker missing")
    # kind precedence mirrors pipeline naming: multi > two-opponents >
    # double > link > single
    ok(gui.item_kind({"is_multi": True, "is_double": True}) == "multi",
       "kind precedence wrong")
    ok(gui.item_kind({"is_two_opponents": True, "is_link_recorded": True})
       == "two-opponents", "kind precedence wrong (two-opponents)")
    ok(gui.item_kind({"is_double": True}) == "double", "double kind")
    ok(gui.item_kind({}) == "single", "single kind")
    ok(gui.item_kind(None) == "?", "None info kind")
    print("   row values, unreadable files, kind precedence")


def test_status_formatting():
    print("-- format_result_status()")
    st, det = gui.format_result_status(
        {"status": "OK", "output": "/x/y/GUYA - Arena.mp4",
         "frames": 3600, "seconds": 60.06, "end_reason": "natural"})
    ok(st == "OK" and "3600f" in det and "60.1s" in det
       and det.endswith("GUYA - Arena.mp4"), f"OK row: {st} {det!r}")
    st, det = gui.format_result_status(
        {"status": "TRUNC", "output": "/x/p.mp4", "frames": 100,
         "seconds": 1.7, "end_reason": "timeout"})
    ok(st == "TRUNC" and "timeout" in det and "partial" in det,
       f"TRUNC row: {det!r}")
    st, det = gui.format_result_status(
        {"status": "INVALID", "detail": "bad sentinel", "error": "bad"})
    ok(st == "INVALID" and det == "bad sentinel", f"INVALID row: {det!r}")
    st, det = gui.format_result_status(
        {"status": "FAILED", "detail": "", "error": "boom"})
    ok(st == "FAILED" and det == "boom", f"FAILED row: {det!r}")
    print("   OK/TRUNC/INVALID/FAILED rows")


# ---------------------------------------------------------------------------
# Widget smoke — only with a real display; headless runners skip cleanly
# ---------------------------------------------------------------------------

def test_widget_smoke():
    print("-- widget construction smoke (display required)")
    if gui.tk is None:
        print("   SKIP: tkinter not available in this Python")
        return
    try:
        root = gui.tk.Tk()
    except gui.tk.TclError as exc:
        print(f"   SKIP: no display ({exc})")
        return
    try:
        root.withdraw()
        app = gui.GuiApp(root)
        good = build_synthetic_record()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "smoke.rec"
            p.write_bytes(good)
            app.model.add([p])
            app._refresh_tree()
            root.update_idletasks()
            ok(len(app.tree.get_children()) == 1, "tree not refreshed")
            s = app.read_settings()
            ok(s.scale == 4 and s.panel == "right" and s.panel_info == "all",
               f"form read-back wrong: {s}")
    finally:
        root.destroy()
    print("   window built, queue filled, settings read back")


def main():
    test_settings_defaults()
    test_settings_marshalling()
    test_queue_model()
    test_row_formatting()
    test_status_formatting()
    test_widget_smoke()
    if _checks == 0:
        print("VACUOUS RUN: zero checks executed")
        return 2
    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
