#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""Tests for the side panel, ROM species names, streak-aware export stems
and the pipeline API.

Plain python, no pytest — same style as tests/test_rec.py / test_naming.py.
Exits non-zero on failure. The stem-parser, panel-info, pipeline and
convert_one sections are asset-free real assertions and always run. The
Pillow render smoke runs only where Pillow is importable (the rec2mp4
conda env; skipped cleanly elsewhere). The species ground-truth section
needs the gitignored local/rom.gba + local/recs. Exit semantics (CI reads
these): 0 when everything that could run passed; 2 only for a vacuous run
(zero checks) or, under REC2MP4_REQUIRE_ASSETS=1 (strict local mode),
whenever the ROM section had to be skipped. No emulation happens here.

Run:  python3 tests/test_panel.py
"""

import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rec2mp4 import panel, pipeline, rec, romdata     # noqa: E402
from rec2mp4 import __main__ as cli                   # noqa: E402

ROM_PATH = os.path.join(ROOT, "local", "rom.gba")
RECS_DIR = os.path.join(ROOT, "local", "recs")

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


# ---------------------------------------------------------------------------
# Streak-aware export stems
# ---------------------------------------------------------------------------

def test_export_stem_parser():
    print("-- pipeline.parse_export_stem()")
    p = pipeline.parse_export_stem("GUYA_Factory-50-7_27-07-2026_10-40")
    ok(p is not None, "canonical new-format stem not parsed")
    ok(p["player"] == "GUYA", f"player wrong: {p['player']!r}")
    ok(p["facility_id"] == 4 and p["facility_word"] == "Factory",
       f"facility wrong: {p}")
    ok(p["level_mode"] == "Level 50", f"level mode wrong: {p['level_mode']}")
    ok(p["streak"] == 7, f"streak wrong: {p['streak']}")
    ok(p["rest"] == "27-07-2026_10-40", f"rest wrong: {p['rest']!r}")

    p = pipeline.parse_export_stem("ASH_Tower-O-123_01-01-2026_00-00")
    ok(p and p["level_mode"] == "Open Level" and p["streak"] == 123
       and p["facility_id"] == 0, f"Open-level Tower stem wrong: {p}")
    # every facility word, case-insensitive
    for word, fid in (("Dome", 1), ("Palace", 2), ("Arena", 3),
                      ("pike", 5), ("PYRAMID", 6)):
        p = pipeline.parse_export_stem(f"AB_{word}-50-1_x")
        ok(p and p["facility_id"] == fid,
           f"facility word {word!r} not mapped to {fid}: {p}")
    # streak 0 and a big streak are valid
    ok(pipeline.parse_export_stem("A_Tower-50-0_x")["streak"] == 0,
       "streak 0 rejected")
    ok(pipeline.parse_export_stem("A_Tower-O-99999_x")["streak"] == 99999,
       "5-digit streak rejected")

    # OLD format (today's PokeDNA exports) and junk -> None
    for stem in ("GUYA_20-07-2026_07-47",          # old format
                 "GUYA_27-07-2026_15-33",          # old format
                 "", "battle", "no_underscores-at-all",
                 "GUYA_Mars-50-7_x",               # not a facility word
                 "GUYA_Factory-51-7_x",            # mode must be O|50
                 "GUYA_Factory-o-7_x",             # mode is case-sensitive
                 "GUYA_Factory-50-_x",             # missing streak
                 "GUYA_Factory-50-x_y",            # non-numeric streak
                 "GUYA_Factory-50-123456_x",       # streak too long
                 "GUYA_Factory-50-7",              # no _rest
                 "_Factory-50-7_x",                # empty player
                 "WAYTOOLONGNAME_Factory-50-7_x"):  # player > 8 chars
        ok(pipeline.parse_export_stem(stem) is None,
           f"junk/old stem accepted: {stem!r}")
    ok(pipeline.parse_export_stem(None) is None, "None stem accepted")
    ok(pipeline.parse_export_stem(123) is None, "non-str stem accepted")
    print("   new format (all facilities, O/50, streak bounds), old format "
          "and junk -> None")


def test_export_stem_consistency():
    print("-- pipeline.check_export_stem()")
    parsed = pipeline.parse_export_stem("GUYA_Factory-50-7_x")
    match = {"facility_id": 4, "facility": "Battle Factory",
             "level_mode": "Level 50"}
    ok(pipeline.check_export_stem(parsed, match) is None,
       "consistent stem flagged")
    warn = pipeline.check_export_stem(
        parsed, {"facility_id": 3, "facility": "Battle Arena",
                 "level_mode": "Level 50"})
    ok(warn and "Factory" in warn and "Battle Arena" in warn
       and "trusting the record" in warn, f"facility mismatch text: {warn!r}")
    warn = pipeline.check_export_stem(
        parsed, {"facility_id": 4, "facility": "Battle Factory",
                 "level_mode": "Open Level"})
    ok(warn and "Level 50" in warn and "Open Level" in warn,
       f"level mismatch text: {warn!r}")
    print("   match -> None; facility/level mismatches -> warning, record "
          "trusted")


def test_streak_in_basename():
    print("-- build_output_basename(streak=...)")
    info = {"facility": "Battle Factory", "level_mode": "Level 50",
            "opponent_a": 83, "opponent_a_kind": "frontier",
            "opponent_a_name": "Frontier trainer #83"}
    base = cli.build_output_basename(info, "S", None, streak=7)
    ok(base == "S - Battle Factory Lv50 vs frontier trainer 83 (streak 7)",
       f"streak not in rich basename: {base!r}")
    ok(cli.build_output_basename(info, "S", None)
       == "S - Battle Factory Lv50 vs frontier trainer 83",
       "no-streak basename changed")
    ok(cli.build_output_basename(info, "S", None, plain=True, streak=7)
       == "S", "plain mode must ignore streak")
    print("   '(streak N)' appended in rich mode only")


def test_export_txt():
    print("-- pipeline.read_export_txt()")
    tmp = Path(tempfile.mkdtemp(prefix="rec2mp4-paneltest-"))
    try:
        rec_path = tmp / "GUYA_Factory-50-7_x.rec"
        rec_path.write_bytes(b"\x00")
        ok(pipeline.read_export_txt(rec_path) is None,
           "missing .txt must be None")
        txt = tmp / "GUYA_Factory-50-7_x.txt"
        txt.write_text("Streak 7\nFactory, Level 50\n\n  \n",
                       encoding="utf-8")
        lines = pipeline.read_export_txt(rec_path)
        ok(lines == ["Streak 7", "Factory, Level 50"],
           f"lines wrong: {lines!r}")
        # tolerant: invalid utf-8 must not raise
        txt.write_bytes(b"ok line\n\xff\xfe broken \xba\n")
        lines = pipeline.read_export_txt(rec_path)
        ok(lines is not None and lines[0] == "ok line" and len(lines) == 2,
           f"invalid utf-8 not tolerated: {lines!r}")
        # caps: 300-char lines, 200 lines
        txt.write_text("x" * 1000 + "\n" + "y\n" * 500, encoding="utf-8")
        lines = pipeline.read_export_txt(rec_path)
        ok(len(lines[0]) == 300 and len(lines) <= 200,
           f"caps not applied: {len(lines[0])} chars, {len(lines)} lines")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("   missing -> None; utf-8 tolerant; trailing blanks dropped; "
          "capped")


# ---------------------------------------------------------------------------
# Panel-info parsing (PIL-free) + sidecar extras
# ---------------------------------------------------------------------------

def test_panel_info():
    print("-- panel.parse_panel_info()")
    ok(panel.parse_panel_info("all") == panel.PANEL_SECTIONS,
       "'all' must select every section")
    ok(panel.parse_panel_info("") == panel.PANEL_SECTIONS,
       "'' must select every section")
    ok(panel.parse_panel_info(None) == panel.PANEL_SECTIONS,
       "None must select every section")
    ok(panel.parse_panel_info("teams, header") == ("header", "teams"),
       "subset must keep draw order")
    ok(panel.parse_panel_info("HEADER") == ("header",),
       "section names must be case-insensitive")
    ok(panel.parse_panel_info("teams,teams") == ("teams",),
       "duplicates must collapse")
    try:
        panel.parse_panel_info("header,bogus")
        ok(False, "unknown section must raise ValueError")
    except ValueError as exc:
        ok("bogus" in str(exc) and "footer" in str(exc),
           f"unhelpful ValueError: {exc}")
    print("   all/empty/subset/case/dupes; unknown -> ValueError")


def test_sidecar_extras():
    print("-- build_sidecar() streak/export_info extras")
    from rec2mp4.driver import ReplayResult
    result = ReplayResult(frames=10, seconds=0.2, end_reason="natural",
                          outcome=1, outcome_text="won")
    base_kwargs = dict(source_rec_name="X.rec", rec_bytes=b"\x00",
                       info={}, rom_crc32=0, options={}, result=result,
                       output_name="X.mp4")
    sc = cli.build_sidecar(**base_kwargs)
    ok("streak" not in sc and "export_info" not in sc,
       "extras must be absent by default (pre-streak sidecars unchanged)")
    sc = cli.build_sidecar(**base_kwargs, streak=7,
                           export_info=["a", "b"])
    ok(sc["streak"] == 7 and sc["export_info"] == ["a", "b"],
       f"extras not stored: {sc.get('streak')}, {sc.get('export_info')}")
    print("   absent by default; present when given")


# ---------------------------------------------------------------------------
# Pillow render smoke (conda env only; clean skip elsewhere)
# ---------------------------------------------------------------------------

def _panel_fixture():
    info = {
        "facility": "Battle Factory", "facility_id": 4,
        "level_mode": "Level 50", "rng_seed": "8f3a2c01",
        "is_double": True, "is_multi": False, "is_two_opponents": False,
        "is_link_recorded": False,
        "opponent_a": 83, "opponent_a_kind": "frontier",
        "opponent_a_name": "Frontier trainer #83",
        "opponent_b": 0, "opponent_b_kind": None, "opponent_b_name": None,
        "recorded_by": "GUYA", "recorded_by_gender": "M",
        "players": ["GUYA"], "players_language": ["ENG"],
        "multiplayer_id": 0,
        "teams": {
            "player": [
                {"nickname": "METAGROSS", "species_internal": 400,
                 "level": 55, "shiny": False, "checksum_ok": True},
                {"nickname": "SPIKE", "species_internal": 397,
                 "level": 50, "shiny": True, "checksum_ok": True},
            ],
            "opponent": [
                {"nickname": "EEVEE", "species_internal": 133,
                 "level": 50, "shiny": False, "checksum_ok": True},
            ],
        },
    }
    extras = {
        "rom_bytes": None,                  # species fall back to '#<id>'
        "outcome_text": "won", "duration_seconds": 83.7, "streak": 7,
        "export_lines": ["Streak 7", "Factory, Level 50",
                         "x" * 100,          # too long -> skipped
                         "exported by PokeDNA"],
        "sections": panel.PANEL_SECTIONS,
        "opponent_a_label": None, "opponent_b_label": None,
    }
    return info, extras


def test_panel_render() -> bool:
    print("-- panel.render_panel() smoke")
    if not _pil_available():
        try:
            panel.render_panel({}, {}, (480, 640))
            ok(False, "render_panel must raise without Pillow")
        except RuntimeError as exc:
            ok("pillow" in str(exc).lower()
               and "miniconda3/envs/rec2mp4" in str(exc),
               f"Pillow error must name the conda env: {exc}")
        print("   SKIP: Pillow not importable here — render smoke needs "
              "the rec2mp4 conda env "
              "(~/miniconda3/envs/rec2mp4/bin/python); "
              "verified the clear no-Pillow error instead")
        return False
    from PIL import Image
    info, extras = _panel_fixture()
    for size in ((480, 640), (240, 320)):   # scale 4 and scale 2
        png = panel.render_panel(info, extras, size)
        ok(png[:8] == b"\x89PNG\r\n\x1a\n", f"not a PNG at {size}")
        img = Image.open(io.BytesIO(png))
        ok(img.size == size, f"size wrong: {img.size} != {size}")
        # dark background, but not a blank image: some light pixels drawn
        px = img.convert("L").tobytes()
        ok(sum(1 for v in px if v > 150) > 50,
           f"panel at {size} looks blank (no bright text pixels)")
    # single-section renders must not crash and stay the right size
    for section in panel.PANEL_SECTIONS:
        extras_one = dict(extras, sections=(section,))
        png = panel.render_panel(info, extras_one, (480, 640))
        ok(Image.open(io.BytesIO(png)).size == (480, 640),
           f"section {section!r} alone broke the canvas")
    # ROM-backed species names when the ROM is around
    if os.path.isfile(ROM_PATH):
        extras_rom = dict(extras, rom_bytes=open(ROM_PATH, "rb").read())
        png = panel.render_panel(info, extras_rom, (480, 640))
        ok(png[:8] == b"\x89PNG\r\n\x1a\n", "ROM-backed render failed")
    # too-small canvas is a hard error, not a garbage render
    try:
        panel.render_panel(info, extras, (10, 10))
        ok(False, "tiny canvas must raise ValueError")
    except ValueError:
        ok(True, "tiny canvas raises ValueError")
    print("   PNG magic + exact size at scale 2/4, text drawn, every "
          "single-section subset, tiny canvas rejected")
    return True


# ---------------------------------------------------------------------------
# Species names — ground truth from the USER'S real records + ROM
# ---------------------------------------------------------------------------

def test_species_ground_truth(rom_bytes: bytes) -> None:
    print("-- romdata.species_name() against local/rom.gba + local/recs")
    # GROUND TRUTH: across the real records, default-named mons carry their
    # species name as nickname (the game default). Every (species id,
    # nickname) pair harvested from the user's own records must match what
    # species_name() reads out of the user's ROM.
    pairs = {}
    for rp in sorted(Path(RECS_DIR).glob("*.rec")):
        info = rec.parse(rp.read_bytes())
        if not info["valid"]:
            continue
        for side in ("player", "opponent"):
            for m in info["teams"][side]:
                if m["nickname"]:
                    pairs.setdefault(m["species_internal"], m["nickname"])
    ok(len(pairs) >= 5,
       f"need >= 5 distinct species from real records, got {len(pairs)}")
    mismatches = {sid: (nick, romdata.species_name(rom_bytes, sid))
                  for sid, nick in pairs.items()
                  if romdata.species_name(rom_bytes, sid) != nick.upper()}
    ok(not mismatches, f"ROM species names != record nicknames: "
                       f"{dict(list(mismatches.items())[:5])}")
    # special cases and doubt paths
    ok(romdata.species_name(rom_bytes, romdata.SPECIES_EGG) == "EGG",
       "id 412 must be the EGG label")
    hole = romdata.species_name(rom_bytes, 252)     # OLD_UNOWN hole
    ok(hole is not None and hole.strip("?") == "",
       f"hole id 252 must decode to '?' placeholders, got {hole!r}")
    ok(romdata.species_name(rom_bytes, 413) is None, "id 413 must be None")
    ok(romdata.species_name(rom_bytes, -1) is None, "negative id -> None")
    ok(romdata.species_name(rom_bytes, True) is None, "bool id -> None")
    ok(romdata.species_name(b"\x00" * 0x1000, 1) is None,
       "tiny ROM must be None")
    ok(romdata.species_name(None, 1) is None, "None ROM must be None")
    print(f"   {len(pairs)} distinct species from the real records all "
          "match the ROM; EGG/hole/doubt paths OK")


# ---------------------------------------------------------------------------
# Pipeline API without an emulator
# ---------------------------------------------------------------------------

def test_pipeline_no_emulator():
    print("-- pipeline import + convert_one() paths that need no emulator")
    s = pipeline.ConvertSettings()
    ok(s.panel == "right" and s.scale == 4 and s.audio is True
       and s.sidecar is True and s.anims == "on"
       and s.text_speed == "record" and s.pix_fmt == "rgb0",
       "ConvertSettings defaults wrong")

    # stack probes: pillow_available agrees with a real import; stack_status
    # reports the keys front-ends rely on; pillow_hint names the conda env.
    ok(pipeline.pillow_available() == _pil_available(),
       "pillow_available() disagrees with a direct import")
    st = pipeline.stack_status()
    for key in ("pillow", "emulator", "ffmpeg", "ok", "interpreter"):
        ok(key in st, f"stack_status missing {key!r}: {st}")
    ok(st["pillow"] == _pil_available(), "stack_status pillow flag wrong")
    ok("miniconda3/envs/rec2mp4" in pipeline.pillow_hint(),
       "pillow_hint must name the rec2mp4 conda env")

    tmp = Path(tempfile.mkdtemp(prefix="rec2mp4-pipe-"))
    logs, errs = [], []
    try:
        # INVALID record: rejected before any context/emulator is touched
        bad = tmp / "bad.rec"
        bad.write_bytes(b"\x00" * 100)
        res = pipeline.convert_one(bad, s, log=logs.append,
                                   err=errs.append)
        ok(res["status"] == "INVALID", f"status: {res['status']}")
        ok("wrong size" in res["detail"], f"detail: {res['detail']!r}")
        ok(res["output"] is None and res["frames"] == 0,
           "INVALID must produce no output")
        # panel_applied is part of the result contract; None until a video is
        # actually composited (INVALID never gets that far).
        ok("panel_applied" in res and res["panel_applied"] is None,
           f"panel_applied must default None: {res.get('panel_applied')!r}")
        ok(logs and logs[0] == "invalid record — skipping:",
           f"log lines wrong: {logs[:2]}")
        ok(not errs, f"INVALID must not write to err: {errs}")

        # unreadable path -> FAILED via err
        logs.clear(), errs.clear()
        res = pipeline.convert_one(tmp / "nope.rec", s, log=logs.append,
                                   err=errs.append)
        ok(res["status"] == "FAILED"
           and res["detail"].startswith("read error:"),
           f"missing file: {res['status']} / {res['detail']!r}")
        ok(errs and errs[0].startswith("cannot read:"),
           f"err lines wrong: {errs}")

        # load_context with a bogus ROM -> PipelineError (no sys.exit)
        try:
            pipeline.load_context(pipeline.ConvertSettings(
                rom=tmp / "no-such.gba", outdir=tmp))
            ok(False, "load_context must raise for a missing ROM")
        except pipeline.PipelineError as exc:
            ok(str(exc).startswith("ROM not found:"),
               f"PipelineError text: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("   defaults; INVALID/FAILED short-circuit before the emulator; "
          "PipelineError preflight")


def main():
    test_export_stem_parser()
    test_export_stem_consistency()
    test_streak_in_basename()
    test_export_txt()
    test_panel_info()
    test_sidecar_extras()
    test_panel_render()
    test_pipeline_no_emulator()
    have_assets = (os.path.isfile(ROM_PATH) and os.path.isdir(RECS_DIR)
                   and any(Path(RECS_DIR).glob("*.rec")))
    if have_assets:
        test_species_ground_truth(open(ROM_PATH, "rb").read())
        print(f"PASS: {_checks} checks")
        return
    # local/ is gitignored — a fresh clone / CI has no ROM or records. The
    # sections above are real assertions, so this run is NOT vacuous:
    # report the skip loudly but exit 0. Exit 2 ("required assets missing")
    # is kept for a vacuous run, and for strict local runs that opt in via
    # REC2MP4_REQUIRE_ASSETS=1.
    print(f"SKIP: {ROM_PATH} and/or local/recs absent — the species "
          "ground-truth section needs your own US Emerald ROM and real "
          ".rec exports and did NOT run")
    if _checks == 0 or os.environ.get("REC2MP4_REQUIRE_ASSETS"):
        print(f"SKIPPED, not passed: {_checks} asset-free check(s) ran, but "
              "the species ground-truth section did not")
        sys.exit(2)
    print(f"PASS: {_checks} asset-free checks "
          "(species ground-truth section skipped)")


if __name__ == "__main__":
    main()
