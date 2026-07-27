# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""Runtime lookups in the USER-SUPPLIED US Emerald ROM.

This module reads display data (Battle Frontier opponent names) out of the
user's own ROM image at runtime. NO name tables, strings or any other
Game Freak data ship with rec2mp4 — only addresses and struct layouts,
which are facts derived from the pret/pokeemerald decompilation project's
published headers and symbol file.

Layout facts (all verified against local/pokeemerald.sym and the decomp):

  struct BattleFrontierTrainer            // include/battle_tower.h:16-25
  {
      u8 facilityClass;                   // +0
      u8 filler1[3];                      // +1..3 (padding)
      u8 trainerName[PLAYER_NAME_LENGTH + 1];   // +4, 8 bytes, Gen-3 charset
      u16 speechBefore[EASY_CHAT_BATTLE_WORDS_COUNT];  // +12, 6 easy-chat words
      u16 speechWin[EASY_CHAT_BATTLE_WORDS_COUNT];     // +24
      u16 speechLose[EASY_CHAT_BATTLE_WORDS_COUNT];    // +36
      const u16 *monSet;                  // +48, ROM pointer (0x08/0x09 bus)
  };                                      // sizeof = 52

  PLAYER_NAME_LENGTH = 7                  // include/constants/global.h:97
  EASY_CHAT_BATTLE_WORDS_COUNT = 6        // include/constants/global.h:99
  FRONTIER_TRAINERS_COUNT = 300           // include/constants/battle_frontier_trainers.h:305

  gBattleFrontierTrainers  @ 0x085D5ACC   // pokeemerald.sym; size 0x3CF0
                                          //  = 15600 = 300 * 52 (confirms sizeof)

The on-screen name "SAILOR MAXWELL" is class name + trainer name. The game
builds it from two lookups (the exact data flow rec2mp4 mirrors):

  trainerClass =
      gFacilityClassToTrainerClass[gFacilityTrainers[trainerId].facilityClass];
                                          // src/battle_tower.c:1455
                                          //  (GetFrontierOpponentClass, the
                                          //   trainerId < FRONTIER_TRAINERS_COUNT arm;
                                          //   gFacilityTrainers points at
                                          //   gBattleFrontierTrainers for the
                                          //   Frontier facilities)
  class name = gTrainerClassNames[trainerClass]
                                          // extern const u8 gTrainerClassNames[][13]
                                          //  include/data.h:138
  trainer name = gFacilityTrainers[trainerId].trainerName
                                          // src/battle_tower.c:1536-1539
                                          //  (GetFrontierTrainerName)

  gFacilityClassToTrainerClass @ 0x0831F5CA  // pokeemerald.sym; size 0x52 = 82
                                             //  = FACILITY_CLASSES_COUNT
                                             //  (include/constants/trainers.h:206)
  gTrainerClassNames           @ 0x0830FCD4  // pokeemerald.sym; size 0x35A = 858
                                             //  = 66 classes * 13 bytes

Species names (used by the side panel / team displays):

  extern const u8 gSpeciesNames[][POKEMON_NAME_LENGTH + 1]  // include/data.h:139
  POKEMON_NAME_LENGTH = 10                   // include/constants/global.h:95
  gSpeciesNames @ 0x083185C8                 // pokeemerald.sym; size 0x11B4
                                             //  = 4532 = 412 * 11 (confirms stride)

  The table is indexed by INTERNAL species id and runs SPECIES_NONE (0) ..
  SPECIES_CHIMECHO (411) — src/data/text/species_names.h (first entry
  [SPECIES_NONE], last entry [SPECIES_CHIMECHO]). Internal order differs
  from the National Dex: ids 252..276 are the SPECIES_OLD_UNOWN_* hole
  (constants/species.h:256-281), whose name entries are placeholder "?"
  glyphs, and Hoenn mons start at SPECIES_TREECKO = 277
  (constants/species.h:283). SPECIES_EGG = 412 = NUM_SPECIES
  (constants/species.h:418-420) has NO row in the table — GetSpeciesName
  (src/pokemon.c:4618) only range-checks species > NUM_SPECIES, and the
  game displays eggs through a separate path — so 412 is special-cased
  here to the label "EGG" instead of reading past the table.

Pure stdlib; returns None on ANY doubt rather than a wrong name.
"""

from __future__ import annotations

# ROM addresses from local/pokeemerald.sym (US Emerald BPEE rev0,
# CRC32 1F1C08FB — the same ROM states.py is bound to).
ROM_BASE = 0x08000000
GBATTLE_FRONTIER_TRAINERS_ADDR = 0x085D5ACC
GFACILITY_CLASS_TO_TRAINER_CLASS_ADDR = 0x0831F5CA
GTRAINER_CLASS_NAMES_ADDR = 0x0830FCD4

FRONTIER_TRAINERS_COUNT = 300      # battle_frontier_trainers.h:305
BFT_ENTRY_SIZE = 52                # sizeof(struct BattleFrontierTrainer)
BFT_NAME_OFFSET = 4                # trainerName[8] after facilityClass + filler
BFT_NAME_LEN = 8                   # PLAYER_NAME_LENGTH + 1
BFT_MONSET_OFFSET = 48             # const u16 *monSet
FACILITY_CLASSES_COUNT = 82        # trainers.h:206 (0x52)
TRAINER_CLASSES_COUNT = 66         # sym size 0x35A / 13
TRAINER_CLASS_NAME_LEN = 13        # gTrainerClassNames[][13], data.h:138

GSPECIES_NAMES_ADDR = 0x083185C8   # pokeemerald.sym; size 0x11B4 = 412 * 11
SPECIES_NAME_LEN = 11              # POKEMON_NAME_LENGTH + 1 (global.h:95)
SPECIES_NAMES_COUNT = 412          # rows 0..411, species_names.h (no EGG row)
SPECIES_EGG = 412                  # constants/species.h:418 (= NUM_SPECIES)

# ---------------------------------------------------------------------------
# Strict Gen-3 charset decode (same table style as rec.py's _g3chr, but any
# byte OUTSIDE the table aborts the decode -> None, instead of yielding '?').
# Frontier trainer/class names only ever use this ASCII-safe subset; hitting
# anything else means we are not looking at a real name (wrong ROM, bad id).
# ---------------------------------------------------------------------------

_G3_PUNCT = {0x00: ' ', 0x1B: 'e',          # 0x1B = e-acute (POKeMANIAC...)
             # Composed ligature glyphs: charmap.txt:51-52 in the decomp
             # ("PKMN = 53 54", "POKEBLOCK = 55 56 57 58 59") — the class
             # names "PKMN BREEDER"/"PKMN RANGER" use the 0x53 0x54 pair.
             0x53: 'PK', 0x54: 'MN',
             0x55: 'PO', 0x56: 'KE', 0x57: 'BL', 0x58: 'OC', 0x59: 'K',
             0xAB: '!', 0xAC: '?', 0xAD: '.', 0xAE: '-', 0xB4: "'",
             0xB5: 'M', 0xB6: 'F',          # male/female signs -> M/F
             0xB8: ',', 0xBA: '/', 0xF0: ':'}


def _g3str_strict(raw: bytes) -> str | None:
    """Decode a Gen-3 string up to EOS 0xFF; None if any byte is unknown."""
    out: list[str] = []
    for b in raw:
        if b == 0xFF:                       # EOS
            break
        if 0xBB <= b <= 0xD4:
            out.append(chr(ord('A') + b - 0xBB))
        elif 0xD5 <= b <= 0xEE:
            out.append(chr(ord('a') + b - 0xD5))
        elif 0xA1 <= b <= 0xAA:
            out.append(chr(ord('0') + b - 0xA1))
        elif b in _G3_PUNCT:
            out.append(_G3_PUNCT[b])
        else:
            return None                     # not a plausible name byte
    s = ''.join(out).strip()
    return s or None


def _rom_slice(rom: bytes, addr: int, n: int) -> bytes | None:
    """addr (0x08-bus) -> n bytes of the ROM image, or None if out of range."""
    off = addr - ROM_BASE
    if off < 0 or off + n > len(rom):
        return None
    return rom[off:off + n]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def frontier_trainer_name(rom_bytes: bytes, trainer_id: int) -> str | None:
    """'<CLASS> <NAME>' (e.g. 'SAILOR MAXWELL') for a ROM frontier trainer.

    trainer_id must be a gBattleFrontierTrainers index (0..299 — the
    opponentA/opponentB range below TRAINER_RECORD_MIXING_FRIEND). Returns
    None on any doubt: id out of range, ROM too small / not US Emerald,
    implausible struct contents, undecodable name bytes.
    """
    if not isinstance(rom_bytes, (bytes, bytearray, memoryview)):
        return None
    rom = bytes(rom_bytes) if not isinstance(rom_bytes, bytes) else rom_bytes
    if not isinstance(trainer_id, int) or not (
            0 <= trainer_id < FRONTIER_TRAINERS_COUNT):
        return None

    entry = _rom_slice(rom,
                       GBATTLE_FRONTIER_TRAINERS_ADDR
                       + trainer_id * BFT_ENTRY_SIZE,
                       BFT_ENTRY_SIZE)
    if entry is None:
        return None

    # Plausibility: monSet must be a ROM pointer (0x08/0x09 cart bus) —
    # cheap proof we are really looking at a BattleFrontierTrainer entry.
    mon_set = int.from_bytes(entry[BFT_MONSET_OFFSET:BFT_MONSET_OFFSET + 4],
                             "little")
    if not (0x08000000 <= mon_set < 0x0A000000):
        return None

    facility_class = entry[0]
    if facility_class >= FACILITY_CLASSES_COUNT:
        return None

    f2c = _rom_slice(rom, GFACILITY_CLASS_TO_TRAINER_CLASS_ADDR,
                     FACILITY_CLASSES_COUNT)
    if f2c is None:
        return None
    trainer_class = f2c[facility_class]
    if trainer_class >= TRAINER_CLASSES_COUNT:
        return None

    class_raw = _rom_slice(rom,
                           GTRAINER_CLASS_NAMES_ADDR
                           + trainer_class * TRAINER_CLASS_NAME_LEN,
                           TRAINER_CLASS_NAME_LEN)
    if class_raw is None:
        return None
    class_name = _g3str_strict(class_raw)

    name = _g3str_strict(entry[BFT_NAME_OFFSET:
                               BFT_NAME_OFFSET + BFT_NAME_LEN])
    if not class_name or not name:
        return None
    return f"{class_name} {name}"


def species_name(rom_bytes, internal_id: int) -> str | None:
    """UPPERCASE species name for a Gen-3 INTERNAL species id, read from the
    user's own ROM (gSpeciesNames, layout facts in the module docstring).

    internal_id is the id stored in party mons (rec.py's species_internal):
    1..411, where 252..276 is the OLD_UNOWN hole (the ROM's rows there are
    placeholder '?' names, returned as-is — callers should treat an all-'?'
    result as unknown) and 412 = EGG (no ROM row; returns the label 'EGG').
    Returns None on any doubt: id out of range, ROM too small / not US
    Emerald, undecodable name bytes.
    """
    if not isinstance(rom_bytes, (bytes, bytearray, memoryview)):
        return None
    rom = bytes(rom_bytes) if not isinstance(rom_bytes, bytes) else rom_bytes
    if not isinstance(internal_id, int) or isinstance(internal_id, bool):
        return None
    if internal_id == SPECIES_EGG:
        return "EGG"
    if not (0 <= internal_id < SPECIES_NAMES_COUNT):
        return None
    raw = _rom_slice(rom, GSPECIES_NAMES_ADDR
                     + internal_id * SPECIES_NAME_LEN, SPECIES_NAME_LEN)
    if raw is None:
        return None
    return _g3str_strict(raw)
