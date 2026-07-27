# rec2mp4 — Opponent-POV playback research ("watch the battle from the other side")

Research question: can a `.rec` be **transformed byte-for-byte** so that the game's own
recorded-battle playback shows the battle from the opponent's perspective — the frontier
trainer's team at the bottom (back sprites) and the recorder's team as the enemy at the top?

**Verdict: YES mechanically, NO faithfully — for the frontier records rec2mp4 handles today.**
A byte-level transform (H2 below) makes the game render the swapped perspective with every
action correctly attributed, and the engine accepts it end-to-end (menu → validation →
`BattleMainCB2`). But for vs-AI (frontier) records the replay **provably diverges** from the
real battle after turn 1 and ends early through the engine's lane-exhaustion bailout, because
the opponent AI's `Random()` consumption during action selection cannot be reproduced. The
desync is **structural** (every frontier record, not bad luck). Faithful opponent-POV *is*
possible for genuine link-battle records (bit 25) — a record type this project has none of.

- Decomp (reference only): `D = <local pokeemerald checkout>`
- Test record: `local/recs/GUYA_20-07-2026_07-47.rec` (Battle Dome singles, shortest —
  baseline conversion: 3142 frames / 52.6 s, natural end, outcome `won`)
- Experiments run 2026-07-27 with the shipped, unmodified `rec2mp4.driver` (mGBA headless,
  US Emerald CRC32 1F1C08FB); transform scripts lived in the session scratchpad only.
- Evidence frames: `docs/research/opponent-pov/*.png` (raw emulator framebuffer captures).

---

## 1. How the engine decides which side renders at the bottom

### 1.1 The game already has a perspective switch — for link records only

Nintendo's own code documents it, in `MoveRecordedBattleToSaveData`
(`D/src/recorded_battle.c:352-354`):

> `// BATTLE_TYPE_RECORDED_IS_MASTER set indicates battle will play`
> `// out from player's perspective (i.e. player with back to camera)`
> `// Otherwise player will appear on "opponent" side`

Flag bits (`D/include/constants/battle.h:60-93`):

| bit | flag | notes |
|---|---|---|
| 1 | `BATTLE_TYPE_LINK` | **forbidden** in a saved record (`BATTLE_TYPE_RECORDED_INVALID`, line 93) |
| 2 | `BATTLE_TYPE_IS_MASTER` | "in not-link battles always set"; a genuine non-master link record has it **clear** |
| 24 | `BATTLE_TYPE_RECORDED` | never stored; OR'd in at playback (`recorded_battle.c:556`) |
| 25 | `BATTLE_TYPE_RECORDED_LINK` | allowed in records (mask `0x7D007E92` excludes it) |
| 31 | `BATTLE_TYPE_RECORDED_IS_MASTER` | allowed; **the perspective bit** |

### 1.2 Controller assignment — what the perspective bit actually does

`InitSinglePlayerBtlControllers` (`D/src/battle_controllers.c:179-215`), singles path,
`BATTLE_TYPE_RECORDED` set:

- **`RECORDED_LINK` + `RECORDED_IS_MASTER`**: battler 0 = `SetControllerToRecordedPlayer`
  at `B_POSITION_PLAYER_LEFT` (bottom), battler 1 = `SetControllerToRecordedOpponent` at
  `B_POSITION_OPPONENT_LEFT` (top).
- **`RECORDED_LINK`, master clear** (comment: *"see how the banks are switched"*): battler
  **1** = RecordedPlayer at the bottom, battler **0** = RecordedOpponent at the top.
- **no `RECORDED_LINK`** (our frontier records): battler 0 = RecordedPlayer (bottom),
  battler 1 = `SetControllerToOpponent` — a **live AI**, not a lane replayer.

Two invariants matter:

1. **Input lanes are indexed by battler id, not by side.** `battleRecord[battler][664]` is
   copied 1:1 into `sBattleRecords` (`recorded_battle.c:583-585`) and consumed by
   `RecordedBattle_GetBattlerAction(battler)` (`recorded_battle.c:208-223`). The non-master
   swap flips which battler *renders* at the bottom; lane ownership never moves.
2. **Parties are bound to sides, unconditionally.** `SetVariablesForRecordedBattle`
   (`recorded_battle.c:524`, party copy at 534-535) always does `gPlayerParty =
   src->playerParty` (bottom side) and `gEnemyParty = src->opponentParty` (top side).

So the record-level recipe for "recorder on top" is: set bit 25, leave bit 31 clear, **swap
the two party blocks**, and leave the lanes alone.

### 1.3 Both sides' actions ARE in the record — including the AI's

`HandleTurnActionSelectionState` records every battler's chosen action/move/target into its
lane regardless of controller (`D/src/battle_main.c:4178`, `4395-4396`, `4573`;
`battle_script_commands.c:5170,5183` for faint replacements). Confirmed on the test record:
`lane0 = 00 00 01 | 00 00 01` (GUYA: USE_MOVE, slot 0 = EARTHQUAKE, target 1 — twice) and
`lane1 = 00 02 01 | 01 | 00 02 00` (AI: move slot 2; switch-in party index 1 after SPHEAL
fainted; move slot 2 again). During *normal* frontier playback lane 1 is dead weight — the
live AI recomputes its turns — but a lane-replaying controller can consume it.

### 1.4 Who is named/drawn where (the fields H2 must fabricate)

- `SetVariablesForRecordedBattle` fills `gLinkPlayers[i]` from `playersName/Gender/
  TrainerId/Language/Battlers[i]` (`recorded_battle.c:539-553`) and sets
  `gRecordedBattleMultiplayerId = src->multiplayerId` (line 560).
- Textboxes: `BattleStringExpandPlaceholders` uses `gRecordedBattleMultiplayerId` when
  `RECORDED_LINK` is set (`D/src/battle_message.c:2321-2325`); `B_TXT_LINK_PLAYER_NAME` =
  `gLinkPlayers[multiplayerId].name`, `B_TXT_LINK_OPPONENT1_NAME` resolves via
  `GetBattlerMultiplayerId(BATTLE_OPPOSITE(...))` (`battle_message.c:2579-2593`), i.e.
  through `playersBattlers[]`.
- Trainer sprites in a `RECORDED_LINK` battle are **always the player characters**: top =
  `PlayerGenderToFrontTrainerPicId(gLinkPlayers[mpId ^ BIT_SIDE].gender)`
  (`D/src/battle_controller_recorded_opponent.c:1234`), bottom back sprite =
  `gLinkPlayers[gRecordedBattleMultiplayerId].gender`
  (`D/src/battle_controller_recorded_player.c:1194`). A fake-link record can never show the
  frontier trainer's class art at the bottom.
- Intro send-out order honors the perspective bit too
  (`D/src/battle_main.c:3594-3660`).

### 1.5 Why vs-AI records cannot replay faithfully without the AI

During battles the per-frame `Random()` tick is disabled (`D/src/main.c:365-366` skips it
for LINK/FRONTIER/RECORDED battles), so `gRngValue` advances **only** at decision points:
battle-script rolls and **AI action selection**. `BattleAI_SetupAIData` burns RNG every
single selection — `AI_THINKING_STRUCT->simulatedRNG[i] = 100 - (Random() % 16)` per
considered move (`D/src/battle_ai_script_commands.c:341`), plus more in target/move choice
(lines 350, 445, 536, 568). Replace the live AI with a lane replayer and those calls vanish
→ every subsequent damage/accuracy/crit roll comes from a shifted RNG stream → the battle
diverges from what was recorded. Nothing in the record stores how many calls the AI made,
so no byte-level transform can compensate. This is why Game Freak only flips perspective
for link records, where **both** lanes were produced by humans and no AI runs at playback.

---

## 2. Experiments (real emulator, unmodified driver)

Both transforms pass `rec.validate()` (sentinel/flags/checksum) and the game's own
`IsRecordedBattleSaveValid` (Frontier Pass accepted the record; `hasBattleRecord=1`).

### 2.1 H2 "fake link record" — perspective swap WORKS, fidelity breaks

Transform (byte level, offsets relative to the 3968-byte struct at sector offset +4):

| field | offset | change |
|---|---|---|
| playerParty / opponentParty | 0 / 600 | **swap the two 600-byte blocks** |
| battleFlags | 1260 | `|= 1<<25` (RECORDED_LINK), `&= ~(1<<2)` (IS_MASTER, mimic genuine non-master record); bit 31 stays 0 |
| playersName[1] | 1208 | fabricated Gen-3 name for the person now at the bottom ("FOE") |
| playersGender[1] | 1233 | 0 |
| playersTrainerId[1] | 1240 | any nonzero |
| playersLanguage[1] | 1253 | 2 (ENG) |
| playersBattlers | 1264 | `[0,1,0,0]` |
| multiplayerId | 1274 | 1 (viewer = the slot whose battler is 1 = bottom) |
| battleRecord lanes | 1308 | **unchanged** (lanes are battler-indexed) |
| checksum | 3964 | recomputed byte-sum of struct bytes 0..3963 |

Result (driver log): battle reached `BattleMainCB2` with
`gBattleTypeFlags=0x03010008` = RECORDED|RECORDED_LINK|DOME|TRAINER, `recorded=True`;
captured 2084 frames (34.9 s); `end_reason=natural`, **outcome 5 = B_OUTCOME_PLAYER_TELEPORTED**
— the lane-exhaustion bailout of `RecordedBattle_GetBattlerAction`
(`recorded_battle.c:208-217`, the decomp's own `// hah` comment), which fades to black and
quits gracefully. No hang, no crash.

Frames (`docs/research/opponent-pov/h2-*.png`):

- `h2-intro-brendan-vs-may.png` — link-battle intro: Brendan (fake slot-1 person) bottom,
  May (GUYA, the real recorder) standing at the top.
- `h2-bottom-sends-spheal.png` — "Go! SPHEAL!": the frontier trainer's mon is sent out
  **from the bottom**, player-style.
- `h2-guya-sends-metagross-top.png` — "GUYA sent out METAG…": the recorder sends out from
  the top; bottom HP box is the player-style one (SPHEAL ♀ Lv60, 193/193, EXP bar).
- `h2-foe-metagross-earthquake.png` — **"Foe METAGROSS used EARTHQUAKE!"** — exactly the
  recorder's real turn-1 action (lane0 byte decode above), correctly attributed to the
  now-enemy side. Names, HP boxes and attribution all line up.

Fidelity check vs the original battle: in the real battle EARTHQUAKE one-shot SPHEAL on
turn 1 and NUMEL on turn 2 (that is the only reading consistent with lane byte counts and
the baseline `won` at 52.6 s). In the transformed replay SPHEAL survived turn 1 at 55/193,
hail got rolled in, and the record ran out with SPHEAL still alive at 40/193 →
teleport-quit at 34.9 s. Different damage rolls = the RNG-shift desync of §1.5, observed.

### 2.2 H1 "party+lane swap" — plays to the end, but it is a different battle

Transform: swap the party blocks AND lanes 0↔1, keep frontier flags (`0x0001000C`),
recompute checksum.

Result: `gBattleTypeFlags=0x0101000C`, natural end at 39.4 s, **outcome 2 = lost** (bottom
side = frontier mons lost — macro-consistent with the original). But the replay is not the
recorded battle: battler 1 (top) is a **live AI** now driving the recorder's mons, and it
invented its own actions — `h1-ai-invents-meteor-mash.png` shows "Foe METAGROSS used
METEOR MASH!" where the real GUYA pressed EARTHQUAKE both turns. `h1-juliana-sent-out-
metagross.png` shows the intro absurdity: "PARASOL LADY JULIANA sent out METAGROSS!" (the
frontier trainer's class sprite owns the recorder's shiny team). The bottom lane (old AI
actions incl. the mid-stream faint-switch byte) only stayed aligned because the fresh AI
happened to also KO SPHEAL in time; any other flow misaligns the byte stream → wrong
actions or the teleport bailout. H1 is a "what-if the AI played your team" generator, not
a POV flip.

### 2.3 H3 — anything better?

No third knob exists. Perspective is decided solely by the §1.2 controller table; there is
no render-side variable to flip for a non-link record, and parties are hard-bound to sides
(§1.1-1.2). The one *correct* application of the mechanism: a **genuine RECORDED_LINK
record** (recorded from a real link battle) can be perspective-flipped faithfully — toggle
bit 31, swap the party blocks, swap `multiplayerId` to the other player's slot — because
both lanes are human-recorded and no AI runs, so the RNG stream is identical. Untested
here (no link records exist in `local/recs/`), but it is exactly the transform the game
itself performs between the two players' consoles.

---

## 3. Risks / oddities catalogued (for the record)

- **Early cut-off is the failure mode, not corruption**: lane exhaustion lands in
  `B_OUTCOME_PLAYER_TELEPORTED` → clean fade + `CB2_QuitRecordedBattle`. The driver's
  outcome poll reports 5; video just ends mid-battle.
- **Bottom trainer is always Brendan/May in a fake-link record** (§1.4) — the frontier
  trainer's class art cannot appear with its team at the bottom.
- Evolution is already impossible in these replays (`battle_main.c:4640/4674` exclude
  LINK/RECORDED_LINK/FRONTIER), and battle anims follow the record's own option byte, so no
  new oddities there.
- Recorder-side in-battle **move reordering** is stored as extra lane bytes
  (`battle_main.c:4395-4396` + `RecordedBattle_ClearBattlerAction`); a RecordedOpponent
  controller replays them too, but any desync makes them fire under the wrong state.
- The Frontier Pass preview screen and the sidecar parser both accept the transformed
  record (it is structurally valid), so a mislabeled "fake" record is indistinguishable
  from a real one — a product would need to tag such outputs loudly.

## 4. Go / no-go recommendation

**NO-GO for productizing "the same battle from the opponent's side"** on the record types
rec2mp4 actually has (all 11 are frontier vs-AI records): the output is either a battle
that visibly stops mid-way with a teleport quit (H2) or a fabricated battle with invented
player-side actions (H1). Neither can honestly be sold as the user's battle.

Paths that would change the answer, in order of realism:

1. **Genuine link records** — if the user ever records a real link battle, the H2-family
   transform (bit 31 toggle + party swap + multiplayerId swap) should be faithful; wire it
   up then, with one emulator validation run.
2. **RNG-trace compensation (heavy, experimental)**: run the *original* record once,
   sample `gRngValue` at each action-selection boundary, then run the H2 transform while
   force-writing those sampled values back at the matching boundaries via the driver's RAM
   access. Feasible with the existing driver primitives, but boundary detection across two
   differently-paced runs is fragile — a research project, not a feature.
3. **"What-if mode" novelty**: ship H1/H2 behind an explicit `--what-if-pov` flag labeled
   as non-faithful. Cheap (the transform is ~40 lines, spec in §2.1), honest only with
   loud labeling; H2's abrupt teleport ending makes for a poor video, so H1 would be the
   less-bad candidate. Not recommended as a headline feature.
