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
  each mon's moves/EVs/IVs with EV `/510` + IV `/186` sums, outcome/duration)
  composited beside the battle — with an optional `--panel-cycle` that rotates
  the moves/EV/IV pages over time; verified end-to-end on a real record (panel
  + undistorted 960×640 game + intact audio). See "The battle-info side panel"
  below.
- **Emulator driver + encoder (`rec2mp4.driver` / `rec2mp4.video`)** — the full
  boot → menu-drive → inject → replay → end-detect → encode chain is what the
  batch above exercised. One real-world fix over the researched plan: the
  intro movie ignores input for its first ~60 frames, so every menu press uses
  a press-with-retry loop instead of a one-shot press.
- **Panel layouts, frame preview, parallel batches, opening/end cards**
  (2026-08-03/04, newer than the batch above) — exercised on real records on
  macOS: a `--panel top` band, a four-side layout with time-cycling stats
  (1640×864, duration matching the game), `--preview` PNGs in ~1 s, a
  4-record `--jobs 4` batch, an end card built from a `state.*` sidecar, and
  an opening card carrying the opponent's own pre-battle line — whose audio
  delay was verified by measurement: silence before the game starts, and the
  game audio bit-identical in level to the un-carded build 3 s later.
  Not yet run on Windows/Linux; the asset-free half is covered by
  `tests/test_layout.py` on all three CI OSes.

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

# See a few composited frames first — no video, ~1 s (see "Preview frames")
python3 -m rec2mp4 my.rec --preview

# A folder, one record per CPU, with a panel layout you designed
python3 -m rec2mp4 local/recs -o out --jobs 0 --layout my-layout.json
```

Options: `-o/--outdir` (default `out/`), `--rom` (default `local/rom.gba`),
`--sav` (default `local/template.sav`), `--headed`, `--scale N`, `--no-audio`,
`--anims on|off|record` (default `on`), `--text-speed slow|mid|fast|record`
(default `record`), `--pov player|opponent` (default `player` — see
"Opponent POV"), `--panel right|left|top|bottom|off` (default `right`),
`--panel-info CSV` (default `all` — see "The battle-info side panel"),
`--layout FILE|default` (a designed panel layout — see "Designing your own
panel"), `--intro-card SECONDS` (the opponent's pre-battle line as an
opening card, default 3), `--end-card SECONDS` (trainer-state card on the
last frames, default 3), `-j/--jobs N` (records converted at once, default 0 = one per
CPU), `--preview [N]` / `--preview-start` / `--preview-every` (composited
preview PNGs instead of a video), `--plain-names`, `--no-sidecar`,
`--info-only`, `--max-seconds N` (replay timeout, default 1800), `--pix-fmt`
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
gold `*` for shinies (`teams`), each mon's decoded battle stats (`moves`,
`evs`, `ivs` — see below), the first lines of a PokeDNA `.txt` export
sidecar (`export`), and outcome/duration/RNG seed (`footer`). Text only —
no Game Freak artwork, and every game-derived string comes from your own
ROM or record at runtime.

**Per-mon stats — `moves`, `evs`, `ivs`.** Each battler's record carries its
full 100-byte party mon, so the panel can decode and show, for every mon:

- `moves` — the mon's up to four move **names, read from YOUR ROM**
  (`gMoveNames`), falling back to `Move #<id>` if a name can't be decoded.
- `evs` — the six effort values (`HP Atk Def SpA SpD Spe`) and a bold
  **`Sum NNN/510`** total.
- `ivs` — the six individual values and a bold **`Sum NNN/186`** total; a
  perfect (31) IV is flagged with a `*`, and an all-31 mon is marked
  `PERFECT`.

A mon whose checksum does not verify shows `(stats unavailable)` rather than
untrusted numbers.

**Cycling the stat views over time — `--panel-cycle SECONDS`.** Because the
output is a non-interactive video, you can rotate the stat pages instead of
stacking them: `--panel-cycle 5` shows each page for 5 seconds and loops for
the whole battle, so a viewer sees the moves page, then the EVs page, then
the IVs page, over and over. The static header/teams context stays put; only
the stat block swaps. `--panel-cycle-pages moves,evs,ivs` (default all three)
picks which pages to include. `--panel-cycle 0` (the default) keeps a single
static panel — put `moves`/`evs`/`ivs` in `--panel-info` to stack them
instead. Example:

```bash
# cycle moves -> EVs -> IVs, 5 s each, over the battle
python3 -m rec2mp4 my.rec --panel-info header,teams --panel-cycle 5

# just flip EVs and IVs, 4 s each
python3 -m rec2mp4 my.rec --panel-cycle 4 --panel-cycle-pages evs,ivs
```

The JSON sidecar records how the panel was drawn: `options.panel_mode`
(`static` or `cycle`) plus `panel_cycle_seconds` and `panel_cycle_pages`
when cycling.

The panel needs Pillow (`pip install pillow` into whatever Python runs
rec2mp4 — the conda env from Setup already has it). Without Pillow the
conversion still works: a warning is printed and videos are written
without the panel. `--panel off` never touches Pillow.

### Designing your own panel — `--layout`, `rec2mp4-designer`

The stacked panel above is one fixed arrangement. A **layout** replaces it
with free-form geometry: information blocks you place and size yourself, on
any of the four sides at once.

```
+-----------------------------------------+
|                  top                    |   left/right are columns
+--------+-----------------------+--------+   (game height)
|  left  |      game video       | right  |
+--------+-----------------------+--------+   top/bottom are bands spanning
|                 bottom                  |   the FULL composited width
+-----------------------------------------+
```

Open the designer — from the GUI's **Design panel…** button (it hands over
the selected record and your ROM, so the live preview shows *your* battle),
or standalone:

```bash
python3 -m rec2mp4.designer            # or: rec2mp4-designer my-layout.json
```

The canvas shows the finished frame. Drag a block to move it, drag its
bottom-right corner to resize, double-click to hide/show it, `Delete` to
remove it, arrow keys to nudge.

**Zoom** with the `−` / `+` / `Fit` buttons, `⌘+` / `⌘-` / `⌘0` (`Ctrl` on
Windows/Linux) or `⌘`/`Ctrl` + scroll wheel; 100 % means "the whole frame
fits", anything above that scrolls, and past 1:1 the preview switches to
nearest-neighbour so you can see exactly where a block's edge lands. Plain
scroll pans vertically, `Shift`+scroll horizontally.

**Undo / redo** with the toolbar buttons or `⌘Z` / `⌘Y` (and `⌘⇧Z`), with the
`Ctrl` equivalents bound for Windows/Linux. Every edit is undoable — drags,
resizes, adding or deleting a block, toggling a panel side, thickness,
backgrounds, colours, presets — and a drag is a single step, not one per
mouse-move. The side pane sets, per **panel**: which
sides exist, thickness (in GBA units — 240×160 is the game), background
colour, a background **image** (cover / contain / stretch / tile / center,
with a dim slider); and per **block**: which section it draws, font scale,
alignment, text/caption/accent colours, its own background + opacity,
border, corner radius, padding, line spacing, word wrap and overflow
behaviour. `text` blocks hold whatever you type (a title, a handle);
`rule`/`frame` blocks are dividers and boxes. Presets seed a right column,
a band, or all four sides at once. **Save** writes a plain JSON file, and
**Use in conversion** hands it back to the GUI.

```bash
python3 -m rec2mp4 my.rec --layout my-layout.json
python3 -m rec2mp4 my.rec --panel top          # a generated band layout
python3 -m rec2mp4 my.rec --layout default     # generated, for --panel's side
```

`--layout default` is worth knowing: because each section gets its **own
box**, a full 3v3's moves/EVs/IVs all fit instead of overflowing one shared
column. When a box still cannot fit its content at the minimum legible font,
the block prints a `+N` marker rather than silently dropping rows.

Layout files are versioned JSON (`rec2mp4_layout: 1`) and are validated on
load: an unknown section, a bad colour, two panels on one side or a
too-thin panel is an error before any emulation starts. Panel thickness is
kept even so every composited dimension stays even for yuv420p at any
`--scale`.

### Preview frames before converting — `--preview`

A full conversion replays the whole battle. To see what the output will look
like first — panel layout, colours, `--scale`, and that the record replays
at all — grab a few composited frames instead:

```bash
python3 -m rec2mp4 my.rec --preview          # 4 PNGs into the output folder
python3 -m rec2mp4 my.rec --preview 6 --preview-start 3 --preview-every 2
```

Each PNG is the **real output frame** — the game video with the real panel
beside it — captured N seconds into the battle. It boots the ROM and drives
the same menu path, then stops as soon as it has the frames it needs
(typically ~1 second of wall clock, versus a full replay). The GUI's
**Preview frames…** button does the same for the selected record and opens a
viewer you can page through and save from; the designer then draws your
layout over that real frame.

### Converting a queue in parallel — `--jobs`

A batch converts **one record per CPU** by default, each in its own process
(`--jobs 0` = auto; `--jobs 1` = the classic in-process loop with live
interleaved logs; the GUI has a *Convert in parallel* checkbox and a worker
count). Output names are reserved in the parent process before dispatch, so
two workers can never claim the same `.mp4`.

Why per record and not finer: a replay is strictly sequential — frame *N+1*
depends on the emulator and RNG state at frame *N* — so a single video
cannot be split across cores. If a worker process dies outright, the records
it had not finished are reported `FAILED` with a "retry with `--jobs 1`" hint
rather than hanging the batch.

**The thread budget matters more than the job count.** One ffmpeg spawns
**55 threads** on a 12-core machine (x264 sizes itself for the whole box), so
N workers left alone ask for 55 × N threads and the machine spends its time
context-switching instead of encoding — a wall of red in `htop`, and kernel
time in `time`. rec2mp4 therefore divides the cores among the workers: each
ffmpeg gets `cores / jobs` threads (`--jobs 1` still gets everything);
`--encoder-threads N` overrides the split.

Measured on 6 records, `--jobs 12`, 12 cores, **idle machine**:

| | uncapped (55 threads each) | capped (1 thread each) |
|---|---|---|
| wall clock | 1 m 11 s | **1 m 03 s** (−12 %) |
| user CPU | 8 m 58 s | 7 m 35 s |
| **kernel CPU** | **1 m 53 s** | **0 m 23 s** (−5×) |

Read that honestly: on an *idle* machine with more cores than work, the
oversubscription mostly burns CPU (−27 % total CPU) rather than wall clock.
The wall-clock cost shows up when the cores are actually contended — a longer
batch, or anything else running. A first measurement here showed a 1.7×
wall-clock win, but that run had been polluted by another batch still
finishing; on a genuinely idle machine the two arms tied. The kernel-time and
total-CPU reductions are the reliable results.

### The opponent's pre-battle line — the opening card (`--intro-card`)

In the Battle Frontier the opponent taunts you before the fight. That line is
**not in the record** — a `.rec` replays the battle only, starting at the
engine's "<TRAINER> would like to battle!" — but it *is* in the ROM, keyed by
the same opponent id the record carries (`gBattleFrontierTrainers[id]
.speechBefore`, six Easy Chat words). rec2mp4 decodes it from **your** ROM and
opens the video with it:

```
              VS PARASOL LADY JULIANA
               Battle Dome - Open Level
                 "I THINK I AM
              SHOPPING TOO MUCH"
```

`--intro-card SECONDS` (default 3, `0` disables). The card is full-frame and
plays before the game video; the audio is delayed to match, so the battle stays
in sync to the sample. It also shows up as frame 0 of `--preview`, and the
sidecar records `intro_card_seconds` + the exact `intro_card_speech`.

Opponents whose greeting is **not** in the ROM get no card: record-mix friends
and apprentices keep theirs in the save (not in the `.rec`), and Frontier Brains
have scripted dialogue rather than an Easy Chat line. Nothing is shipped — the
words come out of your own ROM at runtime, like every other name rec2mp4 shows.

### The trainer's save state — `trainer` section and the end card

PokeDNA writes a `<stem>.txt` next to each exported `.rec` whose
machine-readable `state.*` block carries the save the record came out of:
playtime, Pokédex seen/caught, Battle Points, and the seven Frontier symbols
(`docs/REC-SIDECAR.md`). rec2mp4 reads it and uses it twice:

- the **`trainer` panel section** (in `--panel-info`, and a block kind in the
  designer) shows playtime / Pokédex / BP plus the symbols drawn as seven
  pips in Frontier Pass order (Tower → Pyramid; dark = none, silver, gold);
- the **end card** holds that same summary full-frame over the last seconds
  of the video: `--end-card 3` (the default; `0` turns it off) — the bookend
  to the opening card above.

Every key is optional and a missing one is never shown as a zero — a Ruby
record has no symbols at all, and a record with no `.txt` gets no card. The
card is drawn in the *same* encode pass as the panel composite, so it costs
a few seconds of video, not a second pass over the whole file. The parsed
state is also copied into the JSON sidecar as `trainer_state`.

### Opponent POV (experimental) — `--pov opponent`

`--pov opponent` flips the camera to the **other side** of the battle: the
opponent's team stands at the bottom with player-style HP boxes, and your
recorded team appears as the enemy at the top. It works by dressing the
record up as a *non-master link record* and letting the game's own
link-replay path render it (swap the two party blocks, set the
`RECORDED_LINK` battle-type bit, clear `IS_MASTER`/`RECORDED_IS_MASTER`,
fabricate the now-bottom link player, and repoint `multiplayerId`; the input
lanes are battler-indexed and are left untouched). Every recorded action is
still attributed to the correct side.

**Honest caveat — faithful only for genuine link records.** The game only
offers a perspective switch for *link* battles, where both sides' inputs
were human-recorded and no AI runs at playback. For the **Frontier (vs-AI)
records this tool normally handles it is a "what-if"**: the opponent's moves
are re-decided by a live AI whose RNG consumption cannot be reproduced from
the record, so the replay **diverges after about turn 1** and typically ends
early through the engine's clean teleport-quit fade (a natural fade to
black, not a crash — the video just stops mid-battle, ~35 s). It is *not*
the battle as it happened. Outputs are tagged loudly: the filename gets a
` [opponent POV]` suffix, the panel header shows "Opponent POV
(experimental)", and the JSON sidecar records `pov`, a `pov_faithful` bool
(false for Frontier records) and a `pov_note` explaining the divergence. If
you ever record a real link battle, the same flip is faithful and
`pov_faithful` is true. Full derivation, decomp citations and evidence
frames: [`docs/research/opponent-pov.md`](docs/research/opponent-pov.md).

```bash
# Watch a Frontier record from the opponent's side (a what-if view)
python3 -m rec2mp4 my.rec --pov opponent
```

### Streak-aware export filenames (PokeDNA)

PokeDNA (v3.0.0 and later) names exported records
`<PLAYER>_<Facility>-<O|50>-<streak>_<date>_<time>.rec`
(e.g. `GUYA_Factory-50-7_27-07-2026_10-40.rec`, `O` = Open Level). rec2mp4
recognizes that stem: the streak lands in the output filename
(`... vs SAILOR MAXWELL (streak 7).mp4`), in the panel header
(`Streak 7`) and in the JSON sidecar (`"streak": 7`). The stem is checked
against the record itself; on a mismatch a warning is printed and the
record is trusted. Old-format stems (`GUYA_27-07-2026_10-40.rec`) behave
exactly as before. If a `<same stem>.txt` info file sits next to the
`.rec` (the sidecar PokeDNA v3.0.0 writes with every export), its lines are stored in the JSON
sidecar as `export_info` and the first few short lines are rendered in the
panel's `export` section.

### Where the videos go, and what they are called

Output is grouped **by facility** and each finished file carries its
**outcome**:

```
out/
  Battle Arena/
    GUYA_Arena-O-28_… - Battle Arena Open vs Frontier Brain (streak 28) [WON].mp4
  Battle Dome/
    GUYA_… - Battle Dome Open vs PARASOL LADY JULIANA [WON].mp4
```

The streak comes from PokeDNA's file name and is known before converting; the
**outcome is only knowable once the replay ends**, so the finished file is
renamed at that point (same folder, so the rename is atomic) and the JSON
sidecar — written afterwards — always records the final name. Turn either off
with `--no-facility-folders` / `--no-outcome-in-name`, or the matching
checkboxes in the GUI.

In the GUI, **streak** and **outcome** are columns of their own: the streak
fills in the moment a record is added, the outcome when it finishes.

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
python -m rec2mp4.designer   # the panel designer on its own
```

…or double-click an icon — see "Desktop icon" below for the macOS `.app` and
the Windows shortcut.

What it does:

* **Queue** — "Add .rec files…" (multi-select) or "Add folder…" (every
  `*.rec`, sorted, like the CLI); each record is validated + summarized with
  the pure-stdlib parser **the moment it is added** (no emulator involved),
  so invalid records are flagged red immediately. Duplicates are skipped,
  rows show facility / level mode / battle kind / opponent, and
  double-clicking a row opens a details window with the full record summary
  and, after a conversion, that record's complete log.
* **Settings pane** — mirrors the CLI options 1:1 (animations, text speed,
  scale, audio, panel side + per-section checkboxes — including the
  `trainer` and `moves` / `evs` / `ivs` stat sections — a **"Cycle stats
  every N seconds"** spinbox with per-page (`moves`/`evs`/`ivs`) checkboxes
  for the time-cycling panel, a **layout** field with **Design panel…**, a
  **Convert in parallel** checkbox + worker count, **Cards** spinboxes for the
  opening and end cards, plain names, JSON sidecar, output folder, ROM/save pickers prefilled with
  the `local/` defaults when those files exist).
* **Preview frames…** — replays only the first seconds of the selected
  record and opens a viewer of the real composited frames (arrow keys to
  page, save one or all). No video is encoded; see "Preview frames".
* **Design panel…** — opens the visual panel designer on the current layout,
  seeded with the selected record, your ROM and (after a preview) a real
  game frame to design against. "Use in conversion" saves the layout and
  fills the layout field.
* **Convert** — runs the batch on a worker thread, by default **one record
  per CPU in its own process**; per-row status (`waiting` / `converting` /
  `OK` / `TRUNC` / `FAILED`). **Live progress appears in each record's own
  Details cell** — with several conversions running at once, a single shared
  status line just flickered between them — and the bottom bar reports the
  batch (`3/10 done · 2 converting`). When a record finishes, its final
  result replaces the live text in the same cell. **Cancel** finishes the
  records in flight, then stops. **Cancel all** stops everything at once,
  including the conversions already running: the replays check for it every
  few emulated frames and the ffmpeg stages are polled rather than waited on,
  so a batch halts in about a second even mid-battle. Cancelled records drop
  their partial video and are reported `CANCELLED` — measured: 4 running
  records, all stopped 1.6 s after the click, nothing left behind. "Open
  output folder" opens Finder/Explorer on the output directory.

Converting still needs everything the CLI needs — the emulator stack from
"Setup" (vendored mGBA bindings + ffmpeg) plus **your own** ROM and a
post-game save; if the stack is missing, the GUI surfaces the same install
hint the CLI prints. Queueing and inspecting records works with nothing
installed at all. Honest status: the GUI is newly built and has been
exercised on macOS only (the pure-logic layer is covered by
`tests/test_gui.py` on all three CI OSes; the widget smoke test runs where
a display exists). Screenshots are deliberately not included.

## Desktop icon — double-click to launch

Both platforms get a proper clickable icon. The artwork is **generated**, not
stored as a blob: `tools/make_icons.py` draws it at every size natively (a 16 px
taskbar icon is drawn as a 16 px icon, not squashed down from 1024), and writes
`assets/icon.png`, a multi-size `assets/rec2mp4.ico` and, on macOS,
`assets/rec2mp4.icns`. It is deliberately original — a film-sprocketed screen
with a gold play triangle and a red REC dot — so it carries no game-derived
shape or colour.

```bash
python tools/make_icons.py          # regenerate the icons (needs Pillow)
```

**macOS** — build a real `.app`, then double-click it (or drag it to
`/Applications`, or keep it in the Dock):

```bash
# run this with the env that has the stack, so the app launches THAT python
/path/to/rec2mp4-env/bin/python tools/make_macos_app.py      # -> dist/rec2mp4.app
open dist/rec2mp4.app
```

**Windows** — make Desktop + Start Menu shortcuts carrying the icon:

```powershell
powershell -ExecutionPolicy Bypass -File tools\make_windows_shortcut.ps1
# or: ... -Python C:\path\to\pythonw.exe
```

`tools\rec2mp4-gui.cmd` is a plain double-clickable alternative inside the
repo (a `.cmd` cannot carry its own icon, so the shortcut is the nicer route).

**What these are, honestly:** *launchers*, not frozen distributables. They start
the rec2mp4 in your working copy using an interpreter that has Pillow, the
vendored mGBA bindings and ffmpeg — the same requirements as running it from a
terminal. Both bake in the interpreter and the repo path at build time, and
both can be overridden at launch (`REC2MP4_PYTHON`, `REC2MP4_HOME` on macOS;
`-Python` / `REC2MP4_PYTHON` on Windows). If the interpreter has moved, the
macOS app says so in a dialog instead of failing silently.

A **self-contained** app (no Python needed) is a bigger job and is not built:
PyInstaller/py2app would have to bundle `vendor/mgba`'s native bindings *and* an
ffmpeg binary, and the frozen entry point must call
`multiprocessing.freeze_support()` or `--jobs > 1` spawns new copies of the app
instead of workers. That call is already in both entry points, so the door is
open.

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
