# Emulator stack research — running Emerald headless from Python on this Mac

**Date:** 2026-07-27 · **Researcher:** emulator-stack agent
**Machine:** macOS arm64 (Darwin 24), Homebrew, ffmpeg 8.1.2 (`/opt/homebrew/bin/ffmpeg`),
mGBA 0.10.5_2 via brew (Qt app + `libmgba.0.10.dylib`), Python 3.14.0 (python.org) +
miniconda3 (3.13.4 base, can create any version).

**Verdict up front: `hanzi/libmgba-py` (prebuilt macOS-arm64 zip, release 0.2.0-2) is the
winner and has been FULLY VERIFIED ON THIS MACHINE** — headless ROM boot of the user's
Emerald (`POKEMON EMER / AGB-BPEE`, CRC32 `1F1C08FB` confirmed by the core itself),
deterministic frame stepping at ~4,280 fps unthrottled, raw 240x160 RGBX framebuffer
piped to ffmpeg → valid MP4 (intro renders pixel-perfect), stereo 32,768 Hz s16
audio captured (music content confirmed at −26.5 dB mean / −10.9 dB max), EWRAM/IWRAM
reads, key injection (Start press advanced the intro), and save-state round-trip
(397,312-byte states). Audio capture is **feasible and proven** — this was the main
open question and it is closed.

---

## 1. Candidate A (WINNER): hanzi/libmgba-py — prebuilt mGBA Python bindings

- Repo: https://github.com/hanzi/libmgba-py — a fork/packaging of the official mGBA
  `src/platform/python` bindings, built so you don't have to fight the notoriously
  fragile official build. MPL-2.0. Explicitly the engine under 40Cakes/pokebot-gen3
  ("runs libmgba + mGBA Python bindings under the hood"):
  https://github.com/40Cakes/pokebot-gen3
- **Release 0.2.0 (tag `0.2.0-2`, 2023-09-27) ships a prebuilt macOS arm64 asset**:
  `libmgba-py_0.2.0_macos-arm64.zip`
  (https://github.com/hanzi/libmgba-py/releases/download/0.2.0-2/libmgba-py_0.2.0_macos-arm64.zip).
  Confirmed via the GitHub releases API; also x86_64 mac, win64, ubuntu zips.
- **Not on PyPI, not a wheel.** The zip contains a plain `mgba/` package directory
  (17 `.py` files + `_pylib.abi3.so`, Mach-O 64-bit arm64). You put its parent on
  `sys.path` (pokebot extracts it next to `pokebot.py`). There is **no pip install**;
  the "install" is unzip + one `install_name_tool` fix (below).
- **Python versions:** the extension is a **stable-ABI (`abi3`) cffi module**, so one
  binary covers many Pythons. pokebot-gen3 officially supports **3.11 / 3.12 / 3.13
  (recommends 3.13)** (`requirements.py` in their repo:
  https://github.com/40Cakes/pokebot-gen3/blob/main/requirements.py). **Verified here:
  imports and runs frames under both miniconda 3.13.4 and python.org 3.14.0.**
  Use 3.13 to stay on the supported path.
- **Runtime dependency:** `_pylib.abi3.so` links `@rpath/libmgba.0.10.dylib` and the
  zip does NOT bundle it. Brew's mGBA 0.10.5_2 provides
  `/opt/homebrew/lib/libmgba.0.10.dylib` — **verified compatible** (the 0.10 soname is
  ABI-stable across the 0.10.x series). Two ways to satisfy it:
  1. `DYLD_LIBRARY_PATH=/opt/homebrew/lib python …` (works, verified), or
  2. **one-time rpath patch (preferred, verified):**
     `install_name_tool -add_rpath /opt/homebrew/lib mgba/_pylib.abi3.so`
     → imports cleanly with **no** environment variable.
- **Verified quirks (hit them all during the smoke test):**
  - `mgba.log.silence()` is mandatory — otherwise libmgba floods stderr with
    per-instruction GBA debug logs.
  - `Image.to_pil()` exists only if Pillow is importable (it's conditionally defined).
    The raw buffer path needs no Pillow at all.
  - `Image.save_png()` is **broken** against brew's 0.10.5 dylib
    (`PNGWriteHeader expected 3 arguments, got 4` — signature drift between the
    Jan-2024 build and 0.10.5). Don't use it; use the raw buffer or Pillow.
  - `core.set_keys(k)` takes **key indices** (bit positions, `1 << k`); to pass a
    bitmask use `core.set_keys(raw=mask)`. `mgba.gba.GBA.KEY_*` constants are indices
    (A=0, B=1, Select=2, Start=3, Right=4, Left=5, Up=6, Down=7, R=8, L=9), matching
    the GBA KEYINPUT layout and pokebot's `input_map` bitfield.

### Measured results (this machine, this ROM)

| Check | Result |
|---|---|
| `core.game_title` / `game_code` / `crc32` | `POKEMON EMER` / `AGB-BPEE` / `1F1C08FB` ✔ |
| `desired_video_dimensions()` | (240, 160); `frequency` = 16,777,216 |
| 600 frames unthrottled | 0.14 s → **4,281 fps** (a 5-min battle ≈ 18k frames ≈ 5 s emulation) |
| Framebuffer | `sizeof(color_t)=4`, bytes are **R,G,B,X** → ffmpeg `-pix_fmt rgb0`, 153,600 B/frame |
| Audio @ `set_rate(32768)` | 547.8 samples/frame avg (theoretical 32768 / 59.7275 = 548.6), interleaved s16 stereo |
| ffmpeg pipe test | 900 frames + audio → 15.07 s video, 15.05 s audio, in sync; frame image = correct Emerald intro jungle scene |
| Memory | EWRAM/IWRAM reads via `ffi.memmove` from `core._native.memory.wram/iwram` ✔ |
| Save state | `core.save_state()` → 397,312 bytes; `load_state(VFile)` restores ✔ |
| Save injection | `.sav` bytes loaded from memory via `VFile.fromEmpty()` + `core.load_save(vf)` (no on-disk writes to the user's save) ✔ |

GBA exact frame rate: 16,777,216 Hz / 280,896 cycles-per-frame = **59.727500569606 fps**
(pokebot uses the same constant). Mux with `-r 59.7275005696` video + `-ar 32768` audio
and A/V stay locked.

## 2. Candidate B: official mGBA python bindings (`cmake -DBUILD_PYTHON=ON`)

Same code that hanzi packages (mgba source `src/platform/python`), so the API is
identical — but you must build it: cmake + cffi codegen against mGBA headers. The
build has been a recurring source of breakage for years and is still reported broken
or fiddly in 2024–2025 (cffi parse errors on computed constants, pycparser failures,
`ModuleNotFoundError: cffi` mid-build):
- https://github.com/mgba-emu/mgba/issues/2049 (cffi FFIError on `STACK_TRACE_BREAK_ON_BOTH`)
- https://github.com/mgba-emu/mgba/issues/2057 (Linux build failure)
- https://github.com/mgba-emu/mgba/issues/997 ("how do I even install these")
- https://github.com/mgba-emu/mgba/discussions/3477 (Windows, Python 3.12 CDefError)

No wheels anywhere; 3.13/3.14 compatibility unproven. hanzi's fork also carries small
API niceties pokebot relies on (`save_state() -> bytes`, RTC config). **Skip — only
fall back to this (or hanzi's `build_mac.sh`) if the prebuilt ever stops matching the
installed libmgba dylib.** Related: `dvruette/pygba` (https://github.com/dvruette/pygba)
is a gym-style wrapper that *depends on* these same bindings and still tells you to
build/copy `mgba` yourself — no help on the install side, nothing extra we need.

## 3. Candidate C: mGBA 0.10.5 Qt Lua scripting — REJECTED

The brew-installed mGBA 0.10.5 Qt app has the scripting console (Tools → Scripting),
but:
- **No headless mode.** The Qt app always opens a window; there is no CLI flag to run
  a script without the GUI in 0.10.x. (The separate SDL binary in 0.10 has no
  scripting console; scripting is Qt-only there.)
- **No audio API in scripting.** The scripting docs (stable
  https://mgba.io/docs/scripting.html and dev https://mgba.io/docs/dev/scripting.html)
  expose core control, memory, keys, screenshots — **zero audio functions**. Confirmed
  against the dev (0.11) docs too.
- Frame-accurate offline rendering would fight the realtime GUI loop; capture would be
  screen/AV-Foundation based. Strictly worse on every axis we care about.

Usable only as a *debugging aid* (e.g. watching RAM live in the GUI while developing
the menu driver). `mgba-http` (REST bridge over the Lua socket API) exists for remote
control but inherits the same no-audio, GUI-attached limitations.

## 4. Candidate D: anything newer/better?

- **mGBA 0.11-dev scripting** (docs auto-generated from 0.11-8814): adds
  `Core.screenshotToImage()`, richer callbacks — **still no audio API**, and still no
  documented headless script runner. Not shipped stable (0.10.5 remains latest stable
  as of mid-2026). No change to the conclusion.
- **RetroArch + mgba core, `--record`**: RetroArch's FFmpeg record path is not compiled
  into standard macOS builds; scripting/frame-stepping from Python would go through
  the network command interface (coarse, non-deterministic); no memory API adequate
  for menu state polling. Rejected.
- **BizHawk**: Windows-first (mono on macOS is fragile, no arm64 story), Lua-driven.
  It stays what the project prompt says it is: a *Windows* fallback, not for this Mac.
- **`pygba`** (see §2) — wrapper over the same bindings, adds nothing for us.

## 5. Ranked recommendation

1. **libmgba-py prebuilt 0.2.0 arm64 zip + brew `libmgba.0.10.dylib` + conda env
   (python 3.13) + ffmpeg rawvideo/s16le pipes.** Verified end-to-end on this machine;
   zero build; deterministic; ~70x realtime.
2. If the prebuilt ever breaks (e.g. brew moves to mGBA 0.11 and drops the 0.10
   dylib): **build hanzi/libmgba-py from source** (`build_mac.sh`, needs Xcode CLT +
   brew deps) — it pins a known-good mgba revision. Mitigation that avoids even this:
   copy brew's `libmgba.0.10.5.dylib` into `vendor/mgba/` and add
   `@loader_path` to the rpath (commands below) so a brew upgrade can't break us.
3. Official mGBA `-DBUILD_PYTHON=ON` build — same API, more pain; only if 1–2 die.
4. mGBA Qt Lua scripting — debugging aid only (live RAM watching), never the pipeline.
5. RetroArch/BizHawk — not on this Mac.

## 6. EXACT install commands for this machine

```bash
# 0) libmgba dylib — ALREADY PRESENT (mgba 0.10.5_2 installed):
#    /opt/homebrew/lib/libmgba.0.10.dylib      (else: brew install mgba)

# 1) dedicated conda env, python 3.13 (pokebot-supported; verified here)
~/miniconda3/bin/conda create -y -n rec2mp4 python=3.13
~/miniconda3/envs/rec2mp4/bin/python -m pip install pillow numpy

# 2) vendor the prebuilt bindings into the project (gitignored; MPL-2.0 binary blob —
#    fetch at setup, don't commit)
mkdir -p <rec2mp4>/vendor
cd <rec2mp4>/vendor
curl -L -o libmgba-py.zip \
  https://github.com/hanzi/libmgba-py/releases/download/0.2.0-2/libmgba-py_0.2.0_macos-arm64.zip
unzip -o libmgba-py.zip          # -> vendor/mgba/

# 3) one-time rpath fix so no DYLD_LIBRARY_PATH is ever needed (verified)
install_name_tool -add_rpath /opt/homebrew/lib mgba/_pylib.abi3.so

# 3b) OPTIONAL hardening vs future brew upgrades: freeze the dylib next to the .so
cp /opt/homebrew/lib/libmgba.0.10.5.dylib mgba/libmgba.0.10.dylib
install_name_tool -add_rpath @loader_path mgba/_pylib.abi3.so

# 4) run (vendor/ on sys.path; no env vars needed after step 3)
~/miniconda3/envs/rec2mp4/bin/python -c \
  "import sys; sys.path.insert(0,'<rec2mp4>/vendor'); \
   import mgba.core; print('ok')"
```

## 7. API cheat-sheet (real names, all verified on this machine)

Everything lives in the `mgba` package from the zip. `from mgba import ffi, lib` for
raw cffi. Prior art for every pattern: pokebot-gen3 `modules/libmgba.py`
(https://github.com/40Cakes/pokebot-gen3/blob/main/modules/libmgba.py).

### Boot / teardown
```python
import mgba.core, mgba.image, mgba.log, mgba.vfs, mgba.gba
from mgba import ffi, lib

mgba.log.silence()                              # MANDATORY (else massive stderr spam)
core = mgba.core.load_path(str(rom_path))       # -> mgba.gba.GBA (subclass of Core) or None
core.game_title, core.game_code, core.crc32     # 'POKEMON EMER', 'AGB-BPEE', 0x1F1C08FB
w, h = core.desired_video_dimensions()          # (240, 160) — call BEFORE reset for buffer size
screen = mgba.image.Image(w, h)                 # allocates ffi 'color_t[]' (4 B/px here)
core.set_video_buffer(screen)                   # must precede reset()
core.reset()                                    # required before run_frame/save_state/etc.
```

### Save (.sav) injection — from bytes, no disk writes
```python
vf = mgba.vfs.VFile.fromEmpty()
vf.write(sav_bytes, len(sav_bytes))             # 128 KiB flash image w/ .rec at 0x1F000
vf.seek(0, whence=0)
core.load_save(vf)                              # BEFORE reset(); in-memory only
# (file-backed alternative: core.load_save(mgba.vfs.open_path(path, "r+")))
```

### Frame stepping (deterministic)
```python
core.run_frame()                                # exactly one video frame
core.frame_counter                              # int, frames since reset
# GBA exact rate: core.frequency / 280896 cycles = 59.727500569606 fps
```

### Buttons
```python
G = mgba.gba.GBA                                # KEY_* are BIT INDICES: A=0 B=1 SELECT=2
core.set_keys(G.KEY_START)                      #   START=3 RIGHT=4 LEFT=5 UP=6 DOWN=7 R=8 L=9
core.set_keys(raw=0x0008)                       # raw bitmask form (Start)
core.add_keys(...); core.clear_keys(...)        # same call conventions
# set desired keys BEFORE core.run_frame(); they persist until changed.
# core._core.getKeys(core._core) reads the current bitmask (pokebot's get_inputs).
```

### Video framebuffer (per frame, after run_frame)
```python
raw = ffi.buffer(screen.buffer)                 # 240*160*4 = 153,600 bytes, R,G,B,X order
ffmpeg_proc.stdin.write(raw)                    # ffmpeg: -f rawvideo -pix_fmt rgb0 -s 240x160
img = screen.to_pil()                           # PIL 'RGBX' (needs Pillow); .convert('RGB')
# DO NOT use screen.save_png() — broken vs libmgba 0.10.5 (PNGWriteHeader signature drift)
# ffmpeg mux: -r 59.7275005696 ; upscale: -vf scale=960:640:flags=neighbor
```

### Audio (per frame, after run_frame) — stereo s16 @ chosen rate
```python
audio = core.get_audio_channels()               # mgba.audio.StereoBuffer (blip_buf pair)
audio.set_rate(32768)                           # output sample rate, Hz — set once after reset
n = audio.available                             # samples ready (~548/frame @32768)
buf = ffi.new("short[%d]" % (2 * n))
audio._left.read_into(buf, n, 2, 0)             # interleave L at offset 0, stride 2
audio._right.read_into(buf, n, 2, 1)            # interleave R at offset 1
pcm = bytes(ffi.buffer(buf))                    # s16le interleaved stereo
# equivalently: audio.read_into(buf, n)  (StereoBuffer does both lanes)
# drain (or audio.clear()) EVERY frame or the blip buffer saturates.
# ffmpeg: -f s16le -ar 32768 -ac 2 -i audio.pcm   (verified: real music captured)
```

### Memory reads (poll menu/battle state)
```python
# Fast path (pokebot's read_bytes): direct ffi copy out of the core's arenas
buf = bytearray(length)
ffi.memmove(buf, ffi.cast("char*", core._native.memory.wram) + (addr & 0x3FFFF), length)   # EWRAM 0x02...
ffi.memmove(buf, ffi.cast("char*", core._native.memory.iwram) + (addr & 0x7FFF),  length)  # IWRAM 0x03...
ffi.memmove(buf, ffi.cast("char*", core._native.memory.rom) + (addr - 0x08000000), length) # ROM
# Slow/convenient path: core.memory.wram / .iwram / .vram / .sram — Memory areas with
#   .u8[off], .u16[off], .u32[off] indexed views (offsets relative to area base).
# Writes (Strategy B state injection): ffi.memmove(dest_ptr + off, data, len) — same
#   pattern reversed; pokebot's write_bytes allows EWRAM/IWRAM only.
```

### Save states (checkpoint/replay-scrubbing)
```python
state = core.save_state()                       # -> bytes (397,312 for GBA)
vf = mgba.vfs.VFile.fromEmpty(); vf.write(state, len(state)); vf.seek(0, whence=0)
core.load_state(vf)
# also: save_state_slot(n) / load_state_slot(n), save_raw_state()/load_raw_state()
```

### Read back save flash (verify .rec injection landed)
```python
vf = mgba.vfs.VFile.fromEmpty()
lib.GBASavedataClone(ffi.addressof(core._native.memory.savedata), vf.handle)
vf.seek(0, whence=0); sav = vf.read_all()       # pokebot's read_save_data()
```

### Fast-forward / speed
There is no throttle in the bindings — `run_frame()` already runs as fast as possible
(~4,300 fps measured). "Speed" is purely how fast you call it. For --headed debug
preview, throttle yourself (sleep to 1/59.7275 s per frame) and blit `screen.to_pil()`.
Rendering can be disabled entirely for extra speed during seek-phases by setting
`core._native.video.renderer.disableBG[0..3] / disableOBJ / disableWIN[0..1] /
disableOBJWIN = True` (pokebot's `set_video_enabled(False)`) — but then the
framebuffer is stale; re-enable one frame before you need pixels.

### Extras that exist if needed
- `core.add_frame_callback(fn)` — called at each video-frame end.
- `core._callbacks.savedata_updated.append(fn)` — fires when the game writes flash.
- `core.rtc` — RTC control (hanzi addition; pokebot configures fixed RTC for determinism).
- `mgba.libmgba_version_string()` — runtime version report.

## 8. Risks / gotchas carried forward

1. **Version coupling:** the prebuilt `_pylib.abi3.so` (built ~Jan 2024) must dlopen a
   `libmgba.0.10.dylib`. Brew mgba 0.10.5_2 works today; a future brew bump to 0.11
   would break it (soname change). Mitigate with step 3b (freeze the dylib in
   `vendor/mgba/` + `@loader_path` rpath) or pin the brew formula.
2. `save_png` binding broken (documented above) — always use raw buffer / Pillow.
3. `set_keys` index-vs-bitmask trap (use `raw=` for masks).
4. Audio: `set_rate()` must be called after `reset()`; drain every frame; L/R are
   separate blip bufs — use the interleaving `read_into` pattern or channels drift.
5. Bindings are unmaintained-ish (last release 2023) but tiny, stable, and pokebot's
   whole ecosystem runs on them daily; the API surface we need is frozen.
6. Determinism footnote: mGBA's GPIO RTC is live by default; if replay-path menuing
   ever proves timing-sensitive, fix the RTC via `core.rtc` before `reset()`
   (pokebot does this). The recorded battle itself replays from its stored seed, so
   RTC should not affect fidelity.
7. Python: stay on **3.13** (pokebot-supported, verified). 3.14 imports and runs but
   is outside the ecosystem's tested envelope.

## 9. Sources

- https://github.com/hanzi/libmgba-py (repo, build scripts, MPL-2.0)
- https://github.com/hanzi/libmgba-py/releases (0.2.0-2 assets incl. `libmgba-py_0.2.0_macos-arm64.zip`)
- https://github.com/40Cakes/pokebot-gen3 (Readme: "runs libmgba + mGBA Python bindings")
- https://github.com/40Cakes/pokebot-gen3/blob/main/requirements.py (Python 3.11–3.13, libmgba 0.2.0-2 download logic incl. macOS-arm64 branch)
- https://github.com/40Cakes/pokebot-gen3/blob/main/modules/libmgba.py (the API-usage playbook: audio read_into, ffi.memmove memory access, video-disable renderer pokes, save-state PNG embedding)
- https://mgba.io/docs/scripting.html and https://mgba.io/docs/dev/scripting.html (Lua API: keys/memory/screenshot present, **no audio**, no headless runner)
- https://github.com/mgba-emu/mgba/issues/2049, /issues/2057, /issues/997, /discussions/3477 (official python-binding build fragility, 2018→2025)
- https://github.com/dvruette/pygba (wrapper over the same bindings; no prebuilt help)
- Local verification artifacts (scratchpad, session 2026-07-27): `smoke.py`/`smoke2.py`/`smoke3.py`
  → `smoke3.mp4` (15 s intro w/ correct frames), `smoke3_audio.wav` (music, −26.5 dB mean),
  `smoke_frame600.png`, measured fps/sample counts quoted in §1.
