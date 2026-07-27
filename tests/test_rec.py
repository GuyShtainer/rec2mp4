#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""Tests for rec2mp4.rec — plain python, no pytest. Exits non-zero on failure.

Run:  python3 tests/test_rec.py

With the real (gitignored) assets in local/ (recs/*.rec and template.sav)
every section runs against them, exactly as always. Without them (fresh
clone, CI) the suite prints 'SYNTHETIC MODE' and runs the FULL set of
sections — validate/parse sanity, corruption, patch_options and inject —
on a from-scratch record built with the real sentinel/encryption/checksum
scheme plus a synthetic 128 KiB save image, so CI still exercises real
assertions (exit 0 on success). Exit 2 is reserved for runs where required
assets are missing AND nothing meaningful could run — or for strict local
runs with REC2MP4_REQUIRE_ASSETS=1, which fail (exit 2) whenever any
real-asset section had to be skipped or substituted.
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


# ---------------------------------------------------------------------------
# Synthetic assets (used only when local/ is absent — fresh clone / CI).
# Everything is built from scratch with the byte-exact scheme rec.py decodes:
# Gen-3 charset names, XOR-encrypted party mons in the personality-dependent
# substruct order with the 16-bit word-sum checksum, and the struct's u32
# byte-sum checksum. rec.validate() must return [] for the result.
# ---------------------------------------------------------------------------

_G3_SPECIALS = {' ': 0x00, '!': 0xAB, '?': 0xAC, '.': 0xAD, '-': 0xAE,
                "'": 0xB4, ',': 0xB8, '/': 0xBA, ':': 0xF0}


def g3_encode(text: str) -> bytes:
    """Encode ASCII to the Gen-3 charset subset rec._g3chr() decodes."""
    out = bytearray()
    for c in text:
        if 'A' <= c <= 'Z':
            out.append(0xBB + ord(c) - ord('A'))
        elif 'a' <= c <= 'z':
            out.append(0xD5 + ord(c) - ord('a'))
        elif '0' <= c <= '9':
            out.append(0xA1 + ord(c) - ord('0'))
        else:
            out.append(_G3_SPECIALS[c])
    return bytes(out)


def build_synthetic_mon(pers: int, otid: int, nickname: str,
                        species: int, level: int) -> bytes:
    """One 100-byte encrypted party mon, reversing rec._decode_mon()."""
    m = bytearray(100)
    struct.pack_into("<II", m, 0, pers, otid)
    name = g3_encode(nickname)[:10]
    m[8:8 + len(name)] = name
    for i in range(8 + len(name), 18):
        m[i] = 0xFF                                   # EOS + padding
    # 48-byte plaintext substruct block; Growth position depends on pers%24.
    plain = bytearray(48)
    order = rec._ORDERS[pers % 24]
    struct.pack_into("<H", plain, order.index('G') * 12, species)
    struct.pack_into("<H", plain, order.index('A') * 12, 33)   # some move id
    plain[order.index('E') * 12] = 4                           # some EVs
    struct.pack_into("<H", m, 28,
                     sum(struct.unpack("<24H", plain)) & 0xFFFF)
    key = pers ^ otid
    m[32:80] = struct.pack("<12I",
                           *(w ^ key for w in struct.unpack("<12I", plain)))
    m[84] = level
    return bytes(m)


# (pers, otid, nickname, species_internal, level) per slot — pers values
# chosen to exercise DIFFERENT substruct orders (pers%24 = 0, 12, 13, 7).
SYN_PLAYER_MONS = [(24, 0x0001ABCD, "SYNTHA", 286, 50),
                   (36, 0x00020042, "SYNTHB", 359, 55)]
SYN_OPP_MONS = [(61, 0x0003BEEF, "OPPA", 130, 60),
                (7, 0x0004CAFE, "OPPB", 65, 61)]
SYN_SEED = 0x12345678
SYN_LANE0_LEN = 40


def build_synthetic_record() -> bytes:
    """A fully valid .rec built from scratch: Battle Dome, Open Level,
    singles vs frontier trainer #83, recorded by GUY (ENG)."""
    buf = bytearray(rec.SECTOR_SIZE)
    struct.pack_into("<I", buf, 0, rec.SENTINEL)
    r = memoryview(buf)[rec.STRUCT_OFF:rec.STRUCT_OFF + 3968]
    for i, mon in enumerate(SYN_PLAYER_MONS):
        r[i * 100:(i + 1) * 100] = build_synthetic_mon(*mon)
    for i, mon in enumerate(SYN_OPP_MONS):
        r[600 + i * 100:600 + (i + 1) * 100] = build_synthetic_mon(*mon)
    r[1200:1232] = b"\xFF" * 32                       # playersName[4][8]
    name = g3_encode("GUY")
    r[1200:1200 + len(name)] = name                   # slot 0 = recorder
    # playersGender/playersTrainerId stay zero (male, TID 0 is fine)
    r[1252] = 2                                       # slot-0 language ENG
    struct.pack_into("<I", r, 1256, SYN_SEED)         # rngSeed
    # battleFlags: BATTLE_TYPE_TRAINER (bit3) — nonzero, no forbidden bits
    struct.pack_into("<I", r, 1260, 0x00000008)
    struct.pack_into("<H", r, 1268, 83)               # opponentA: frontier
    r[1276] = 1                                       # lvlMode: Open Level
    r[1277] = 1                                       # facility: Battle Dome
    r[1279] = 0                                       # anims ON, text slow
    r[1284:1292] = b"\xFF" * 8                        # recordMixFriendName
    r[1308:3964] = b"\xFF" * (3964 - 1308)            # battleRecord lanes
    for i in range(SYN_LANE0_LEN):                    # lane 0: 40 input bytes
        r[1308 + i] = 0x12
    struct.pack_into("<I", r, rec.CHECKSUM_RANGE,
                     sum(bytes(r[:rec.CHECKSUM_RANGE])) & 0xFFFFFFFF)
    return bytes(buf)


def build_synthetic_save() -> bytes:
    """A 128 KiB non-uniform stand-in save image for the inject test (the
    inject checks are content-agnostic byte comparisons, but a patterned
    image makes the 'rest untouched' assertions meaningful)."""
    return bytes(range(256)) * (rec.SAV_MIN_SIZE // 256)


def test_synthetic_record(data: bytes):
    print("-- synthetic record sanity (validate + parse round-trip)")
    ok(len(data) == rec.SECTOR_SIZE, "synthetic record has the wrong size")
    ok(rec.validate(data) == [],
       f"synthetic record must be fully valid, got {rec.validate(data)}")
    info = rec.parse(data)
    ok(info["valid"] and info["errors"] == [], "parse() disagrees")
    ok(info["facility"] == "Battle Dome" and info["facility_id"] == 1,
       f"facility wrong: {info['facility']!r}")
    ok(info["level_mode"] == "Open Level", "level mode wrong")
    ok(info["rng_seed"] == "%08x" % SYN_SEED,
       f"seed wrong: {info['rng_seed']}")
    ok(info["recorded_by"] == "GUY" and info["players"] == ["GUY"],
       f"recorder name wrong: {info['recorded_by']!r} / {info['players']}")
    ok(info["players_language"] == ["ENG"],
       f"language wrong: {info['players_language']}")
    ok(info["input_lanes"] == [SYN_LANE0_LEN, 0, 0, 0],
       f"input lanes wrong: {info['input_lanes']}")
    ok(info["opponent_a"] == 83 and info["opponent_a_kind"] == "frontier",
       f"opponent wrong: {info['opponent_a']} {info['opponent_a_kind']}")
    ok(info["battle_scene_off"] is False and info["text_speed"] == "slow",
       "options byte wrong")
    for side, spec in (("player", SYN_PLAYER_MONS),
                       ("opponent", SYN_OPP_MONS)):
        team = info["teams"][side]
        ok(len(team) == len(spec), f"{side} team size wrong: {len(team)}")
        ok(all(m["checksum_ok"] for m in team),
           f"{side} team has a bad mon checksum — encryption reverse broken")
        ok([m["nickname"] for m in team] == [s[2] for s in spec],
           f"{side} nicknames wrong: {[m['nickname'] for m in team]}")
        ok([m["species_internal"] for m in team] == [s[3] for s in spec],
           f"{side} species wrong (substruct-order reverse broken): "
           f"{[m['species_internal'] for m in team]}")
        ok([m["level"] for m in team] == [s[4] for s in spec],
           f"{side} levels wrong")
        ok(not any(m["shiny"] for m in team),
           f"{side} team unexpectedly shiny")
    summary = rec.summarize(info)
    ok("Battle Dome" in summary and "GUY" in summary,
       "summarize() missing facility or recorder")
    print("   sentinel/flags/checksum valid; both encrypted teams decode "
          "byte-exact")


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


def test_inject(data: bytes, sav: bytes):
    print("-- inject() round-trip")
    ok(len(sav) >= rec.SAV_MIN_SIZE,
       f"save image unexpectedly small ({len(sav)} B)")

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


def test_patch_options(data: bytes):
    print("-- patch_options()")
    opt_off = rec.STRUCT_OFF + 1279
    base = rec.parse(data)

    on = rec.patch_options(data, animations=True)
    ok(rec.validate(on) == [], "animations=True result no longer validates")
    ok(not (on[opt_off] & 1), "animations=True did not clear bit0")
    off = rec.patch_options(data, animations=False)
    ok(rec.validate(off) == [], "animations=False result no longer validates")
    ok(off[opt_off] & 1, "animations=False did not set bit0")
    ok(rec.parse(on)["battle_scene_off"] is False
       and rec.parse(off)["battle_scene_off"] is True,
       "parse() does not reflect the patched battle-scene bit")

    for speed in (0, 1, 2):
        p = rec.patch_options(data, text_speed=speed)
        ok(rec.validate(p) == [], f"text_speed={speed} no longer validates")
        ok((p[opt_off] >> 1) & 7 == speed, f"text_speed={speed} not applied")

    # only the options byte and the checksum may differ
    both = rec.patch_options(data, animations=True, text_speed=2)
    ck = rec.STRUCT_OFF + rec.CHECKSUM_RANGE
    diff = [i for i in range(rec.SECTOR_SIZE)
            if both[i] != data[i] and not (i == opt_off or ck <= i < ck + 4)]
    ok(not diff, f"patch_options touched unexpected offsets: {diff[:8]}")

    # no-op patch (request the state the record already has) -> byte-identical
    same = rec.patch_options(data, animations=not base["battle_scene_off"])
    ok(same == data, "no-op patch is not byte-identical")

    try:
        rec.patch_options(data, text_speed=7)
        ok(False, "patch_options accepted text_speed=7")
    except rec.RecError:
        pass
    bad = bytearray(data)
    bad[rec.STRUCT_OFF + 10] ^= 0xFF
    try:
        rec.patch_options(bytes(bad), animations=True)
        ok(False, "patch_options accepted a corrupt record")
    except rec.RecError:
        pass
    print("   bit0/text-speed patched, checksum refreshed, RecError paths OK")


def main():
    # Exit semantics (CI reads these):
    #   0 — every section ran and passed, on real assets OR (fresh clone /
    #       CI, where gitignored local/ is absent) on the full synthetic
    #       suite ('SYNTHETIC MODE').
    #   2 — required assets missing with NO synthetic substitute possible
    #       (real records present but template.sav absent -> inject can't
    #       run), a vacuous run (zero checks), or any skip/substitution
    #       under REC2MP4_REQUIRE_ASSETS=1 (strict local mode).
    skipped = []
    synthetic = False
    paths = sorted(glob.glob(os.path.join(RECS_DIR, "*.rec")))
    if paths:
        test_real_recs(paths)
        reference = open(paths[0], "rb").read()
        if os.path.isfile(TEMPLATE_SAV):
            sav = open(TEMPLATE_SAV, "rb").read()
        else:
            sav = None
            skipped.append(f"{TEMPLATE_SAV} absent — inject test skipped")
    else:
        synthetic = True
        print("SYNTHETIC MODE: no .rec files in "
              f"{RECS_DIR} (gitignored local assets absent) — running the "
              "FULL suite on a from-scratch synthetic record + save")
        reference = build_synthetic_record()
        test_synthetic_record(reference)
        sav = build_synthetic_save()
    test_corruption(reference)
    test_patch_options(reference)
    if sav is not None:
        test_inject(reference, sav)
    if os.environ.get("REC2MP4_REQUIRE_ASSETS") and (synthetic or skipped):
        print("SKIPPED, not passed (REC2MP4_REQUIRE_ASSETS=1): "
              f"{_checks} check(s) ran but real assets were "
              + ("substituted with synthetic ones"
                 if synthetic else "partially missing")
              + " — supply local/recs/*.rec and local/template.sav")
        sys.exit(2)
    if skipped or _checks == 0:
        for s in skipped:
            print(f"SKIP: {s}")
        print(f"SKIPPED, not passed: {_checks} check(s) ran but required "
              "assets were missing — supply local/recs/*.rec and "
              "local/template.sav for a real run")
        sys.exit(2)
    print(f"PASS: {_checks} checks"
          + (" (SYNTHETIC MODE — no real assets)" if synthetic else ""))


if __name__ == "__main__":
    main()
