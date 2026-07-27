# rec2mp4

Turn Pokémon Emerald **Battle Record** exports (`.rec`) into `.mp4` videos.

A `.rec` file is a raw dump of save **sector 31** (4096 bytes) — the Frontier Pass
"Battle Record" that Emerald writes after a recordable Battle Frontier or link
battle. It contains an RNG seed, both teams, and the raw per-player button-input
streams — *not* video. The only faithful way to turn that into pixels is to let
the real engine interpret it: reimplementing playback would mean a bit-exact
clone of the entire Gen-3 battle engine (damage math, AI, animation timing, and
the exact PRNG consumption order), where any single divergence desyncs the input
stream and produces a battle that never happened. So rec2mp4 injects the record
into a save image, boots a **user-supplied** US Emerald ROM in a headless mGBA
core, drives the menus to Frontier Pass → BATTLE RECORD, lets the game replay
the battle by itself, and pipes the emulator's frames and audio into ffmpeg.

## Status — honest, current

**Validated end-to-end on 2026-07-27: all 10 real hardware-exported records
(Battle Dome, Palace and Arena; singles and doubles) converted to MP4 in one
batch run — 10/10 OK, zero failures, every replay reaching its natural end**
(42 s to 4 m 52 s of battle each, 960x640 h264 + AAC at the GBA's exact
59.7275 fps). Spot-checked frames show the correct battle: the trainers,
teams, HP and battle text match what the parser reads out of the record.

Detail of what that covers:

- **Parser / validator / injector (`rec2mp4.rec`)** — verified against the 10
  real records, plus corruption tests (checksum, sentinel, forbidden
  battle-flag bits, truncation) and an inject round-trip. Pure stdlib, tested
  on Python 3.12–3.14 (97 checks passing).
- **`--info-only` CLI** — summarizes records with no emulator, ffmpeg, ROM or
  save needed.
- **Emulator driver + encoder (`rec2mp4.driver` / `rec2mp4.video`)** — the full
  boot → menu-drive → inject → replay → end-detect → encode chain is what the
  batch above exercised. One real-world fix over the researched plan: the
  intro movie ignores input for its first ~60 frames, so every menu press uses
  a press-with-retry loop instead of a one-shot press.

Not yet validated: Windows, non-US ROMs (unsupported by design — the RAM
addresses are US-specific), link-battle records (none in the test set), and
Strategy B (save-free playback — see the roadmap note in
`docs/research/replay-path.md`).

## Requirements

You must supply your own game data. **None of it is distributed with, or
committed to, this repository** (`local/` is gitignored):

- `local/rom.gba` — a clean **US Emerald** ROM (BPEE rev0, CRC32 `1F1C08FB`),
  dumped from your own cartridge.
- A **128 KiB `.sav`** whose save has the **Frontier Pass** (post–Hall of Fame).
  The menu path to Battle Record playback goes through the Frontier Pass, so a
  fresh or pre-champion save cannot reach it.
- `.rec` files — 4096-byte sector-31 dumps, e.g. exported by
  [PokeDNA](https://github.com/GuyShtainer/PokeDNA)'s battle-record export, or
  cut from any 128 KiB save (bytes `0x1F000..0x1FFFF`).

**Which local save is used:** the default `local/template.sav` now holds a copy
of **`local/alt-saves/all-shiny.sav`** (post-game, Frontier Pass + Game Clear
set, both save slots checksum-OK) — the save all validation ran with. The
original EZ-Flash cart backup that first sat at that path turned out to be an
**erased-flash image (every byte 0xFF — no save at all)**; a fresh cart re-dump
is needed if that cart's real save is ever wanted. Full findings:
`docs/research/save-check.md`.

## Setup (macOS)

Verified on macOS arm64 (Apple Silicon). The emulator core is
[hanzi/libmgba-py](https://github.com/hanzi/libmgba-py) (prebuilt mGBA Python
bindings, MPL-2.0), fetched into the gitignored `vendor/` directory — never
committed. Full rationale and fallbacks: `docs/research/emulator-stack.md`.

```bash
# 0) system deps (Homebrew)
brew install ffmpeg mgba          # provides /opt/homebrew/lib/libmgba.0.10.dylib

# 1) dedicated Python 3.13 env (the bindings' supported envelope)
~/miniconda3/bin/conda create -y -n rec2mp4 python=3.13
~/miniconda3/envs/rec2mp4/bin/python -m pip install pillow numpy

# 2) fetch the prebuilt mGBA bindings into vendor/ (gitignored)
mkdir -p vendor && cd vendor
curl -L -o libmgba-py.zip \
  https://github.com/hanzi/libmgba-py/releases/download/0.2.0-2/libmgba-py_0.2.0_macos-arm64.zip
unzip -o libmgba-py.zip           # -> vendor/mgba/

# 3) one-time rpath fix (no DYLD_LIBRARY_PATH needed afterwards)
install_name_tool -add_rpath /opt/homebrew/lib mgba/_pylib.abi3.so

# 3b) optional hardening against future brew mGBA upgrades (0.11 breaks the soname):
cp /opt/homebrew/lib/libmgba.0.10.5.dylib mgba/libmgba.0.10.dylib
install_name_tool -add_rpath @loader_path mgba/_pylib.abi3.so
cd ..

# 4) smoke test
~/miniconda3/envs/rec2mp4/bin/python -c \
  "import sys; sys.path.insert(0,'vendor'); import mgba.core; print('ok')"
```

`--info-only` needs none of the above — any Python ≥ 3.10 will do.

## Usage

Run from the project root (or `pip install -e .` for a `rec2mp4` command):

```bash
# Inspect a record — no emulator, ROM or save required
python3 -m rec2mp4 local/recs/GUYA_27-07-2026_08-54.rec --info-only

# Convert a single record to out/<basename>.mp4
python3 -m rec2mp4 local/recs/GUYA_27-07-2026_08-54.rec \
    --sav local/alt-saves/all-shiny.sav

# Batch: every *.rec in a folder (continues past failures, summary at the end)
python3 -m rec2mp4 local/recs -o out --sav local/alt-saves/all-shiny.sav

# Headed mode: refresh a preview PNG every 60 frames (this stack has no real
# window; the PNG path is logged, needs Pillow in the emulator env)
python3 -m rec2mp4 my.rec --headed --sav local/alt-saves/all-shiny.sav

# Video only, no audio track; 2x upscale instead of the default 4x (960x640)
python3 -m rec2mp4 my.rec --no-audio --scale 2 --sav local/alt-saves/all-shiny.sav
```

Options: `-o/--outdir` (default `out/`), `--rom` (default `local/rom.gba`),
`--sav` (default `local/template.sav`), `--headed`, `--scale N`, `--no-audio`,
`--info-only`, `--max-seconds N` (replay timeout, default 1800), `--pix-fmt`
(raw-framebuffer format handed to ffmpeg, default `rgb0`). Exit code is
non-zero if any record fails.

## Windows notes (untested)

The whole pipeline has only ever been exercised on macOS arm64. That said,
nothing is macOS-specific in principle: libmgba-py release `0.2.0-2` also ships
a prebuilt `win64` zip (the [pokebot-gen3](https://github.com/40Cakes/pokebot-gen3)
project fetches and runs it on Windows daily — follow its setup for the
matching libmgba DLL), and ffmpeg is available via `winget`/`choco`. The
`install_name_tool` steps are macOS-only; on Windows the DLL just needs to be
next to the extension module. BizHawk remains a manual, Windows-only fallback
for capturing a replay by hand. Contributions welcome.

## How it works

The CLI validates the `.rec` exactly the way the game does (sentinel, battle
flags, u32 byte-sum checksum) and splices it into sector 31 of an in-memory
copy of your save — your files on disk are never modified. A headless mGBA
core boots the ROM with that save, and the driver advances one frame at a
time, polling `gMain.callback2` and friends in emulated RAM to know exactly
which screen it is on, pressing buttons only when the game is provably ready
(intro → title → CONTINUE → Start menu → Frontier Pass → BATTLE RECORD). From
there the game replays the battle by itself from the record's seed and input
streams; the driver detects the natural end by watching for the game's
end-of-playback callback chain in RAM, then stops capture after a short tail.
Every frame and audio chunk in between is streamed to ffmpeg at the GBA's
exact 59.7275 fps. Addresses, decomp citations and the full state machine:
`docs/research/replay-path.md`; emulator/encoder details:
`docs/research/emulator-stack.md`.

## Licensing

- **Code:** GPL-3.0-or-later (see `LICENSE`). © 2026 Guy Shtainer.
- **No Nintendo assets:** this repository contains no ROMs, saves, sprites,
  game text or other copyrighted game data, and never will. `local/`, `out/`,
  `*.gba`, `*.sav`, `*.rec` and `*.mp4` are gitignored; you must dump your own
  cartridge.
- **`vendor/` is fetched, not committed:** the mGBA Python bindings
  (MPL-2.0) and mGBA itself are downloaded at setup time and stay untracked.
- **Privacy note:** `.rec` files contain user data — player names, trainer IDs
  and teams of everyone in the recorded battle. Share them (and videos made
  from them) accordingly.
