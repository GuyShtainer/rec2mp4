# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
#
# rec2mp4.driver — headless mGBA driver: boots US Emerald with an injected
# save, drives the menus to Frontier Pass -> BATTLE RECORD, and streams the
# recorded-battle playback (video frames + audio) to caller callbacks.
#
# Emulator stack: hanzi/libmgba-py prebuilt macOS-arm64 bindings living in
# <project>/vendor/mgba (see docs/research/emulator-stack.md). The bindings
# are imported lazily inside EmulatorDriver.__init__ so that save-file-only
# operations (e.g. `rec2mp4 --info-only`) work without them installed.
#
# Menu path + RAM state machine: docs/research/replay-path.md (Strategy A).
#
# headed=True note: the libmgba-py stack has NO real window. Headed mode is
# emulated by writing a PNG preview of the current framebuffer into the
# driver's temp directory every 60 frames (requires Pillow; falls back to a
# one-time warning without it) and logging its path.

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import states as S

_VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor"

# Exact install commands (verified end-to-end on this machine — see
# docs/research/emulator-stack.md §6). Reproduced verbatim in the error
# raised when the bindings cannot be imported.
_INSTALL_HELP = """\
The mGBA Python bindings (hanzi/libmgba-py) are not installed. Install with:

  ~/miniconda3/bin/conda create -y -n rec2mp4 python=3.13
  ~/miniconda3/envs/rec2mp4/bin/python -m pip install pillow numpy
  mkdir -p {vendor}
  cd {vendor}
  curl -L -o libmgba-py.zip \\
    https://github.com/hanzi/libmgba-py/releases/download/0.2.0-2/libmgba-py_0.2.0_macos-arm64.zip
  unzip -o libmgba-py.zip
  install_name_tool -add_rpath /opt/homebrew/lib mgba/_pylib.abi3.so
  cp /opt/homebrew/lib/libmgba.0.10.5.dylib mgba/libmgba.0.10.dylib
  install_name_tool -add_rpath @loader_path mgba/_pylib.abi3.so

(brew mgba 0.10.5 must be installed: `brew install mgba`.) Then run rec2mp4
under that env's python:
  ~/miniconda3/envs/rec2mp4/bin/python -m rec2mp4 ..."""


@dataclass
class ReplayResult:
    frames: int       # video frames streamed to on_frame (replay only)
    seconds: float    # frames / exact GBA frame rate
    end_reason: str   # 'natural' | 'timeout' | 'error:<step>'


class _StepStall(Exception):
    """A menu-driving step did not reach its wait condition in time."""

    def __init__(self, step: str, detail: str = ""):
        self.step = step
        self.detail = detail
        super().__init__(f"step '{step}' stalled{': ' + detail if detail else ''}")


class _GlobalTimeout(Exception):
    """max_seconds of emulated time exceeded."""


class EmulatorDriver:
    """Boot the ROM with an injected save and play back the recorded battle.

    The save image is loaded entirely in memory (VFile.fromEmpty) — the
    caller's rom/sav files are never modified and no working .sav ever
    touches disk. A temp directory exists only in headed mode, for the
    preview PNG.
    """

    def __init__(self, rom_path: str, sav_bytes: bytes, headed: bool = False,
                 log=print):
        self._log_fn = log or (lambda *_a, **_k: None)
        self._headed = bool(headed)
        self._closed = False
        self._tempdir = None
        self._core = None
        self._frames_run = 0
        self._capturing = False
        self._captured = 0
        self._on_frame = None
        self._on_audio = None
        self._max_frames = 0
        self._preview_warned = False
        self._preview_logged = False

        rom_path = str(rom_path)
        if not os.path.isfile(rom_path):
            raise RuntimeError(f"ROM not found: {rom_path}")
        if not isinstance(sav_bytes, (bytes, bytearray)) or len(sav_bytes) < 0x20000:
            raise RuntimeError(
                "sav_bytes must be a full 128 KiB flash image "
                f"(got {len(sav_bytes) if isinstance(sav_bytes, (bytes, bytearray)) else type(sav_bytes)})")

        # The save is loaded from memory (below) — no on-disk working .sav.
        # A tempdir is needed only for the headed-mode preview PNG.
        sav_bytes = bytes(sav_bytes)
        if self._headed:
            self._tempdir = tempfile.mkdtemp(prefix="rec2mp4-")

        # --- lazy import of the emulator bindings ------------------------
        if _VENDOR_DIR.is_dir() and str(_VENDOR_DIR) not in sys.path:
            sys.path.insert(0, str(_VENDOR_DIR))
        try:
            import mgba.core    # noqa: F401
            import mgba.image   # noqa: F401
            import mgba.log     # noqa: F401
            import mgba.vfs     # noqa: F401
            import mgba.gba     # noqa: F401
            from mgba import ffi
        except Exception as exc:  # ImportError or a dlopen OSError
            self.close()
            raise RuntimeError(
                f"could not import the mgba bindings ({exc!r}).\n"
                + _INSTALL_HELP.format(vendor=_VENDOR_DIR)) from exc
        self._mgba = mgba
        self._ffi = ffi

        # MANDATORY before creating a core, else libmgba floods stderr with
        # per-instruction debug logs (huge slowdown).
        mgba.log.silence()

        core = mgba.core.load_path(rom_path)
        if core is None:
            self.close()
            raise RuntimeError(f"mGBA could not load ROM: {rom_path}")
        self._core = core

        try:
            title = core.game_title
            code = core.game_code
            crc = core.crc32
            self._log(f"ROM loaded: {title!r} {code!r} CRC32 {crc:08X}")
            # VERIFY-ON-RUN: the user's clean US Emerald is CRC32 1F1C08FB;
            # a different ROM invalidates every address in states.py.
            if crc != 0x1F1C08FB:
                self._log(f"WARNING: ROM CRC32 {crc:08X} != 1F1C08FB "
                          "(US Emerald) — states.py addresses may be wrong")
        except Exception:
            pass  # metadata is best-effort

        # Video buffer must be attached BEFORE reset().
        w, h = core.desired_video_dimensions()
        if (w, h) != (S.SCREEN_W, S.SCREEN_H):
            self.close()
            raise RuntimeError(f"unexpected video dimensions {w}x{h}")
        self._screen = mgba.image.Image(w, h)
        core.set_video_buffer(self._screen)

        # Save must be loaded BEFORE reset(). In-memory VFile — the fully
        # smoke-tested route (emulator-stack.md §1 "Save injection" + §7):
        # any flash writes stay in RAM, nothing ever touches disk. Keep a
        # reference so the wrapper is not garbage-collected under the core.
        vf = mgba.vfs.VFile.fromEmpty()
        vf.write(sav_bytes, len(sav_bytes))
        vf.seek(0, whence=0)
        core.load_save(vf)
        self._save_vf = vf

        core.reset()

        # Audio: set_rate must be called after reset(); the blip buffers must
        # then be drained EVERY frame or they saturate.
        self._audio = core.get_audio_channels()
        self._audio.set_rate(S.AUDIO_RATE)

        self._log(f"core ready (save: in-memory, {len(sav_bytes)} B, "
                  f"headed={self._headed})")

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    def run_replay(self, on_frame, on_audio, max_seconds: int = 1800) -> ReplayResult:
        """Drive menus to the recorded battle, stream ONLY replay frames/audio.

        on_frame(frame: bytes)  — one 240x160 RGBX frame (153,600 bytes)
        on_audio(pcm: bytes)    — interleaved stereo s16le at 32,768 Hz
        """
        if self._core is None:
            raise RuntimeError("driver is closed")
        self._on_frame = on_frame
        self._on_audio = on_audio
        self._max_frames = max(1, int(max_seconds * S.FRAME_RATE))
        self._capturing = False
        self._captured = 0

        try:
            self._step_boot_to_intro()
            self._step_intro_to_title()
            self._step_title_to_main_menu()
            self._step_continue_to_overworld()
            self._check_frontier_pass_flag()
            self._step_open_start_menu()
            self._step_start_menu_to_pass()
            pass_data = self._step_wait_pass_open()
            self._step_cursor_to_record(pass_data)
            self._step_select_record()
            self._step_wait_playback_start()
            self._step_wait_battle_running()
            self._step_wait_battle_end()
            end_reason = "natural"
        except _StepStall as exc:
            self._log(f"ERROR: {exc}")
            end_reason = f"error:{exc.step}"
        except _GlobalTimeout:
            self._log(f"ERROR: max_seconds={max_seconds} exceeded "
                      f"({self._frames_run} frames emulated)")
            end_reason = "timeout"
        finally:
            self._capturing = False
            self._on_frame = None
            self._on_audio = None

        seconds = self._captured / S.FRAME_RATE
        self._log(f"replay done: {self._captured} frames "
                  f"({seconds:.2f}s), end_reason={end_reason}")
        return ReplayResult(frames=self._captured, seconds=seconds,
                            end_reason=end_reason)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._core = None
        if self._tempdir:
            shutil.rmtree(self._tempdir, ignore_errors=True)
            self._tempdir = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # frame pump
    # ------------------------------------------------------------------ #

    def _advance(self, keys: int = 0):
        """Run exactly one frame with `keys` (raw bitmask) held."""
        if self._frames_run >= self._max_frames:
            raise _GlobalTimeout()
        core = self._core
        # set_keys takes bit INDICES positionally; raw= is the bitmask form.
        core.set_keys(raw=keys)
        core.run_frame()
        self._frames_run += 1
        self._pump_audio()
        if self._capturing:
            if self._on_frame is not None:
                # 240*160*4 bytes, R,G,B,X order (ffmpeg -pix_fmt rgb0).
                self._on_frame(bytes(self._ffi.buffer(self._screen.buffer)))
            self._captured += 1
        if self._headed and self._frames_run % 60 == 0:
            self._write_preview()

    def _pump_audio(self):
        """Drain the blip buffers every frame (mandatory); forward if capturing."""
        audio = self._audio
        n = audio.available
        if n <= 0:
            return
        if self._capturing and self._on_audio is not None:
            buf = self._ffi.new("short[%d]" % (2 * n))
            # Interleave: L at offset 0 stride 2, R at offset 1 stride 2.
            audio._left.read_into(buf, n, 2, 0)
            audio._right.read_into(buf, n, 2, 1)
            self._on_audio(bytes(self._ffi.buffer(buf)))
        else:
            audio.clear()

    def _press(self, keymask: int,
               hold: int = S.PRESS_HOLD_FRAMES,
               release: int = S.PRESS_RELEASE_FRAMES):
        """Press-and-release: the game latches newKeys from transitions."""
        for _ in range(hold):
            self._advance(keymask)
        for _ in range(release):
            self._advance(0)

    # ------------------------------------------------------------------ #
    # memory polling
    # ------------------------------------------------------------------ #

    def _read(self, addr: int, n: int) -> bytes:
        """Read n bytes of EWRAM (0x02...) / IWRAM (0x03...) via direct ffi copy."""
        buf = bytearray(n)
        mem = self._core._native.memory
        ffi = self._ffi
        if 0x02000000 <= addr < 0x03000000:
            ffi.memmove(buf, ffi.cast("char*", mem.wram) + (addr & 0x3FFFF), n)
        elif 0x03000000 <= addr < 0x04000000:
            ffi.memmove(buf, ffi.cast("char*", mem.iwram) + (addr & 0x7FFF), n)
        else:
            raise ValueError(f"unsupported address 0x{addr:08X}")
        return bytes(buf)

    def _u8(self, addr: int) -> int:
        return self._read(addr, 1)[0]

    def _u16(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 2), "little")

    def _u32(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 4), "little")

    def _cb2(self) -> int:
        return self._u32(S.GMAIN_CALLBACK2)

    def _fade_active(self) -> bool:
        return bool(self._u8(S.GPALETTE_FADE_ACTIVE_ADDR) & S.GPALETTE_FADE_ACTIVE_MASK)

    def _task_active(self, func_t: int) -> bool:
        """True iff some gTasks[i] has isActive != 0 and func == func_t."""
        blob = self._read(S.GTASKS, S.TASK_COUNT * S.TASK_SIZE)
        for i in range(S.TASK_COUNT):
            off = i * S.TASK_SIZE
            if blob[off + S.TASK_ISACTIVE_OFFSET] == 0:
                continue
            func = int.from_bytes(blob[off:off + 4], "little")
            if func == func_t:
                return True
        return False

    # ------------------------------------------------------------------ #
    # wait helpers
    # ------------------------------------------------------------------ #

    def _wait(self, pred, step: str, timeout: int, keys: int = 0):
        """Advance (holding `keys`) until pred() or timeout frames -> stall."""
        for _ in range(timeout):
            if pred():
                return
            self._advance(keys)
        raise _StepStall(step, f"condition not met within {timeout} frames "
                               f"(cb2=0x{self._cb2():08X}, frame {self._frames_run})")

    def _wait_bool(self, pred, timeout: int, keys: int = 0) -> bool:
        """Like _wait but returns False instead of raising."""
        for _ in range(timeout):
            if pred():
                return True
            self._advance(keys)
        return False

    def _drive(self, target, keymask: int, step: str, timeout: int,
               source=None, interval: int = 30):
        """Press `keymask` repeatedly until target() holds.

        The game can silently ignore key transitions (e.g. the intro ignores
        input for its first ~60 frames — measured on hardware-verified ROM),
        so a single press is never trusted: while target() is false, press
        again every `interval` frames, but only when `source()` (the state
        the press is meant for) still holds and no palette fade is running.
        Between presses the loop coasts frame-by-frame checking target().
        """
        frames = 0
        while frames < timeout:
            if target():
                return
            if (source is None or source()) and not self._fade_active():
                self._press(keymask)
                frames += S.PRESS_HOLD_FRAMES + S.PRESS_RELEASE_FRAMES
            for _ in range(interval):
                if target():
                    return
                self._advance()
                frames += 1
        raise _StepStall(step, f"target not reached within {timeout} frames "
                               f"(cb2=0x{self._cb2():08X}, frame {self._frames_run})")

    # ------------------------------------------------------------------ #
    # Strategy A state machine (docs/research/replay-path.md §1-2)
    # ------------------------------------------------------------------ #

    def _step_boot_to_intro(self):
        # Copyright + GameCube screens are unskippable; the save is loaded
        # there. Intro movie is skippable with any key once the fade is idle.
        self._wait(lambda: self._cb2() == S.MAINCB2_INTRO_T and not self._fade_active(),
                   "boot-to-intro", S.BOOT_TIMEOUT_FRAMES)
        self._log(f"[f{self._frames_run}] intro movie reached; skipping with A")

    def _step_intro_to_title(self):
        # The intro ignores input for its first ~60 frames, so keep pressing
        # A while callback2 stays MainCB2_Intro; coast through the EndIntro /
        # InitTitleScreen transients until the title steady state.
        self._drive(target=lambda: self._cb2() == S.TITLE_MAINCB2_T
                    and self._task_active(S.TASK_TITLE_SCREEN_PHASE3_T),
                    keymask=S.KEYMASK_A, step="title",
                    timeout=2 * S.STEP_TIMEOUT_FRAMES,
                    source=lambda: self._cb2() == S.MAINCB2_INTRO_T)
        self._log(f"[f{self._frames_run}] title screen; pressing Start")

    def _step_title_to_main_menu(self):
        # CONTINUE is preselected at item 0 on a cold boot with a valid save.
        self._drive(target=lambda: self._cb2() == S.CB2_MAIN_MENU_T
                    and self._task_active(S.TASK_HANDLE_MAIN_MENU_INPUT_T)
                    and not self._fade_active(),
                    keymask=S.KEYMASK_START, step="main-menu",
                    timeout=S.STEP_TIMEOUT_FRAMES,
                    source=lambda: self._cb2() == S.TITLE_MAINCB2_T
                    and self._task_active(S.TASK_TITLE_SCREEN_PHASE3_T))
        self._log(f"[f{self._frames_run}] main menu; selecting CONTINUE")

    def _step_continue_to_overworld(self):
        self._drive(target=lambda: self._cb2() == S.CB2_OVERWORLD_T
                    and self._u32(S.GMAIN_CALLBACK1) == S.CB1_OVERWORLD_T
                    and not self._fade_active(),
                    keymask=S.KEYMASK_A, step="overworld",
                    timeout=2 * S.STEP_TIMEOUT_FRAMES,
                    source=lambda: self._cb2() == S.CB2_MAIN_MENU_T
                    and self._task_active(S.TASK_HANDLE_MAIN_MENU_INPUT_T)
                    and not self._fade_active())
        # Let the map-name popup / field scripts settle before pressing Start.
        for _ in range(S.OVERWORLD_SETTLE_FRAMES):
            self._advance()
        self._log(f"[f{self._frames_run}] overworld steady")

    def _check_frontier_pass_flag(self):
        # FLAG_SYS_FRONTIER_PASS (0x8D2) gates the start-menu name entry
        # opening the pass. SaveBlock1+0x138A bit2 via gSaveBlock1Ptr.
        sb1 = self._u32(S.GSAVEBLOCK1_PTR)
        if not (0x02000000 <= sb1 < 0x02040000):
            raise _StepStall("frontier-pass-flag",
                             f"gSaveBlock1Ptr looks invalid: 0x{sb1:08X}")
        flag_byte = self._u8(sb1 + S.SB1_FRONTIER_PASS_FLAG_OFFSET)
        if not (flag_byte & S.SB1_FRONTIER_PASS_FLAG_MASK):
            raise _StepStall("frontier-pass-flag",
                             "FLAG_SYS_FRONTIER_PASS is NOT set in this save — "
                             "the Frontier Pass is unreachable via menus; use a "
                             "save whose trainer has obtained the pass")
        self._log(f"[f{self._frames_run}] FLAG_SYS_FRONTIER_PASS confirmed set")

    def _step_open_start_menu(self):
        # Field controls may briefly eat Start; retry until gMenuCallback
        # flips to HandleStartMenuInput|1.
        for attempt in range(1, 6):
            self._press(S.KEYMASK_START)
            if self._wait_bool(
                    lambda: self._u32(S.GMENU_CALLBACK) == S.HANDLE_START_MENU_INPUT_T,
                    S.START_MENU_RETRY_FRAMES):
                self._log(f"[f{self._frames_run}] start menu open "
                          f"(attempt {attempt})")
                return
            self._log(f"[f{self._frames_run}] start menu not open yet; "
                      f"re-pressing Start (attempt {attempt})")
        raise _StepStall("start-menu-open", "Start press never opened the menu")

    def _step_start_menu_to_pass(self):
        # Find the player-name entry (MENU_ACTION_PLAYER = 4) at runtime —
        # index 4 on a full save, but derive it, never assume.
        num = self._u8(S.SNUM_START_MENU_ACTIONS)
        if not (1 <= num <= S.START_MENU_MAX_ACTIONS):
            raise _StepStall("start-menu-navigate",
                             f"sNumStartMenuActions = {num} out of range")
        actions = self._read(S.SCURRENT_START_MENU_ACTIONS, num)
        try:
            k = actions.index(S.MENU_ACTION_PLAYER)
        except ValueError:
            raise _StepStall("start-menu-navigate",
                             f"MENU_ACTION_PLAYER not in menu {actions.hex()}")
        self._log(f"[f{self._frames_run}] start menu: {num} entries, "
                  f"player-name entry at index {k}")
        # Tap Down and verify after each tap (never blind-fire).
        for _ in range(num * 4):
            if self._u8(S.SSTART_MENU_CURSOR_POS) == k:
                break
            self._press(S.KEYMASK_DOWN)
        else:
            raise _StepStall("start-menu-navigate",
                             f"cursor never reached index {k} "
                             f"(at {self._u8(S.SSTART_MENU_CURSOR_POS)})")
        # Only press A while the menu still accepts input and no fade runs.
        self._wait(lambda: self._u32(S.GMENU_CALLBACK) == S.HANDLE_START_MENU_INPUT_T
                   and not self._fade_active(),
                   "start-menu-navigate", S.STEP_TIMEOUT_FRAMES)
        self._log(f"[f{self._frames_run}] selecting player-name entry "
                  "(opens Frontier Pass)")
        self._press(S.KEYMASK_A)

    def _step_wait_pass_open(self) -> int:
        # Task_HandleFrontierPassInput exists only after the fade-in finished.
        self._wait(lambda: self._cb2() == S.CB2_FRONTIER_PASS_T
                   and self._task_active(S.TASK_HANDLE_FRONTIER_PASS_INPUT_T),
                   "frontier-pass-open", S.STEP_TIMEOUT_FRAMES)
        pass_data = self._u32(S.SPASS_DATA_PTR)
        if not (0x02000000 <= pass_data < 0x02040000):
            raise _StepStall("frontier-pass-open",
                             f"sPassData looks invalid: 0x{pass_data:08X}")
        has_record = self._u8(pass_data + S.PASS_FLAGS_OFFSET) & S.PASS_HAS_BATTLE_RECORD_MASK
        if not has_record:
            raise _StepStall("record-missing",
                             "the game rejected sector 31 (hasBattleRecord=0) — "
                             "the injected .rec failed CanCopyRecordedBattleSaveData")
        self._log(f"[f{self._frames_run}] Frontier Pass open, battle record "
                  f"accepted (sPassData=0x{pass_data:08X})")
        return pass_data

    def _step_cursor_to_record(self, pass_data: int):
        # Free-moving hand cursor spawns at (176,104). Two phases (a pure
        # diagonal exits RECORD's y-band before entering its x-band):
        # hold LEFT until cursorArea == POINTS(5), then UP until RECORD(3).
        area_addr = pass_data + S.PASS_CURSOR_AREA_OFFSET
        self._wait(lambda: self._u8(area_addr) == S.CURSOR_AREA_POINTS,
                   "pass-cursor", S.PASS_CURSOR_PHASE_TIMEOUT_FRAMES,
                   keys=S.KEYMASK_LEFT)
        self._wait(lambda: self._u8(area_addr) == S.CURSOR_AREA_RECORD,
                   "pass-cursor", S.PASS_CURSOR_PHASE_TIMEOUT_FRAMES,
                   keys=S.KEYMASK_UP)
        # A is only processed on a frame where the cursor did NOT move:
        # release the D-pad and coast 2 frames before pressing A.
        self._advance(0)
        self._advance(0)
        self._log(f"[f{self._frames_run}] cursor on BATTLE RECORD")

    def _step_select_record(self):
        # No confirmation dialog: A -> CB2_ShowFrontierPassFeature -> fade ->
        # PlayRecordedBattle. Re-press A if callback2 stays on the pass.
        for attempt in range(1, 6):
            self._press(S.KEYMASK_A)
            if self._wait_bool(lambda: self._cb2() != S.CB2_FRONTIER_PASS_T,
                               S.PASS_ACCEPT_RETRY_FRAMES):
                self._log(f"[f{self._frames_run}] BATTLE RECORD accepted "
                          f"(attempt {attempt})")
                return
            self._log(f"[f{self._frames_run}] still on the pass; re-pressing A "
                      f"(attempt {attempt})")
        raise _StepStall("record-select", "A never left CB2_FrontierPass")

    def _step_wait_playback_start(self):
        # CB2_RecordedBattle = 128-frame black countdown; capture starts here
        # (music starts under it — replay-path §2 "battle running" row).
        self._wait(lambda: self._cb2() == S.CB2_RECORDED_BATTLE_T,
                   "playback-start", S.STEP_TIMEOUT_FRAMES)
        self._capturing = True
        self._log(f"[f{self._frames_run}] playback countdown started — "
                  "capture ON")

    def _step_wait_battle_running(self):
        # 128-frame countdown + battle init; generous 30 s cap.
        self._wait(lambda: self._cb2() == S.BATTLE_MAIN_CB2_T,
                   "battle-start", S.BOOT_TIMEOUT_FRAMES)
        flags = self._u32(S.GBATTLE_TYPE_FLAGS)
        recorded = bool(flags & S.BATTLE_TYPE_RECORDED_MASK)
        self._log(f"[f{self._frames_run}] battle running "
                  f"(gBattleTypeFlags=0x{flags:08X}, recorded={recorded})")
        if not recorded:
            # VERIFY-ON-RUN: should be impossible on this path; log-only.
            self._log("WARNING: BATTLE_TYPE_RECORDED bit not set")

    def _step_wait_battle_end(self):
        # Whitelist poll (replay-path §3); bounded only by max_seconds.
        outcome_logged = False
        last_progress = self._frames_run
        while True:
            cb2 = self._cb2()
            if cb2 in S.REPLAY_END_CALLBACKS_T:
                break
            if not outcome_logged:
                outcome = self._u8(S.GBATTLE_OUTCOME)
                if outcome:
                    self._log(f"[f{self._frames_run}] battle outcome decided: "
                              f"{outcome} (1=won 2=lost)")
                    outcome_logged = True
            if self._frames_run - last_progress >= 600:
                last_progress = self._frames_run
                self._log(f"[f{self._frames_run}] battle in progress "
                          f"({self._captured} frames captured)")
            self._advance()
        self._log(f"[f{self._frames_run}] replay finished "
                  f"(cb2=0x{self._cb2():08X}); appending "
                  f"{S.END_TAIL_FRAMES}-frame tail")
        # Screen is already fully black at the first whitelist hit; keep a
        # ~2 s tail so the audio fade breathes, then stop capture.
        for _ in range(S.END_TAIL_FRAMES):
            self._advance()
        self._capturing = False

    # ------------------------------------------------------------------ #
    # headed-mode preview
    # ------------------------------------------------------------------ #

    def _write_preview(self):
        """No real window exists in this stack: dump a PNG every 60 frames."""
        path = os.path.join(self._tempdir, "preview.png")
        try:
            # to_pil() only exists when Pillow is importable; save_png() is
            # broken vs libmgba 0.10.5 and must never be used.
            img = self._screen.to_pil()
        except Exception:
            if not self._preview_warned:
                self._preview_warned = True
                self._log("headed: Pillow unavailable — no PNG previews "
                          "(pip install pillow)")
            return
        try:
            img.convert("RGB").save(path)
        except Exception as exc:
            if not self._preview_warned:
                self._preview_warned = True
                self._log(f"headed: preview write failed: {exc!r}")
            return
        if not self._preview_logged:
            self._preview_logged = True
            self._log(f"headed: preview PNG refreshed every 60 frames at {path}")

    # ------------------------------------------------------------------ #

    def _log(self, msg: str):
        self._log_fn(f"[driver] {msg}")
