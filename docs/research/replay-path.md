# rec2mp4 — Replay path: power-on → watching the recorded battle → end detection

Research target: **US Emerald (BPEE rev0)**, ROM CRC32 1F1C08FB — the exact build pret/pokeemerald
reproduces, so every `pokeemerald.sym` address below is directly valid for the user's ROM.

- Decomp (reference only): `D = <local pokeemerald checkout>`
- Symbols: `<rec2mp4>/local/pokeemerald.sym`
  (curl'd from `https://raw.githubusercontent.com/pret/pokeemerald/symbols/pokeemerald.sym`, gitignored, reusable)
- Record format: PokeDNA `docs/analysis-2026-07-17/record-spec.md`

**Convention used everywhere below:** all game code is Thumb, so a function whose link-time
address is `0x08XXXXXX` appears in `gMain.callback2` (and in `gTasks[].func`, `gMenuCallback`)
as **`0x08XXXXXX | 1`**. Every "callback2 == F" test in this plan means
`read_u32(0x030022C4) == (F | 1)`.

---

## 0. gMain layout (US: gMain = 0x030022C0, size 0x43C)

Derived from `struct Main`, `D/include/main.h:8-41`. `SetMainCallback2` stores the pointer AND
zeroes `gMain.state` (`D/src/main.c:197-201`).

| offset | abs address | size | field |
|---|---|---|---|
| +0x000 | 0x030022C0 | 4 | callback1 (overworld: `CB1_Overworld`) |
| +0x004 | 0x030022C4 | 4 | **callback2 — the master poll target** |
| +0x008 | 0x030022C8 | 4 | savedCallback (battle exit target) |
| +0x00C | 0x030022CC | 4 | vblankCallback |
| +0x020 | 0x030022E0 | 4 | vblankCounter1 (free-running frame counter) |
| +0x02C | 0x030022EC | 2 | heldKeys (after L=A remap) |
| +0x02E | 0x030022EE | 2 | newKeys (JOY_NEW source) |
| +0x438 | 0x030026F8 | 1 | state (zeroed by every SetMainCallback2) |
| +0x439 | 0x030026F9 | 1 | bit1 = inBattle |

Other polling primitives:

- `gPaletteFade` = 0x02037FD4 (`.sym`); bitfield layout `D/include/palette.h:35-53` →
  **`active` = bit 7 of byte 0x02037FDB** (multipurpose1 u32 @+0, delayCounter:6 @+4,
  y:5/targetY:5 straddle bytes 4-5, blendColor:15 fills bits 0-14 of the u16 at +6, active is
  bit 15 of that u16 = byte +7 bit 7). Test: `read8(0x02037FDB) & 0x80`.
- `gTasks` = 0x03005E00, 16 entries × 0x28 (`struct Task`, `D/include/task.h:13-21`):
  `func` u32 @+0, `isActive` u8 @+4. "Task T is running" =
  ∃i<16: `read8(0x03005E00+0x28*i+4)!=0 && read_u32(0x03005E00+0x28*i)==(T|1)`.
- `gMenuCallback` = 0x03005DF4 (start-menu dispatch pointer, `D/src/start_menu.c:78`).

Input injection rule (mGBA `emu:setKeys`): the game latches `newKeys` from a *transition*
(`ReadKeys` each frame), so every "press X" = hold X for 2 frames, release for ≥2 frames.
"Hold X" = keep it set across frames.

---

## 1. Exact user-visible path and button presses (Strategy A, template.sav injected)

Pre-step on the PC, before boot: inject the `.rec` verbatim into `.sav` bytes
0x1F000..0x1FFFF (sector 31), and **verify FLAG_SYS_FRONTIER_PASS is set in the save** (§4).
The BATTLE RECORD area itself is gated *only* on sector-31 validity
(`CanCopyRecordedBattleSaveData`, `D/src/recorded_battle.c:286-292` → full read+validate of
the sector; cached into `sPassData->hasBattleRecord` at pass open,
`D/src/frontier_pass.c:640`), but *reaching* the pass needs the flag (step 6).

| # | screen | button(s) | what happens (decomp cite) |
|---|---|---|---|
| 1 | Copyright + GameCube logo | none (not skippable) | `CB2_InitCopyrightScreenAfterBootup` also loads the save and inits the heap (`D/src/intro.c:1147-1160`) |
| 2 | Intro movie | **A** (any key) | `MainCB2_Intro` skips on `gMain.newKeys != 0 && !gPaletteFade.active` (`D/src/intro.c:1042-1052`) → `MainCB2_EndIntro` → title |
| 3 | Title screen | **Start** (or A) | `Task_TitleScreenPhase3`: `JOY_NEW(A_BUTTON) || JOY_NEW(START_BUTTON)` → fade → `CB2_GoToMainMenu` → `CB2_InitMainMenu` (`D/src/title_screen.c:780-827`) |
| 4 | Main menu (CONTINUE preselected, item 0) | **A** | `HandleMainMenuInput` A-branch (`D/src/main_menu.c:888-894`) → `Task_HandleMainMenuAPressed` → `ACTION_CONTINUE` → `SetMainCallback2(CB2_ContinueSavedGame)` (`D/src/main_menu.c:1064-1069`). Cold boot cursor = item 0 (`sCurrItemAndOptionMenuCheck` EWRAM starts 0, `D/src/main_menu.c:172,689`); CONTINUE is item 0 in every menu type that has it (`:512-515`) |
| 5 | Overworld loads | none | `CB2_ContinueSavedGame` (`D/src/overworld.c:1705+`) → `CB2_LoadMap`/`CB2_ReturnToField` → steady `CB2_Overworld` |
| 6 | Overworld | **Start** | opens start menu (`ShowStartMenu`/`Task_ShowStartMenu`, `D/src/start_menu.c:560-590`); retry if eaten (§2 step 5) |
| 7 | Start menu | **Down × k, then A** | move cursor to the **player-name entry** (`MENU_ACTION_PLAYER`, id 4). Full menu order: Pokédex, Pokémon, Bag, PokéNav, *name*, Save, Option, Exit (`BuildNormalStartMenu`, `D/src/start_menu.c:315-337`) → k=4 with a full save; **derive k at runtime** from `sCurrentStartMenuActions` (§2 step 6). A → `StartMenuPlayerNameCallback`: with `FLAG_SYS_FRONTIER_PASS` set → `ShowFrontierPass(CB2_ReturnToFieldWithOpenMenu)` (`D/src/start_menu.c:699-717`) |
| 8 | Frontier Pass (free-moving hand cursor, NOT a list menu) | **hold Left**, then **hold Up**, release, **A** | cursor moves 2 px/frame while held (`Task_HandleFrontierPassInput`, `D/src/frontier_pass.c:987-1063`). Cursor spawns at (176,104) = TRAINER CARD area when outside the frontier (`AllocateFrontierPassData`, `:620-635`). BATTLE RECORD hitbox (in `GetCursorAreaFromCoords(x-5, y+5)` space): **y 80..102, x 20..108** (`sPassAreasLayout[CURSOR_AREA_RECORD-1]`, `:350`). A is only processed on a frame where the cursor did NOT move (`:1015`), so release the D-pad, wait 2 frames, then press A |
| 9 | (no confirmation dialog) | none | A on RECORD → `TryCallPassAreaFunction` (needs `hasBattleRecord`, `:963-968`) → `CB2_ShowFrontierPassFeature` → fade-out → **`PlayRecordedBattle(CB2_ReturnFromRecord)`** (`D/src/frontier_pass.c:938-961`) |
| 10 | Black screen ~128 frames, then battle | none | `PlayRecordedBattle` re-reads+validates sector 31, sets all battle vars, starts `Task_StartAfterCountdown` (128 frames) under `CB2_RecordedBattle`, then `savedCallback=CB2_RecordedBattleEnd`, `SetMainCallback2(CB2_InitBattle)` (`D/src/recorded_battle.c:513-524, 586-611`) |
| 11 | Recorded battle plays itself | none (inputs come from the record) | `CB2_InitBattle` → `CB2_InitBattleInternal` → `CB2_HandleStartBattle` (or `CB2_HandleStartMultiPartnerBattle`/`CB2_HandleStartMultiBattle` for multi records) → `BattleMainCB2` (`D/src/battle_main.c:588-616, 673-693, 1141, 1402, 1852`) |

Total scripted presses: A (intro) · Start (title) · A (continue) · Start (overworld) ·
Down×k + A (start menu) · Left-hold + Up-hold + A (pass). Everything else is waiting.

Abort lever: **holding B** during playback quits it cleanly
(`BattleMainCB2` → `CB2_QuitRecordedBattle` when `BATTLE_TYPE_RECORDED` and playback stoppable,
`D/src/battle_main.c:1871-1877, 1893-1903`).

## 2. Robust RAM wait conditions for every step (poll once per frame)

All CB2 comparisons are against `read_u32(0x030022C4)` with the Thumb bit (`|1`).
Never blind-fire inputs: fades eat them (`HandleStartMenuInput` A-branch starts a fade and the
action callback waits `!gPaletteFade.active`, `D/src/start_menu.c:592-717`; frontier pass
fades in through `InitFrontierPass` states 9-10 before its input task exists,
`D/src/frontier_pass.c:795-813`).

| step | WAIT until | THEN |
|---|---|---|
| boot→intro | callback2 == `MainCB2_Intro` (0x0816CC00) **and** `!gPaletteFade.active` | press A |
| intro→title | callback2 == `MainCB2` (title-screen local, 0x080AAB2C) **and** task `Task_TitleScreenPhase3` (0x080AAD64) active in gTasks | press Start |
| title→main menu | callback2 == `CB2_MainMenu` (0x0802F6B0) **and** task `Task_HandleMainMenuInput` (0x0803024C) active **and** `!gPaletteFade.active` | press A (CONTINUE is item 0) |
| menu→overworld | callback2 == `CB2_Overworld` (0x08085E5C) **and** callback1 == `CB1_Overworld` (0x08085E04) **and** `!gPaletteFade.active`; then wait ~30 extra frames for the map-name popup/field scripts | press Start |
| start menu open? | `gMenuCallback` (0x03005DF4) == `HandleStartMenuInput` (0x0809FAC4)\|1 within 90 frames; **else press Start again** (retry loop — field controls may be locked briefly) | proceed |
| start-menu navigate | read `sNumStartMenuActions` (0x0203760F) and `sCurrentStartMenuActions[9]` (0x02037610); find index k of value 4 = MENU_ACTION_PLAYER (`D/src/start_menu.c:53-57`). Tap Down until `sStartMenuCursorPos` (0x0203760E) == k (verify after each tap) | press A |
| pass open | callback2 == `CB2_FrontierPass` (0x080C5438) **and** task `Task_HandleFrontierPassInput` (0x080C5A48) active (guarantees fade-in finished: created only when `InitFrontierPass` returns TRUE, `D/src/frontier_pass.c:713-718`) | move cursor |
| cursor on RECORD | let `P = read_u32(0x02039CEC)` (`sPassData`); struct `FrontierPassData` (`D/src/frontier_pass.c:665-680`): `state` u16 @P+4, `cursorArea` u8 **@P+12**, `hasBattleRecord` = bit0 of byte @P+14. Sanity-check `hasBattleRecord==1` (else the record failed validation — abort run). Phase 1: hold **Left** until `cursorArea==5` (POINTS — the box directly under RECORD; a pure diagonal exits RECORD's y-band before entering its x-band, so two phases). Phase 2: hold **Up** until `cursorArea==3` (CURSOR_AREA_RECORD, enum `D/src/frontier_pass.c:64-80`). Release, wait 2 frames | press A |
| playback accepted | callback2 == `CB2_ShowFrontierPassFeature` (0x080C5934), then `CB2_RecordedBattle` (0x08185E8C) — if still `CB2_FrontierPass` after 60 frames, re-press A | wait |
| battle running | callback2 == `BattleMainCB2` (0x08038420) (reached via `CB2_InitBattle` 0x08036760 → one of the three start CB2s). **Start recording video no later than `CB2_RecordedBattle`** (screen is black; music starts there) | hands off |
| battle over | see §3 | stop capture |

Nice extra logs while battling: `gBattleOutcome` u8 @0x0202433A becomes nonzero
(1=won, 2=lost, …) when the outcome is decided — a few seconds before the exit fade;
`gBattleTypeFlags` u32 @0x02022FEC has bit24 (BATTLE_TYPE_RECORDED) set during playback.

## 3. END DETECTION — recommended poll

Exit chain for a finished replay (all cited):
`HandleEndTurn_FinishBattle` → `RecordedBattle_SetPlaybackFinished` + fast fade to black
(`D/src/battle_main.c:5099-5148`) → `FreeResetData_ReturnToOvOrDoEvolutions` (waits for the
fade, `:5155-5178`) → `ReturnFromBattleToOverworld` → `SetMainCallback2(gMain.savedCallback)`
(`:5217-5248`), where savedCallback == `CB2_RecordedBattleEnd` (set in `Task_StartAfterCountdown`,
`D/src/recorded_battle.c:513-521`). `CB2_RecordedBattleEnd` restores parties and chains to
`CB2_ReturnFromRecord` (`D/src/recorded_battle.c:499-511`) → `CB2_ReshowFrontierPass` →
`CB2_FrontierPass` (`D/src/frontier_pass.c:915-936, 890-913`).

**Recommended poll:** once callback2 has been observed == `BattleMainCB2|1`, poll every frame:

```
done = callback2 in { CB2_RecordedBattleEnd|1,   # 0x08185AB1  (first frame after exit)
                      CB2_ReturnFromRecord|1,    # 0x080C58D5
                      CB2_ReshowFrontierPass|1,  # 0x080C5869
                      CB2_FrontierPass|1 }       # 0x080C5439  (steady state)
```

Each link in that chain owns callback2 for ≥1 full frame, so a once-per-frame poll cannot miss
all of them; matching the whole set makes it order-proof. At the first hit the screen is
already fully black (the battle fade completes *inside* `BattleMainCB2` before the callback
switch), so it is the perfect video cut point — trim to first-hit + a small tail.

Do **not** use plain `callback2 != BattleMainCB2` as the done test: an evolution scene would
also leave `BattleMainCB2` temporarily (`TryEvolvePokemon`, `D/src/battle_main.c:5180-5215`).
(Practically unreachable here — frontier battles award no EXP — but the whitelist costs
nothing.) The B-hold quit path (`CB2_QuitRecordedBattle`, `D/src/battle_main.c:1893-1903`)
funnels into the same savedCallback chain, so the same poll detects a manual abort.

Belt-and-braces secondary signal (optional): `gBattleOutcome` (0x0202433A) transitions
0→nonzero near the end, and is reset to 0 by `CB2_RecordedBattleEnd` (`D/src/recorded_battle.c:501`).

## 4. Frontier Pass flag — required for the menu path

- **Yes, required for Strategy A**: without `FLAG_SYS_FRONTIER_PASS` the start-menu name entry
  opens the plain Trainer Card instead of the pass (`D/src/start_menu.c:707-713`). The record
  area inside the pass needs no flag — only a valid sector 31.
- Numeric id: `FLAG_SYS_FRONTIER_PASS = SYSTEM_FLAGS + 0x72`, `SYSTEM_FLAGS = 0x860`
  (`D/include/constants/flags.h:1348,1482`) → **flag 0x8D2 (2258)**.
- Mapping to SaveBlock1: `flags[NUM_FLAG_BYTES]` at **SaveBlock1 + 0x1270**
  (`D/include/global.h:1020`); `FlagGet` uses `flags[id/8] & (1 << (id & 7))`
  (`D/src/event_data.c:196-230`). 0x8D2/8 = 0x11A, bit 2 →
  **byte SaveBlock1+0x138A, mask 0x04**.
- Live-RAM check (after CONTINUE): `sb1 = read_u32(0x03005D8C)` (gSaveBlock1Ptr);
  flag set ⇔ `read8(sb1 + 0x138A) & 0x04`.
- In the .sav file the sibling agent must de-chunk SaveBlock1 across its rotating sectors
  (sector id 1 starts at SaveBlock1 offset 0, 3968 data bytes per sector → offset 0x138A
  falls in the **second** SaveBlock1 sector, id 2, at data offset 0x138A-0xF80 = 0x40A) and
  test bit 2 of that byte.

## 5. STRATEGY B (save-free) — honest feasibility

Goal: no user save at all. Ship a **synthetic 128 KiB flash image**: sectors 0-30 = 0xFF
(virgin flash), sector 31 = the `.rec`. Boot behavior with that image:
`LoadGameSave` → status EMPTY → `Sav2_ClearSetDefault()` gives sane default SaveBlock2/options
and the heap is initialized (`CB2_InitCopyrightScreenAfterBootup`, `D/src/intro.c:1147-1160`).
Main menu shows NEW GAME only — the pass is unreachable by menus, so we hijack:

1. Drive to the main menu exactly as in §2 (intro skip, title, wait for
   `CB2_MainMenu` + `Task_HandleMainMenuInput` active + fade idle, **no buttons held**).
2. With the emulator scripting API, perform a forced call of
   `PlayRecordedBattle(CB2_After)` (0x08185E24):
   set `r0 = CB2_InitTitleScreen|1` (0x080AA7A5 — a safe, self-reinitializing after-callback;
   `SetMainCallback2` zeroes `gMain.state` so it restarts cleanly),
   set `lr = current pc` (so the hijacked frame finishes normally after the call returns),
   set `pc = PlayRecordedBattle|1` (CPU is already in Thumb inside CB2_MainMenu).
3. `PlayRecordedBattle` only needs: a valid sector 31 (present), heap (present),
   `gSaveBlock2Ptr->frontier.lvlMode` (defaults fine — it is saved/restored around playback,
   `D/src/recorded_battle.c:502,580`), and the task system (live). Playback settings
   (text speed, battle-scene on/off) come from the **record**, not the save (§6).
4. End detection: same chain, except the terminal callback2 is `CB2_InitTitleScreen|1`
   instead of the frontier-pass pair — poll `{CB2_RecordedBattleEnd|1, CB2_InitTitleScreen|1}`.

Known risks (why this is the polish milestone, not the first):
- The leftover main-menu task keeps running under `CB2_RecordedBattle` for the 128-frame
  countdown. It is inert **iff no keys arrive** (`HandleMainMenuInput` only acts on JOY_NEW,
  `D/src/main_menu.c:884-928`) — so the script must hold zero keys during the countdown.
  `CB2_InitBattleInternal` then wipes it with `ResetTasks()` (`D/src/battle_main.c:673-681`).
- Register pokes require mGBA's scripting register API and correct Thumb-state handling at
  `pc` writes — must be bench-tested; behavior at mid-frame hijack is emulator-specific.
- Main-menu VRAM/window residue until battle init clears it (cosmetic only; the screen is
  faded/black during the countdown).
- Names/graphics for opponents 400-499 (apprentices) resolve from SaveBlock2 apprentice data
  → zeroed defaults may render blank apprentice names in intro text (cosmetic).

Verdict: **plausible and worth attempting** — a single, well-defined hijack point with the
whole state machine after it identical to Strategy A; fidelity of the battle itself is
unaffected (seed/parties/inputs all come from the record). But it depends on
emulator-side register control, so build Strategy A first (fully specified above, zero
unknowns), then bench Strategy B. A middle option ("Strategy A-lite") if template.sav ever
becomes unavailable: synthesize a minimal valid save (sectors 0-13 with correct footers,
flag 0x8D2 set, player parked in Battle Frontier) — doable with PokeDNA's save writer, but a
bigger clean-room job than the register hijack.

## 6. Notes

- **Intro/CONTINUE**: `local/template.sav` is the user's real cart backup (sector 31 erased) —
  a valid save, so the main menu is HAS_SAVED_GAME (or mystery-gift variant) and **CONTINUE
  exists and is item 0** in all variants (`D/src/main_menu.c:512-515`). Cold boot always
  starts with item 0 selected. The new-game Birch intro is never entered.
- **Record byte 1279 (struct offset) = playback pacing**: bit0 `battleScene`
  (1 = animations OFF) and bits1-3 `textSpeed` are *stored in the record* and applied at
  playback (`SetVariablesForRecordedBattle` → `sBattleScene`/`sTextSpeed`,
  `D/src/recorded_battle.c:571-572`; recorded from the recorder's options, `:388-389`).
  They dominate video length. rec2mp4 MAY offer `--fast` by patching byte 0x503 of the
  `.rec` (scene off, text fastest) **but must then recompute the u32 byte-sum checksum at
  0xF80** — default should be faithful playback (leave untouched).
- The pass caches `hasBattleRecord` once at open (`AllocateFrontierPassData`,
  `D/src/frontier_pass.c:640`) and `PlayRecordedBattle` re-validates the sector at press time
  — injection must happen before boot (it does).
- RNG during menu driving is irrelevant to fidelity: playback reseeds from the record's
  `rngSeed` (`gRecordedBattleRngSeed` 0x0203BD2C, `D/src/recorded_battle.c:566`).
- Frame counter for timeouts: `gMain.vblankCounter1` u32 @0x030022E0.
- Multi-battle records take the `CB2_HandleStartMultiPartnerBattle`/`CB2_HandleStartMultiBattle`
  init route (`D/src/battle_main.c:683-693`) — same terminal `BattleMainCB2`, same end chain;
  nothing facility-specific needed.

## 7. Address table (US Emerald, from pokeemerald.sym; ROM callbacks appear in RAM as addr|1)

### RAM

| address | symbol | use |
|---|---|---|
| 0x030022C0 | gMain | base |
| 0x030022C4 | gMain.callback2 | master state poll |
| 0x030022C8 | gMain.savedCallback | battle exit target |
| 0x030022E0 | gMain.vblankCounter1 | frame counter |
| 0x030022EE | gMain.newKeys | input debug |
| 0x02037FD4 | gPaletteFade | fade struct |
| 0x02037FDB | gPaletteFade byte 7 | bit7 = fade active |
| 0x03005DF4 | gMenuCallback | == HandleStartMenuInput\|1 when start menu open |
| 0x03005D8C | gSaveBlock1Ptr | → +0x138A bit2 = FLAG_SYS_FRONTIER_PASS (0x8D2) |
| 0x03005D90 | gSaveBlock2Ptr | defaults check (Strategy B) |
| 0x03005E00 | gTasks | 16 × 0x28; func @+0, isActive @+4 |
| 0x0203760E | sStartMenuCursorPos | start-menu cursor |
| 0x0203760F | sNumStartMenuActions | start-menu size |
| 0x02037610 | sCurrentStartMenuActions | find MENU_ACTION_PLAYER (4) |
| 0x02039CEC | sPassData (ptr) | +4 state u16, +12 cursorArea u8, +14 bit0 hasBattleRecord |
| 0x02039CF0 | sPassGfx (ptr) | +0 cursorSprite ptr (fallback) |
| 0x02022FEC | gBattleTypeFlags | bit24 = RECORDED during playback |
| 0x0202433A | gBattleOutcome | nonzero ⇒ outcome decided |
| 0x03005D04 | gBattleMainFunc | fine-grained battle phase (optional) |
| 0x0203BD2C | gRecordedBattleRngSeed | sanity: equals record's seed |
| 0x0203BD34 | sBattleRecords | 4×664 replay lanes in RAM |
| 0x03006210 | gSaveFileStatus | 1=OK, EMPTY/CORRUPT for Strategy B |
| 0x03005D80 | gRngValue | RNG (diagnostics) |

### ROM (Thumb functions; compare against value|1)

| address | symbol | role |
|---|---|---|
| 0x0816CEAC | CB2_InitCopyrightScreenAfterBootup | boot |
| 0x0816CC00 | MainCB2_Intro | intro (skippable: any key) |
| 0x0816CC54 | MainCB2_EndIntro | intro fade-out |
| 0x080AA7A4 | CB2_InitTitleScreen | title init / Strategy B after-callback |
| 0x080AAB2C | MainCB2 (title_screen.c local) | title steady state |
| 0x080AAD64 | Task_TitleScreenPhase3 | title accepts Start/A |
| 0x0802F6DC | CB2_InitMainMenu | main-menu init |
| 0x0802F6B0 | CB2_MainMenu | main-menu steady state |
| 0x0802FBA4 | Task_DisplayMainMenu | menu printing |
| 0x0803024C | Task_HandleMainMenuInput | menu accepts input |
| 0x0803027C | Task_HandleMainMenuAPressed | post-A dispatch |
| 0x08086230 | CB2_ContinueSavedGame | CONTINUE chosen |
| 0x08085FCC | CB2_LoadMap | map load |
| 0x080860C8 | CB2_ReturnToField | field return |
| 0x08085E5C | CB2_Overworld | overworld steady state |
| 0x08085E04 | CB1_Overworld | overworld callback1 |
| 0x08086194 | CB2_ReturnToFieldWithOpenMenu | pass exit target (unused by us) |
| 0x0809FA34 | Task_ShowStartMenu | start-menu task |
| 0x0809FAC4 | HandleStartMenuInput | gMenuCallback when menu ready |
| 0x080C51C4 | ShowFrontierPass | pass entry API |
| 0x080C544C | CB2_InitFrontierPass | pass init |
| 0x080C5438 | CB2_FrontierPass | pass steady state (also END terminal) |
| 0x080C5A48 | Task_HandleFrontierPassInput | pass accepts input |
| 0x080C5BD8 | Task_PassAreaZoom | map/card zoom (not on our path) |
| 0x080C5934 | CB2_ShowFrontierPassFeature | RECORD chosen |
| 0x080C5470 | CB2_HideFrontierPass | pass B-cancel |
| 0x080C5868 | CB2_ReshowFrontierPass | END chain 3 |
| 0x080C58D4 | CB2_ReturnFromRecord | END chain 2 |
| 0x08185290 | CanCopyRecordedBattleSaveData | record validity gate |
| 0x08185E24 | PlayRecordedBattle | playback entry (Strategy B call target) |
| 0x08185E8C | CB2_RecordedBattle | 128-frame pre-battle countdown |
| 0x08185B1C | Task_StartAfterCountdown | countdown task |
| 0x08185AB0 | CB2_RecordedBattleEnd | END chain 1 (earliest end signal) |
| 0x08186444 | RecordedBattle_SetPlaybackFinished | playback-finished marker fn |
| 0x08036760 | CB2_InitBattle | battle init entry |
| 0x080367D4 | CB2_InitBattleInternal | battle init (calls ResetTasks) |
| 0x08036FAC | CB2_HandleStartBattle | single/double start |
| 0x08037458 | CB2_HandleStartMultiPartnerBattle | multi (tower-link) start |
| 0x08037DF4 | CB2_HandleStartMultiBattle | multi start |
| 0x08038420 | BattleMainCB2 | battle steady state |
| 0x080384E4 | CB2_QuitRecordedBattle | B-hold abort |
| 0x0803DF70 | ReturnFromBattleToOverworld | jumps to savedCallback |
