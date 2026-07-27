#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""Tests for party-mon decode (moves/EVs/IVs/nature), romdata.move_name /
frontier_trainer_rawname, and the opponent-POV trainer-name injection.

Plain python, no pytest — same style as tests/test_rec.py / test_pov.py.
Asset-free sections (a from-scratch synthetic mon with a KNOWN moves/EV/IV/
nature payload, and the to_opponent_pov name injection) always run in a fresh
clone / CI. Sections that need the user's own assets — range checks over real
local/recs exports, and the ROM ground-truth for move_name /
frontier_trainer_rawname — run only when those files are present; otherwise
they are announced as skipped. When only the asset-free suite runs (a bare
clone / CI) it prints "PASS (SYNTHETIC MODE)" and exits 0 — that suite is the
real regression guard (the classic speed<->spAttack EV/IV swap, the 5-bit IV
unpack, nature=%25, checksum gating). Exit 2 is reserved for a vacuous run
where literally nothing executed.

Run:  python3 tests/test_decode.py
      /path/to/conda/python tests/test_decode.py
"""

import os
import struct
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

from rec2mp4 import rec, romdata           # noqa: E402
import test_rec                            # synthetic-record helpers  # noqa: E402

RECS_DIR = os.path.join(ROOT, "local", "recs")
ROM_PATH = os.path.join(ROOT, "local", "rom.gba")

_checks = 0


def ok(cond, msg):
    # No bare `assert`: it would be stripped under `python3 -O`.
    global _checks
    _checks += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# A from-scratch mon with a KNOWN payload, built by reversing the exact
# scheme rec._decode_mon decodes. Every stat gets a distinct value so a
# speed<->spAttack mix-up (the classic silent bug) cannot pass silently.
# ---------------------------------------------------------------------------

# personality 24: pers%24 = 0 -> order "GAEM"; pers%25 = 24 -> nature Quirky.
KNOWN_PERS = 24
KNOWN_OTID = 0x0BEEF001
KNOWN_SPECIES = 376
# (move id, pp) in slot order; slot 3 is MOVE_NONE to prove it is dropped.
KNOWN_MOVES = [(33, 35), (53, 15), (85, 20), (0, 0)]
# on-cart EV order: hp, attack, defense, SPEED, spAttack, spDefense
KNOWN_EV_CART = {"hp": 252, "atk": 0, "def": 0, "spe": 252, "spa": 4, "spd": 0}
# distinct IVs so the mapping is unambiguous
KNOWN_IV = {"hp": 31, "atk": 30, "def": 29, "spe": 28, "spa": 27, "spd": 26}


def build_known_mon() -> bytes:
    m = bytearray(100)
    struct.pack_into("<II", m, 0, KNOWN_PERS, KNOWN_OTID)
    name = test_rec.g3_encode("KNOWN")
    m[8:8 + len(name)] = name
    for i in range(8 + len(name), 18):
        m[i] = 0xFF

    plain = bytearray(48)
    order = rec._ORDERS[KNOWN_PERS % 24]
    g = order.index('G') * 12
    a = order.index('A') * 12
    e = order.index('E') * 12
    mi = order.index('M') * 12

    struct.pack_into("<H", plain, g, KNOWN_SPECIES)          # Growth: species
    for i, (mid, _) in enumerate(KNOWN_MOVES):               # Attacks: moves
        struct.pack_into("<H", plain, a + i * 2, mid)
    for i, (_, pp) in enumerate(KNOWN_MOVES):                # Attacks: pp
        plain[a + 8 + i] = pp
    # EVs in on-cart order hp, attack, defense, speed, spAttack, spDefense
    plain[e + 0] = KNOWN_EV_CART["hp"]
    plain[e + 1] = KNOWN_EV_CART["atk"]
    plain[e + 2] = KNOWN_EV_CART["def"]
    plain[e + 3] = KNOWN_EV_CART["spe"]
    plain[e + 4] = KNOWN_EV_CART["spa"]
    plain[e + 5] = KNOWN_EV_CART["spd"]
    # IV u32 at misc offset +4: hp|atk<<5|def<<10|spe<<15|spa<<20|spd<<25
    iv = (KNOWN_IV["hp"] | KNOWN_IV["atk"] << 5 | KNOWN_IV["def"] << 10
          | KNOWN_IV["spe"] << 15 | KNOWN_IV["spa"] << 20
          | KNOWN_IV["spd"] << 25)
    struct.pack_into("<I", plain, mi + 4, iv)

    struct.pack_into("<H", m, 28, sum(struct.unpack("<24H", plain)) & 0xFFFF)
    key = KNOWN_PERS ^ KNOWN_OTID
    m[32:80] = struct.pack("<12I",
                           *(w ^ key for w in struct.unpack("<12I", plain)))
    m[84] = 50
    return bytes(m)


def test_known_roundtrip():
    print("-- synthetic known-payload exact round-trip")
    d = rec._decode_mon(build_known_mon())
    ok(d is not None and d["checksum_ok"], "known mon must decode + checksum-ok")

    # moves: slot 3 (MOVE_NONE) dropped, order preserved, pp carried.
    want_moves = [{"id": mid, "pp": pp} for mid, pp in KNOWN_MOVES if mid != 0]
    ok(d["moves"] == want_moves, f"moves mismatch: {d['moves']} != {want_moves}")

    # EVs mapped into hp/atk/def/spa/spd/spe (speed must NOT land in spa).
    want_evs = {k: KNOWN_EV_CART[k] for k in rec.STAT_KEYS}
    want_evs["sum"] = sum(KNOWN_EV_CART.values())
    ok(d["evs"] == want_evs, f"EV mismatch: {d['evs']} != {want_evs}")
    ok(d["evs"]["spe"] == 252 and d["evs"]["spa"] == 4,
       "Speed EV must map to 'spe' and spAttack EV to 'spa' (not swapped)")

    want_ivs = {k: KNOWN_IV[k] for k in rec.STAT_KEYS}
    want_ivs["sum"] = sum(KNOWN_IV.values())
    ok(d["ivs"] == want_ivs, f"IV mismatch: {d['ivs']} != {want_ivs}")
    ok(d["ivs"]["spe"] == 28 and d["ivs"]["spa"] == 27,
       "Speed IV must map to 'spe' and spAttack IV to 'spa' (not swapped)")

    ok(d["nature"] == {"id": 24, "name": "Quirky"},
       f"nature mismatch: {d['nature']}")
    print("   moves/EVs/IVs/nature all exact; speed<->spAttack not swapped")


def test_checksum_gating():
    print("-- decode keys are OMITTED on a checksum mismatch")
    m = bytearray(build_known_mon())
    m[28] ^= 0xFF                                    # corrupt stored checksum
    d = rec._decode_mon(bytes(m))
    ok(d is not None and d["checksum_ok"] is False,
       "corrupted mon must decode with checksum_ok=False")
    for k in ("moves", "evs", "ivs", "nature"):
        ok(k not in d, f"'{k}' must be omitted when checksum_ok is False")
    print("   moves/evs/ivs/nature absent when checksum_ok is False")


# ---------------------------------------------------------------------------
# Independent second decode used to cross-check the substruct order on REAL
# mons (an implementation deliberately written differently from rec.py).
# ---------------------------------------------------------------------------

def indep_decode(m: bytes):
    """Independent decode -> (hp_ev, speed_ev, spatk_ev, speed_iv, spatk_iv)
    or None. Different code path than rec._decode_mon (byte-wise, no _ORDERS
    slicing helpers reused for the values)."""
    pers, otid = struct.unpack_from("<II", m, 0)
    key = pers ^ otid
    dec = bytearray(48)
    for i in range(12):
        w = struct.unpack_from("<I", m, 32 + i * 4)[0] ^ key
        struct.pack_into("<I", dec, i * 4, w)
    if (sum(struct.unpack("<24H", dec)) & 0xFFFF) != \
            struct.unpack_from("<H", m, 28)[0]:
        return None
    order = rec._ORDERS[pers % 24]
    e = order.index('E') * 12
    mi = order.index('M') * 12
    hp_ev, _, _, speed_ev, spatk_ev = dec[e], dec[e + 1], dec[e + 2], \
        dec[e + 3], dec[e + 4]
    iv = struct.unpack_from("<I", dec, mi + 4)[0]
    speed_iv = (iv >> 15) & 0x1F
    spatk_iv = (iv >> 20) & 0x1F
    return hp_ev, speed_ev, spatk_ev, speed_iv, spatk_iv


def test_real_ranges_and_crosscheck(paths):
    print(f"-- real records: decode ranges + independent cross-check "
          f"({len(paths)} file(s))")
    distinct = {}
    for p in paths:
        data = p.read_bytes()
        if rec.validate(data):
            continue
        info = rec.parse(data)
        for side in ("player", "opponent"):
            for i, raw in enumerate(_iter_mon_bytes(data, side)):
                m = info["teams"][side]
                # match decoded dict to its raw 100 bytes by re-decoding
                d = rec._decode_mon(raw)
                if d is None or not d.get("checksum_ok"):
                    continue
                key = (d["species_internal"], d["nickname"], d["level"],
                       d["ivs"]["sum"], d["evs"]["sum"])
                distinct.setdefault(key, (raw, d))

    ok(len(distinct) >= 8,
       f"expected >=8 distinct checksum-ok mons, got {len(distinct)}")

    crosschecked = 0
    for raw, d in distinct.values():
        for st in rec.STAT_KEYS:
            ok(0 <= d["ivs"][st] <= 31, f"IV {st} out of range: {d['ivs']}")
            ok(0 <= d["evs"][st] <= 255, f"EV {st} out of range: {d['evs']}")
        ok(0 <= d["ivs"]["sum"] <= 186, f"IV sum out of range: {d['ivs']}")
        ok(0 <= d["evs"]["sum"] <= 510, f"EV sum out of range: {d['evs']}")
        ok(1 <= len(d["moves"]) <= 4, f"move count: {d['moves']}")
        for mv in d["moves"]:
            ok(1 <= mv["id"] <= 354, f"bad move id: {mv}")
            ok(mv["pp"] > 0, f"pp must be > 0: {mv}")
        ok(0 <= d["nature"]["id"] <= 24, f"nature id: {d['nature']}")
        ok(d["nature"]["name"] == rec.NATURES[d["nature"]["id"]],
           f"nature name/id disagree: {d['nature']}")

        ind = indep_decode(raw)
        ok(ind is not None, "independent decode disagreed on checksum")
        hp_ev, speed_ev, spatk_ev, speed_iv, spatk_iv = ind
        ok(d["evs"]["hp"] == hp_ev, "hp EV disagrees with independent decode")
        ok(d["evs"]["spe"] == speed_ev,
           "Speed EV disagrees (order bug: speed vs spAttack)")
        ok(d["evs"]["spa"] == spatk_ev,
           "spAttack EV disagrees (order bug: speed vs spAttack)")
        ok(d["ivs"]["spe"] == speed_iv,
           "Speed IV disagrees (order bug: speed vs spAttack)")
        ok(d["ivs"]["spa"] == spatk_iv,
           "spAttack IV disagrees (order bug: speed vs spAttack)")
        crosschecked += 1
    print(f"   {len(distinct)} distinct mons in range; {crosschecked} "
          "cross-checked vs an independent decode (speed vs spAttack correct)")


def _iter_mon_bytes(data: bytes, side: str):
    base = 0 if side == "player" else 600
    r = data[rec.STRUCT_OFF:rec.STRUCT_OFF + rec.STRUCT_SIZE]
    for i in range(6):
        yield r[base + i * 100:base + (i + 1) * 100]


# ---------------------------------------------------------------------------
# ROM ground truth: move_name + frontier_trainer_rawname
# ---------------------------------------------------------------------------

def test_rom_lookups(rom: bytes, paths):
    print("-- ROM ground truth: move_name + frontier_trainer_rawname")
    # move_name basic contract
    ok(romdata.move_name(rom, 0) is None, "move id 0 (MOVE_NONE) must be None")
    ok(romdata.move_name(rom, -1) is None, "negative id must be None")
    ok(romdata.move_name(rom, 355) is None, "id == MOVES_COUNT must be None")
    ok(romdata.move_name(rom, True) is None, "bool id must be rejected")
    m1 = romdata.move_name(rom, 1)
    ok(m1 and m1.isascii() and "?" not in m1 and m1.strip(),
       f"move id 1 must decode to real ASCII, got {m1!r}")

    # move_name on real decoded moves -> no '?'
    checked = 0
    for p in paths:
        data = p.read_bytes()
        if rec.validate(data):
            continue
        info = rec.parse(data)
        for side in ("player", "opponent"):
            for d in info["teams"][side]:
                if not d.get("checksum_ok"):
                    continue
                for mv in d["moves"]:
                    nm = romdata.move_name(rom, mv["id"])
                    ok(nm and nm.isascii() and "?" not in nm and nm.strip(),
                       f"move id {mv['id']} decoded to {nm!r}")
                    checked += 1
    ok(checked >= 8, f"expected many real move names, checked {checked}")

    # frontier_trainer_rawname: plain name; frontier_trainer_name adds class.
    raw120 = romdata.frontier_trainer_rawname(rom, 120)
    full120 = romdata.frontier_trainer_name(rom, 120)
    ok(raw120 == "NORTON", f"frontier_trainer_rawname(120) -> {raw120!r}, "
       "expected 'NORTON'")
    ok(raw120 and len(raw120) <= 7, f"raw name must fit 7 chars: {raw120!r}")
    ok(full120 and full120.endswith(raw120) and full120 != raw120,
       f"frontier_trainer_name must be '<CLASS> {raw120}', got {full120!r}")
    ok(romdata.frontier_trainer_rawname(rom, -1) is None, "id -1 -> None")
    ok(romdata.frontier_trainer_rawname(rom, 300) is None, "id 300 -> None")
    ok(romdata.frontier_trainer_rawname(rom, True) is None, "bool id -> None")
    print(f"   move_name id1={m1!r}; {checked} real moves named (no '?'); "
          f"rawname(120)={raw120!r} name(120)={full120!r}")


# ---------------------------------------------------------------------------
# to_opponent_pov(bottom_trainer_name=...) — asset-free (synthetic record)
# ---------------------------------------------------------------------------

def _pov_name(data: bytes) -> str:
    b = rec.STRUCT_OFF
    return rec._g3str(data[b + 1208:b + 1216])


def test_pov_name_injection():
    print("-- to_opponent_pov(bottom_trainer_name=...) name injection")
    syn = test_rec.build_synthetic_record()          # single, fake-link record

    default = rec.to_opponent_pov(syn)
    ok(rec.validate(default) == [], "default POV output must validate")
    ok(_pov_name(default) == "FOE",
       f"default bottom trainer must stay 'FOE', got {_pov_name(default)!r}")

    named = rec.to_opponent_pov(syn, bottom_trainer_name="NORTON")
    ok(rec.validate(named) == [], "named POV output must validate")
    ok(_pov_name(named) == "NORTON",
       f"named bottom trainer must be 'NORTON', got {_pov_name(named)!r}")

    # None is treated the same as the default placeholder.
    none_name = rec.to_opponent_pov(syn, bottom_trainer_name=None)
    ok(_pov_name(none_name) == "FOE", "None must fall back to 'FOE'")

    # Over-long names are truncated by the 7-char Gen-3 slot encoder.
    long_name = rec.to_opponent_pov(syn, bottom_trainer_name="ABCDEFGHIJ")
    ok(len(_pov_name(long_name)) <= 7, "name slot must cap at 7 chars")

    # Parties still swapped + record still valid (transform otherwise intact).
    before, after = rec.parse(syn), rec.parse(named)
    ok([m["species_internal"] for m in after["teams"]["player"]]
       == [m["species_internal"] for m in before["teams"]["opponent"]],
       "POV must still swap the parties")
    print("   default='FOE', named='NORTON', None='FOE', long truncated <=7")


# ---------------------------------------------------------------------------

def main():
    # Asset-free: always run. These carry the speed<->spAttack EV/IV guard,
    # the 5-bit IV unpack, nature=%25, checksum gating and the POV name
    # injection — none of which need the user's ROM / records.
    test_known_roundtrip()
    test_checksum_gating()
    test_pov_name_injection()
    ran_assetfree = _checks > 0

    ran_asset = False
    paths = sorted(Path(RECS_DIR).glob("*.rec")) if os.path.isdir(RECS_DIR) \
        else []
    if paths:
        test_real_ranges_and_crosscheck(paths)
        ran_asset = True
    else:
        print(f"SKIP: {RECS_DIR} absent — real-record range/cross-check needs "
              "your own .rec exports and did NOT run")

    if os.path.isfile(ROM_PATH) and paths:
        test_rom_lookups(Path(ROM_PATH).read_bytes(), paths)
        ran_asset = True
    else:
        print(f"SKIP: {ROM_PATH} (and/or recs) absent — the ROM ground-truth "
              "for move_name / frontier_trainer_rawname did NOT run")

    if ran_asset:
        print(f"PASS: {_checks} checks")
        return

    # No user assets (a bare clone / CI): the asset-free suite is the real
    # regression guard and it fully ran — that is a legitimate green, mirroring
    # test_rec.py's SYNTHETIC MODE. Exit 2 is reserved for a vacuous run where
    # literally nothing executed.
    if ran_assetfree:
        print(f"PASS (SYNTHETIC MODE): {_checks} asset-free check(s) ran; the "
              "real-record/ROM sections were skipped (need local/recs + "
              "local/rom.gba)")
        return

    print("SKIPPED, not passed: nothing ran (no asset-free checks executed)")
    sys.exit(2)


if __name__ == "__main__":
    main()
