# SAVE-CHECK: which save can be the Strategy-A template?

**Question:** which of the user's Emerald saves boots to the overworld with the
Frontier Pass usable, so the Battle Record playback (sector 31 injection) is reachable?

**Verdict: use `local/alt-saves/all-shiny.sav`.** `template.sav` is a fully erased
(all-0xFF) flash image and is unusable; both alt-saves are valid post-game saves with
the Frontier Pass, and `all-shiny.sav` additionally already carries a real Battle
Record in sector 31 (useful as a format reference to diff against our injections).

All facts below were derived from the pokeemerald decomp at
`projects/PokeDNA/daycare map/pokeemerald` (REFERENCE ONLY — no code copied).
Paths in citations are relative to that decomp root.

---

## 1. Save-format derivation (with citations)

### 1.1 Sector geometry and footer

* A save sector is 4096 bytes: 3968 data + 128 footer, of which 12 bytes are used —
  `include/save.h:8-10`.
* Footer layout from `struct SaveSector` (`include/save.h:70-78`): after
  `data[3968]` and `unused[128-12]` the used footer fields sit at fixed offsets
  inside the 4096-byte sector:

  | offset | field | type |
  |---|---|---|
  | 4084 (0xFF4) | `id` (section id 0..13) | u16 |
  | 4086 (0xFF6) | `checksum` | u16 |
  | 4088 (0xFF8) | `signature` | u32 |
  | 4092 (0xFFC) | `counter` | u32 |

* Valid-sector signature `SECTOR_SIGNATURE = 0x8012025` (i.e. 0x08012025) —
  `include/save.h:15`.
* 2 save slots × 14 sectors: slot 1 = physical sectors 0–13, slot 2 = 14–27; then
  HoF 28–29, Trainer Hill 30, **Recorded Battle 31 (0x1F000)** —
  `include/save.h:19-31` and the layout comment `src/save.c:26-42`.
* Sectors **rotate within a slot** (same comment, `src/save.c:35-40`), so the
  physical position ≠ section id; a parser must map by the footer `id`.

### 1.2 Section contents and checksum

* `sSaveSlotLayout` (`src/save.c:44-75`): section id 0 = SaveBlock2; ids 1–4 =
  SaveBlock1 split into 3968-byte chunks; ids 5–13 = PokemonStorage.
* Checksum (`CalculateChecksum`, `src/save.c:674-686`): sum the section's *valid*
  bytes as little-endian u32 words, then fold `(sum>>16) + sum` to u16. Valid sizes
  come from the struct sizes (`SAVEBLOCK_CHUNK`, `src/save.c:44-50`):
  `sizeof(SaveBlock2)=0xF2C`, `sizeof(SaveBlock1)=0x3D88` → chunk sizes
  3968/3968/3968/3848, `sizeof(PokemonStorage)=0x83D0` → 8×3968 + 2000.
  (Empirically confirmed: with exactly these sizes all 14 sections of both slots of
  both alt-saves checksum-validate.)

### 1.3 Active-slot selection

`GetSaveValidStatus` (`src/save.c:514-640`): a slot is OK when all 14 section ids
are present with correct signature+checksum; each valid sector's `counter` is
recorded. When **both** slots are OK, the slot with the **higher counter** wins,
with a special wraparound case for counters {0, 0xFFFFFFFF} (`src/save.c:587-604`).
If only one slot is OK, it is used.

### 1.4 SaveBlock1 — location and flags

`struct SaveBlock1` (`include/global.h:984-1021`):

* `0x00` `struct Coords16 pos` (s16 x, s16 y);
* `0x04` `struct WarpData location` — `s8 mapGroup` at +0, `s8 mapNum` at +1
  (`include/global.h:581-588`), i.e. **mapGroup at SB1+0x04, mapNum at SB1+0x05**;
* `0x1270` `u8 flags[NUM_FLAG_BYTES]` (`include/global.h:1020`), where
  `NUM_FLAG_BYTES = ROUND_BITS_TO_BYTES(FLAGS_COUNT)` (`include/global.h:148`).

`FLAGS_COUNT` (`include/constants/flags.h:1638-1641`):
`DAILY_FLAGS_END = FLAG_UNUSED_0x95F + (7 - 0x95F % 8)` = 0x95F (0x95F%8 == 7), so
**`FLAGS_COUNT = 0x960` = 2400 flags → 300 flag bytes** (0x1270..0x139B; the next
field `vars[]` at 0x139C confirms, `include/global.h:1021`).

### 1.5 FLAG_SYS_FRONTIER_PASS — resolved number

* `TRAINER_FLAGS_START = 0x500`; `TRAINER_FLAGS_END = 0x500 + MAX_TRAINERS_COUNT -
  1` (`include/constants/flags.h:1343-1344`) with `MAX_TRAINERS_COUNT = 864`
  (`include/constants/opponents.h:865`) → TRAINER_FLAGS_END = 0x85F.
* `SYSTEM_FLAGS = TRAINER_FLAGS_END + 1 = 0x860` (`include/constants/flags.h:1348`).
* `FLAG_SYS_FRONTIER_PASS = SYSTEM_FLAGS + 0x72` (`include/constants/flags.h:1482`)
  → **0x8D2 = 2258**.
* Bit position: byte `2258 >> 3 = 282` (0x11A), bit `2258 & 7 = 2` → absolute
  SaveBlock1 offset **0x138A, mask 0x04**; that byte lives in SB1 chunk 1
  (section id 2) at data offset 0x40A.
* Sanity companions: `FLAG_SYS_GAME_CLEAR = SYSTEM_FLAGS + 0x4 = 0x864`
  (`include/constants/flags.h:1354`), `FLAG_BADGE08_GET = SYSTEM_FLAGS + 0xE = 0x86E`
  (`include/constants/flags.h:1366`).

### 1.6 SaveBlock2 — player identity

`struct SaveBlock2` (`include/global.h:508-517`), `PLAYER_NAME_LENGTH = 7`
(`include/constants/global.h:97`):

| offset | field |
|---|---|
| 0x00 | `playerName[8]` (Gen-3 text, 0xFF-terminated) |
| 0x08 | `playerGender` (0 = male, 1 = female) |
| 0x0A | `playerTrainerId[4]` |
| 0x0E | `playTimeHours` (u16) |
| 0x10 | `playTimeMinutes` (u8) |
| 0x11 | `playTimeSeconds` (u8) |

### 1.7 Playback does NOT cross-check the host save

`IsRecordedBattleSaveValid` (`src/recorded_battle.c:294-304`) validates the sector-31
record purely internally: `battleFlags != 0`, not `BATTLE_TYPE_RECORDED_INVALID`, and
a byte-sum checksum over the `RecordedBattleSave` struct
(`src/recorded_battle.c:477-489` reads it via `TryReadSpecialSaveSector`). There is
**no comparison against the host save's trainer ID or name** — any save with the
Frontier Pass can play back any injected `.rec`. (Player names/IDs shown during
playback come from the record itself, `src/recorded_battle.c:543-549`.)

### 1.8 Map-name resolution

`map_groups.h` is build-generated (absent from the tree); group/num were resolved
from `data/maps/map_groups.json` — group 0 is `gMapGroup_TownsAndRoutes`;
index 32 = `Route117`, index 38 = `Route123`.

---

## 2. Parser + results

Throwaway script: `scratchpad/save_check.py` (session scratchpad, intentionally not
in the project). It implements exactly §1: signature/checksum-validate all 14
sections per slot, pick the active slot per §1.3, reassemble SB1 from section ids
1–4, and read the fields above.

### A) `local/template.sav` — UNUSABLE

* 131072 bytes, but **every byte is 0xFF** (all 32 sectors blank, no signatures).
* This is a backup of an **erased** flash chip (or a failed EZ-Flash dump) — it
  contains no save at all, despite being the cart the `.rec`s came from. The task
  brief said "sector 31 erased"; in fact *all* sectors are erased.
* Tell the user: re-dump the cart if its real save is wanted; as-is this file
  cannot boot past the title screen's "new game".

### B) `local/alt-saves/all-shiny.sav` — VALID, HAS PASS ✅ (chosen)

* Both slots fully OK; active = slot 1 (counter 1658 vs 1657).
* Player **NICK** (M), TID 852968965 (visible 17925), playtime **135:30:18**.
* Location: mapGroup 0, mapNum 32 = **Route 117**, pos (51,6) — normal overworld.
* `FLAG_SYS_FRONTIER_PASS(0x8D2)` = **set**; `FLAG_SYS_GAME_CLEAR(0x864)` = set;
  Badge 8 = set → post-game, consistent (sanity check passes).
* Sector 31 already **contains a real Battle Record** (written by the game on
  hardware) — a bonus reference blob for validating our injected `.rec` framing.

### C) `local/alt-saves/picods-fixed.sav` — VALID, HAS PASS ✅ (backup choice)

* Both slots fully OK; active = slot 2 (counter 1317 vs 1316).
* Player **GUYA** (F), TID 781985402 (visible 9850), playtime **67:14:35**.
* Location: mapGroup 0, mapNum 38 = **Route 123**, pos (18,4) — normal overworld.
* Frontier Pass = **set**; Game Clear = set; Badge 8 = set → consistent.
* Sector 31 is erased/blank (fine — we overwrite it anyway).

### Choice rationale

Both alt-saves qualify (overworld, pass usable, playback host-agnostic per §1.7).
`all-shiny.sav` is picked because its sector 31 holds a genuine hardware-written
record to diff against, and it is the more progressed save; `picods-fixed.sav` is a
drop-in fallback if `all-shiny.sav` misbehaves in the emulator.
