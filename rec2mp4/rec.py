# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""Parse, validate and inject Pokemon Emerald Battle Records (.rec files).

A .rec is the raw 4096-byte save sector 31 of a US Emerald 128 KiB .sav:

  +0x000  u32 sentinel 0x0000B39D (bytes 9D B3 00 00)
  +0x004  RecordedBattleSave struct, 3968 bytes, little-endian:
    +0     playerParty[6]      6 x 100-byte standard encrypted party mons
    +600   opponentParty[6]
    +1200  playersName[4][8]   Gen-3 charset, EOS 0xFF
    +1232  playersGender[4]    0 male / 1 female
    +1236  playersTrainerId[4] u32 (full 32-bit; low 16 = visible ID)
    +1252  playersLanguage[4]  1 JP, 2 EN, 3 FR, 4 IT, 5 DE, 7 ES
    +1256  rngSeed u32         gRngValue at battle start (replay determinism)
    +1260  battleFlags u32     != 0 and no bit of 0x7D007E92 may be set
    +1264  playersBattlers[4]  battler position per link player
    +1268  opponentA u16       0..299 ROM frontier trainer, 300..399 record-mix
                               friend, 400..499 apprentice, 1022 Frontier Brain
    +1270  opponentB u16       second opponent (two-opponent battles)
    +1272  partnerId u16       multi-battle partner
    +1274  multiplayerId u16   which playersName[] slot is the recorder
    +1276  lvlMode u8          0 = Level 50, 1 = Open Level
    +1277  frontierFacility u8 0..6 Tower/Dome/Palace/Arena/Factory/Pike/Pyramid
    +1278  frontierBrainSymbol u8
    +1279  u8 bitfield         bit0 = battle animations OFF, bits1-3 text speed
    +1280  AI_scripts u32
    +1284  recordMixFriendName[8]  (set when an id is in 300..399)
    +1292  recordMixFriendClass u8
    +1293  apprenticeId u8
    +1294  easyChatSpeech[6] u16
    +1306  recordMixFriendLanguage u8
    +1307  apprenticeLanguage u8
    +1308  battleRecord[4][664]  opaque input lanes, 0xFF fill after the data
    +3964  checksum u32        plain u32 sum of struct bytes 0..3963
  +0xF84  124 bytes of 0x00 padding

This module adapts PokeDNA's proven record parser (tools/read_rec.py in
https://github.com/GuyShtainer/PokeDNA, same author, GPL); the byte-exact spec
with pokeemerald decomp citations lives with that project.

Pure stdlib, Python >= 3.10.
"""

from __future__ import annotations

import struct

SECTOR_SIZE = 4096

# Validity constants (see record-spec.md section 3)
SENTINEL = 0x0000B39D            # u32 at offset 0; erased flash (0xFF) fails
STRUCT_OFF = 4                   # RecordedBattleSave starts after the sentinel
STRUCT_SIZE = 3968
CHECKSUM_RANGE = 3964            # byte-sum covers struct bytes 0..3963
FORBIDDEN_FLAGS = 0x7D007E92     # BATTLE_TYPE_RECORDED_INVALID bit mask

# battleFlags bits meaningful for display
FLAG_DOUBLE = 1 << 0
FLAG_MULTI = 1 << 6
FLAG_TWO_OPPONENTS = 1 << 15
FLAG_RECORDED_LINK = 1 << 25

# battleFlags bits the opponent-POV transform touches (BATTLE_TYPE_* in the
# decomp include/constants/battle.h — reference only).
FLAG_IS_MASTER = 1 << 2           # BATTLE_TYPE_IS_MASTER
FLAG_RECORDED_IS_MASTER = 1 << 31  # BATTLE_TYPE_RECORDED_IS_MASTER (perspective)

# Sector 31 location inside a 128 KiB .sav
SAV_SECTOR31_OFF = 0x1F000
SAV_MIN_SIZE = 0x20000

FACILITY = ["Battle Tower", "Battle Dome", "Battle Palace", "Battle Arena",
            "Battle Factory", "Battle Pike", "Battle Pyramid"]

LANGUAGE = {1: "JPN", 2: "ENG", 3: "FRE", 4: "ITA", 5: "GER", 7: "SPA"}

TEXT_SPEED = {0: "slow", 1: "mid", 2: "fast"}

TRAINER_FRONTIER_BRAIN = 1022


class RecError(Exception):
    """Raised for unusable inputs (invalid record / too-small save)."""


# ---------------------------------------------------------------------------
# Gen-3 text (subset used by names) — from the proven read_rec.py tables
# ---------------------------------------------------------------------------

def _g3chr(b: int) -> str:
    if 0xBB <= b <= 0xD4:
        return chr(ord('A') + b - 0xBB)
    if 0xD5 <= b <= 0xEE:
        return chr(ord('a') + b - 0xD5)
    if 0xA1 <= b <= 0xAA:
        return chr(ord('0') + b - 0xA1)
    return {0x00: ' ', 0xAB: '!', 0xAC: '?', 0xAD: '.', 0xAE: '-', 0xB4: "'",
            0xB5: 'M', 0xB6: 'F', 0xB8: ',', 0xBA: '/', 0xF0: ':'}.get(b, '?')


def _g3str(raw: bytes) -> str:
    out = []
    for b in raw:
        if b == 0xFF:            # EOS
            break
        out.append(_g3chr(b))
    return ''.join(out).strip()


# Encode-side specials (inverse of _g3chr's dict, letters handled by range).
_G3_SPECIALS = {' ': 0x00, '!': 0xAB, '?': 0xAC, '.': 0xAD, '-': 0xAE,
                "'": 0xB4, ',': 0xB8, '/': 0xBA, ':': 0xF0}


def _g3encode(text: str, length: int = 8) -> bytes:
    """Encode ASCII to the Gen-3 charset (inverse of _g3chr), 0xFF-terminated.

    Produces exactly `length` bytes: the encoded glyphs, a 0xFF EOS, then
    0xFF padding — the on-cart form of a trainer name slot. Unencodable
    characters are dropped; the text is truncated to leave room for EOS.
    """
    out = bytearray()
    for c in text[:length - 1]:
        if 'A' <= c <= 'Z':
            out.append(0xBB + ord(c) - ord('A'))
        elif 'a' <= c <= 'z':
            out.append(0xD5 + ord(c) - ord('a'))
        elif '0' <= c <= '9':
            out.append(0xA1 + ord(c) - ord('0'))
        elif c in _G3_SPECIALS:
            out.append(_G3_SPECIALS[c])
    out.append(0xFF)                         # EOS
    out += b'\xFF' * (length - len(out))
    return bytes(out[:length])


# ---------------------------------------------------------------------------
# Party-mon decoding: 24 substruct orders (Growth/Attacks/EVs/Misc),
# index = personality % 24 — standard Gen-3 box-mon encryption.
# ---------------------------------------------------------------------------

_ORDERS = ["GAEM", "GAME", "GEAM", "GEMA", "GMAE", "GMEA", "AGEM", "AGME",
           "AEGM", "AEMG", "AMGE", "AMEG", "EGAM", "EGMA", "EAGM", "EAMG",
           "EMGA", "EMAG", "MGAE", "MGEA", "MAGE", "MAEG", "MEGA", "MEAG"]

# Generic-English nature names in canonical Gen-3 index order (Hardy..Quirky).
# id = personality % 25 (src/pokemon.c GetNatureFromPersonality). These are the
# universal English labels, not Game Freak data lifted from the ROM.
NATURES = [
    "Hardy", "Lonely", "Brave", "Adamant", "Naughty",
    "Bold", "Docile", "Relaxed", "Impish", "Lax",
    "Timid", "Hasty", "Serious", "Jolly", "Naive",
    "Modest", "Mild", "Quiet", "Bashful", "Rash",
    "Calm", "Gentle", "Sassy", "Careful", "Quirky",
]

# Canonical stat-key order used across decode + panel. Note this is the
# DISPLAY order (HP, Atk, Def, SpA, SpD, Spe) — NOT the on-cart EV/IV byte
# order, which stores Speed before the special stats (see _decode_mon).
STAT_KEYS = ("hp", "atk", "def", "spa", "spd", "spe")
STAT_LABELS = {"hp": "HP", "atk": "Atk", "def": "Def",
               "spa": "SpA", "spd": "SpD", "spe": "Spe"}

MAX_MON_MOVES = 4                    # include/constants/pokemon.h


def _decode_mon(m: bytes) -> dict | None:
    """m = one 100-byte party mon -> dict, or None if the slot is empty."""
    pers, otid = struct.unpack_from("<II", m, 0)
    if pers == 0 and otid == 0 and m[32:80] == b"\x00" * 48:
        return None
    key = pers ^ otid
    words = struct.unpack_from("<12I", m, 32)
    dec = struct.pack("<12I", *(w ^ key for w in words))
    ck = sum(struct.unpack("<24H", dec)) & 0xFFFF
    order = _ORDERS[pers % 24]
    g = order.index('G') * 12
    species = struct.unpack_from("<H", dec, g)[0]
    if species == 0:
        return None
    out = {
        "nickname": _g3str(m[8:18]),
        "species_internal": species,       # internal Gen-3 id (1..411, 412=Egg)
        "level": m[84],                    # plaintext, party-mon offset +84
        "shiny": ((otid >> 16) ^ (otid & 0xFFFF)
                  ^ (pers >> 16) ^ (pers & 0xFFFF)) < 8,
        "checksum_ok": ck == struct.unpack_from("<H", m, 28)[0],
    }
    # Only when the decrypted region's checksum matches do the substruct
    # fields carry meaning; on a mismatch the caller must NOT trust them, so
    # the moves/EV/IV/nature keys are omitted entirely (their presence is the
    # signal that decode succeeded).
    if out["checksum_ok"]:
        a = order.index('A') * 12          # Attacks substruct (Substruct1)
        e = order.index('E') * 12          # EVs substruct (Substruct2)
        mi = order.index('M') * 12         # Misc substruct (Substruct3)

        # Substruct1 (pokemon.h:109-113): u16 moves[4] then u8 pp[4].
        move_ids = struct.unpack_from("<4H", dec, a)
        pp = dec[a + 8:a + 12]
        out["moves"] = [{"id": mid, "pp": pp[i]}
                        for i, mid in enumerate(move_ids) if mid != 0]

        # Substruct2 (pokemon.h:115-129): EVs in on-cart order
        # hp, attack, defense, SPEED, spAttack, spDefense (Speed BEFORE the
        # special stats — the classic silent bug is mapping it into spa).
        ev_hp, ev_atk, ev_def, ev_spe, ev_spa, ev_spd = dec[e:e + 6]
        out["evs"] = {
            "hp": ev_hp, "atk": ev_atk, "def": ev_def,
            "spa": ev_spa, "spd": ev_spd, "spe": ev_spe,
            "sum": ev_hp + ev_atk + ev_def + ev_spa + ev_spd + ev_spe,
        }

        # Substruct3 (pokemon.h:141-146): a u32 at substruct offset +4 packing
        # (LSB-first) hpIV:5, attackIV:5, defenseIV:5, speedIV:5, spAttackIV:5,
        # spDefenseIV:5, isEgg:1, abilityNum:1. Speed is again BEFORE special.
        iv = struct.unpack_from("<I", dec, mi + 4)[0]
        iv_hp = iv & 0x1F
        iv_atk = (iv >> 5) & 0x1F
        iv_def = (iv >> 10) & 0x1F
        iv_spe = (iv >> 15) & 0x1F
        iv_spa = (iv >> 20) & 0x1F
        iv_spd = (iv >> 25) & 0x1F
        out["ivs"] = {
            "hp": iv_hp, "atk": iv_atk, "def": iv_def,
            "spa": iv_spa, "spd": iv_spd, "spe": iv_spe,
            "sum": iv_hp + iv_atk + iv_def + iv_spa + iv_spd + iv_spe,
        }

        nat = pers % 25
        out["nature"] = {"id": nat, "name": NATURES[nat]}
    return out


def _classify_opponent(opp_id: int, r: bytes) -> tuple[str, str]:
    """Classify an opponentA/opponentB/partner id -> (kind, display name)."""
    if opp_id == TRAINER_FRONTIER_BRAIN:
        return "frontier_brain", "Frontier Brain"
    if 0 <= opp_id < 300:
        return "frontier", f"Frontier trainer #{opp_id}"
    if 300 <= opp_id < 400:
        name = _g3str(r[1284:1292])        # recordMixFriendName
        lang = LANGUAGE.get(r[1306], f"lang{r[1306]}")
        return "record_mix_friend", f"{name or '?'} (record-mix friend, {lang})"
    if 400 <= opp_id < 500:
        return "apprentice", f"Apprentice #{r[1293]}"
    return "unknown", f"#{opp_id}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate(data: bytes) -> list[str]:
    """Check a .rec the way the game does; return [] if valid.

    Order mirrors the game's own read path: size/sentinel (save.c read of
    sector 31), then battleFlags != 0, forbidden-bit mask, byte-sum checksum
    (IsRecordedBattleSaveValid).
    """
    errors: list[str] = []
    if len(data) != SECTOR_SIZE:
        errors.append(f"wrong size: {len(data)} bytes "
                      f"(a .rec is exactly {SECTOR_SIZE} bytes)")
        return errors                      # nothing else can be checked safely
    sentinel = int.from_bytes(data[0:4], "little")
    if sentinel != SENTINEL:
        errors.append(f"bad sentinel: 0x{sentinel:08X} at offset 0 "
                      f"(expected 0x{SENTINEL:08X}) — no record present")
    r = data[STRUCT_OFF:STRUCT_OFF + STRUCT_SIZE]
    flags = int.from_bytes(r[1260:1264], "little")
    if flags == 0:
        errors.append("battleFlags is zero — the game treats this record "
                      "as absent")
    forbidden = flags & FORBIDDEN_FLAGS
    if forbidden:
        errors.append(f"battleFlags 0x{flags:08X} has forbidden bits "
                      f"0x{forbidden:08X} (invalid mask 0x{FORBIDDEN_FLAGS:08X}"
                      ") — not a recordable battle type")
    computed = sum(r[:CHECKSUM_RANGE]) & 0xFFFFFFFF
    stored = int.from_bytes(r[CHECKSUM_RANGE:CHECKSUM_RANGE + 4], "little")
    if computed != stored:
        errors.append(f"checksum mismatch: computed 0x{computed:08X}, "
                      f"stored 0x{stored:08X}")
    return errors


def parse(data: bytes) -> dict:
    """Parse a .rec into a plain dict (best effort even when invalid)."""
    errors = validate(data)
    info: dict = {
        "valid": not errors,
        "errors": errors,
        "facility": "?",
        "facility_id": -1,
        "level_mode": "?",
        "rng_seed": "00000000",
        "battle_flags": "00000000",
        "is_double": False,
        "is_multi": False,
        "is_two_opponents": False,
        "is_link_recorded": False,
        "opponent_a": 0,
        "opponent_b": 0,
        "partner": 0,
        "multiplayer_id": 0,
        "recorded_by": "",
        "players": [],
        "input_lanes": [0, 0, 0, 0],
        "teams": {"player": [], "opponent": []},
        # extras (not in the minimum contract, useful for the CLI)
        "opponent_a_kind": "unknown",
        "opponent_a_name": "",
        "opponent_b_kind": None,
        "opponent_b_name": None,
        "recorded_by_gender": "?",
        "players_language": [],
        "battle_scene_off": False,
        "text_speed": "?",
        "frontier_brain_symbol": 0,
    }
    if len(data) != SECTOR_SIZE:
        return info                        # can't index anything reliably

    r = data[STRUCT_OFF:STRUCT_OFF + STRUCT_SIZE]
    seed, flags = struct.unpack_from("<II", r, 1256)
    opp_a, opp_b, partner, mpid = struct.unpack_from("<4H", r, 1268)
    lvl_mode, facility_id, brain_symbol, opt = r[1276], r[1277], r[1278], r[1279]

    names = [_g3str(r[1200 + p * 8: 1200 + p * 8 + 8]) for p in range(4)]
    langs = [LANGUAGE.get(r[1252 + p], f"lang{r[1252 + p]}") for p in range(4)]

    lanes = []
    for p in range(4):
        lane = r[1308 + p * 664: 1308 + (p + 1) * 664]
        n = 0
        while n < 664 and lane[n] != 0xFF:
            n += 1
        lanes.append(n)

    teams: dict = {}
    for side, base in (("player", 0), ("opponent", 600)):
        teams[side] = [d for i in range(6)
                       if (d := _decode_mon(r[base + i * 100:
                                              base + (i + 1) * 100]))]

    kind_a, name_a = _classify_opponent(opp_a, r)
    # A second opponent is signaled by the battle type (battleFlags bit15
    # TWO_OPPONENTS / bit6 MULTI — record-spec.md section "layout"), NOT by
    # opponentB != 0: frontier trainer id 0 is a valid opponent. Fall back
    # to opp_b != 0 only when neither type bit is set.
    has_opp_b = bool(flags & (FLAG_TWO_OPPONENTS | FLAG_MULTI)) or opp_b != 0
    kind_b, name_b = (_classify_opponent(opp_b, r) if has_opp_b
                      else (None, None))

    info.update({
        "facility": FACILITY[facility_id] if facility_id < len(FACILITY)
                    else f"Unknown facility ({facility_id})",
        "facility_id": facility_id,
        "level_mode": "Open Level" if lvl_mode else "Level 50",
        "rng_seed": "%08x" % seed,
        "battle_flags": "%08x" % flags,
        "is_double": bool(flags & FLAG_DOUBLE),
        "is_multi": bool(flags & FLAG_MULTI),
        "is_two_opponents": bool(flags & FLAG_TWO_OPPONENTS),
        "is_link_recorded": bool(flags & FLAG_RECORDED_LINK),
        "opponent_a": opp_a,
        "opponent_b": opp_b,
        "partner": partner,
        "multiplayer_id": mpid,
        "recorded_by": names[mpid] if mpid < 4 else "?",
        "players": [n for n in names if n],
        "input_lanes": lanes,
        "teams": teams,
        "opponent_a_kind": kind_a,
        "opponent_a_name": name_a,
        "opponent_b_kind": kind_b,
        "opponent_b_name": name_b,
        "recorded_by_gender": ("F" if r[1232 + mpid] else "M") if mpid < 4
                              else "?",
        "players_language": [langs[p] for p in range(4) if names[p]],
        "battle_scene_off": bool(opt & 1),          # bit0: animations OFF
        "text_speed": TEXT_SPEED.get((opt >> 1) & 7, str((opt >> 1) & 7)),
        "frontier_brain_symbol": brain_symbol,
    })
    return info


def inject(rec: bytes, sav: bytes) -> bytes:
    """Return new save bytes with sector 31 (0x1F000..0x1FFFF) = rec.

    The caller's inputs are never modified. Raises RecError if the record
    is invalid or the save is smaller than 128 KiB (64 KiB dumps have no
    sector 31).
    """
    errors = validate(rec)
    if errors:
        raise RecError("refusing to inject an invalid record: "
                       + "; ".join(errors))
    if len(sav) < SAV_MIN_SIZE:
        raise RecError(f"save too small: {len(sav)} bytes — a 128 KiB "
                       f"(>= 0x{SAV_MIN_SIZE:X}-byte) .sav is required")
    out = bytearray(sav)
    out[SAV_SECTOR31_OFF:SAV_SECTOR31_OFF + SECTOR_SIZE] = rec
    return bytes(out)


def extract(sav: bytes) -> bytes:
    """Return sector 31 (0x1F000..0x1FFFF) of a save image: the Battle Record
    exactly as a .rec export holds it.

    The caller's buffer is never modified. Raises RecError if the image is
    smaller than 128 KiB (a 64 KiB dump has no sector 31). The result is NOT
    validated here -- an erased or never-recorded sector comes back as the
    0xFF fill and fails validate() with a sentinel error, which is the
    caller's cue to say "this save holds no record".
    """
    if len(sav) < SAV_MIN_SIZE:
        raise RecError(f"save too small: {len(sav)} bytes — a 128 KiB "
                       f"(>= 0x{SAV_MIN_SIZE:X}-byte) .sav is required; a "
                       "64 KiB dump has no sector 31")
    return bytes(sav[SAV_SECTOR31_OFF:SAV_SECTOR31_OFF + SECTOR_SIZE])


def patch_options(rec: bytes, animations: bool | None = None,
                  text_speed: int | None = None) -> bytes:
    """Return a copy of the record with its presentation options overridden.

    Struct byte +1279 is a snapshot of the RECORDER's in-game options: bit0 =
    battle animations OFF (a recorder who played with "BATTLE SCENE OFF" gets
    replays without move effects or the shiny sparkle), bits1-3 = text speed
    (0 slow / 1 mid / 2 fast). Playback reads these from the record itself
    (recorded_battle.c keeps them in sBattleScene/sTextSpeed and hands them to
    the battle via GetBattleSceneInRecordedBattle), so patching the byte
    changes presentation only. It cannot desync the deterministic replay:
    battle animations draw randomness exclusively from the separate Random2()
    stream (no battle_anim_*.c source calls plain Random()), and recorded
    inputs are consumed per decision, not per frame. The record checksum is
    recomputed. animations=True means effects VISIBLE (bit0 cleared).
    """
    errors = validate(rec)
    if errors:
        raise RecError("refusing to patch an invalid record: "
                       + "; ".join(errors))
    out = bytearray(rec)
    opt_off = STRUCT_OFF + 1279
    opt = out[opt_off]
    if animations is not None:
        opt = (opt & ~0x01) | (0 if animations else 1)
    if text_speed is not None:
        if text_speed not in TEXT_SPEED:
            raise RecError(f"text_speed must be one of {sorted(TEXT_SPEED)} "
                           f"(got {text_speed})")
        opt = (opt & ~0x0E) | ((text_speed & 7) << 1)
    if opt == out[opt_off]:
        return bytes(out)
    out[opt_off] = opt
    csum = sum(out[STRUCT_OFF:STRUCT_OFF + CHECKSUM_RANGE]) & 0xFFFFFFFF
    out[STRUCT_OFF + CHECKSUM_RANGE:
        STRUCT_OFF + CHECKSUM_RANGE + 4] = csum.to_bytes(4, "little")
    return bytes(out)


# Fabricated identity for the link player that ends up at the bottom after
# the POV flip (a fake-link record's bottom trainer is always drawn as a
# player character — the frontier trainer's class art cannot appear there,
# see docs/research/opponent-pov.md §1.4). Name kept short/valid.
_POV_FAKE_NAME = "FOE"
_POV_FAKE_TRAINER_ID = 0x00003F3F        # any nonzero (low 16 = visible ID)


def to_opponent_pov(rec: bytes, bottom_trainer_name: str | None = None) -> bytes:
    """Transform a record so playback renders from the OPPONENT's side.

    Implements the "fake link record" (H2) transform from
    docs/research/opponent-pov.md §2.1. The game only offers a
    perspective switch for link records, so this dresses the record up as a
    non-master ``RECORDED_LINK`` battle: the two 600-byte party blocks are
    swapped, the flags are rewritten, a link-player identity is fabricated
    for the trainer that now sits at the bottom, and ``multiplayerId`` points
    at it. The engine's link-replay controllers then draw the (former)
    opponent's team at the bottom with player-style HP boxes and the
    recorder's team as the enemy at the top, with every recorded action
    correctly attributed — the input lanes are indexed by battler id and are
    left untouched.

    FAITHFUL ONLY FOR GENUINE LINK RECORDS (battleFlags already had
    ``RECORDED_LINK``). For the vs-AI Frontier records this project handles,
    the replay provably diverges after ~turn 1 — the opponent AI's ``Random``
    consumption at playback cannot be reproduced from the record — and ends
    early through the engine's lane-exhaustion teleport bailout (a clean fade
    to black, ``B_OUTCOME_PLAYER_TELEPORTED``; no crash). It is a "what-if"
    view of a Frontier battle, not the battle as it happened. See the
    research doc §1.5 for why.

    Byte transform (offsets relative to the struct at +4):
      * playerParty(+0) <-> opponentParty(+600), 600 bytes each
      * battleFlags(+1260): ``|= RECORDED_LINK``, ``&= ~IS_MASTER``,
        ``&= ~RECORDED_IS_MASTER`` (bit 31 clear = opponent-side render)
      * playersName[1](+1208), playersGender[1](+1233)=0,
        playersTrainerId[1](+1240)=nonzero, playersLanguage[1](+1253)=2
        -- FAKE-LINK records only; a genuine RECORDED_LINK record keeps its
        real opponent identity so the faithful view labels the real trainer.
      * playersBattlers(+1264)=[0,1,0,0], multiplayerId(+1274)=1
      * battleRecord lanes(+1308): unchanged
      * checksum(+3964): recomputed

    SINGLE battles only: raises RecError for a double/multi record, whose
    four-lane battler layout the fixed step-4 mapping cannot represent.

    The caller's bytes are never modified. Raises RecError if the input is
    not a valid record; the output is re-validated (must return []).
    """
    errors = validate(rec)
    if errors:
        raise RecError("refusing to POV-transform an invalid record: "
                       + "; ".join(errors))
    out = bytearray(rec)
    base = STRUCT_OFF
    foff = base + 1260
    orig_flags = int.from_bytes(out[foff:foff + 4], "little")

    # SINGLE battles only. The fixed battler layout written in step 4
    # (playersBattlers=[0,1,0,0], multiplayerId=1) maps a two-lane singles
    # record. A double/multi record has four populated battler lanes that
    # this layout would mis-attribute, so refuse rather than emit a
    # structurally-valid but wrongly-mapped replay.
    if orig_flags & (FLAG_DOUBLE | FLAG_MULTI):
        raise RecError("opponent POV is only supported for single battles "
                       "(this record is a double/multi battle)")

    # A genuine link record (RECORDED_LINK already set) carries both real
    # trainers' identities; only a fake-link (vs-AI Frontier) record needs a
    # fabricated bottom trainer. Preserving the real name/id is what makes
    # the link flip faithful (docs/research/opponent-pov.md §3).
    genuine_link = bool(orig_flags & FLAG_RECORDED_LINK)

    # 1. Swap the two 600-byte party blocks (parties are bound to sides).
    p0, p1 = base + 0, base + 600
    player_party = bytes(out[p0:p0 + 600])
    out[p0:p0 + 600] = out[p1:p1 + 600]
    out[p1:p1 + 600] = player_party

    # 2. battleFlags -> non-master RECORDED_LINK (renders opponent side).
    flags = (orig_flags | FLAG_RECORDED_LINK) & ~FLAG_IS_MASTER \
        & ~FLAG_RECORDED_IS_MASTER & 0xFFFFFFFF
    out[foff:foff + 4] = flags.to_bytes(4, "little")

    # 3. Fabricate link-player slot 1 (the trainer now at the bottom) — only
    #    for a fake-link record. A genuine link record keeps its real
    #    opponent name/id/language so the faithful view labels the real
    #    trainer. For a fake-link record we now write the REAL opponent's
    #    on-screen NAME (up to 7 Gen-3 chars) when the caller supplies it,
    #    instead of the generic "FOE" — the user saw a real NPC's name
    #    replaced by a placeholder. NOTE: only the on-screen NAME is
    #    corrected; the link character SPRITE stays a generic player
    #    character — the link-replay path cannot draw the frontier trainer's
    #    class art at the bottom (engine limitation, see opponent-pov.md §1.4).
    if not genuine_link:
        name = bottom_trainer_name if bottom_trainer_name else _POV_FAKE_NAME
        out[base + 1208:base + 1216] = _g3encode(name, 8)
        out[base + 1233] = 0                                   # gender male
        out[base + 1240:base + 1244] = \
            _POV_FAKE_TRAINER_ID.to_bytes(4, "little")
        out[base + 1253] = 2                                   # language ENG

    # 4. Battler positions + viewer slot: battler 1 renders at the bottom.
    out[base + 1264:base + 1268] = bytes((0, 1, 0, 0))         # playersBattlers
    out[base + 1274:base + 1276] = (1).to_bytes(2, "little")   # multiplayerId

    # 5. Lanes untouched (battler-indexed). Recompute the struct checksum.
    csum = sum(out[base:base + CHECKSUM_RANGE]) & 0xFFFFFFFF
    out[base + CHECKSUM_RANGE:base + CHECKSUM_RANGE + 4] = \
        csum.to_bytes(4, "little")

    result = bytes(out)
    errors = validate(result)
    if errors:                               # defensive: must never happen
        raise RecError("internal error: opponent-POV transform produced an "
                       "invalid record: " + "; ".join(errors))
    return result


def summarize(info: dict) -> str:
    """Human-readable multi-line summary of a parse() result."""
    if not info.get("valid"):
        lines = ["NOT a valid battle record"]
        lines += [f"  - {e}" for e in info.get("errors", [])]
        return "\n".join(lines)

    kinds = [label for label, on in (("double", info["is_double"]),
                                     ("multi", info["is_multi"]),
                                     ("two-opponents",
                                      info["is_two_opponents"]),
                                     ("link-recorded",
                                      info["is_link_recorded"])) if on]
    head = f"{info['facility']}, {info['level_mode']}"
    if kinds:
        head += " [" + ", ".join(kinds) + "]"
    lines = [head]
    lines.append(f"  seed {info['rng_seed']}  flags {info['battle_flags']}  "
                 f"anims {'OFF' if info.get('battle_scene_off') else 'on'}  "
                 f"text {info.get('text_speed', '?')}")
    langs = info.get("players_language") or []
    by = info.get("recorded_by") or "?"
    lines.append(f"  recorded by {by} ({info.get('recorded_by_gender', '?')})"
                 + (f", language {langs[0]}"
                    if langs and info.get("multiplayer_id", 0) == 0 else ""))
    opp = info.get("opponent_a_name") or f"#{info['opponent_a']}"
    if info.get("opponent_b_name"):
        opp += f" + {info['opponent_b_name']}"
    lines.append(f"  opponent: {opp}"
                 + (f"  partner: #{info['partner']}"
                    if info.get("partner") else ""))
    if len(info.get("players", [])) > 1:
        lines.append("  players: " + ", ".join(info["players"]))
    for side in ("player", "opponent"):
        mons = ", ".join(
            f"{m['nickname'] or '#' + str(m['species_internal'])} "
            f"Lv{m['level']}" + (" *shiny*" if m["shiny"] else "")
            + ("" if m["checksum_ok"] else " [BAD SUM]")
            for m in info["teams"][side]) or "(empty)"
        lines.append(f"  {side:8s}: {mons}")
    lanes = [f"lane{p}={n}B" for p, n in enumerate(info["input_lanes"]) if n]
    lines.append("  inputs  : " + (", ".join(lanes) if lanes else "(none)"))
    return "\n".join(lines)
