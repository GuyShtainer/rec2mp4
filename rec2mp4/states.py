# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
#
# rec2mp4.states — named US-Emerald (BPEE rev0, CRC32 1F1C08FB) address/value
# constants used by rec2mp4.driver.
#
# Sources (research, NOT copied code):
#   docs/research/replay-path.md   — addresses derived from pret/pokeemerald's
#                                    published symbol file (pokeemerald.sym,
#                                    "symbols" branch) + struct layouts read
#                                    from the decomp headers. Each constant
#                                    cites the decomp symbol it came from.
#   docs/research/emulator-stack.md — key indices / timing facts (libmgba-py).
#
# Convention: all game code is Thumb, so a ROM function at 0x08XXXXXX shows up
# in gMain.callback2 / gTasks[].func / gMenuCallback as (0x08XXXXXX | 1).
# Constants ending in _T already include that Thumb bit — compare RAM u32
# reads against the _T values directly.

# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------

# Exact GBA frame rate: 16,777,216 Hz master clock / 280,896 cycles per frame.
FRAME_RATE = 16777216 / 280896  # = 59.727500569606... fps

# Audio output sample rate fed to StereoBuffer.set_rate() (verified value from
# the emulator-stack research: ~548 samples/frame, A/V lock confirmed).
AUDIO_RATE = 32768

# Per-step stall timeout (~15 s of emulated frames).
STEP_TIMEOUT_FRAMES = int(15 * FRAME_RATE) + 1  # 897

# Boot -> intro can sit on the unskippable copyright/GameCube screens for a
# while; give it a longer leash (~30 s).
BOOT_TIMEOUT_FRAMES = int(30 * FRAME_RATE) + 1

# After CB2_Overworld goes steady, wait this many extra frames for the
# map-name popup / field scripts before pressing Start (replay-path §2).
OVERWORLD_SETTLE_FRAMES = 30

# Start pressed on the overworld: gMenuCallback must flip to
# HandleStartMenuInput|1 within this many frames, else re-press (replay-path §2).
START_MENU_RETRY_FRAMES = 90

# A pressed on BATTLE RECORD: callback2 must leave CB2_FrontierPass within
# this many frames, else re-press A (replay-path §2 "playback accepted").
PASS_ACCEPT_RETRY_FRAMES = 60

# Frontier-pass hand cursor moves 2 px/frame while a direction is held
# (Task_HandleFrontierPassInput); each hold phase crosses < 100 px, so 300
# frames is a generous per-phase cap.
PASS_CURSOR_PHASE_TIMEOUT_FRAMES = 300

# Video tail appended after the end-of-replay callback2 is observed (~2 s).
END_TAIL_FRAMES = 120

# Safety trim: once the battle outcome is decided (gBattleOutcome != 0), the
# game normally reaches an end-of-replay callback within ~1 s. An opponent-POV
# ("fake link") replay of a vs-AI record desyncs and can instead sit forever in
# a garbled post-battle state (the "??? ???" textbox), which would otherwise run
# the whole max_seconds (~30 min) and produce an unwatchable file. If the end
# callbacks have not fired this many frames after the outcome was decided, stop
# capture and end cleanly. ~20 s is far longer than any legitimate faint/win
# sequence, so normal replays are never trimmed early.
OUTCOME_GRACE_FRAMES = int(20 * FRAME_RATE)  # ~1194 frames

# --------------------------------------------------------------------------
# GBA key bitmasks (KEYINPUT layout; libmgba KEY_* constants are these bit
# INDICES — the driver always passes masks via core.set_keys(raw=mask)).
# --------------------------------------------------------------------------

KEYMASK_A      = 1 << 0
KEYMASK_B      = 1 << 1
KEYMASK_SELECT = 1 << 2
KEYMASK_START  = 1 << 3
KEYMASK_RIGHT  = 1 << 4
KEYMASK_LEFT   = 1 << 5
KEYMASK_UP     = 1 << 6
KEYMASK_DOWN   = 1 << 7
KEYMASK_R      = 1 << 8
KEYMASK_L      = 1 << 9

# The game latches gMain.newKeys from key *transitions* (ReadKeys runs once a
# frame), so a "press" = hold >= 2 frames then release >= 2 frames.
PRESS_HOLD_FRAMES = 2
PRESS_RELEASE_FRAMES = 2

# --------------------------------------------------------------------------
# Video geometry (libmgba framebuffer)
# --------------------------------------------------------------------------

SCREEN_W = 240
SCREEN_H = 160
FRAME_BYTES = SCREEN_W * SCREEN_H * 4  # sizeof(color_t) == 4, byte order R,G,B,X

# --------------------------------------------------------------------------
# RAM addresses (pokeemerald.sym, US Emerald)
# --------------------------------------------------------------------------

GMAIN = 0x030022C0                    # gMain (struct Main, include/main.h)
GMAIN_CALLBACK1 = 0x030022C0          # gMain.callback1 (+0x00) — CB1_Overworld when in field
GMAIN_CALLBACK2 = 0x030022C4          # gMain.callback2 (+0x04) — the master state poll
GMAIN_SAVED_CALLBACK = 0x030022C8     # gMain.savedCallback (+0x08) — battle exit target
GMAIN_VBLANK_COUNTER1 = 0x030022E0    # gMain.vblankCounter1 (+0x20) — free-running frame counter
GMAIN_HELD_KEYS = 0x030022EC          # gMain.heldKeys (+0x2C)
GMAIN_NEW_KEYS = 0x030022EE           # gMain.newKeys (+0x2E) — input debug
GMAIN_STATE = 0x030026F8              # gMain.state (+0x438) — zeroed by SetMainCallback2
GMAIN_IN_BATTLE = 0x030026F9          # gMain (+0x439) bit1 = inBattle

GPALETTE_FADE = 0x02037FD4            # gPaletteFade (struct PaletteFadeControl, include/palette.h)
GPALETTE_FADE_ACTIVE_ADDR = 0x02037FDB  # gPaletteFade byte +7; bit7 == 'active' bitfield
GPALETTE_FADE_ACTIVE_MASK = 0x80        # fade in progress — never press buttons while set

GMENU_CALLBACK = 0x03005DF4           # gMenuCallback (src/start_menu.c) — start-menu dispatch ptr

GSAVEBLOCK1_PTR = 0x03005D8C          # gSaveBlock1Ptr — deref for live flag checks
GSAVEBLOCK2_PTR = 0x03005D90          # gSaveBlock2Ptr (Strategy B diagnostics)
# FLAG_SYS_FRONTIER_PASS = 0x8D2 lives at flags[0x8D2/8] with SaveBlock1's
# flags[] at +0x1270 (include/global.h) -> byte SaveBlock1+0x138A, bit 2.
SB1_FRONTIER_PASS_FLAG_OFFSET = 0x138A
SB1_FRONTIER_PASS_FLAG_MASK = 0x04

GTASKS = 0x03005E00                   # gTasks[16] (struct Task, include/task.h)
TASK_COUNT = 16
TASK_SIZE = 0x28                      # sizeof(struct Task)
TASK_FUNC_OFFSET = 0x00               # Task.func (u32, compare against addr|1)
TASK_ISACTIVE_OFFSET = 0x04           # Task.isActive (u8)

SSTART_MENU_CURSOR_POS = 0x0203760E   # sStartMenuCursorPos (src/start_menu.c)
SNUM_START_MENU_ACTIONS = 0x0203760F  # sNumStartMenuActions (src/start_menu.c)
SCURRENT_START_MENU_ACTIONS = 0x02037610  # sCurrentStartMenuActions[9] (src/start_menu.c)
START_MENU_MAX_ACTIONS = 9
MENU_ACTION_PLAYER = 4                # start-menu action id of the player-name entry
                                      # (MENU_ACTION_PLAYER, src/start_menu.c) — the
                                      # only caller of ShowFrontierPass

SPASS_DATA_PTR = 0x02039CEC           # sPassData (src/frontier_pass.c) — ptr to FrontierPassData
PASS_STATE_OFFSET = 4                 # FrontierPassData.state (u16)
# FrontierPassData layout (frontier_pass.c:107-120): callback u32 @0,
# state u16 @4, battlePoints u16 @6, cursorX s16 @8, cursorY s16 @10,
# cursorArea u8 @12, previousCursorArea u8 @13, bitfield byte @14.
PASS_CURSOR_X_OFFSET = 8              # FrontierPassData.cursorX (s16) — see caveat below
PASS_CURSOR_Y_OFFSET = 10            # FrontierPassData.cursorY (s16)
PASS_CURSOR_AREA_OFFSET = 12          # FrontierPassData.cursorArea (u8) — updated LIVE
PASS_FLAGS_OFFSET = 14                # FrontierPassData byte holding hasBattleRecord bit0
PASS_HAS_BATTLE_RECORD_MASK = 0x01    # CanCopyRecordedBattleSaveData() result, cached at open
CURSOR_AREA_RECORD = 3                # CURSOR_AREA_RECORD (enum, src/frontier_pass.c)
CURSOR_AREA_POINTS = 5                # CURSOR_AREA_POINTS — box directly below RECORD

# --- Live Frontier-Pass hand-cursor position (for closed-loop steering) ---
# CAVEAT: sPassData->cursorX/cursorY (offsets 8/10 above) hold ONLY the
# initial value and the A-press snapshot — they are NOT updated while the
# hand moves. Task_HandleFrontierPassInput (frontier_pass.c:991-1019) moves
# the CURSOR SPRITE by 2 px/frame (sPassGfx->cursorSprite->x/y) and syncs it
# back into sPassData only inside TryCallPassAreaFunction (:982-983, on A).
# So live steering must read the sprite coords via sPassGfx.
SPASS_GFX_PTR = 0x02039CF0            # sPassGfx (frontier_pass.c) -> FrontierPassGfx
PASS_GFX_CURSOR_SPRITE_OFFSET = 0     # FrontierPassGfx.cursorSprite (first member)
SPRITE_X_OFFSET = 0x20                # struct Sprite.x (s16, include/sprite.h:204)
SPRITE_Y_OFFSET = 0x22                # struct Sprite.y (s16)

# RECORD hitbox in SPRITE coords. GetCursorAreaFromCoords (:869) tests
# (spriteX-5, spriteY+5) against sPassAreasLayout[RECORD-1] =
# {yStart 80, yEnd 102, xStart 20, xEnd 108} (:350). The sprite is therefore
# over RECORD when spriteX-5 in [20,108] and spriteY+5 in [80,102]:
PASS_RECORD_SPRITE_X_LO = 25          # 20 + 5
PASS_RECORD_SPRITE_X_HI = 113         # 108 + 5
PASS_RECORD_SPRITE_Y_LO = 75          # 80 - 5
PASS_RECORD_SPRITE_Y_HI = 97          # 102 - 5
# Aim for the band centre. Even targets stay reachable from the even start
# coords (176,104)/(176,48) in 2 px steps; the loop stops the instant
# cursorArea == RECORD, so exact centring is never actually required.
PASS_RECORD_SPRITE_X_AIM = 68
PASS_RECORD_SPRITE_Y_AIM = 86
PASS_CURSOR_DEADZONE = 3              # px; |coord - aim| <= this -> stop that axis
PASS_CURSOR_STEP = 2                  # px/frame the sprite moves while held (:993)
PASS_CURSOR_STEER_TIMEOUT_FRAMES = 600  # generous cap for the full route

GBATTLE_TYPE_FLAGS = 0x02022FEC       # gBattleTypeFlags (u32)
BATTLE_TYPE_RECORDED_MASK = 1 << 24   # BATTLE_TYPE_RECORDED — set during playback (sanity)

GBATTLE_OUTCOME = 0x0202433A          # gBattleOutcome (u8) — nonzero once outcome decided
GBATTLE_MAIN_FUNC = 0x03005D04        # gBattleMainFunc — fine-grained battle phase (optional)
GRECORDED_BATTLE_RNG_SEED = 0x0203BD2C  # gRecordedBattleRngSeed — equals the record's seed
GSAVE_FILE_STATUS = 0x03006210        # gSaveFileStatus (1 = OK)

# --------------------------------------------------------------------------
# ROM callbacks / task functions (pokeemerald.sym; _T = link address | 1,
# the Thumb-bit form that actually appears in RAM function pointers).
# --------------------------------------------------------------------------

CB2_INIT_COPYRIGHT_T = 0x0816CEAC | 1        # CB2_InitCopyrightScreenAfterBootup (src/intro.c)
MAINCB2_INTRO_T = 0x0816CC00 | 1             # MainCB2_Intro — intro movie, any-key skippable
MAINCB2_END_INTRO_T = 0x0816CC54 | 1         # MainCB2_EndIntro — intro fade-out

CB2_INIT_TITLE_SCREEN_T = 0x080AA7A4 | 1     # CB2_InitTitleScreen (src/title_screen.c)
TITLE_MAINCB2_T = 0x080AAB2C | 1             # MainCB2 (title_screen.c local) — title steady state
TASK_TITLE_SCREEN_PHASE3_T = 0x080AAD64 | 1  # Task_TitleScreenPhase3 — title accepts Start/A

CB2_INIT_MAIN_MENU_T = 0x0802F6DC | 1        # CB2_InitMainMenu (src/main_menu.c)
CB2_MAIN_MENU_T = 0x0802F6B0 | 1             # CB2_MainMenu — main-menu steady state
TASK_HANDLE_MAIN_MENU_INPUT_T = 0x0803024C | 1  # Task_HandleMainMenuInput — menu accepts input
CB2_CONTINUE_SAVED_GAME_T = 0x08086230 | 1   # CB2_ContinueSavedGame — CONTINUE chosen (transient)

CB2_LOAD_MAP_T = 0x08085FCC | 1              # CB2_LoadMap (src/overworld.c, transient)
CB2_RETURN_TO_FIELD_T = 0x080860C8 | 1       # CB2_ReturnToField (transient)
CB2_OVERWORLD_T = 0x08085E5C | 1             # CB2_Overworld — overworld steady state
CB1_OVERWORLD_T = 0x08085E04 | 1             # CB1_Overworld — overworld callback1

HANDLE_START_MENU_INPUT_T = 0x0809FAC4 | 1   # HandleStartMenuInput (src/start_menu.c) —
                                             # gMenuCallback when the start menu takes input

CB2_FRONTIER_PASS_T = 0x080C5438 | 1         # CB2_FrontierPass — pass steady state (also END terminal)
TASK_HANDLE_FRONTIER_PASS_INPUT_T = 0x080C5A48 | 1  # Task_HandleFrontierPassInput — pass takes input
CB2_SHOW_FRONTIER_PASS_FEATURE_T = 0x080C5934 | 1   # CB2_ShowFrontierPassFeature — RECORD chosen
CB2_RESHOW_FRONTIER_PASS_T = 0x080C5868 | 1  # CB2_ReshowFrontierPass — END chain link 3
CB2_RETURN_FROM_RECORD_T = 0x080C58D4 | 1    # CB2_ReturnFromRecord — END chain link 2

PLAY_RECORDED_BATTLE_T = 0x08185E24 | 1      # PlayRecordedBattle (src/recorded_battle.c)
CB2_RECORDED_BATTLE_T = 0x08185E8C | 1       # CB2_RecordedBattle — 128-frame pre-battle countdown
TASK_START_AFTER_COUNTDOWN_T = 0x08185B1C | 1  # Task_StartAfterCountdown
CB2_RECORDED_BATTLE_END_T = 0x08185AB0 | 1   # CB2_RecordedBattleEnd — earliest end-of-replay CB2

CB2_INIT_BATTLE_T = 0x08036760 | 1           # CB2_InitBattle (src/battle_main.c)
CB2_HANDLE_START_BATTLE_T = 0x08036FAC | 1   # CB2_HandleStartBattle (single/double)
CB2_HANDLE_START_MULTI_PARTNER_T = 0x08037458 | 1  # CB2_HandleStartMultiPartnerBattle
CB2_HANDLE_START_MULTI_T = 0x08037DF4 | 1    # CB2_HandleStartMultiBattle
BATTLE_MAIN_CB2_T = 0x08038420 | 1           # BattleMainCB2 — battle steady state for the replay
CB2_QUIT_RECORDED_BATTLE_T = 0x080384E4 | 1  # CB2_QuitRecordedBattle — B-hold abort path

# End-of-replay whitelist (replay-path §3): once BattleMainCB2 has been
# observed, the replay is over at the first frame callback2 equals ANY of
# these. Each link holds callback2 for >= 1 frame, so a once-per-frame poll
# cannot miss the chain; matching the whole set is order-proof. The screen is
# already fully black at the first hit. Do NOT use "!= BattleMainCB2" alone
# (evolution scenes would false-trigger).
REPLAY_END_CALLBACKS_T = frozenset({
    CB2_RECORDED_BATTLE_END_T,   # 0x08185AB1 — first frame after battle exit
    CB2_RETURN_FROM_RECORD_T,    # 0x080C58D5
    CB2_RESHOW_FRONTIER_PASS_T,  # 0x080C5869
    CB2_FRONTIER_PASS_T,         # 0x080C5439 — steady state
})
