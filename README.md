# rec2mp4

[![ci](https://github.com/GuyShtainer/rec2mp4/actions/workflows/ci.yml/badge.svg)](https://github.com/GuyShtainer/rec2mp4/actions/workflows/ci.yml)

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
  on Python 3.12–3.14 (118 checks passing; 54 in the asset-free synthetic
  mode CI runs).
- **`--info-only` CLI** — summarizes records with no emulator, ffmpeg, ROM or
  save needed.
- **Battle-info side panel** — since the refactor into `rec2mp4.pipeline`
  (the conversion engine the CLI wraps), each video gets a text-only info
  panel (teams with species names read from your ROM, opponents, streak,
  outcome/duration) composited beside the battle; verified end-to-end on a
  real record (panel + undistorted 960×640 game + intact audio). See "The
  battle-info side panel" below.
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

## Setup

The emulator core is [hanzi/libmgba-py](https://github.com/hanzi/libmgba-py)
(prebuilt mGBA Python bindings, MPL-2.0), fetched into the gitignored
`vendor/` directory — never committed. One script does the whole fetch on
macOS (arm64/Intel), Windows x64 and Linux x64, including the macOS
rpath/dylib fix-ups, and ends with an `import mgba.core` smoke test:

```bash
# system deps first — macOS: brew install ffmpeg mgba
#                     Windows: choco install ffmpeg   (the mGBA DLL is bundled)
#                     Linux:   sudo apt install ffmpeg
python tools/fetch_bindings.py     # idempotent; safe to re-run any time
python -m pip install pillow numpy # optional: headed-mode PNG previews
```

The replay itself was validated on macOS arm64 with Python 3.13 (a dedicated
env is tidy but not required: `conda create -n rec2mp4 python=3.13`). The
bindings are `abi3` builds, so any CPython ≥ 3.10 should load them — CI
imports them on Python 3.12 on Windows and macOS. `--info-only` and the
tests need none of this — pure stdlib, any Python ≥ 3.10.

<details>
<summary>Appendix: manual macOS setup (what the script automates)</summary>

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

</details>

## Usage

Run from the project root (or `pip install -e .` for a `rec2mp4` command):

```bash
# Inspect a record — no emulator, ROM or save required
python3 -m rec2mp4 local/recs/GUYA_27-07-2026_08-54.rec --info-only

# Convert a single record. Output name carries the battle's data, e.g.
#   out/GUYA_27-07-2026_08-54 - Battle Arena Open vs SAILOR MAXWELL.mp4
# plus a matching .json sidecar (see "Output names & the JSON sidecar")
python3 -m rec2mp4 local/recs/GUYA_27-07-2026_08-54.rec \
    --sav local/alt-saves/all-shiny.sav

# Batch: every *.rec in a folder (continues past failures, summary at the end)
python3 -m rec2mp4 local/recs -o out --sav local/alt-saves/all-shiny.sav

# Headed mode: refresh a preview PNG every 60 frames (this stack has no real
# window; the PNG path is logged, needs Pillow in the emulator env)
python3 -m rec2mp4 my.rec --headed --sav local/alt-saves/all-shiny.sav

# Video only, no audio track; 2x upscale instead of the default 4x (960x640)
python3 -m rec2mp4 my.rec --no-audio --scale 2 --sav local/alt-saves/all-shiny.sav

# Exactly as the recorder saw it (their BATTLE SCENE / text-speed settings)
python3 -m rec2mp4 my.rec --anims record --text-speed record

# No side panel (plain game video, exactly the pre-panel output)
python3 -m rec2mp4 my.rec --panel off
```

Options: `-o/--outdir` (default `out/`), `--rom` (default `local/rom.gba`),
`--sav` (default `local/template.sav`), `--headed`, `--scale N`, `--no-audio`,
`--anims on|off|record` (default `on`), `--text-speed slow|mid|fast|record`
(default `record`), `--panel right|left|off` (default `right`),
`--panel-info CSV` (default `all` — see "The battle-info side panel"),
`--plain-names`, `--no-sidecar`, `--info-only`,
`--max-seconds N` (replay timeout, default 1800), `--pix-fmt`
(raw-framebuffer format handed to ffmpeg, default `rgb0`).
Exit code is non-zero if any record fails.

### The battle-info side panel (`--panel`, `--panel-info`)

By default every video gets a dark, text-only info panel composited beside
the battle (`--panel right`; `left` swaps sides, `off` produces the plain
game video byte-for-byte as before). The panel is half the game's width at
the same height (480×640 next to the default 960×640 game), so the default
output is 1440×640. The game capture itself is untouched — the panel is
stacked on at finalize time with ffmpeg `hstack` (video re-encoded once,
audio stream-copied, `+faststart`, atomic replace), which is also why the
panel can show the **outcome and duration**: they are known by then.

What it shows (pick sections with `--panel-info header,teams,...`; default
`all`): facility + level mode + battle kind (`header`), streak when known,
recorder and co-players (`players`), opponent display names (`opponents`),
both teams with **species names read from YOUR ROM** (`gSpeciesNames` at a
known US-Emerald address — internal-id order, no name tables ship with the
tool), nickname shown only when it differs from the species, level and a
gold `*` for shinies (`teams`), the first lines of a PokeDNA `.txt` export
sidecar (`export`), and outcome/duration/RNG seed (`footer`). Text only —
no Game Freak artwork, and every game-derived string comes from your own
ROM or record at runtime.

The panel needs Pillow (`pip install pillow` into whatever Python runs
rec2mp4 — the conda env from Setup already has it). Without Pillow the
conversion still works: a warning is printed and videos are written
without the panel. `--panel off` never touches Pillow.

### Streak-aware export filenames (PokeDNA)

PokeDNA's upcoming export format names records
`<PLAYER>_<Facility>-<O|50>-<streak>_<date>_<time>.rec`
(e.g. `GUYA_Factory-50-7_27-07-2026_10-40.rec`, `O` = Open Level). rec2mp4
recognizes that stem: the streak lands in the output filename
(`... vs SAILOR MAXWELL (streak 7).mp4`), in the panel header
(`Streak 7`) and in the JSON sidecar (`"streak": 7`). The stem is checked
against the record itself; on a mismatch a warning is printed and the
record is trusted. Old-format stems (`GUYA_27-07-2026_10-40.rec`) behave
exactly as before. If a `<same stem>.txt` info file sits next to the
`.rec` (PokeDNA's future export sidecar), its lines are stored in the JSON
sidecar as `export_info` and the first few short lines are rendered in the
panel's `export` section.

### Output names & the JSON sidecar

By default each video is named with the battle's own data:

```
<stem> - <Facility> <Open|Lv50>[ <double|multi|two-opponents|link>] vs <Opponent>[ and <OpponentB>].mp4
e.g.  GUYA_19-07-2026_10-09 - Battle Dome Open double vs SAILOR MAXWELL.mp4
```

Battle Frontier opponent names (`SAILOR MAXWELL`, ...) are **read from your
own ROM at runtime** — no trainer names, game text or other Game Freak data
ship with this tool, only ROM addresses and struct layouts. If a name can't
be resolved with certainty, the tool falls back to a descriptive label
(`frontier trainer 83`, the record-mix friend's name stored in the record
itself, `Apprentice N`, `Frontier Brain`). Names are sanitized to ASCII,
Windows-safe characters. Re-converting the same record overwrites its own
output; if the target name already exists from a *different* record, a
` (2)`, ` (3)` ... suffix is appended instead. `--plain-names` restores the
old `<stem>.mp4` naming.

Next to every converted (or timeout-truncated) video, a `<same basename>.json`
sidecar preserves everything known about the conversion: the full parsed
record (facility, level mode, battle flags, players, both teams, RNG seed,
input-lane sizes, opponents), the source `.rec` filename and the SHA-1 of its
4096 bytes, the ROM's CRC32, the options used (`anims`/`text-speed`/`scale`/
audio/`pix-fmt`), the replay result (frames, seconds, end reason) **and the
battle outcome** (`won`/`lost`/`draw`, read from the game's own
`gBattleOutcome` during playback), plus the rec2mp4 version and an ISO-8601
timestamp. Sidecars inherit the record's privacy caveats (player names,
trainer IDs, teams) — share them as deliberately as the videos. Disable with
`--no-sidecar`.

**About `--anims`:** the record stores a snapshot of the *recorder's* in-game
options (struct byte +1279); someone who battled with BATTLE SCENE OFF gets
replays with no move effects and no shiny sparkle. rec2mp4 defaults to
patching animations ON in the injected copy (checksum recomputed) so videos
show everything. This is presentation-only and cannot desync the replay:
Emerald's battle animations draw randomness exclusively from the separate
`Random2()` stream — that separation exists precisely so link-battle peers
with different scene settings stay in sync — and recorded inputs are consumed
per decision, not per frame. Verified empirically: the same record converted
with animations off (59.4 s) and on (73.6 s) reaches the same outcome with
identical HP trajectories.

## GUI (desktop)

A minimal desktop front-end over the exact same pipeline, built on stdlib
`tkinter` — **zero extra dependencies** (python.org and conda installers on
macOS/Windows ship Tk support):

```bash
python -m rec2mp4.gui        # from a clone
rec2mp4-gui                  # if installed with pip (gui-script entry point)
```

What it does:

* **Queue** — "Add .rec files…" (multi-select) or "Add folder…" (every
  `*.rec`, sorted, like the CLI); each record is validated + summarized with
  the pure-stdlib parser **the moment it is added** (no emulator involved),
  so invalid records are flagged red immediately. Duplicates are skipped,
  rows show facility / level mode / battle kind / opponent, and
  double-clicking a row opens a details window with the full record summary
  and, after a conversion, that record's complete log.
* **Settings pane** — mirrors the CLI options 1:1 (animations, text speed,
  scale, audio, side panel + per-section checkboxes, plain names, JSON
  sidecar, output folder, ROM/save pickers prefilled with the `local/`
  defaults when those files exist).
* **Convert** — runs the batch on a worker thread; per-row status
  (`waiting` / `converting` / `OK` / `TRUNC` / `FAILED`) plus a live
  progress line fed by the driver's own log and frame counter. **Cancel**
  finishes the record in flight, then stops. "Open output folder" opens
  Finder/Explorer on the output directory.

Converting still needs everything the CLI needs — the emulator stack from
"Setup" (vendored mGBA bindings + ffmpeg) plus **your own** ROM and a
post-game save; if the stack is missing, the GUI surfaces the same install
hint the CLI prints. Queueing and inspecting records works with nothing
installed at all. Honest status: the GUI is newly built and has been
exercised on macOS only (the pure-logic layer is covered by
`tests/test_gui.py` on all three CI OSes; the widget smoke test runs where
a display exists). Screenshots are deliberately not included.

## Windows notes

The **full replay** has only ever been exercised end-to-end on macOS arm64,
but the Windows plumbing is CI-validated on every push (`windows-latest`,
Python 3.12 — see the badge above):

- **Covered by CI on Windows:** the whole parser/validator/injector suite and
  the naming/sidecar suite (`tests/test_rec.py` in synthetic mode,
  `tests/test_naming.py`); the real ffmpeg encode path
  (`python -m rec2mp4.video` self-test, ffmpeg via choco); and the emulator
  bindings — `tools/fetch_bindings.py` downloads the libmgba-py `0.2.0-2`
  `win64` zip (which bundles `mgba.dll` and every dependency DLL — the same
  zip [pokebot-gen3](https://github.com/40Cakes/pokebot-gen3) runs on Windows
  daily) and proves `import mgba.core` works. The driver also calls
  `os.add_dll_directory(vendor/mgba)` on Windows before importing.
- **Still manual, by design:** the full replay (boot → menus → playback →
  MP4) needs your own US Emerald ROM, post-game save and `.rec` files, which
  CI never has. Run it yourself with
  `python -m rec2mp4 my.rec --rom local\rom.gba --sav local\template.sav`
  and please report how it goes. BizHawk remains a manual, Windows-only
  fallback for capturing a replay by hand. Non-US ROMs stay unsupported (the
  RAM addresses are US-specific). Contributions welcome.

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
- **Trademarks:** Pokémon, Game Boy Advance and related names are trademarks of
  Nintendo, Creatures Inc. and GAME FREAK inc. This project is a fan-made tool,
  not affiliated with or endorsed by any of them. Reverse-engineered knowledge
  used here is limited to facts and RAM/save-layout addresses derived from the
  pret decompilation project's published symbols; no game code is bundled.
