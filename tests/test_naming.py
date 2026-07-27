#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""Tests for rich output naming, romdata ROM lookups and the JSON sidecar.

Plain python, no pytest — same style as tests/test_rec.py. Exits non-zero
on failure. The sanitize/builder/collision/sidecar sections are asset-free
real assertions and always run; only the ROM ground-truth section needs the
gitignored local/rom.gba. Exit semantics (CI reads these): 0 when everything
that could run passed (ROM section included when the ROM is present, cleanly
skipped when absent — the run is NOT vacuous either way); 2 only for a
vacuous run (zero checks) or, under REC2MP4_REQUIRE_ASSETS=1 (strict local
mode), whenever the ROM section had to be skipped. No emulation happens here.

Run:  python3 tests/test_naming.py
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rec2mp4 import romdata                          # noqa: E402
from rec2mp4 import __main__ as cli                  # noqa: E402
from rec2mp4.driver import ReplayResult              # noqa: E402

ROM_PATH = os.path.join(ROOT, "local", "rom.gba")

_checks = 0


def ok(cond, msg):
    # No bare `assert`: it would be stripped under `python3 -O`, turning
    # the whole suite into a vacuous PASS.
    global _checks
    _checks += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def _info(**over):
    """Minimal rec.parse()-shaped dict for the filename builder."""
    base = {
        "facility": "Battle Dome", "level_mode": "Open Level",
        "is_double": False, "is_multi": False, "is_two_opponents": False,
        "is_link_recorded": False,
        "opponent_a": 83, "opponent_a_kind": "frontier",
        "opponent_a_name": "Frontier trainer #83",
        "opponent_b": 0, "opponent_b_kind": None, "opponent_b_name": None,
    }
    base.update(over)
    return base


def test_sanitize():
    print("-- sanitize_filename()")
    ok(cli.sanitize_filename('a<b>c:d"e/f\\g|h?i*j') == "abcdefghij",
       "Windows-reserved chars not stripped")
    ok(cli.sanitize_filename("x\x00y\x1fz\x7fw") == "xyzw",
       "control chars not stripped")
    ok(cli.sanitize_filename("café résumé") == "caf rsum",
       "non-ASCII not stripped")
    ok(cli.sanitize_filename("  a   b  ") == "a b",
       "whitespace not collapsed")
    ok(cli.sanitize_filename("name...  ") == "name",
       "trailing dots/spaces kept (Windows-unsafe)")
    ok(cli.sanitize_filename("???") == "", "all-stripped input not empty")
    # Windows reserved device names (reserved even with an extension:
    # 'CON.mp4' opens the console device) must be defused
    ok(cli.sanitize_filename("CON") == "_CON", "CON not defused")
    ok(cli.sanitize_filename("nul") == "_nul", "nul (lowercase) not defused")
    ok(cli.sanitize_filename("COM1") == "_COM1", "COM1 not defused")
    ok(cli.sanitize_filename("con.backup") == "_con.backup",
       "reserved stem before a dot not defused")
    ok(cli.sanitize_filename("CONSOLE") == "CONSOLE",
       "CONSOLE wrongly treated as reserved")
    ok(cli.sanitize_filename("COM10") == "COM10",
       "COM10 wrongly treated as reserved (only COM1-COM9 are)")
    print("   reserved/control/non-ASCII stripped, whitespace collapsed, "
          "device names defused")


def test_builder():
    print("-- build_output_basename()")
    # frontier opponent without ROM bytes -> descriptive fallback
    ok(cli.build_output_basename(_info(is_double=True), "STEM", None)
       == "STEM - Battle Dome Open double vs frontier trainer 83",
       "frontier fallback name wrong")
    # level 50, singles (no kind token)
    ok(cli.build_output_basename(_info(level_mode="Level 50"), "S", None)
       == "S - Battle Dome Lv50 vs frontier trainer 83",
       "Lv50 singles name wrong")
    # kind priority: multi wins over double
    ok(" multi vs " in cli.build_output_basename(
        _info(is_multi=True, is_double=True), "S", None),
       "multi should outrank double")
    ok(" two-opponents vs " in cli.build_output_basename(
        _info(is_two_opponents=True, is_double=True), "S", None),
       "two-opponents should outrank double")
    ok(" link vs " in cli.build_output_basename(
        _info(is_link_recorded=True), "S", None),
       "link token missing")
    # second opponent
    two = cli.build_output_basename(
        _info(is_two_opponents=True, opponent_b=12,
              opponent_b_kind="frontier",
              opponent_b_name="Frontier trainer #12"), "S", None)
    ok(two.endswith("vs frontier trainer 83 and frontier trainer 12"),
       f"opponent B not appended: {two!r}")
    # fallback labels for the non-ROM opponent kinds
    ok(cli.build_output_basename(
        _info(opponent_a=350, opponent_a_kind="record_mix_friend",
              opponent_a_name="ALICE (record-mix friend, ENG)"), "S", None)
       .endswith("vs ALICE"), "record-mix friend name not extracted")
    ok(cli.build_output_basename(
        _info(opponent_a=350, opponent_a_kind="record_mix_friend",
              opponent_a_name="? (record-mix friend, ENG)"), "S", None)
       .endswith("vs record-mix friend"), "unnamed friend fallback wrong")
    # all-'?' name (undecodable glyphs, e.g. a Japanese friend) -> fallback,
    # not a filename with a dangling 'vs' after sanitize strips every '?'
    ok(cli.build_output_basename(
        _info(opponent_a=350, opponent_a_kind="record_mix_friend",
              opponent_a_name="??? (record-mix friend, JPN)"), "S", None)
       .endswith("vs record-mix friend"), "all-'?' friend fallback wrong")
    ok(cli.build_output_basename(
        _info(opponent_a=405, opponent_a_kind="apprentice",
              opponent_a_name="Apprentice #7"), "S", None)
       .endswith("vs Apprentice 7"), "apprentice label wrong")
    ok(cli.build_output_basename(
        _info(opponent_a=1022, opponent_a_kind="frontier_brain",
              opponent_a_name="Frontier Brain"), "S", None)
       .endswith("vs Frontier Brain"), "brain label wrong")
    # stem with Windows-hostile chars is sanitized as part of the whole
    ok(cli.build_output_basename(_info(), 'we<ird:stem?', None)
       .startswith("weirdstem - "), "hostile stem not sanitized")
    # plain mode
    ok(cli.build_output_basename(_info(), "MY_REC", None, plain=True)
       == "MY_REC", "plain mode must keep just the stem")
    ok(cli.build_output_basename(_info(), "???", None, plain=True)
       == "record", "empty-after-sanitize stem needs a fallback")
    # reserved device-name stem in plain mode must not yield 'CON.mp4'
    ok(cli.build_output_basename({}, "CON", None, plain=True) == "_CON",
       "reserved plain stem not defused")
    # length cap: a 300-char stem must not exceed the 255-byte name limit
    long_stem = "x" * 300
    capped = cli.build_output_basename(_info(), long_stem, None)
    ok(len(capped) <= 180 and not capped.endswith((" ", ".")),
       f"rich basename not capped: {len(capped)} chars")
    ok(len(cli.build_output_basename({}, long_stem, None, plain=True)) <= 180,
       "plain basename not capped")
    print("   facility/level/kind tokens, opponents, fallbacks, plain mode, "
          "180-char cap")


def test_collisions():
    print("-- resolve_output_path()")
    tmp = Path(tempfile.mkdtemp(prefix="rec2mp4-naming-"))
    try:
        used = set()
        p1 = cli.resolve_output_path(tmp, "base", "a.rec", used)
        ok(p1 == tmp / "base.mp4", f"first path wrong: {p1}")
        # same batch, different record, same basename -> bumped
        p2 = cli.resolve_output_path(tmp, "base", "b.rec", used)
        ok(p2 == tmp / "base (2).mp4", f"in-batch collision not bumped: {p2}")
        # existing file whose sidecar names a DIFFERENT source -> bumped
        (tmp / "old.mp4").write_bytes(b"x")
        (tmp / "old.json").write_text(json.dumps({"source_rec": "other.rec"}))
        p3 = cli.resolve_output_path(tmp, "old", "mine.rec", set())
        ok(p3 == tmp / "old (2).mp4", f"foreign file not protected: {p3}")
        # existing file whose sidecar names the SAME source -> overwrite
        (tmp / "same.mp4").write_bytes(b"x")
        (tmp / "same.json").write_text(json.dumps({"source_rec": "mine.rec"}))
        p4 = cli.resolve_output_path(tmp, "same", "mine.rec", set())
        ok(p4 == tmp / "same.mp4", f"re-run of same record bumped: {p4}")
        # existing file with no sidecar (basename embeds the stem) -> overwrite
        (tmp / "bare.mp4").write_bytes(b"x")
        p5 = cli.resolve_output_path(tmp, "bare", "mine.rec", set())
        ok(p5 == tmp / "bare.mp4", f"sidecar-less re-run bumped: {p5}")
        # chain: (2) also taken by a foreign file -> (3)
        (tmp / "old (2).mp4").write_bytes(b"x")
        (tmp / "old (2).json").write_text(
            json.dumps({"source_rec": "third.rec"}))
        p6 = cli.resolve_output_path(tmp, "old", "mine.rec", set())
        ok(p6 == tmp / "old (3).mp4", f"suffix chain broken: {p6}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("   overwrite own output, bump past foreign ones, (2)/(3) chain")


def test_sidecar_shape():
    print("-- build_sidecar() (synthetic, no emulation)")
    result = ReplayResult(frames=5000, seconds=83.7, end_reason="natural",
                          outcome=1, outcome_text="won")
    info = _info()
    sc = cli.build_sidecar(
        source_rec_name="X.rec", rec_bytes=b"\xAB" * 4096, info=info,
        rom_crc32=0x1F1C08FB,
        options={"anims": "on", "text_speed": "record", "scale": 4,
                 "audio": True, "pix_fmt": "rgb0"},
        result=result, output_name="X - Battle Dome Open vs Y.mp4")
    for key in ("rec2mp4_version", "generated_at", "source_rec",
                "source_rec_sha1", "rom_crc32", "output", "options",
                "replay", "record"):
        ok(key in sc, f"sidecar missing key {key!r}")
    ok(sc["source_rec"] == "X.rec", "source_rec wrong")
    import hashlib
    ok(sc["source_rec_sha1"] == hashlib.sha1(b"\xAB" * 4096).hexdigest(),
       "sha1 mismatch")
    ok(sc["rom_crc32"] == "1f1c08fb", f"crc32 format: {sc['rom_crc32']!r}")
    ok(sc["replay"] == {"frames": 5000, "seconds": 83.7,
                        "end_reason": "natural", "outcome": 1,
                        "outcome_text": "won"}, f"replay block: {sc['replay']}")
    ok(sc["record"] is info, "record must be the full parse() dict")
    ok(sc["options"]["scale"] == 4 and sc["options"]["audio"] is True,
       "options not carried through")
    ok("T" in sc["generated_at"] and "+00:00" in sc["generated_at"],
       f"generated_at not ISO-8601 UTC: {sc['generated_at']!r}")
    dumped = json.dumps(sc)                 # must be JSON-serializable
    ok(json.loads(dumped)["rec2mp4_version"] == sc["rec2mp4_version"],
       "sidecar does not survive a JSON round-trip")
    # default-outcome ReplayResult (older/truncated path) still serializes
    r0 = ReplayResult(frames=1, seconds=0.02, end_reason="timeout")
    sc0 = cli.build_sidecar(source_rec_name="Y.rec", rec_bytes=b"\x00",
                            info=info, rom_crc32=0, options={}, result=r0,
                            output_name="Y.mp4")
    ok(sc0["replay"]["outcome"] == 0
       and sc0["replay"]["outcome_text"] == "unknown",
       "default outcome fields wrong")
    print("   keys, sha1, crc32, replay/outcome block, JSON round-trip")


def test_romdata_ground_truth(rom_bytes: bytes):
    print("-- romdata.frontier_trainer_name() against local/rom.gba")
    # GROUND TRUTH: opponent_a=83 of GUYA_19-07-2026_10-09.rec displays as
    # SAILOR MAXWELL in real gameplay footage of that record.
    got = romdata.frontier_trainer_name(rom_bytes, 83)
    ok(got == "SAILOR MAXWELL",
       f"trainer 83 decoded to {got!r}, expected 'SAILOR MAXWELL'")
    # every ROM frontier trainer id must decode to something plausible
    decoded = [romdata.frontier_trainer_name(rom_bytes, i)
               for i in range(romdata.FRONTIER_TRAINERS_COUNT)]
    bad = [i for i, name in enumerate(decoded)
           if not name or " " not in name or len(name) > 24]
    ok(not bad, f"implausible decodes for ids {bad[:8]}")
    # None on any doubt: out of range, wrong type, truncated ROM
    ok(romdata.frontier_trainer_name(rom_bytes, 300) is None,
       "id 300 (record-mix range) must be None")
    ok(romdata.frontier_trainer_name(rom_bytes, -1) is None,
       "negative id must be None")
    ok(romdata.frontier_trainer_name(rom_bytes, 1022) is None,
       "Frontier Brain id must be None (not a ROM table entry)")
    ok(romdata.frontier_trainer_name(b"\x00" * 0x1000, 83) is None,
       "tiny ROM must be None")
    ok(romdata.frontier_trainer_name(None, 83) is None,
       "None ROM must be None")
    # corrupt one name byte to a non-charset value -> strict decode bails
    entry_off = (romdata.GBATTLE_FRONTIER_TRAINERS_ADDR - romdata.ROM_BASE
                 + 83 * romdata.BFT_ENTRY_SIZE)
    corrupt = bytearray(rom_bytes)
    corrupt[entry_off + romdata.BFT_NAME_OFFSET] = 0xF7   # not in the charset
    ok(romdata.frontier_trainer_name(bytes(corrupt), 83) is None,
       "undecodable name byte must yield None")
    # end-to-end through the filename builder with real ROM bytes
    name = cli.build_output_basename(
        _info(is_double=True), "GUYA_19-07-2026_10-09", rom_bytes)
    ok(name == "GUYA_19-07-2026_10-09 - Battle Dome Open double "
               "vs SAILOR MAXWELL",
       f"full basename wrong: {name!r}")
    print(f"   id 83 -> 'SAILOR MAXWELL'; all 300 ids plausible; "
          f"doubt paths -> None")


def main():
    test_sanitize()
    test_builder()
    test_collisions()
    test_sidecar_shape()
    if os.path.isfile(ROM_PATH):
        test_romdata_ground_truth(open(ROM_PATH, "rb").read())
        print(f"PASS: {_checks} checks")
        return
    # local/ is gitignored — a fresh clone / CI has no ROM. The sections
    # above are real assertions, so this run is NOT vacuous: report the
    # skip loudly but exit 0. Exit 2 ("required assets missing") is kept
    # for a vacuous run, and for strict local runs that opt in via
    # REC2MP4_REQUIRE_ASSETS=1 (never mistake the skipped ground-truth
    # section for having run).
    print(f"SKIP: {ROM_PATH} absent — the romdata ground-truth section "
          "needs your own US Emerald ROM and did NOT run")
    if _checks == 0 or os.environ.get("REC2MP4_REQUIRE_ASSETS"):
        print(f"SKIPPED, not passed: {_checks} asset-free check(s) ran, but "
              "the ROM ground-truth section did not")
        sys.exit(2)
    print(f"PASS: {_checks} asset-free checks "
          "(ROM ground-truth section skipped)")


if __name__ == "__main__":
    main()
