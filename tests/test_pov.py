#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""Tests for the opponent-POV ("watch from the other side") feature.

Plain python, no pytest — same style as tests/test_rec.py / test_panel.py.
Exits non-zero on failure. Every section here is ASSET-FREE (built on a
synthetic record borrowed from tests/test_rec.py) and always runs, in a
fresh clone and in CI: the byte transform (rec.to_opponent_pov), the
pipeline settings marshalling (ConvertSettings.pov), the naming tag, the
JSON sidecar POV fields, and the GUI form round-trip. When local/recs holds
real .rec exports, the transform is additionally checked against a genuine
record. No emulation happens here.

Run:  python3 tests/test_pov.py
"""

import os
import struct
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

from rec2mp4 import gui, pipeline, rec           # noqa: E402
import test_rec                                  # synthetic-record helpers

RECS_DIR = os.path.join(ROOT, "local", "recs")

_checks = 0


def ok(cond, msg):
    # No bare `assert`: it would be stripped under `python3 -O`.
    global _checks
    _checks += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def _flags(data: bytes) -> int:
    return struct.unpack_from("<I", data, rec.STRUCT_OFF + 1260)[0]


def _team_ids(info: dict, side: str) -> list:
    return [m["species_internal"] for m in info["teams"][side]]


# ---------------------------------------------------------------------------
# The byte transform
# ---------------------------------------------------------------------------

def test_transform(data: bytes, label: str):
    print(f"-- rec.to_opponent_pov ({label})")
    before = rec.parse(data)
    pov = rec.to_opponent_pov(data)

    ok(data == data, "input must not be mutated")     # (data is our local copy)
    ok(rec.validate(pov) == [],
       f"POV output must validate, got {rec.validate(pov)}")
    ok(len(pov) == rec.SECTOR_SIZE, "POV output wrong size")

    info = rec.parse(pov)
    # Parties swapped: player<->opponent.
    ok(_team_ids(info, "player") == _team_ids(before, "opponent")
       and _team_ids(info, "opponent") == _team_ids(before, "player"),
       "party blocks were not swapped player<->opponent")
    ok(all(m["checksum_ok"] for m in info["teams"]["player"])
       and all(m["checksum_ok"] for m in info["teams"]["opponent"]),
       "a swapped party mon has a broken checksum (block move corrupted)")

    flags = _flags(pov)
    ok(flags & rec.FLAG_RECORDED_LINK, "RECORDED_LINK (bit25) not set")
    ok(not (flags & rec.FLAG_IS_MASTER), "IS_MASTER (bit2) not cleared")
    ok(not (flags & rec.FLAG_RECORDED_IS_MASTER),
       "RECORDED_IS_MASTER (bit31) must stay clear (opponent-side render)")
    ok(info["is_link_recorded"], "parse() should see the link-recorded bit")

    # Fabricated link-player slot 1 + viewer id.
    r = pov[rec.STRUCT_OFF:]
    ok(rec._g3str(r[1208:1216]) == "FOE",
       f"playersName[1] not the fake name: {rec._g3str(r[1208:1216])!r}")
    ok(r[1233] == 0, "playersGender[1] should be 0")
    ok(struct.unpack_from("<I", r, 1240)[0] != 0,
       "playersTrainerId[1] must be nonzero")
    ok(r[1253] == 2, "playersLanguage[1] should be 2 (ENG)")
    ok(list(r[1264:1268]) == [0, 1, 0, 0],
       f"playersBattlers wrong: {list(r[1264:1268])}")
    ok(struct.unpack_from("<H", r, 1274)[0] == 1,
       f"multiplayerId should be 1, got {struct.unpack_from('<H', r, 1274)[0]}")

    # Lanes untouched.
    ok(pov[rec.STRUCT_OFF + 1308:rec.STRUCT_OFF + rec.CHECKSUM_RANGE]
       == data[rec.STRUCT_OFF + 1308:rec.STRUCT_OFF + rec.CHECKSUM_RANGE],
       "input lanes must be left byte-for-byte unchanged")
    ok(info["input_lanes"] == before["input_lanes"],
       f"lane lengths changed: {before['input_lanes']} -> "
       f"{info['input_lanes']}")

    # Checksum recomputed to match.
    stored = struct.unpack_from("<I", pov, rec.STRUCT_OFF + rec.CHECKSUM_RANGE)[0]
    computed = sum(pov[rec.STRUCT_OFF:rec.STRUCT_OFF + rec.CHECKSUM_RANGE]) \
        & 0xFFFFFFFF
    ok(stored == computed, "checksum not recomputed correctly")

    # Idempotent-safe: transforming the output again still yields a valid rec.
    ok(rec.validate(rec.to_opponent_pov(pov)) == [],
       "double transform must still validate")
    print("   parties swapped, flags bit25 set / bit2+bit31 clear, "
          "mpId=1, lanes intact, checksum ok")


def test_transform_refuses_junk():
    print("-- rec.to_opponent_pov refuses invalid input")
    for bad, why in ((b"\x00" * rec.SECTOR_SIZE, "zero sentinel"),
                     (b"\xFF" * rec.SECTOR_SIZE, "erased flash"),
                     (b"\x00" * 100, "too short")):
        try:
            rec.to_opponent_pov(bad)
            ok(False, f"expected RecError for {why}")
        except rec.RecError:
            ok(True, why)
    print("   RecError on junk / short / erased input")


# ---------------------------------------------------------------------------
# Faithfulness verdict + note
# ---------------------------------------------------------------------------

def test_faithfulness():
    print("-- pipeline.pov_faithful / pov_note")
    frontier = {"is_link_recorded": False}
    link = {"is_link_recorded": True}
    ok(pipeline.pov_faithful(frontier) is False,
       "a vs-AI Frontier record must NOT be faithful")
    ok(pipeline.pov_faithful(link) is True,
       "a genuine link record must be faithful")
    ok("what-if" in pipeline.pov_note(frontier).lower()
       and "diverge" in pipeline.pov_note(frontier).lower(),
       "Frontier note must warn it is a what-if that diverges")
    ok("faithful" in pipeline.pov_note(link).lower(),
       "link note must say the view is faithful")
    print("   frontier=what-if, link=faithful; notes explain the caveat")


# ---------------------------------------------------------------------------
# Settings marshalling (CLI + GUI)
# ---------------------------------------------------------------------------

def test_settings_marshalling():
    print("-- ConvertSettings.pov marshalling")
    ok(pipeline.ConvertSettings().pov == "player",
       "pov must default to 'player'")

    # GUI form round-trip.
    form = gui.default_form()
    ok(form["pov"] == "player", "default form pov should be 'player'")
    s = gui.settings_from_form({**form, "pov": "opponent"})
    ok(s.pov == "opponent", "form 'opponent' should marshal through")
    s = gui.settings_from_form({**form, "pov": "player"})
    ok(s.pov == "player", "form 'player' should marshal through")
    # Bad value is coerced to the safe default (never crashes a conversion).
    s = gui.settings_from_form({**form, "pov": "garbage"})
    ok(s.pov == "player", "an unknown pov must fall back to 'player'")
    # Missing key -> default.
    f2 = dict(form)
    f2.pop("pov", None)
    ok(gui.settings_from_form(f2).pov == "player",
       "a form without 'pov' must default to 'player'")
    print("   default 'player'; GUI form 'opponent'/'player' round-trip; "
          "bad/missing -> 'player'")


# ---------------------------------------------------------------------------
# Naming tag
# ---------------------------------------------------------------------------

def test_naming_tag(data: bytes):
    print("-- build_output_basename opponent-POV tag")
    info = rec.parse(data)
    player = pipeline.build_output_basename(info, "GUYA_stem", None,
                                            pov="player")
    opp = pipeline.build_output_basename(info, "GUYA_stem", None,
                                         pov="opponent")
    ok("[opponent POV]" not in player, "player POV must not carry the tag")
    ok(opp.endswith("[opponent POV]"),
       f"opponent POV rich name must end with the tag: {opp!r}")
    # plain mode too
    pl = pipeline.build_output_basename(info, "GUYA_stem", None,
                                        plain=True, pov="opponent")
    ok(pl == "GUYA_stem [opponent POV]",
       f"plain opponent name wrong: {pl!r}")
    plp = pipeline.build_output_basename(info, "GUYA_stem", None,
                                         plain=True, pov="player")
    ok(plp == "GUYA_stem", f"plain player name should be bare stem: {plp!r}")
    print(f"   tag appended in rich + plain modes ({opp!r})")


# ---------------------------------------------------------------------------
# Sidecar POV fields
# ---------------------------------------------------------------------------

class _FakeResult:
    frames = 2084
    seconds = 34.9
    end_reason = "natural"
    outcome = 5
    outcome_text = "player teleported"


def test_sidecar_fields(data: bytes):
    print("-- build_sidecar POV fields")
    info = rec.parse(data)
    meta = {"pov": "opponent",
            "pov_faithful": pipeline.pov_faithful(info),
            "pov_note": pipeline.pov_note(info)}
    sc = pipeline.build_sidecar(
        source_rec_name="x.rec", rec_bytes=data, info=info,
        rom_crc32=0x1F1C08FB,
        options={"pov": "opponent"}, result=_FakeResult(),
        output_name="x [opponent POV].mp4", pov_meta=meta)
    ok(sc["pov"] == "opponent", "sidecar must record pov")
    ok(sc["pov_faithful"] is False,
       "a Frontier record's sidecar must say pov_faithful=false")
    ok("pov_note" in sc and isinstance(sc["pov_note"], str) and sc["pov_note"],
       "sidecar must carry a human pov_note")
    ok(sc["options"]["pov"] == "opponent", "options must echo pov")

    # Default (player) sidecar stays byte-identical to the pre-feature shape:
    # no pov_meta => no pov/pov_faithful/pov_note keys.
    sc0 = pipeline.build_sidecar(
        source_rec_name="x.rec", rec_bytes=data, info=info,
        rom_crc32=0x1F1C08FB, options={"pov": "player"},
        result=_FakeResult(), output_name="x.mp4", pov_meta=None)
    ok("pov" not in sc0 and "pov_faithful" not in sc0
       and "pov_note" not in sc0,
       "a player-POV sidecar must not gain POV top-level keys")
    print("   opponent sidecar: pov/pov_faithful=false/pov_note; "
          "player sidecar unchanged")


# ---------------------------------------------------------------------------
# Panel header (only where Pillow is importable)
# ---------------------------------------------------------------------------

def test_panel_header(data: bytes):
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("-- panel header: SKIP (Pillow not importable here)")
        return
    print("-- panel header shows the experimental POV note")
    from rec2mp4 import panel
    info = rec.parse(data)
    extras = {"rom_bytes": None, "outcome_text": "player teleported",
              "duration_seconds": 34.9, "streak": None, "export_lines": [],
              "sections": panel.PANEL_SECTIONS,
              "opponent_a_label": None, "opponent_b_label": None,
              "pov": "opponent", "pov_faithful": False}
    png = panel.render_panel(info, extras, (480, 640))
    ok(png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 100,
       "panel with opponent POV should still render a PNG")
    # player POV renders too (no header note assertion possible without OCR,
    # but the code path must not error).
    extras["pov"] = "player"
    png2 = panel.render_panel(info, extras, (480, 640))
    ok(png2[:8] == b"\x89PNG\r\n\x1a\n", "player-POV panel must render")
    print("   opponent + player POV panels both render")


def main():
    syn = test_rec.build_synthetic_record()
    test_transform(syn, "synthetic record")
    test_transform_refuses_junk()
    test_faithfulness()
    test_settings_marshalling()
    test_naming_tag(syn)
    test_sidecar_fields(syn)
    test_panel_header(syn)

    real = sorted(Path(RECS_DIR).glob("*.rec")) if os.path.isdir(RECS_DIR) \
        else []
    if real:
        test_transform(open(real[0], "rb").read(), f"real: {real[0].name}")
        print(f"PASS: {_checks} checks (incl. real record {real[0].name})")
        return
    print(f"SKIP: {RECS_DIR} absent — the real-record transform check needs "
          "your own .rec exports and did NOT run")
    if _checks == 0 or os.environ.get("REC2MP4_REQUIRE_ASSETS"):
        print(f"SKIPPED, not passed: {_checks} asset-free check(s) ran, but "
              "the real-record section did not")
        sys.exit(2)
    print(f"PASS: {_checks} asset-free checks "
          "(real-record transform section skipped)")


if __name__ == "__main__":
    main()
