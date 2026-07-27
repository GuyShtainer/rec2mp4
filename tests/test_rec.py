#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""Tests for rec2mp4.rec — plain python, no pytest. Exits non-zero on failure.

Run:  python3 tests/test_rec.py

Uses the real (gitignored) assets in local/: recs/*.rec and template.sav.
Sections that need an absent asset are skipped with a message.
"""

import glob
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rec2mp4 import rec  # noqa: E402

RECS_DIR = os.path.join(ROOT, "local", "recs")
TEMPLATE_SAV = os.path.join(ROOT, "local", "template.sav")

_checks = 0


def ok(cond, msg):
    # No bare `assert`: it would be stripped under `python3 -O`, turning
    # the whole suite into a vacuous PASS.
    global _checks
    _checks += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def fix_checksum(buf: bytearray) -> None:
    """Recompute the struct byte-sum checksum after an intentional edit."""
    s = sum(buf[rec.STRUCT_OFF:rec.STRUCT_OFF + rec.CHECKSUM_RANGE]) \
        & 0xFFFFFFFF
    struct.pack_into("<I", buf, rec.STRUCT_OFF + rec.CHECKSUM_RANGE, s)


def test_real_recs(paths):
    print(f"-- {len(paths)} real record(s) in {RECS_DIR}")
    for path in paths:
        data = open(path, "rb").read()
        errors = rec.validate(data)
        ok(errors == [], f"{path}: expected valid, got {errors}")
        info = rec.parse(data)
        ok(info["valid"] and info["errors"] == [],
           f"{path}: parse() disagrees with validate()")
        ok(0 <= info["facility_id"] <= 6,
           f"{path}: facility_id {info['facility_id']} out of range")
        ok(len(info["rng_seed"]) == 8 and int(info["rng_seed"], 16) >= 0,
           f"{path}: bad rng_seed {info['rng_seed']!r}")
        ok(len(info["input_lanes"]) == 4
           and all(0 <= n <= 664 for n in info["input_lanes"]),
           f"{path}: bad input_lanes {info['input_lanes']}")
        team = info["teams"]["player"]
        ok(any(m["checksum_ok"] for m in team),
           f"{path}: no player-team mon with a good checksum")
        ok(isinstance(info["recorded_by"], str) and info["recorded_by"],
           f"{path}: empty recorded_by")
        summary = rec.summarize(info)
        ok(info["facility"] in summary,
           f"{path}: summarize() missing facility name")
        lanes = [n for n in info["input_lanes"] if n]
        print(f"   {os.path.basename(path)}: {info['facility']} "
              f"({info['level_mode']}), seed {info['rng_seed']}, "
              f"by {info['recorded_by']}, lanes {lanes}")


def test_corruption(data: bytes):
    print("-- corruption tests")
    # 1) flip one byte inside the checksummed struct -> checksum error
    bad = bytearray(data)
    bad[rec.STRUCT_OFF + 100] ^= 0x01           # inside playerParty
    errs = rec.validate(bytes(bad))
    ok(any("checksum" in e for e in errs),
       f"byte flip not caught as checksum error: {errs}")

    # 2) wrong sentinel -> sentinel error (sentinel is outside the checksum)
    bad = bytearray(data)
    bad[0] ^= 0xFF
    errs = rec.validate(bytes(bad))
    ok(any("sentinel" in e for e in errs),
       f"bad sentinel not caught: {errs}")

    # 3) set a forbidden battleFlags bit (bit1 = LINK), fix the checksum so
    #    ONLY the forbidden-bit rule fires
    bad = bytearray(data)
    bad[rec.STRUCT_OFF + 1260] |= 0x02
    fix_checksum(bad)
    errs = rec.validate(bytes(bad))
    ok(any("forbidden" in e for e in errs),
       f"forbidden battleFlags bit not caught: {errs}")
    ok(not any("checksum" in e for e in errs),
       f"fix_checksum failed, unrelated checksum error: {errs}")

    # 4) battleFlags == 0 (checksum fixed) -> "absent record" error
    bad = bytearray(data)
    bad[rec.STRUCT_OFF + 1260:rec.STRUCT_OFF + 1264] = b"\x00" * 4
    fix_checksum(bad)
    errs = rec.validate(bytes(bad))
    ok(any("zero" in e for e in errs),
       f"zero battleFlags not caught: {errs}")

    # 5) truncate -> size error, and nothing else runs
    errs = rec.validate(data[:4000])
    ok(len(errs) == 1 and "size" in errs[0],
       f"truncation not caught cleanly: {errs}")
    errs = rec.validate(b"")
    ok(len(errs) == 1 and "size" in errs[0],
       f"empty input not caught cleanly: {errs}")

    # parse() of corrupted data must not raise and must flag invalid
    info = rec.parse(bytes(bad))
    ok(info["valid"] is False and info["errors"],
       "parse() of corrupt data not flagged invalid")
    info = rec.parse(data[:4000])
    ok(info["valid"] is False and info["teams"] == {"player": [],
                                                    "opponent": []},
       "parse() of truncated data returned nonsense")
    print(f"   all corruption variants rejected with reasons")


def test_inject(data: bytes):
    print("-- inject() round-trip")
    sav = open(TEMPLATE_SAV, "rb").read()
    ok(len(sav) >= rec.SAV_MIN_SIZE,
       f"template.sav unexpectedly small ({len(sav)} B)")

    src = bytearray(sav)                       # prove caller buffer untouched
    out = rec.inject(data, src)
    ok(bytes(src) == sav, "inject() mutated the caller's save buffer")
    ok(len(out) == len(sav), "inject() changed the save length")
    ok(out[0x1F000:0x20000] == data,
       "re-extracted sector 31 differs from the injected record")
    ok(out[:0x1F000] == sav[:0x1F000],
       "inject() touched bytes before sector 31")
    ok(out[0x20000:] == sav[0x20000:],
       "inject() touched bytes after sector 31")

    # round-trip: extracted sector must validate + parse identically
    back = out[0x1F000:0x20000]
    ok(rec.validate(back) == [], "extracted sector no longer validates")
    ok(rec.parse(back) == rec.parse(data), "round-trip parse() differs")

    # RecError: invalid record refused
    bad = bytearray(data)
    bad[rec.STRUCT_OFF + 50] ^= 0xFF
    try:
        rec.inject(bytes(bad), sav)
        ok(False, "inject() accepted a corrupt record")
    except rec.RecError:
        pass

    # RecError: save too small (64 KiB dump has no sector 31)
    try:
        rec.inject(data, sav[:0x10000])
        ok(False, "inject() accepted a 64 KiB save")
    except rec.RecError:
        pass
    print("   sector 31 byte-equal, rest untouched, RecError paths OK")


def main():
    # A skipped required-asset section must NOT report success: exit 2 so
    # an exit-code-reading orchestrator/CI never mistakes "nothing ran"
    # (local/ is gitignored, so a fresh clone has no assets) for a pass.
    skipped = []
    paths = sorted(glob.glob(os.path.join(RECS_DIR, "*.rec")))
    if not os.path.isdir(RECS_DIR) or not paths:
        skipped.append(f"no .rec files in {RECS_DIR} — "
                       "real-record, corruption and inject tests need one")
    else:
        test_real_recs(paths)
        reference = open(paths[0], "rb").read()
        test_corruption(reference)
        if os.path.isfile(TEMPLATE_SAV):
            test_inject(reference)
        else:
            skipped.append(f"{TEMPLATE_SAV} absent — inject test skipped")
    if skipped or _checks == 0:
        for s in skipped:
            print(f"SKIP: {s}")
        print(f"SKIPPED, not passed: {_checks} check(s) ran but required "
              "assets were missing — supply local/recs/*.rec and "
              "local/template.sav for a real run")
        sys.exit(2)
    print(f"PASS: {_checks} checks")


if __name__ == "__main__":
    main()
