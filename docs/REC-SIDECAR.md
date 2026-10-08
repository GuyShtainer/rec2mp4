# The `.txt` sidecar: save state to open the video on

**Status:** PokeDNA writes this as of 2026-08-03. rec2mp4 **consumes it as of 2026-08-03**:
`pipeline.parse_state_sidecar()` reads the block (every rule below enforced and covered by
`tests/test_layout.py`), the **`trainer` panel section** draws playtime / Pokédex / BP plus the
seven symbols as silver/gold pips in Frontier Pass order, and `--end-card SECONDS` (default 3)
holds the same summary full-frame over the last seconds of the video. The parsed block is also
copied into each video's JSON sidecar as `trainer_state`. The prose above the block is shown by
the separate `export` section and is never parsed.

## What and why

Every battle record PokeDNA exports produces two files with the same stem:

```
BattleArena_Open_Arena-O-20_2026-08-03.rec    the recording mGBA replays
BattleArena_Open_Arena-O-20_2026-08-03.txt    this sidecar
```

Guy's ask: a rendered video should **open on who this actually is** — the real state of the save
the recording came out of — rather than starting cold on an anonymous battle.

None of that state is in the `.rec`. Sector 31 stores the seed, the two teams and the input
lanes; the trainer's progress lives elsewhere in the save and is **read at export time or lost**.
So the sidecar is the only place it exists once the card moves on.

## Format

Plain ASCII, LF-terminated lines. Two parts: a human-readable header (already there — player,
record, seed, both teams, the streak table) and then a **machine-readable block** whose lines are
all `state.<key>: <value>`. Parse only lines that start with `state.`; everything above is prose
and its wording is not stable.

```
state.playtime: 116h 0m 10s
state.dex_seen: 221
state.dex_caught: 162
state.bp: 18
state.bp_card: 15
state.symbols: -------
state.symbols_silver: 0
state.symbols_gold: 0
```

| key | meaning |
|---|---|
| `state.playtime` | total time on the save, `<H>h <M>m <S>s`. H is a u16 and does wrap at 65535 in-game. |
| `state.dex_seen` | Pokédex entries SEEN |
| `state.dex_caught` | entries CAUGHT. Always `<= dex_seen` — they are two separate bit arrays and a mon can be seen without being caught, never the reverse. |
| `state.bp` | **spendable** Battle Points, 0..9999 |
| `state.bp_card` | the BP figure printed on the trainer card. Usually equals `state.bp` but the game updates them at different moments, so do not assume. Prefer `state.bp` for "how many do I have". |
| `state.symbols` | exactly 7 characters, one per facility in Frontier Pass order: **Tower, Dome, Palace, Arena, Factory, Pike, Pyramid**. `-` none, `s` silver, `G` gold. |
| `state.symbols_silver` / `_gold` | counts, for convenience. `silver + gold <= 7`; a facility with gold is counted as gold only. |

## Rules for the consumer

1. **Every key is optional.** PokeDNA omits a block rather than guessing when its input is
   missing: the `state.*` numeric block needs SaveBlock2, and the three `state.symbols*` lines are
   **Emerald-only** (Ruby/Sapphire/FRLG have no Battle Frontier). A Ruby record will have the
   first block and not the second. Render what is present; never substitute a zero for an absent
   key, because "0 BP" and "this game has no BP" are different statements.
2. **Match on the file stem**, not on directory order — a folder can hold many pairs.
3. **A missing `.txt` is not an error.** Records exported before this existed have none. Fall back
   to the title card rec2mp4 already produces.
4. **Do not parse the prose.** The header above the `state.` block is for humans and its wording
   will change. The one exception is the existing filename tag (`_Arena-O-20`), which rec2mp4
   already relies on and stays as it is.
5. Unknown `state.*` keys must be ignored, not fatal — this block will grow.

## Suggested use in the video

An opening card, held ~3 seconds before the battle starts, reading roughly:

```
        GUYA          IDNo 12345
        116h played
        Pokedex  221 seen / 162 caught
        Battle Points  18
        Symbols  - - - - - - -          (Tower..Pyramid)
```

The symbol row is the one worth drawing properly rather than printing as seven characters —
silver/gold pips in Frontier Pass order read instantly and match the in-game Frontier Pass screen
Guy referenced.

**What was built instead:** Guy asked for this on the **last** frames, so the card is an *end*
card (`--end-card SECONDS`, default 3), not an opening one — and it is composited in the same
ffmpeg pass as the info panel, because a separate concat pass would re-encode the entire video a
second time. The pips and their Frontier-Pass order are as described above (facility initials
T D P A F K Y under them). The same data is available *throughout* the video as the `trainer`
panel section, which is the better place for it if you would rather not add a card at all
(`--end-card 0`).

## Producer side (for reference, do not duplicate)

`g3_record_sidecar()` in `projects/PokeDNA/source/gen3_record.c` builds the whole file; the
`state.*` block is at the end. It is pure C and covered by
`projects/PokeDNA/tests/host_streak_test.c`, which asserts every key is present, that the symbol
field is exactly 7 characters of `-`/`s`/`G`, and that `dex_seen >= dex_caught`, against Guy's
real Emerald save.

## Possible future ask: the greeting for NON-ROM opponents

rec2mp4 now opens each video with the opponent's pre-battle Easy Chat line,
decoded from the user's ROM by the opponent id in the record
(`gBattleFrontierTrainers[id].speechBefore`). That covers the 300 Frontier
trainers — but **not** the opponent kinds whose greeting lives in the SAVE
rather than the ROM:

* **record-mix friends** — the greeting sits in the save's Battle Tower
  records (`EmeraldBattleTowerRecord.greeting[6]`), and
* **apprentices** — in their `SaveBlock2` apprentice slot.

Neither is in the `.rec`, so those records get no opening card. If that is
worth fixing, PokeDNA is the only place the data exists at export time: six
`u16` Easy Chat word ids (12 bytes) per opponent, e.g.

```
state.opponent_a_speech: 0x0a01 0x1212 0x0a29 0x1a0a 0x1039 0x1421
```

Raw ids are preferable to pre-rendered text — rec2mp4 already decodes ids
against the user's own ROM, so ids keep the "ship no game text" posture.
(Frontier Brains are a third case: their dialogue is map-script text, not an
Easy Chat line — a name-only "VS" card is the realistic option there.)
