# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""Per-record conversion pipeline — the engine behind the CLI (and any GUI).

Public API:

    settings = ConvertSettings(rom=..., sav=..., outdir=..., panel="right")
    ctx = load_context(settings)          # once per batch: ROM/sav bytes,
                                          # module imports, Pillow probe
    result = convert_one(path, settings, log=print, ctx=ctx)
    # result: {"status": "OK"|"TRUNC"|"INVALID"|"FAILED", "output": ...,
    #          "frames": ..., "seconds": ..., "detail": ..., "error": ...}

convert_one validates -> parses -> (injects -> replays -> encodes ->
composites the side panel) exactly like the CLI always did; the CLI is now
argument parsing + a loop + summary printing over these calls. All
human-readable progress goes through `log` (stdout-ish) and `err`
(stderr-ish) callables so a GUI can capture it; with the defaults the
printed output is byte-identical to the pre-refactor CLI.

Also here: the rich output naming + JSON sidecar helpers (moved from
__main__), and the parser for PokeDNA's streak-aware export filename stem
'<PLAYER>_<Facility>-<O|50>-<streak>_<rest>' plus its optional '<stem>.txt'
info sidecar.
"""

from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__, layout as layout_mod, rec, romdata

# Where a panel can sit, in CLI/GUI presentation order. right/left keep the
# classic flow renderer; top/bottom are bands and always go through a layout.
PANEL_SIDES = ("right", "left", "top", "bottom")

# Default paths resolve relative to the repo root (= two parents up from
# this file: <root>/rec2mp4/pipeline.py). User-supplied paths are taken
# as-is (absolute, or relative to the current directory).
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROM = REPO_ROOT / "local" / "rom.gba"
DEFAULT_SAV = REPO_ROOT / "local" / "template.sav"
DEFAULT_OUTDIR = REPO_ROOT / "out"

# mGBA's framebuffer is 4 bytes/pixel in R,G,B,X order -> ffmpeg "rgb0"
# (verified in docs/research/emulator-stack.md).
DEFAULT_PIX_FMT = "rgb0"

# Side panel geometry: half the game width, full game height (both sides
# of the hstack must share a height; 120/160 keep every dimension even).
PANEL_WIDTH_UNITS = 120
PANEL_HEIGHT_UNITS = 160


class PipelineError(Exception):
    """A batch-level precondition failed (bad ROM/save/stack/options)."""


class ConversionCancelled(Exception):
    """The caller asked to stop this conversion (Cancel all)."""


def _default_err(msg: object) -> None:
    print(msg, file=sys.stderr)


# The interpreter that has the whole stack (Pillow + vendored mGBA + ffmpeg)
# — named in every "install/relaunch here" message so the user can copy it.
# REC2MP4_PYTHON overrides it (e.g. a dedicated conda env); otherwise it is
# whichever Python is running rec2mp4 right now.
CONDA_PYTHON = os.environ.get("REC2MP4_PYTHON") or sys.executable


def pillow_available() -> bool:
    """True if Pillow can be imported in THIS interpreter.

    The single side-panel probe, shared by load_context (which degrades the
    panel when it is False) and any front-end that wants to warn *before*
    converting instead of silently dropping the panel. No import side effects
    beyond Pillow's own.
    """
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def pillow_hint(interpreter: str | None = None) -> str:
    """One actionable sentence naming this interpreter + how to get Pillow."""
    py = interpreter or sys.executable or "this Python"
    return (
        f"the info panel needs Pillow, which {py} does not have. "
        f"Install it here:  {py} -m pip install pillow  — or relaunch the GUI "
        f"with the rec2mp4 conda env:  {CONDA_PYTHON} -m rec2mp4.gui  — "
        "or set the side panel to 'off'.")


def stack_status() -> dict:
    """Probe THIS interpreter for the optional heavy deps a conversion needs.

    Pure detection, no exceptions escape: front-ends call it at launch to tell
    the user (loudly) when they are running under a Python where a full
    conversion cannot produce the panel (Pillow) or any video (mGBA bindings /
    ffmpeg). `ok` is True only when everything a default conversion uses is
    present.
    """
    status = {"interpreter": sys.executable, "pillow": pillow_available(),
              "emulator": False, "ffmpeg": False, "errors": {}}
    try:
        from . import driver as _driver  # noqa: F401
        from . import video as _video
        status["emulator"] = True
        try:
            _video._find_ffmpeg()
            status["ffmpeg"] = True
        except Exception as exc:                      # ffmpeg not on PATH
            status["errors"]["ffmpeg"] = str(exc)
    except Exception as exc:                          # bindings missing
        status["errors"]["emulator"] = str(exc)
    status["ok"] = (status["pillow"] and status["emulator"]
                    and status["ffmpeg"])
    return status


# ---------------------------------------------------------------------------
# Settings + per-batch context
# ---------------------------------------------------------------------------

@dataclass
class ConvertSettings:
    """Everything a conversion needs, CLI- and GUI-agnostic.

    Path fields left as None fall back to the repo defaults (DEFAULT_ROM /
    DEFAULT_SAV / DEFAULT_OUTDIR).
    """
    rom: str | Path | None = None
    sav: str | Path | None = None
    outdir: str | Path | None = None
    scale: int = 4
    audio: bool = True
    anims: str = "on"                 # on | off | record
    text_speed: str = "record"        # slow | mid | fast | record
    plain_names: bool = False
    sidecar: bool = True
    pix_fmt: str = DEFAULT_PIX_FMT
    max_seconds: float = 1800
    headed: bool = False
    panel: str = "right"              # right | left | top | bottom | off
    panel_info: str = "all"           # CSV of panel.PANEL_SECTIONS
    pov: str = "player"               # player | opponent (experimental)
    panel_cycle: float = 0.0          # seconds per stat page; 0 = static panel
    panel_cycle_pages: tuple = ()     # which of moves/evs/ivs to cycle
                                      # ((): all three when panel_cycle > 0)
    # A designed layout (rec2mp4.layout): a path to a .json, a layout dict, or
    # a Layout object. When set it REPLACES the classic flow panel — the
    # layout itself says which sides carry panels and where every block sits.
    layout: Any = None
    # Batch parallelism: how many records to convert at once, each in its own
    # process (the emulator is single-threaded, so one record per CPU is the
    # win). 0 = auto (one per CPU, capped by the batch size); 1 = sequential.
    jobs: int = 0
    # Seconds of trainer-state end card held after the battle (the PokeDNA
    # '<stem>.txt' state.* block: playtime, Pokedex, BP, Frontier symbols).
    # 0 disables it; with no sidecar there is nothing to draw and the stage
    # is skipped either way. See docs/REC-SIDECAR.md.
    end_card: float = 3.0
    # Seconds of opening card held BEFORE the battle, carrying the opponent's
    # pre-battle line — read from the user's ROM by opponent id, because the
    # record replays only the battle itself and never that line. 0 disables
    # it; an opponent with no ROM speech (record-mix friend / apprentice) gets
    # no card.
    intro_card: float = 3.0
    # Threads each ffmpeg may use. 0 = let ffmpeg decide, which is right for a
    # single conversion and catastrophic for a parallel batch: x264 picks
    # ~1.5x the core count (55 threads on a 12-core Mac), so N workers ask for
    # 55*N threads and the machine spends its time context-switching instead
    # of encoding. convert_batch sets this to cpu_count // jobs.
    encoder_threads: int = 0
    # Put each video in a subfolder named after its facility
    # (out/Battle Arena/..., out/Battle Dome/...).
    facility_folders: bool = True
    # Append the battle's outcome to the file name. The outcome is only known
    # AFTER the replay, so the finished file is renamed at the end — which is
    # also why the name can carry it at all.
    outcome_in_name: bool = True


@dataclass
class ConvertContext:
    """Once-per-batch work: bytes read, modules imported, options checked."""
    rom_path: Path
    sav_path: Path
    outdir: Path
    rom_bytes: bytes
    rom_crc32: int
    sav_bytes: bytes
    driver_mod: Any
    video_mod: Any
    writer_kwargs: dict
    writer_accepts_log: bool
    panel_enabled: bool
    panel_sections: tuple
    panel_cycle: float = 0.0
    panel_cycle_pages: tuple = ()
    # Resolved rec2mp4.layout.Layout when the video uses designed panels;
    # None means the classic single-side flow panel (settings.panel).
    layout: Any = None
    used_out_paths: set = field(default_factory=set)


def _writer_accepts_kwarg(mp4writer_cls, kwarg: str) -> bool:
    """True if Mp4Writer.__init__ takes `kwarg` (or **kwargs)."""
    try:
        params = inspect.signature(mp4writer_cls.__init__).parameters
    except (TypeError, ValueError):        # C-implemented / no signature
        return True
    return (kwarg in params
            or any(prm.kind is inspect.Parameter.VAR_KEYWORD
                   for prm in params.values()))


def load_context(settings: ConvertSettings, log: Callable = print,
                 err: Callable | None = None) -> ConvertContext:
    """Do the once-per-batch work; raises PipelineError on any bad input.

    Reads the ROM ONCE (naming + the panel resolve display names from these
    bytes at runtime — no name tables ship with the tool — and the sidecar
    records the ROM's crc32), reads and sanity-checks the save, imports the
    emulator/encoder stack, validates the panel options and probes Pillow.
    """
    if err is None:
        err = _default_err
    rom_path = Path(settings.rom) if settings.rom else DEFAULT_ROM
    sav_path = Path(settings.sav) if settings.sav else DEFAULT_SAV
    outdir = Path(settings.outdir) if settings.outdir else DEFAULT_OUTDIR

    if not rom_path.is_file():
        raise PipelineError(
            f"ROM not found: {rom_path}\n"
            "  supply your own US Emerald ROM with --rom "
            "(never distributed with this tool)")
    if not sav_path.is_file():
        raise PipelineError(
            f"save not found: {sav_path}\n"
            "  supply a 128 KiB Emerald save with --sav")
    rom_bytes = rom_path.read_bytes()
    rom_crc32 = zlib.crc32(rom_bytes) & 0xFFFFFFFF
    sav_bytes = sav_path.read_bytes()
    if len(sav_bytes) < rec.SAV_MIN_SIZE:
        raise PipelineError(
            f"{sav_path} is {len(sav_bytes)} bytes — a full "
            f"128 KiB (0x{rec.SAV_MIN_SIZE:X}-byte) .sav is required "
            "(64 KiB dumps have no sector 31)")
    if sav_bytes == b"\xff" * len(sav_bytes):
        raise PipelineError(
            f"{sav_path} is blank (every byte 0xFF — an "
            "erased-flash dump, no save data).\n"
            "  The replay path needs a real post-game save with the "
            "Frontier Pass; see README 'Requirements'\n"
            "  (e.g. --sav local/alt-saves/all-shiny.sav).")

    try:
        from . import driver as driver_mod
        from . import video as video_mod
    except ImportError as exc:
        raise PipelineError(
            f"emulator/encoder stack unavailable ({exc})\n"
            "  Run the setup in README 'Setup (macOS)' — the mGBA "
            "bindings live in vendor/ (fetched, not committed);\n"
            "  details in docs/research/emulator-stack.md. "
            "--info-only works without any of this.") from exc

    writer_kwargs = {"scale": settings.scale, "audio": settings.audio}
    if _writer_accepts_kwarg(video_mod.Mp4Writer, "pix_fmt"):
        writer_kwargs["pix_fmt"] = settings.pix_fmt
    elif settings.pix_fmt != DEFAULT_PIX_FMT:
        raise PipelineError("this Mp4Writer does not accept --pix-fmt")
    if settings.encoder_threads and _writer_accepts_kwarg(video_mod.Mp4Writer,
                                                          "threads"):
        writer_kwargs["threads"] = int(settings.encoder_threads)
    writer_accepts_log = _writer_accepts_kwarg(video_mod.Mp4Writer, "log")

    # --- panel options (validated even when disabled -> early CLI errors)
    if settings.panel not in PANEL_SIDES + ("off",):
        raise PipelineError(
            "--panel must be %s or off (got %r)"
            % (", ".join(PANEL_SIDES), settings.panel))
    from . import panel as panel_mod       # PIL-free import
    try:
        panel_sections = panel_mod.parse_panel_info(settings.panel_info)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc
    # --- stat-cycling options (validated even when the panel is off) ------
    try:
        panel_cycle = float(settings.panel_cycle or 0.0)
    except (TypeError, ValueError):
        raise PipelineError(
            f"--panel-cycle must be a number of seconds "
            f"(got {settings.panel_cycle!r})")
    if panel_cycle < 0:
        raise PipelineError(
            f"--panel-cycle must be >= 0 (got {panel_cycle})")
    try:
        panel_cycle_pages = panel_mod.parse_cycle_pages(
            settings.panel_cycle_pages)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc
    # --- designed layout (optional). A layout REPLACES the flow panel: it
    # carries its own sides, sizes, backgrounds and block rectangles. The
    # top/bottom bands are only reachable this way (the flow renderer draws a
    # tall column), so --panel top|bottom auto-generates a default layout.
    layout_obj = None
    try:
        layout_obj = resolve_layout(settings, panel_sections)
    except layout_mod.LayoutError as exc:
        raise PipelineError(f"bad panel layout: {exc}") from exc

    panel_enabled = settings.panel != "off"
    if panel_enabled and not pillow_available():
        panel_enabled = False
        err("warning: side panel disabled — " + pillow_hint()
            + " Videos will be written without the info panel.")
    if panel_enabled and layout_obj is not None:
        log(f"panel layout '{layout_obj.name}': {layout_obj.describe()}")

    outdir.mkdir(parents=True, exist_ok=True)

    return ConvertContext(
        rom_path=rom_path, sav_path=sav_path, outdir=outdir,
        rom_bytes=rom_bytes, rom_crc32=rom_crc32, sav_bytes=sav_bytes,
        driver_mod=driver_mod, video_mod=video_mod,
        writer_kwargs=writer_kwargs, writer_accepts_log=writer_accepts_log,
        panel_enabled=panel_enabled, panel_sections=panel_sections,
        panel_cycle=panel_cycle, panel_cycle_pages=panel_cycle_pages,
        layout=layout_obj)


def resolve_layout(settings: ConvertSettings, panel_sections=None):
    """settings -> a rec2mp4.layout.Layout, or None for the classic panel.

    * `settings.layout` set (path / dict / Layout) wins — unless the panel is
      switched off entirely.
    * `--panel top|bottom` has no flow-renderer equivalent, so it becomes a
      generated default layout for that side.
    * `--panel right|left` (the default) stays on the classic flow renderer.

    Raises layout.LayoutError on an unreadable/invalid layout.
    """
    if settings.panel == "off":
        return None
    src = settings.layout
    if src is None or src == "":
        # DEFAULT since 2026-08-08: the block layout, not the old flow panel.
        # The flow panel packed every section into one column and shrank the
        # moves/EV/IV rows until they were unreadable (and truncated the tail
        # outright on a 3v3). Blocks give each section its own box, so the
        # stat pages are legible and complete. '--layout classic' brings the
        # old renderer back.
        return layout_mod.default_layout(
            settings.panel if settings.panel in layout_mod.SIDES else "right",
            sections=panel_sections)
    if str(src).strip().lower() == "classic":
        return None                       # the original flow renderer
    if isinstance(src, layout_mod.Layout):
        lay = src
    elif isinstance(src, dict):
        lay = layout_mod.Layout.from_dict(src)
    elif str(src).strip().lower() == "default":
        # '--layout default': the generated block layout for the chosen side,
        # with no file to manage. Every section gets its own box, so a full
        # 3v3's moves/EVs/IVs all fit instead of overflowing one column.
        lay = layout_mod.default_layout(
            settings.panel if settings.panel in layout_mod.SIDES else "right",
            sections=panel_sections)
    else:
        lay = layout_mod.Layout.load(src)
    if not lay.panels:
        raise layout_mod.LayoutError(
            f"layout '{lay.name}' has no panels — nothing would be drawn "
            "(use --panel off for a plain game video)")
    return lay


# ---------------------------------------------------------------------------
# Rich output naming + JSON sidecar (moved verbatim from __main__)
# ---------------------------------------------------------------------------

_WINDOWS_BAD_CHARS = set('<>:"/\\|?*')

# Windows reserves these device names even WITH an extension appended
# ('CON.mp4' resolves to the console device), so a reserved stem must be
# defused before '.mp4'/'.json' is added.
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str) -> str:
    """ASCII, Windows-safe file name: strip <>:\"/\\|?*, control chars and
    non-ASCII, collapse whitespace, drop trailing dots/spaces, and defuse
    Windows-reserved device names (CON, NUL, COM1, ...) with a '_' prefix."""
    kept = [ch for ch in name
            if ch not in _WINDOWS_BAD_CHARS and 0x20 <= ord(ch) <= 0x7E]
    name = " ".join("".join(kept).split()).rstrip(" .")
    if name.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
        name = "_" + name
    return name


def opponent_label(info: dict, which: str,
                   rom_bytes: bytes | None) -> str | None:
    """Display name for opponent 'a' or 'b' of a rec.parse() dict.

    ROM frontier trainers (id 0..299) are resolved to '<CLASS> <NAME>' from
    the user's own ROM; every doubt falls back to a stable descriptive label.
    """
    kind = info.get(f"opponent_{which}_kind")
    if kind is None:
        return None
    opp_id = info.get(f"opponent_{which}", 0)
    if kind == "frontier":
        name = (romdata.frontier_trainer_name(rom_bytes, opp_id)
                if rom_bytes else None)
        return name or f"frontier trainer {opp_id}"
    if kind == "frontier_brain":
        return "Frontier Brain"
    if kind == "record_mix_friend":
        # rec.parse() renders 'NAME (record-mix friend, LANG)' — keep NAME.
        # A name that is nothing but '?' (undecodable glyphs, e.g. a
        # Japanese friend name) is as good as unnamed — and sanitize would
        # strip it to nothing anyway ('?' is a Windows-reserved char).
        raw = (info.get(f"opponent_{which}_name") or "").split(" (")[0].strip()
        return raw if raw and raw.strip("?") else "record-mix friend"
    if kind == "apprentice":
        # rec.parse() renders 'Apprentice #N' -> 'Apprentice N'.
        return (info.get(f"opponent_{which}_name") or
                f"Apprentice {opp_id}").replace("#", "")
    return f"trainer {opp_id}"


def opponent_speech(info: dict, rom_bytes: bytes | None,
                    which: str = "before") -> list[str] | None:
    """What the opponent says before/after the battle, from the USER'S ROM.

    The record replays the battle only — it starts at the engine's
    "<TRAINER> would like to battle!" and never carries the Frontier
    trainer's own line, which the facility shows around the battle. But the
    line IS in the ROM, keyed by the opponent id the record does carry, so
    an opening card can show the real words.

    None for anything that is not a ROM frontier trainer: a record-mix
    friend or apprentice keeps their greeting in the SAVE (not in the .rec
    and not in the ROM), and a Frontier Brain's dialogue is scripted
    elsewhere.
    """
    if not rom_bytes or info.get("opponent_a_kind") != "frontier":
        return None
    return romdata.frontier_trainer_speech(rom_bytes,
                                           info.get("opponent_a", -1), which)


POV_TAG = " [opponent POV]"


def pov_faithful(info: dict) -> bool:
    """True when an opponent-POV flip of this record would be FAITHFUL.

    Only genuine link records (battleFlags already carrying RECORDED_LINK)
    replay correctly from the other side — both lanes were human-recorded and
    no AI runs at playback. Frontier vs-AI records (the ones this project
    handles) are a "what-if": the replay diverges after ~turn 1. See
    docs/research/opponent-pov.md §1.5.
    """
    return bool(info.get("is_link_recorded"))


def pov_note(info: dict) -> str:
    """Human caveat string recorded in the sidecar for opponent-POV outputs."""
    if pov_faithful(info):
        return ("Genuine link record: the opponent-side view is faithful — "
                "both sides' inputs were human-recorded, so the replay "
                "matches the real battle.")
    return ("What-if view: this is a vs-AI Frontier record, so the "
            "opponent-side replay is NOT the battle as it happened. The "
            "opponent AI's moves are re-decided and diverge after ~turn 1; "
            "the video may end early via the engine's clean teleport-quit "
            "fade. See docs/research/opponent-pov.md.")


# How an outcome reads in a file name. The record's outcome is from the
# RECORDER's point of view, so "WON" means the person who saved the record won.
OUTCOME_TAGS = {"won": "WON", "lost": "LOST", "draw": "DRAW"}


def outcome_tag(outcome_text: str | None) -> str | None:
    """'won' -> 'WON'; None for anything unresolved, so the name stays clean."""
    if not outcome_text:
        return None
    text = str(outcome_text).strip().lower()
    if text in ("", "unknown"):
        return None
    return OUTCOME_TAGS.get(text, text.upper().replace(" ", "-"))


def facility_folder(info: dict) -> str:
    """Subfolder name for a record's facility ('Battle Arena'), or ''."""
    name = sanitize_filename(str(info.get("facility") or "").strip())
    return name or "Unknown facility"


def output_dir_for(outdir: Path, info: dict,
                   settings: ConvertSettings) -> Path:
    """Where this record's video belongs, honouring --no-facility-folders."""
    if not getattr(settings, "facility_folders", False):
        return Path(outdir)
    return Path(outdir) / facility_folder(info)


def build_output_basename(info: dict, stem: str,
                          rom_bytes: bytes | None,
                          plain: bool = False,
                          streak: int | None = None,
                          pov: str = "player",
                          outcome: str | None = None) -> str:
    """'<stem> - <Facility> <Open|Lv50>[ <kind>] vs <Opp>[ and <OppB>]
    [ (streak N)][ [opponent POV]]'.

    Windows-safe ASCII, no extension. plain=True keeps just the stem
    (streak included only in rich mode); the opponent-POV tag is appended in
    both modes. Capped at 180 chars so '<base> (NN).mp4'/'.json' and
    Mp4Writer's temp file stay under every OS's 255-byte per-name limit.
    """
    tag = POV_TAG if pov == "opponent" else ""
    out_tag = outcome_tag(outcome)
    if out_tag:
        tag = " [%s]" % out_tag + tag
    if plain:
        # Reserve room for the tag BEFORE the cap so a long stem can never
        # slice the honest " [opponent POV]" suffix off the end.
        base = sanitize_filename(stem)[:180 - len(tag)].rstrip(" .")
        return (base + tag) or "record"
    level = "Open" if info.get("level_mode") == "Open Level" else "Lv50"
    kind = ""
    for token, key in (("multi", "is_multi"),
                       ("two-opponents", "is_two_opponents"),
                       ("double", "is_double"),
                       ("link", "is_link_recorded")):
        if info.get(key):
            kind = " " + token
            break
    opp = opponent_label(info, "a", rom_bytes) or "unknown opponent"
    opp_b = opponent_label(info, "b", rom_bytes)
    if opp_b:
        opp += f" and {opp_b}"
    base = f"{stem} - {info.get('facility', '?')} {level}{kind} vs {opp}"
    if streak is not None:
        base += f" (streak {streak})"
    # Reserve room for the tag BEFORE the cap so a long stem/opponent name
    # can never slice the honest " [opponent POV]" suffix off the end.
    base = sanitize_filename(base)[:180 - len(tag)].rstrip(" .")
    return (base + tag) or "record"


def resolve_output_path(outdir: Path, base: str, source_rec_name: str,
                        used: set[str]) -> Path:
    """Collision-safe '<base>.mp4' path inside outdir.

    Re-converting the SAME record overwrites its own output (the sidecar
    next to an existing file names its source .rec). A file produced from a
    DIFFERENT record — or claimed earlier in this batch — bumps to
    '<base> (2)', '<base> (3)', ...
    """
    n = 1
    while True:
        cand = base if n == 1 else f"{base} ({n})"
        n += 1
        path = outdir / (cand + ".mp4")
        key = str(path).lower()
        if key in used:
            continue                        # claimed by this batch already
        if path.exists():
            sidecar = path.with_suffix(".json")
            if sidecar.is_file():
                try:
                    prev_src = json.loads(
                        sidecar.read_text(encoding="utf-8")).get("source_rec")
                except (OSError, ValueError):
                    prev_src = None
                if prev_src is not None and prev_src != source_rec_name:
                    continue                # someone else's output — keep it
            # No/unreadable sidecar, or same source record: the basename
            # embeds this record's stem, so overwriting is a re-run.
        used.add(key)
        return path


def build_sidecar(*, source_rec_name: str, rec_bytes: bytes, info: dict,
                  rom_crc32: int, options: dict, result,
                  output_name: str, streak: int | None = None,
                  export_info: list[str] | None = None,
                  pov_meta: dict | None = None,
                  trainer_state: dict | None = None) -> dict:
    """All the data we know about one conversion, JSON-serializable."""
    sc = {
        "rec2mp4_version": __version__,
        "generated_at": datetime.now(timezone.utc)
                        .isoformat(timespec="seconds"),
        "source_rec": source_rec_name,
        "source_rec_sha1": hashlib.sha1(rec_bytes).hexdigest(),
        "rom_crc32": f"{rom_crc32 & 0xFFFFFFFF:08x}",
        "output": output_name,
        "options": options,
        "replay": {
            "frames": result.frames,
            "seconds": round(result.seconds, 3),
            "end_reason": result.end_reason,
            "outcome": getattr(result, "outcome", 0),
            "outcome_text": getattr(result, "outcome_text", "unknown"),
        },
        "record": info,
    }
    # Streak-aware extras only when present, so pre-streak inputs keep
    # producing byte-identical sidecars.
    if streak is not None:
        sc["streak"] = streak
    if export_info is not None:
        sc["export_info"] = export_info
    # The save state the record came out of (PokeDNA's state.* block). Only
    # the keys that were actually present — never a substituted zero.
    if trainer_state:
        sc["trainer_state"] = trainer_state
    # Opponent-POV outputs record the mode + an honest faithfulness verdict
    # and caveat; player-POV (default) sidecars stay byte-identical.
    if pov_meta is not None:
        sc.update(pov_meta)
    return sc


# ---------------------------------------------------------------------------
# PokeDNA streak-aware export stems: <PLAYER>_<Facility>-<O|50>-<streak>_<rest>
# e.g. GUYA_Factory-50-7_27-07-2026_10-40  (old format: GUYA_27-07-2026_10-40)
# ---------------------------------------------------------------------------

# Facility word -> rec.FACILITY index ("Battle Tower" ... "Battle Pyramid").
_FACILITY_WORDS = {name.split()[-1].lower(): i
                   for i, name in enumerate(rec.FACILITY)}

_EXPORT_STEM_RE = re.compile(
    r"^(?P<player>[A-Za-z0-9.'\- ]{1,8})"
    r"_(?P<facility>[A-Za-z]+)-(?P<mode>O|50)-(?P<streak>\d{1,5})"
    r"_(?P<rest>.+)$")


def parse_export_stem(stem) -> dict | None:
    """Parse PokeDNA's streak-aware export filename stem, or None.

    New format: '<PLAYER>_<Facility>-<O|50>-<streak>_<rest>' where Facility
    is a real facility word (Tower/Dome/Palace/Arena/Factory/Pike/Pyramid,
    any case), 'O' = Open Level, '50' = Level 50. Anything else — the old
    '<PLAYER>_<date>_<time>' stems, junk, unknown facility words — is None.
    """
    if not isinstance(stem, str):
        return None
    m = _EXPORT_STEM_RE.match(stem)
    if not m:
        return None
    facility_id = _FACILITY_WORDS.get(m["facility"].lower())
    if facility_id is None:
        return None                     # not a facility word -> not the format
    return {
        "player": m["player"],
        "facility_word": m["facility"],
        "facility_id": facility_id,
        "level_mode": "Open Level" if m["mode"] == "O" else "Level 50",
        "streak": int(m["streak"]),
        "rest": m["rest"],
    }


def check_export_stem(parsed: dict, info: dict) -> str | None:
    """Consistency of a parsed export stem vs the record itself.

    Returns a warning string on mismatch (the record is trusted; the
    filename may have been renamed/mangled), None when consistent.
    """
    problems = []
    if parsed.get("facility_id") != info.get("facility_id"):
        problems.append(f"filename says {parsed.get('facility_word')} but "
                        f"the record is {info.get('facility', '?')}")
    if parsed.get("level_mode") != info.get("level_mode"):
        problems.append(f"filename says {parsed.get('level_mode')} but "
                        f"the record is {info.get('level_mode', '?')}")
    if not problems:
        return None
    return "; ".join(problems) + " — trusting the record"


# --- the machine-readable 'state.*' block of PokeDNA's .txt sidecar --------
# Spec: docs/REC-SIDECAR.md. The save state the record came out of — playtime,
# Pokedex, Battle Points, Frontier symbols — none of which is in the .rec.
# Consumer rules from that doc, all enforced below:
#   * every key is optional (a Ruby record has no symbols at all),
#   * never substitute a zero for an absent key,
#   * unknown 'state.*' keys are ignored, not fatal,
#   * the prose above the block is not parsed.

# Frontier Pass order — the 7 characters of state.symbols map to these.
FACILITY_SYMBOLS = ("Tower", "Dome", "Palace", "Arena", "Factory", "Pike",
                    "Pyramid")

_STATE_INT_KEYS = ("dex_seen", "dex_caught", "bp", "bp_card",
                   "symbols_silver", "symbols_gold")
_PLAYTIME_RE = re.compile(r"^\s*(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s\s*$")


def parse_state_sidecar(lines) -> dict:
    """'state.<key>: <value>' lines -> a dict of the ones we understand.

    Returns {} when the block is absent. Values are typed: ints for the
    numeric keys, str for playtime/symbols, plus a derived 'playtime_hours'
    and 'symbols_list' ([('Tower', 'none'|'silver'|'gold'), ...]) when the
    inputs are well formed. A malformed value is dropped rather than
    guessed — "no key" and "zero" must stay distinguishable.
    """
    out: dict = {}
    for raw in lines or []:
        text = str(raw).strip()
        if not text.startswith("state."):
            continue
        key, _, value = text[len("state."):].partition(":")
        key, value = key.strip().lower(), value.strip()
        if not key or not value:
            continue
        if key in _STATE_INT_KEYS:
            try:
                out[key] = int(value)
            except ValueError:
                continue
        elif key == "playtime":
            out["playtime"] = value
            m = _PLAYTIME_RE.match(value)
            if m:
                out["playtime_hours"] = int(m.group(1))
                out["playtime_seconds"] = (int(m.group(1)) * 3600
                                           + int(m.group(2)) * 60
                                           + int(m.group(3)))
        elif key == "symbols":
            if len(value) == len(FACILITY_SYMBOLS) and \
                    all(ch in "-sG" for ch in value):
                out["symbols"] = value
                out["symbols_list"] = [
                    (name, {"-": "none", "s": "silver", "G": "gold"}[ch])
                    for name, ch in zip(FACILITY_SYMBOLS, value)]
        # unknown state.* keys: ignored on purpose (the block will grow)
    return out


def read_export_txt(rec_path: Path) -> list[str] | None:
    """Lines of PokeDNA's future '<same stem>.txt' info sidecar, or None.

    Tolerant by design (the format is not final): utf-8 with replacement,
    trailing whitespace stripped, trailing blank lines dropped, capped at
    200 lines / 300 chars each so a mis-pointed huge file cannot balloon
    the JSON sidecar.
    """
    txt_path = Path(rec_path).with_suffix(".txt")
    try:
        if not txt_path.is_file():
            return None
        raw = txt_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = [ln.rstrip()[:300] for ln in raw.splitlines()[:200]]
    while lines and not lines[-1]:
        lines.pop()
    return lines


# ---------------------------------------------------------------------------
# Side-panel compositing (post-capture; the video pipeline is untouched
# during the replay — the panel is stacked on at finalize time)
# ---------------------------------------------------------------------------

def _panel_extras(info: dict, settings: ConvertSettings,
                  ctx: ConvertContext, replay_result,
                  streak: int | None,
                  export_info: list[str] | None) -> dict:
    """The `extras` dict shared by the static PNG and the cycling pages."""
    # The 'state.*' block is data for the trainer section/end card; the prose
    # above it is what the 'export' section shows. Never mix the two.
    lines = list(export_info or [])
    return {
        "rom_bytes": ctx.rom_bytes,
        "speech": opponent_speech(info, ctx.rom_bytes),
        "outcome_text": getattr(replay_result, "outcome_text", "unknown"),
        "duration_seconds": getattr(replay_result, "seconds", None),
        "streak": streak,
        "state": parse_state_sidecar(lines),
        "export_lines": [ln for ln in lines if not ln.startswith("state.")],
        "sections": ctx.panel_sections,
        "opponent_a_label": opponent_label(info, "a", ctx.rom_bytes),
        "opponent_b_label": opponent_label(info, "b", ctx.rom_bytes),
        "pov": settings.pov,
        "pov_faithful": pov_faithful(info),
    }


def _panel_size(settings: ConvertSettings) -> tuple[int, int]:
    return (settings.scale * PANEL_WIDTH_UNITS,
            settings.scale * PANEL_HEIGHT_UNITS)


def composite_size(settings: ConvertSettings,
                   ctx: ConvertContext) -> tuple[int, int]:
    """Pixel size of the FINISHED frame (game + whatever panels it carries).

    The end card is drawn at exactly this size; composite_panels' ffmpeg
    output must match it.
    """
    scale = max(1, int(settings.scale))
    gw, gh = 240 * scale, 160 * scale
    if not ctx.panel_enabled or settings.panel == "off":
        return (gw, gh)
    if ctx.layout is not None:
        return ctx.layout.composite_size_px(scale)
    pw, ph = _panel_size(settings)
    return (gw + pw, gh)


def panel_inputs(info: dict, settings: ConvertSettings, ctx: ConvertContext,
                 replay_result, streak: int | None,
                 export_info: list[str] | None,
                 warn: Callable | None = None) -> list[dict]:
    """Render every panel this conversion needs -> compositor inputs.

    Returns a list of {"side", "size": (w, h), "pages": [png, ...]} in
    ffmpeg input order. One entry with a single page = a still panel; more
    pages = the time-cycling stat panel. Classic (flow) and designed
    (layout) panels both come out of here, so the compositor never has to
    care which renderer drew the pixels.
    """
    from . import panel as panel_mod
    extras = _panel_extras(info, settings, ctx, replay_result, streak,
                           export_info)
    cycle_on = ctx.panel_cycle > 0
    if ctx.layout is None:                       # classic flow panel
        size = _panel_size(settings)
        if cycle_on:
            pages = panel_mod.panel_pages(info, extras, size,
                                          ctx.panel_cycle_pages)
        else:
            pages = [panel_mod.render_panel(info, extras, size)]
        return [{"side": settings.panel, "size": size, "pages": pages}]

    lay = ctx.layout
    cycle_pages = panel_mod.parse_cycle_pages(ctx.panel_cycle_pages) \
        if cycle_on else ()
    inputs = []
    for spec in lay.ordered_panels():
        size = lay.panel_size_px(spec.side, settings.scale)
        specs = layout_mod.panel_page_specs(spec, cycle_pages) if cycle_on \
            else [spec]
        pages = panel_mod.render_layout_pages(info, extras, specs, size,
                                              warn=warn)
        inputs.append({"side": spec.side, "size": size, "pages": pages})
    return inputs


def composite_size(settings: ConvertSettings,
                   ctx: ConvertContext) -> tuple[int, int]:
    """Pixel size of the FINISHED frame (game + whatever panels it carries).

    The end card is drawn at exactly this size; composite_panels' ffmpeg
    output must match it.
    """
    scale = max(1, int(settings.scale))
    gw, gh = 240 * scale, 160 * scale
    if not ctx.panel_enabled or settings.panel == "off":
        return (gw, gh)
    if ctx.layout is not None:
        return ctx.layout.composite_size_px(scale)
    pw, ph = _panel_size(settings)
    return (gw + pw, gh)


def panel_inputs(info: dict, settings: ConvertSettings, ctx: ConvertContext,
                 replay_result, streak: int | None,
                 export_info: list[str] | None,
                 warn: Callable | None = None) -> list[dict]:
    """Render every panel this conversion needs -> compositor inputs.

    Returns a list of {"side", "size": (w, h), "pages": [png, ...]} in
    ffmpeg input order. One entry with a single page = a still panel; more
    pages = the time-cycling stat panel. Classic (flow) and designed
    (layout) panels both come out of here, so the compositor never has to
    care which renderer drew the pixels.
    """
    from . import panel as panel_mod
    extras = _panel_extras(info, settings, ctx, replay_result, streak,
                           export_info)
    cycle_on = ctx.panel_cycle > 0
    if ctx.layout is None:                       # classic flow panel
        size = _panel_size(settings)
        if cycle_on:
            pages = panel_mod.panel_pages(info, extras, size,
                                          ctx.panel_cycle_pages)
        else:
            pages = [panel_mod.render_panel(info, extras, size)]
        return [{"side": settings.panel, "size": size, "pages": pages}]

    lay = ctx.layout
    cycle_pages = panel_mod.parse_cycle_pages(ctx.panel_cycle_pages) \
        if cycle_on else ()
    inputs = []
    for spec in lay.ordered_panels():
        size = lay.panel_size_px(spec.side, settings.scale)
        specs = layout_mod.panel_page_specs(spec, cycle_pages) if cycle_on \
            else [spec]
        pages = panel_mod.render_layout_pages(info, extras, specs, size,
                                              warn=warn)
        inputs.append({"side": spec.side, "size": size, "pages": pages})
    return inputs


def build_filter_graph(inputs: list[dict], fps: str | None = None
                       ) -> tuple[str, str]:
    """(filter_complex, output label) for stacking panels around the game.

    Input 0 is always the game video; `inputs[i]` is ffmpeg input i+1, in the
    given order. Left/right are hstacked with the game first, then top/bottom
    are vstacked around that row — so the bands span the FULL composited
    width (a title bar / footer) while the columns match the game's height.

    shortest=1 on every stack makes the stack itself end when the game video
    ends: the panel inputs are infinite (a looped still) or longer (the
    cycling concat), and without it '-shortest' has nothing finite to bound
    against when the video has no audio track.
    """
    parts = []
    label = {}
    for i, inp in enumerate(inputs, start=1):
        w, h = int(inp["size"][0]), int(inp["size"][1])
        # fps= normalises the concat demuxer's variable-rate image stream to
        # the game's frame rate before the stack (a no-op for a looped still).
        rate = (",fps=%s" % fps) if fps else ""
        parts.append("[%d:v]scale=%d:%d%s,setsar=1[p%d]" % (i, w, h, rate, i))
        label[inp["side"]] = "[p%d]" % i

    row = ["[0:v]"]
    if "left" in label:
        row.insert(0, label["left"])
    if "right" in label:
        row.append(label["right"])
    if len(row) > 1:
        parts.append("%shstack=inputs=%d:shortest=1[row]"
                     % ("".join(row), len(row)))
        out = "[row]"
    else:
        out = "[0:v]"

    col = [out]
    if "top" in label:
        col.insert(0, label["top"])
    if "bottom" in label:
        col.append(label["bottom"])
    if len(col) > 1:
        parts.append("%svstack=inputs=%d:shortest=1[v]"
                     % ("".join(col), len(col)))
        out = "[v]"
    return ";".join(parts), out


def _concat_script(page_paths: list[str], per: float, slots: int) -> str:
    """ffconcat body cycling `page_paths`, `per` seconds each, `slots` slots.

    The LAST file must be repeated once more (a concat-demuxer quirk) or its
    duration is dropped.
    """
    n = len(page_paths)
    lines = ["ffconcat version 1.0"]
    for k in range(slots):
        lines.append("file '%s'" % page_paths[k % n].replace("'", r"'\''"))
        lines.append("duration %.4f" % per)
    lines.append("file '%s'" % page_paths[(slots - 1) % n]
                 .replace("'", r"'\''"))
    return "\n".join(lines) + "\n"


def _card_specs(intro_png, intro_seconds, card_png, card_seconds) -> list:
    """[(where, png, seconds), ...] for the cards that actually have content."""
    out = []
    if intro_png and float(intro_seconds or 0) > 0:
        out.append(("intro", intro_png, float(intro_seconds)))
    if card_png and float(card_seconds or 0) > 0:
        out.append(("end", card_png, float(card_seconds)))
    return out


def _source_has_audio(ffmpeg: str, video_path: str) -> bool:
    return bool(probe_video(ffmpeg, video_path).get("has_audio"))


def _add_cards(graph: str, out_label: str, cards: list, card_size, fps: str,
               next_index: int, out_dir: str, base: str) -> tuple:
    """Splice card segments onto the front/back of the composited video.

    Returns (extra ffmpeg input args, new filter graph, new output label,
    temp files to clean up). Cards join with the concat FILTER inside the
    pass that is already re-encoding every frame — a separate concat pass
    would re-encode the whole video again just to add a few seconds.
    """
    cmd: list[str] = []
    tmps: list[str] = []
    labels: dict[str, str] = {}
    cw, ch = (card_size or (0, 0))
    scale = ("scale=%d:%d," % (int(cw), int(ch))) if cw and ch else ""
    for n, (where, png, seconds) in enumerate(cards):
        idx = next_index + n
        fd, p = tempfile.mkstemp(prefix=f".{base}.{where}card.", suffix=".png",
                                 dir=out_dir)
        with os.fdopen(fd, "wb") as fh:
            fh.write(png)
        tmps.append(p)
        cmd += ["-loop", "1", "-framerate", fps, "-t", "%.3f" % seconds,
                "-i", p]
        graph += ";[%d:v]%ssetsar=1,fps=%s[%scard]" % (idx, scale, fps, where)
        labels[where] = "[%scard]" % where
    order = ([labels["intro"]] if "intro" in labels else [])
    order += [out_label]
    order += ([labels["end"]] if "end" in labels else [])
    graph += ";%sconcat=n=%d:v=1:a=0[vout]" % ("".join(order), len(order))
    return cmd, graph, "[vout]", tmps


def _audio_args(cards: list, has_audio: bool) -> tuple:
    """(filter-graph suffix, output args) for the game's audio track.

    Prepending a card shifts every game frame later by the intro's length;
    the audio has to move with it or the whole battle is out of sync. That
    means an adelay — and therefore re-encoding the audio, which is cheap
    next to the video and only happens when an intro card is in play. With
    no intro the audio is still stream-copied exactly as before.
    """
    if not has_audio:
        return ("", ["-map", "0:a?"])
    intro = next((s for w, _p, s in cards if w == "intro"), 0.0)
    if intro <= 0:
        return ("", ["-map", "0:a?", "-c:a", "copy"])
    return (";[0:a]adelay=%d:all=1[aout]" % int(round(intro * 1000)),
            ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"])


def run_ffmpeg(cmd: list, should_abort: Callable | None = None,
               poll: float = 0.2):
    """subprocess.run for ffmpeg, but interruptible.

    A conversion's ffmpeg stages are the part Cancel all could not reach
    while they ran under subprocess.run — the request just sat there until
    the encode finished. Polling instead lets the stage be torn down in a
    fraction of a second; ffmpeg only ever writes to a temp file that the
    caller's `finally` removes, so terminating it cannot damage anything.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    try:
        while True:
            try:
                stderr = proc.communicate(timeout=poll)[1]
                return proc.returncode, stderr
            except subprocess.TimeoutExpired:
                if should_abort is not None and should_abort():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise ConversionCancelled()
    finally:
        if proc.poll() is None:                        # never orphan ffmpeg
            proc.kill()
            proc.wait()


def _thread_args(threads: int) -> list:
    """ffmpeg thread caps, or nothing when threads is 0 (ffmpeg decides)."""
    n = max(0, int(threads or 0))
    if not n:
        return []
    return ["-threads", str(n), "-filter_threads", str(n),
            "-filter_complex_threads", str(n)]


def composite_panels(video_path: str, inputs: list[dict], video_mod,
                     seconds_per_page: float = 0.0,
                     game_seconds: float = 0.0,
                     card_png: bytes | None = None,
                     card_seconds: float = 0.0,
                     card_size: tuple | None = None,
                     intro_png: bytes | None = None,
                     intro_seconds: float = 0.0,
                     threads: int = 0,
                     should_abort: Callable | None = None,
                     log: Callable = print) -> None:
    """Stack every rendered panel around the finished game video, atomically.

    `inputs` is panel_inputs()'s list: one entry per side, each carrying its
    pixel size and 1..N page PNGs. A single page is looped as a still; several
    pages are held `seconds_per_page` each via the concat demuxer, looping for
    the whole video. Video is re-encoded (the geometry changes), audio is
    stream-copied, and the result only replaces `video_path` on success — any
    ffmpeg failure leaves the plain game video untouched.

    `card_png` (the trainer-state end card) is appended here rather than in a
    pass of its own: this stage already re-encodes every frame, and a separate
    concat pass would re-encode the whole video a SECOND time just to add a
    few seconds — measurably the most expensive thing the pipeline did.
    """
    if not inputs:
        raise RuntimeError("no panels to composite")
    ffmpeg = video_mod._find_ffmpeg()
    out_dir = os.path.dirname(video_path) or "."
    base = os.path.basename(video_path)
    fps = "%d/%d" % video_mod.GBA_FPS
    per = max(0.1, float(seconds_per_page or 0.0))

    tmp_paths: list[str] = []
    out_tmp = None
    cycling = False
    try:
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-i", video_path]
        for idx, inp in enumerate(inputs):
            page_paths = []
            for k, data in enumerate(inp["pages"]):
                fd, p = tempfile.mkstemp(
                    prefix=f".{base}.{inp['side']}{idx}p{k}.", suffix=".png",
                    dir=out_dir)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                tmp_paths.append(p)
                page_paths.append(p)
            if len(page_paths) > 1 and seconds_per_page > 0:
                cycling = True
                slots = int(max(0.0, game_seconds) / per) + 2
                fd, list_path = tempfile.mkstemp(
                    prefix=f".{base}.{inp['side']}{idx}.pages.",
                    suffix=".txt", dir=out_dir)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(_concat_script(page_paths, per, slots))
                tmp_paths.append(list_path)
                cmd += ["-f", "concat", "-safe", "0", "-i", list_path]
            else:
                cmd += ["-loop", "1", "-framerate", fps, "-i", page_paths[0]]

        graph, out_label = build_filter_graph(inputs, fps=fps)
        cards = _card_specs(intro_png, intro_seconds, card_png, card_seconds)
        audio = _source_has_audio(ffmpeg, video_path) if cards else False
        if cards:
            card_cmd, graph, out_label, tmps = _add_cards(
                graph, out_label, cards, card_size, fps, len(inputs) + 1,
                out_dir, base)
            cmd += card_cmd
            tmp_paths += tmps
        fd, out_tmp = tempfile.mkstemp(prefix=f".{base}.panelmux.",
                                       suffix=".mp4", dir=out_dir)
        os.close(fd)
        audio_graph, audio_args = _audio_args(cards, audio)
        cmd += _thread_args(threads)
        cmd += ["-filter_complex", graph + audio_graph, "-map", out_label]
        cmd += audio_args
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart"]
        # -shortest would end the file when the (shorter) audio track does,
        # cutting a silent card off; the stacks' shortest=1 already bounds
        # the looping panel inputs to the game video.
        if not cards:
            cmd += ["-shortest"]
        cmd += [out_tmp]
        code, err_bytes = run_ffmpeg(cmd, should_abort)
        if code != 0:
            tail = err_bytes.decode("utf-8", "replace").strip()[-2000:]
            raise RuntimeError(
                f"ffmpeg panel composite failed (exit {code}):\n"
                f"{tail or '(no ffmpeg stderr captured)'}")
        if cycling:
            # Durations must line up: the muxed output should track the game
            # (plus any card spliced on in the same pass).
            expect = game_seconds + sum(s for _w, _p, s in cards)
            dur = _probe_duration(ffmpeg, out_tmp)
            if dur is not None and expect > 0 \
                    and abs(dur - expect) > max(1.0, 0.1 * expect):
                raise RuntimeError(
                    f"cycling-panel output duration {dur:.2f}s does not match "
                    f"the expected {expect:.2f}s")
        os.replace(out_tmp, video_path)
        out_tmp = None
    finally:
        for p in tmp_paths + [out_tmp]:
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


def probe_video(ffmpeg: str, path: str) -> dict:
    """{'width','height','has_audio','sample_rate'} of a finished file.

    Read from the file itself rather than assumed from the settings, so the
    end card matches whatever actually got muxed.
    """
    ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
    if not os.path.isfile(ffprobe):
        ffprobe = "ffprobe"
    out = {"width": 0, "height": 0, "has_audio": False, "sample_rate": 32768}
    try:
        res = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json",
             "-show_streams", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        data = json.loads(res.stdout.decode("utf-8", "replace") or "{}")
    except (OSError, ValueError):
        return out
    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and not out["width"]:
            out["width"] = int(st.get("width") or 0)
            out["height"] = int(st.get("height") or 0)
        elif st.get("codec_type") == "audio" and not out["has_audio"]:
            out["has_audio"] = True
            try:
                out["sample_rate"] = int(st.get("sample_rate") or 32768)
            except (TypeError, ValueError):
                pass
    return out


def attach_cards(video_path: str, video_mod, intro_png: bytes | None = None,
                 intro_seconds: float = 0.0, end_png: bytes | None = None,
                 end_seconds: float = 0.0, threads: int = 0,
                 should_abort: Callable | None = None,
                 log: Callable = print) -> None:
    """Splice cards onto a video that has NO panel pass to fold them into.

    Only used with `--panel off`: when a panel is composited, the cards ride
    along in that pass instead (composite_panels). Each card is encoded as
    its own short segment matching the video's exact geometry and — when the
    source has audio — its audio layout with silence, then joined with the
    concat filter so the audio moves with the video. Any failure leaves the
    original video untouched: a card is a bonus, never a reason to lose a
    conversion.
    """
    cards = _card_specs(intro_png, intro_seconds, end_png, end_seconds)
    if not cards:
        return
    ffmpeg = video_mod._find_ffmpeg()
    info = probe_video(ffmpeg, video_path)
    if not info["width"] or not info["height"]:
        raise RuntimeError("cannot read the video's geometry for the card")
    fps = "%d/%d" % video_mod.GBA_FPS
    out_dir = os.path.dirname(video_path) or "."
    base = os.path.basename(video_path)

    tmps: list[str] = []
    out_tmp = None
    try:
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-i", video_path]
        seg_labels = {}
        for n, (where, png, seconds) in enumerate(cards):
            fd, p = tempfile.mkstemp(prefix=f".{base}.{where}.", suffix=".png",
                                     dir=out_dir)
            with os.fdopen(fd, "wb") as fh:
                fh.write(png)
            tmps.append(p)
            cmd += ["-loop", "1", "-framerate", fps, "-t", "%.3f" % seconds,
                    "-i", p]
            seg_labels[where] = n + 1
        parts = []
        for where, idx in seg_labels.items():
            parts.append("[%d:v]scale=%d:%d,setsar=1,fps=%s[%sv]"
                         % (idx, info["width"], info["height"], fps, where))
        order, n_streams = [], 0
        if "intro" in seg_labels:
            order.append("[introv]")
            n_streams += 1
        order.append("[0:v]")
        n_streams += 1
        if "end" in seg_labels:
            order.append("[endv]")
            n_streams += 1
        parts.append("%sconcat=n=%d:v=1:a=0[vout]"
                     % ("".join(order), n_streams))
        audio_graph, audio_args = _audio_args(cards, info["has_audio"])
        graph = ";".join(parts) + audio_graph
        fd, out_tmp = tempfile.mkstemp(prefix=f".{base}.cardcat.",
                                       suffix=".mp4", dir=out_dir)
        os.close(fd)
        cmd += _thread_args(threads)
        cmd += ["-filter_complex", graph, "-map", "[vout]"] + audio_args
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_tmp]
        code, err_bytes = run_ffmpeg(cmd, should_abort)
        if code != 0:
            raise RuntimeError(
                "ffmpeg card concat failed (exit %d):\n%s"
                % (code, err_bytes.decode("utf-8", "replace").strip()[-2000:]))
        os.replace(out_tmp, video_path)
        out_tmp = None
    finally:
        for p in tmps + [out_tmp]:
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


def _probe_duration(ffmpeg: str, path: str) -> float | None:
    """Container duration in seconds via ffprobe (ffmpeg's sibling), or None."""
    ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
    if not os.path.isfile(ffprobe):
        ffprobe = "ffprobe"
    try:
        res = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return float(res.stdout.decode("ascii", "replace").strip())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Frame preview — see the composited frame BEFORE spending a full conversion
# ---------------------------------------------------------------------------

# Preview defaults: enough frames, far enough apart, to show the intro, the
# first turn and the panel — while emulating only a few seconds of battle.
PREVIEW_COUNT = 4
PREVIEW_SPACING_SECONDS = 3.0
PREVIEW_START_SECONDS = 2.5


def design_context(layout=None, rom_bytes: bytes | None = None,
                   panel_cycle: float = 0.0, panel_cycle_pages=(),
                   panel_sections=None) -> ConvertContext:
    """A ConvertContext carrying only what the RENDERERS read.

    The panel designer and any other "draw me a frame" caller needs no ROM
    file, no save, no emulator and no output folder — just the layout, the
    ROM bytes for name lookups (optional) and the cycling options. Never pass
    this to convert_one: its driver/video modules are deliberately None.
    """
    from . import panel as panel_mod
    here = Path(".")
    return ConvertContext(
        rom_path=here, sav_path=here, outdir=here,
        rom_bytes=rom_bytes or b"", rom_crc32=0, sav_bytes=b"",
        driver_mod=None, video_mod=None, writer_kwargs={},
        writer_accepts_log=False, panel_enabled=True,
        panel_sections=tuple(panel_sections or panel_mod.PANEL_SECTIONS),
        panel_cycle=float(panel_cycle or 0.0),
        panel_cycle_pages=tuple(panel_cycle_pages or ()),
        layout=layout)


def compose_frame(game_img, info: dict, extras: dict, ctx: ConvertContext,
                  settings: ConvertSettings, page: int = 0, warn=None):
    """Paste the game frame + rendered panels into one composited PIL Image.

    Exactly the geometry composite_panels() asks ffmpeg for, done in Pillow —
    so a preview (and the designer's live view) shows the real output frame,
    panels and all. `game_img` may be None: the game area is then filled with
    a neutral placeholder, which is what the designer uses before any record
    has been replayed.
    """
    from PIL import Image, ImageDraw
    from . import panel as panel_mod

    scale = max(1, int(settings.scale))
    gw, gh = 240 * scale, 160 * scale
    if game_img is None:
        game = Image.new("RGB", (gw, gh), (28, 32, 40))
        d = ImageDraw.Draw(game)
        for i in range(0, gh, max(8, gh // 20)):     # subtle "no video" hatch
            d.line([(0, i), (gw, i)], fill=(34, 39, 48))
        d.rectangle([0, 0, gw - 1, gh - 1], outline=(70, 80, 95))
    else:
        game = game_img if game_img.size == (gw, gh) \
            else game_img.resize((gw, gh), Image.NEAREST)

    if not ctx.panel_enabled or settings.panel == "off":
        return game

    lay = ctx.layout
    if lay is None:                                  # classic single panel
        size = _panel_size(settings)
        png = panel_mod.render_panel(info, extras, size)
        side = settings.panel
        pan = Image.open(io.BytesIO(png)).convert("RGB")
        out = Image.new("RGB", (gw + size[0], gh), (0, 0, 0))
        out.paste(pan if side == "left" else game, (0, 0))
        out.paste(game if side == "left" else pan, (size[0] if side == "left"
                                                    else gw, 0))
        return out

    cycle_pages = panel_mod.parse_cycle_pages(ctx.panel_cycle_pages) \
        if ctx.panel_cycle > 0 else ()
    total_w, total_h = lay.composite_size_px(scale)
    out = Image.new("RGB", (total_w, total_h), (0, 0, 0))
    left_w = lay.units_for("left") * scale
    top_h = lay.units_for("top") * scale
    out.paste(game, (left_w, top_h))
    for spec in lay.ordered_panels():
        size = lay.panel_size_px(spec.side, scale)
        # Only a CYCLING panel splits into pages; a static one draws every
        # block it has (page is then ignored).
        specs = (layout_mod.panel_page_specs(spec, cycle_pages)
                 if ctx.panel_cycle > 0 else [spec])
        use = specs[page % len(specs)] if specs else spec
        img = panel_mod.render_layout_panel_image(info, extras, use, size,
                                                  warn=warn)
        if spec.side == "left":
            pos = (0, top_h)
        elif spec.side == "right":
            pos = (left_w + gw, top_h)
        elif spec.side == "top":
            pos = (0, 0)
        else:                                        # bottom
            pos = (0, top_h + gh)
        out.paste(img, pos)
    return out


def preview_extras(info: dict, settings: ConvertSettings, ctx: ConvertContext,
                   streak=None, export_info=None,
                   seconds: float | None = None) -> dict:
    """`extras` for a panel drawn before the battle's outcome is known."""
    class _Pending:                       # duck-types a ReplayResult
        outcome_text = "unknown"
        seconds = 0.0
    pending = _Pending()
    pending.seconds = float(seconds or 0.0)
    return _panel_extras(info, settings, ctx, pending, streak, export_info)


def preview_frames(rec_path, settings: ConvertSettings,
                   ctx: ConvertContext | None = None,
                   count: int = PREVIEW_COUNT,
                   spacing_seconds: float = PREVIEW_SPACING_SECONDS,
                   start_seconds: float = PREVIEW_START_SECONDS,
                   log: Callable = print, err: Callable | None = None,
                   progress_cb: Callable | None = None) -> dict:
    """Replay just far enough to grab a few composited frames. No video.

    Boots the ROM with the record injected exactly as a conversion would
    (same option patches, same opponent-POV flip), captures `count` frames
    `spacing_seconds` apart starting `start_seconds` into the battle, and
    composites each one with the panel(s) the current settings/layout ask
    for. Nothing is written to disk — the caller gets PNG bytes.

    Returns {"status": "OK"|"INVALID"|"FAILED", "frames": [{"seconds", "png",
    "index"}], "info", "detail", "error"}. The panel's outcome/duration read
    "unknown" here: the battle has not finished.
    """
    rp = Path(rec_path)
    name = rp.name
    if err is None:
        err = _default_err
    if not pillow_available():
        return {"status": "FAILED", "frames": [], "info": None,
                "name": name,
                "detail": "preview needs Pillow — " + pillow_hint(),
                "error": "Pillow missing"}
    count = max(1, int(count))
    spacing = max(0.1, float(spacing_seconds))
    start = max(0.0, float(start_seconds))

    if ctx is None:
        ctx = load_context(settings, log=log, err=err)
    prep = prepare_record(rp, settings, ctx, log=log, err=err)
    if "result" in prep:
        res = prep["result"]
        return {"status": res["status"], "frames": [], "info": None,
                "name": name, "detail": res["detail"],
                "error": res.get("error")}
    data, info = prep["data"], prep["info"]
    streak, export_info = prep["streak"], prep["export_info"]

    fps = 16777216 / 280896
    first = int(round(start * fps))
    step = max(1, int(round(spacing * fps)))
    wanted = [first + i * step for i in range(count)]
    limit = wanted[-1] + 1

    grabbed: list[tuple] = []
    try:
        data = transform_record(data, info, settings, ctx, log=log)
        injected = rec.inject(data, ctx.sav_bytes)
        idx = [0]
        want = set(wanted)

        def on_frame(frame, _i=idx):
            n = _i[0]
            _i[0] += 1
            if n in want:
                grabbed.append((n, frame))
                if progress_cb is not None:
                    progress_cb({"phase": "preview", "grabbed": len(grabbed),
                                 "wanted": count, "frames": n,
                                 "seconds": n / fps})

        with ctx.driver_mod.EmulatorDriver(
                str(ctx.rom_path), injected, headed=False,
                log=lambda m: log(f"    {m}")) as drv:
            result = drv.run_replay(on_frame, lambda _pcm: None,
                                    max_seconds=settings.max_seconds,
                                    capture_limit=limit)
        log(f"preview: {len(grabbed)} frame(s) captured "
            f"({result.frames} replayed, end: {result.end_reason})")
    except Exception as exc:
        err(f"preview FAILED: {type(exc).__name__}: {exc}")
        return {"status": "FAILED", "frames": [], "info": info, "name": name,
                "detail": f"{type(exc).__name__}: {exc}", "error": str(exc)}

    if not grabbed:
        return {"status": "FAILED", "frames": [], "info": info, "name": name,
                "detail": "the replay ended before any preview frame",
                "error": "no frames captured"}

    from PIL import Image
    out_frames = []
    warnings: list[str] = []
    # The opening card is part of the finished video, so it belongs in the
    # preview — as frame 0, exactly where it will play. It carries no
    # game_png: it is not a game frame, and the designer must not draw over it.
    if float(settings.intro_card or 0) > 0:
        try:
            from . import panel as panel_mod
            card_extras = preview_extras(info, settings, ctx, streak,
                                         export_info)
            if card_extras.get("speech"):
                size = composite_size(settings, ctx)
                out_frames.append({
                    "index": -1, "seconds": 0.0, "size": size,
                    "label": "intro card",
                    "png": panel_mod.render_intro_card(info, card_extras,
                                                       size)})
        except Exception as exc:
            err(f"intro card not previewed ({type(exc).__name__}: {exc})")
    for page, (n, raw) in enumerate(grabbed):
        secs = n / fps
        game = Image.frombytes("RGBX", (240, 160), raw).convert("RGB")
        extras = preview_extras(info, settings, ctx, streak, export_info,
                                seconds=secs)
        img = compose_frame(game, info, extras, ctx, settings, page=page,
                            warn=warnings.append)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        # The BARE game frame travels along too: the panel designer draws it
        # under the layout so you design against a real battle, not a
        # placeholder rectangle.
        gbuf = io.BytesIO()
        scale = max(1, int(settings.scale))
        game.resize((240 * scale, 160 * scale), Image.NEAREST).save(gbuf,
                                                                    "PNG")
        out_frames.append({"index": n, "seconds": secs, "png": buf.getvalue(),
                           "game_png": gbuf.getvalue(), "size": img.size})
    for w in dict.fromkeys(warnings):
        err(w)
    return {"status": "OK", "frames": out_frames, "info": info, "name": name,
            "detail": f"{len(out_frames)} preview frame(s)", "error": None}


# ---------------------------------------------------------------------------
# convert_one
# ---------------------------------------------------------------------------

def _result(name: str, status: str, detail: str, **over) -> dict:
    base = {
        "name": name, "status": status, "detail": detail,
        "output": None, "sidecar": None,
        "frames": 0, "seconds": 0.0, "end_reason": None,
        "outcome_text": None, "error": None, "info": None, "streak": None,
        # Panel state for this record, surfaced so a GUI can SHOW it per row:
        #   True  -> requested and composited onto the video,
        #   False -> requested but dropped (Pillow missing / composite failed),
        #   None  -> not requested (--panel off), or never got that far.
        "panel_applied": None,
    }
    base.update(over)
    return base


def prepare_record(rec_path, settings: ConvertSettings,
                   ctx: ConvertContext | None = None,
                   log: Callable = print,
                   err: Callable | None = None) -> dict:
    """Everything a conversion knows BEFORE the emulator starts.

    Reads + validates + parses the record, picks up the streak from a PokeDNA
    export stem and the optional '<stem>.txt' sidecar, and builds the output
    basename. Shared by convert_one, the preview and the parallel batch's
    output-name reservation, so all three agree on names byte-for-byte.

    Returns {"result": <early _result dict>} when the record is unreadable or
    invalid, else {"data", "info", "streak", "export_info", "base"}.
    """
    rp = Path(rec_path)
    name = rp.name
    if err is None:
        err = _default_err
    try:
        data = rp.read_bytes()
    except OSError as exc:
        err(f"cannot read: {exc}")
        return {"result": _result(name, "FAILED", f"read error: {exc}",
                                  error=str(exc))}
    errors = rec.validate(data)
    if errors:
        log("invalid record — skipping:")
        for e in errors:
            log(f"  - {e}")
        return {"result": _result(name, "INVALID", errors[0],
                                  error=errors[0])}

    info = rec.parse(data)
    log(rec.summarize(info))

    # --- streak-aware export stems + optional .txt info sidecar ---------
    streak = None
    export_info = read_export_txt(rp)
    parsed_stem = parse_export_stem(rp.stem)
    if parsed_stem is not None:
        streak = parsed_stem["streak"]
        log(f"  streak {streak} (from the export filename)")
        warning = check_export_stem(parsed_stem, info)
        if warning:
            log(f"  warning: {warning}")
    if export_info is not None:
        log(f"  export info: {rp.with_suffix('.txt').name} "
            f"({len(export_info)} line(s))")

    rom_bytes = ctx.rom_bytes if ctx is not None else None
    base = build_output_basename(info, rp.stem, rom_bytes,
                                 plain=settings.plain_names, streak=streak,
                                 pov=settings.pov)
    return {"data": data, "info": info, "streak": streak,
            "export_info": export_info, "base": base}


def transform_record(data: bytes, info: dict, settings: ConvertSettings,
                     ctx: ConvertContext, log: Callable = print) -> bytes:
    """Apply the presentation overrides and the optional opponent-POV flip.

    Returns the record bytes to inject. Shared by convert_one and the frame
    preview so a preview shows exactly what the conversion will produce.
    """
    # Presentation overrides are patched into the record itself
    # (the game reads BATTLE SCENE / text speed from the record).
    if settings.anims != "record" or settings.text_speed != "record":
        want_anims = None if settings.anims == "record" \
            else settings.anims == "on"
        want_speed = None if settings.text_speed == "record" \
            else {"slow": 0, "mid": 1, "fast": 2}[settings.text_speed]
        patched = rec.patch_options(data, animations=want_anims,
                                    text_speed=want_speed)
        if patched != data:
            changes = []
            if want_anims is not None and \
                    (want_anims == info["battle_scene_off"]):
                changes.append(
                    f"animations {'ON' if want_anims else 'OFF'} "
                    f"(recorded "
                    f"{'OFF' if info['battle_scene_off'] else 'on'})")
            if want_speed is not None and \
                    settings.text_speed != info["text_speed"]:
                changes.append(f"text {settings.text_speed} "
                               f"(recorded {info['text_speed']})")
            if changes:
                log("  override: " + ", ".join(changes))
            data = patched
    # Opponent-POV flip: applied AFTER any presentation patch so the two
    # transforms compose (each recomputes the checksum; to_opponent_pov
    # runs last, so the final checksum is correct). Experimental —
    # faithful only for genuine link records (see rec.to_opponent_pov).
    if settings.pov == "opponent":
        # For a vs-AI Frontier record, label the bottom (now-watched)
        # trainer with the REAL NPC's name from the ROM instead of the
        # generic "FOE" placeholder. Only the on-screen NAME is
        # corrected; the link character SPRITE stays a generic player
        # character (a limitation of the engine's link-replay path).
        bottom_name = None
        if info.get("opponent_a_kind") == "frontier":
            bottom_name = romdata.frontier_trainer_rawname(
                ctx.rom_bytes, info["opponent_a"])
        data = rec.to_opponent_pov(data, bottom_trainer_name=bottom_name)
        log(f"  opponent POV name: {bottom_name or 'FOE'}")
        faithful = pov_faithful(info)
        log("  opponent POV (experimental): "
            + ("faithful (genuine link record)" if faithful
               else "WHAT-IF — vs-AI Frontier record; the replay "
                    "diverges after ~turn 1 and may end early"))
    return data


def convert_one(rec_path, settings: ConvertSettings, log: Callable = print,
                progress_cb: Callable | None = None, *,
                ctx: ConvertContext | None = None,
                err: Callable | None = None,
                out_path=None,
                should_abort: Callable | None = None) -> dict:
    """Convert one .rec to .mp4; never raises for a bad record/replay.

    log(str)  — human-readable progress (the CLI's stdout lines).
    err(str)  — problems (the CLI's stderr lines); defaults to stderr.
    progress_cb(dict) — optional, called every ~second of captured video
        with {"phase": "replay", "frames": n, "seconds": s} (for GUIs).
    ctx — a load_context() result; created on the fly when omitted (which
        raises PipelineError if the batch preconditions fail).
    out_path — a pre-resolved destination .mp4. The parallel batch reserves
        every name in the parent process (workers cannot share the
        collision-avoidance set), so it passes the reservation in here.

    Returns a dict: status "OK" (natural end) / "TRUNC" (timeout or
    mid-battle stall; partial video kept) / "INVALID" (bad record; nothing
    written) / "FAILED" (I/O or replay error), plus output path, replay
    stats, parsed record info and error text. `detail` is the CLI's batch
    summary line.
    """
    rp = Path(rec_path)
    name = rp.name
    if err is None:
        err = _default_err

    if ctx is None:
        # Validate the record BEFORE touching the ROM / save / emulator stack,
        # so a bad record is rejected as INVALID even on a host that has none
        # of them (CI, a laptop with only the parser) instead of surfacing as
        # "ROM not found". Silent pre-pass; the real pass below logs.
        pre = prepare_record(rp, settings, None, log=lambda *_: None,
                             err=lambda *_: None)
        if "result" in pre:
            return prepare_record(rp, settings, None, log=log,
                                  err=err)["result"]
        ctx = load_context(settings, log=log, err=err)

    prep = prepare_record(rp, settings, ctx, log=log, err=err)
    if "result" in prep:
        return prep["result"]
    data, info = prep["data"], prep["info"]
    streak, export_info = prep["streak"], prep["export_info"]

    rec_sha1_bytes = data                   # pre-patch bytes for the sidecar
    if out_path is None:
        dest = output_dir_for(ctx.outdir, info, settings)
        dest.mkdir(parents=True, exist_ok=True)
        out_path = resolve_output_path(dest, prep["base"], rp.name,
                                       ctx.used_out_paths)
    writer = None
    try:
        data = transform_record(data, info, settings, ctx, log=log)
        injected = rec.inject(data, ctx.sav_bytes)

        writer_kwargs = dict(ctx.writer_kwargs)
        if ctx.writer_accepts_log:
            writer_kwargs["log"] = log

        with ctx.driver_mod.EmulatorDriver(
                str(ctx.rom_path), injected, headed=settings.headed,
                log=lambda m: log(f"    {m}")) as drv:
            writer = ctx.video_mod.Mp4Writer(str(out_path), **writer_kwargs)
            on_frame = writer.add_video
            if progress_cb is not None:
                frame_count = [0]

                def on_frame(frame, _add=writer.add_video,
                             _n=frame_count):    # noqa: F811
                    _add(frame)
                    _n[0] += 1
                    if _n[0] % 60 == 0:
                        progress_cb({"phase": "replay", "frames": _n[0],
                                     "seconds": _n[0] * 280896 / 16777216})
            result = drv.run_replay(on_frame, writer.add_audio,
                                    max_seconds=settings.max_seconds,
                                    should_abort=should_abort)
            final_path = writer.close()
            writer = None
        log(f"replay done: {result.frames} frames, "
            f"{result.seconds:.1f}s, end: {result.end_reason}, "
            f"outcome: {result.outcome_text}")

        # --- side panel: composite at finalize (outcome/duration known) -
        # panel_applied: None = not requested; True/False = requested and
        # (composited / dropped). A GUI reads this to SHOW 'panel on/off'
        # per row so a silent drop can never masquerade as a full render.
        panel_applied = None
        panel_mode = None               # "static" | "cycle" (for the sidecar)
        panel_cycle_pages_used: tuple = ()
        end_card_seconds = 0.0
        intro_card_seconds = 0.0
        # Both cards are rendered BEFORE the composite so all three share one
        # re-encode. The intro card carries the opponent's pre-battle line,
        # which the record itself never had (see opponent_speech).
        card_png = intro_png = None
        card_extras = _panel_extras(info, settings, ctx, result, streak,
                                    export_info)
        if float(settings.end_card or 0) > 0 and card_extras.get("state"):
            try:
                from . import panel as panel_mod
                card_png = panel_mod.render_end_card(
                    info, card_extras, composite_size(settings, ctx))
            except Exception as exc:
                err(f"end card not drawn ({type(exc).__name__}: {exc})")
                card_png = None
        if float(settings.intro_card or 0) > 0 and card_extras.get("speech"):
            try:
                from . import panel as panel_mod
                intro_png = panel_mod.render_intro_card(
                    info, card_extras, composite_size(settings, ctx))
            except Exception as exc:
                err(f"intro card not drawn ({type(exc).__name__}: {exc})")
                intro_png = None
        if settings.panel != "off":
            panel_applied = False
        if ctx.panel_enabled:
            try:
                from . import panel as panel_mod
                inputs = panel_inputs(info, settings, ctx, result, streak,
                                      export_info, warn=err)
                pages = max(len(i["pages"]) for i in inputs)
                composite_panels(str(final_path), inputs, ctx.video_mod,
                                 seconds_per_page=ctx.panel_cycle,
                                 game_seconds=result.seconds,
                                 card_png=card_png,
                                 card_seconds=settings.end_card,
                                 card_size=composite_size(settings, ctx),
                                 intro_png=intro_png,
                                 intro_seconds=settings.intro_card,
                                 threads=settings.encoder_threads,
                                 should_abort=should_abort, log=log)
                if intro_png is not None:
                    intro_card_seconds = float(settings.intro_card)
                    log(f"intro card: {settings.intro_card:g}s opening card — "
                        f"\"{' '.join(card_extras['speech'])}\"")
                if card_png is not None:
                    end_card_seconds = float(settings.end_card)
                    log(f"end card: {settings.end_card:g}s trainer-state card "
                        f"(playtime/Pokedex/BP/symbols) — same encode pass")
                sides = ", ".join(
                    "%s %dx%d" % (i["side"], i["size"][0], i["size"][1])
                    for i in inputs)
                if ctx.panel_cycle > 0 and pages > 1:
                    panel_mode = "cycle"
                    panel_cycle_pages_used = panel_mod.parse_cycle_pages(
                        ctx.panel_cycle_pages)
                    log(f"panel: cycling info panel composited ({sides}, "
                        f"{pages} page(s) "
                        f"[{', '.join(panel_cycle_pages_used)}] @ "
                        f"{ctx.panel_cycle:g}s each)")
                else:
                    panel_mode = "static"
                    log(f"panel: info panel composited ({sides})")
                panel_applied = True
            except ConversionCancelled:
                raise
            except Exception as exc:
                err(f"panel failed ({type(exc).__name__}: {exc}) — "
                    "video kept without the panel")
        elif settings.panel != "off":
            # requested but load_context already disabled it (Pillow missing);
            # say so per-record too, not just once at batch preflight.
            err(f"panel: {settings.panel} requested but not applied — "
                + pillow_hint())

        # --- cards with NO panel: their own concat pass (nothing else
        # re-encodes the video in this configuration).
        if not ctx.panel_enabled and (intro_png or card_png):
            try:
                attach_cards(str(final_path), ctx.video_mod,
                             intro_png=intro_png,
                             intro_seconds=settings.intro_card,
                             end_png=card_png, end_seconds=settings.end_card,
                             threads=settings.encoder_threads,
                             should_abort=should_abort, log=log)
                if intro_png is not None:
                    intro_card_seconds = float(settings.intro_card)
                    log(f"intro card: {settings.intro_card:g}s opening card "
                        f"spliced on")
                if card_png is not None:
                    end_card_seconds = float(settings.end_card)
                    log(f"end card: {settings.end_card:g}s trainer-state card "
                        f"appended (playtime/Pokedex/BP/symbols)")
            except ConversionCancelled:
                raise
            except Exception as exc:
                err(f"cards failed ({type(exc).__name__}: {exc}) — "
                    "video kept without them")

        # --- outcome in the name: only knowable now, so the finished file
        # is renamed (same directory, so os.replace is atomic). The sidecar is
        # written afterwards and therefore records the final name.
        if settings.outcome_in_name and outcome_tag(result.outcome_text):
            try:
                final_base = build_output_basename(
                    info, rp.stem, ctx.rom_bytes, plain=settings.plain_names,
                    streak=streak, pov=settings.pov,
                    outcome=result.outcome_text)
                renamed = resolve_output_path(
                    Path(final_path).parent, final_base, rp.name,
                    ctx.used_out_paths)
                if str(renamed) != str(final_path):
                    os.replace(final_path, renamed)
                    final_path = str(renamed)
                    log(f"named for the outcome: {Path(final_path).name}")
            except OSError as exc:
                err(f"could not rename for the outcome ({exc}) — "
                    f"keeping {Path(final_path).name}")

        sidecar_path = None
        if settings.sidecar:
            options = {"anims": settings.anims,
                       "text_speed": settings.text_speed,
                       "scale": settings.scale,
                       "audio": settings.audio,
                       "pix_fmt": settings.pix_fmt,
                       "pov": settings.pov}
            if settings.panel != "off":
                options["panel"] = (settings.panel if ctx.panel_enabled
                                    else "off (Pillow missing)")
                if ctx.panel_enabled and ctx.layout is not None:
                    # A designed layout can carry several panels; name them
                    # all (and the layout) so the sidecar still describes the
                    # video exactly.
                    options["panel"] = ",".join(
                        p.side for p in ctx.layout.ordered_panels())
                    options["panel_layout"] = {
                        "name": ctx.layout.name,
                        "panels": [{"side": p.side, "units": p.units,
                                    "blocks": len([b for b in p.blocks
                                                   if b.visible])}
                                   for p in ctx.layout.ordered_panels()]}
                # Record HOW the panel was drawn so the sidecar tells static
                # from a time-cycling panel (and which pages / at what cadence).
                if panel_mode is not None:
                    options["panel_mode"] = panel_mode
                    if panel_mode == "cycle":
                        options["panel_cycle_seconds"] = ctx.panel_cycle
                        options["panel_cycle_pages"] = list(
                            panel_cycle_pages_used)
            pov_meta = None
            if settings.pov == "opponent":
                pov_meta = {"pov": "opponent",
                            "pov_faithful": pov_faithful(info),
                            "pov_note": pov_note(info)}
            if end_card_seconds > 0:
                options["end_card_seconds"] = end_card_seconds
            if intro_card_seconds > 0:
                options["intro_card_seconds"] = intro_card_seconds
                options["intro_card_speech"] = " ".join(
                    card_extras.get("speech") or [])
            sidecar = build_sidecar(
                source_rec_name=rp.name, rec_bytes=rec_sha1_bytes,
                info=info, rom_crc32=ctx.rom_crc32,
                options=options, result=result,
                output_name=Path(final_path).name,
                streak=streak, export_info=export_info, pov_meta=pov_meta,
                trainer_state=parse_state_sidecar(export_info))
            sidecar_path = Path(final_path).with_suffix(".json")
            sidecar_path.write_text(json.dumps(sidecar, indent=2) + "\n",
                                    encoding="utf-8")
            log(f"sidecar: {sidecar_path}")

        common = dict(output=str(final_path),
                      sidecar=str(sidecar_path) if sidecar_path else None,
                      frames=result.frames, seconds=result.seconds,
                      end_reason=result.end_reason,
                      outcome_text=result.outcome_text,
                      info=info, streak=streak, panel_applied=panel_applied)
        if result.end_reason == "cancelled":
            # Stopped by Cancel all: drop the partial file rather than leave a
            # half-battle mp4 that looks like a real conversion.
            try:
                os.unlink(final_path)
            except OSError:
                pass
            log("cancelled — partial video removed")
            return _result(name, "CANCELLED", "cancelled",
                           **dict(common, output=None))
        if result.end_reason in ("natural", "trimmed"):
            # "trimmed" = a complete video up to the battle's real end, cut
            # short of a stuck post-battle loop (expected for an opponent-POV
            # what-if of a vs-AI record). Report OK; note the trim.
            log(f"wrote {final_path}")
            note = " [POV trimmed at battle end]" \
                if result.end_reason == "trimmed" else ""
            return _result(name, "OK",
                           f"{result.frames}f {result.seconds:.1f}s "
                           f"({result.end_reason}){note} -> {final_path}",
                           **common)
        # timeout / mid-battle stall: keep the partial .mp4 for
        # inspection but never report the record as OK.
        err(f"TRUNCATED ({result.end_reason}) — partial video "
            f"kept at {final_path}")
        return _result(name, "TRUNC",
                       f"{result.frames}f {result.seconds:.1f}s "
                       f"({result.end_reason}) partial -> {final_path}",
                       **common)
    except ConversionCancelled:
        # Cancel all during an ffmpeg stage. Everything written so far is a
        # temp file the stage's own `finally` already removed, plus possibly
        # the finished game video — drop that too rather than leave a
        # panel-less half-product behind.
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        for leftover in (locals().get("final_path"), out_path):
            if leftover:
                try:
                    os.unlink(leftover)
                except OSError:
                    pass
        log("cancelled")
        return _result(name, "CANCELLED", "cancelled", info=info,
                       streak=streak)
    except Exception as exc:                       # keep the batch going
        if writer is not None:
            try:                                    # no orphaned ffmpeg
                writer.close()
            except Exception:
                pass
        err(f"FAILED: {type(exc).__name__}: {exc}")
        return _result(name, "FAILED", f"{type(exc).__name__}: {exc}",
                       error=str(exc), info=info, streak=streak)


# ---------------------------------------------------------------------------
# convert_batch — the whole queue, sequentially or one record per CPU
# ---------------------------------------------------------------------------
#
# WHY per-record processes and not something finer-grained: a replay is a
# strictly sequential emulation (frame N+1 depends on frame N and on the
# game's RNG state), so a single video cannot be split across cores — the
# only safe split is "different records on different cores". Both halves of
# a record's work are already CPU-bound and single-threaded per record (mGBA
# in-process, ffmpeg in a child), and the GIL makes threads useless for the
# emulator, so each job gets its own PROCESS.
#
# Everything a worker needs is either picklable (ConvertSettings) or rebuilt
# inside the worker (ConvertContext: ROM/save bytes + the mGBA bindings).
# Output NAMES are reserved in the parent before dispatch, because the
# collision-avoidance set (ConvertContext.used_out_paths) cannot be shared
# across processes — two workers would otherwise race for '<base>.mp4'.

_WORKER: dict = {}


def cpu_jobs() -> int:
    """How many records to run at once by default: one per CPU."""
    return max(1, os.cpu_count() or 1)


def encoder_thread_budget(jobs: int) -> int:
    """How many threads ONE worker's ffmpeg may use, given `jobs` workers.

    The whole machine is one thread budget. Left alone, every ffmpeg sizes
    itself for the whole box (x264 defaults to ~1.5x the cores), so a batch of
    N workers oversubscribes it N-fold — measurable as kernel time, not
    throughput. Dividing the cores among the workers keeps the total near one
    thread per core; 1 worker still gets everything.
    """
    jobs = max(1, int(jobs or 1))
    if jobs <= 1:
        return 0                       # 0 = ffmpeg's own choice (all cores)
    return max(1, cpu_jobs() // jobs)


def resolve_jobs(jobs, n_items: int) -> int:
    """Requested jobs (0/None = auto) -> a concrete worker count."""
    n_items = max(0, int(n_items))
    if n_items <= 1:
        return 1
    try:
        want = int(jobs) if jobs is not None else 0
    except (TypeError, ValueError):
        want = 0
    if want <= 0:
        want = cpu_jobs()
    return max(1, min(want, n_items))


def _worker_init(settings: ConvertSettings, msgq, abort_event=None) -> None:
    """One-time per worker process: stash the settings + the progress queue.

    The heavy ConvertContext (ROM bytes, mGBA import) is built lazily on the
    first job so an import failure is reported as that job's FAILED status
    instead of poisoning the whole pool at startup.
    """
    _WORKER["settings"] = settings
    _WORKER["msgq"] = msgq
    _WORKER["abort"] = abort_event
    _WORKER["ctx"] = None


def _worker_convert(idx: int, path: str, out_path: str | None) -> dict:
    """Convert one record inside a pool worker; never raises."""
    settings = _WORKER["settings"]
    msgq = _WORKER.get("msgq")
    lines: list[str] = []

    def post(kind, payload):
        if msgq is not None:
            try:
                msgq.put((kind, idx, payload))
            except Exception:               # a dead queue must not kill a job
                pass

    def log(msg):
        text = str(msg)
        lines.append(text)
        post("log", text)

    def err(msg):
        text = "! " + str(msg)
        lines.append(text)
        post("log", text)

    post("start", None)
    try:
        if _WORKER.get("ctx") is None:
            _WORKER["ctx"] = load_context(settings, log=log, err=err)
        ev = _WORKER.get("abort")
        res = convert_one(path, settings, log=log, err=err,
                          ctx=_WORKER["ctx"], out_path=out_path,
                          should_abort=(ev.is_set if ev is not None else None),
                          progress_cb=lambda p: post("progress", p))
    except PipelineError as exc:
        res = _result(Path(path).name, "FAILED", str(exc), error=str(exc))
    except Exception as exc:                # a worker must always answer
        res = _result(Path(path).name, "FAILED",
                      f"{type(exc).__name__}: {exc}", error=str(exc))
    res["index"] = idx
    res["log_lines"] = lines
    return res


def reserve_output_paths(paths, settings: ConvertSettings,
                         ctx: ConvertContext) -> list:
    """Pre-resolve every job's .mp4 path in the parent (parallel batches).

    Uses the same prepare_record() the conversion itself uses, so the names
    are identical to a sequential run — and, because one process owns the
    whole `used_out_paths` set, two records can never claim the same file.
    Returns a list aligned with `paths`; entries are None for records that
    cannot be named (unreadable/invalid — the worker reports them properly).
    """
    quiet = lambda *_a, **_k: None            # noqa: E731  (silent probe)
    out = []
    for p in paths:
        try:
            prep = prepare_record(p, settings, ctx, log=quiet, err=quiet)
        except Exception:                      # never block the batch
            out.append(None)
            continue
        if "result" in prep:
            out.append(None)
            continue
        dest = output_dir_for(ctx.outdir, prep["info"], settings)
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError:
            dest = Path(ctx.outdir)
        out.append(str(resolve_output_path(dest, prep["base"],
                                           Path(p).name,
                                           ctx.used_out_paths)))
    return out


def convert_batch(paths, settings: ConvertSettings, *,
                  jobs=None, ctx: ConvertContext | None = None,
                  log: Callable = print, err: Callable | None = None,
                  on_start: Callable | None = None,
                  on_result: Callable | None = None,
                  on_log: Callable | None = None,
                  on_progress: Callable | None = None,
                  cancelled: Callable | None = None,
                  aborted: Callable | None = None) -> list:
    """Convert a whole queue; returns one result dict per input, in order.

    jobs      — None/0 = auto (one process per CPU, capped by the queue
                length), 1 = the classic in-process sequential loop.
    on_start(i, path)         — a record started converting
    on_log(i, text)           — one log line for record i ('! ' = stderr)
    on_progress(i, dict)      — replay progress for record i
    on_result(i, result)      — record i finished (result dict)
    cancelled()               — polled; True stops dispatching new records
                                (already-running ones finish)
    aborted()                 — polled; True ALSO stops the conversions that
                                are already running, at the next emulated
                                frame ("Cancel all"). Their partial videos are
                                removed and they come back as CANCELLED.

    Raises PipelineError if the batch preconditions fail (bad ROM/save/stack).
    Individual records never raise — they come back as FAILED/INVALID.
    """
    paths = [Path(p) for p in paths]
    if err is None:
        err = _default_err
    results: list = [None] * len(paths)
    if not paths:
        return results

    n_jobs = resolve_jobs(settings.jobs if jobs is None else jobs, len(paths))

    def emit(i, res):
        results[i] = res
        if on_result is not None:
            on_result(i, res)

    # ---- sequential: the original in-process loop ------------------------
    def run_sequential(ctx):
        if ctx is None:
            ctx = load_context(settings, log=log, err=err)
        for i, p in enumerate(paths):
            if cancelled is not None and cancelled():
                break
            if on_start is not None:
                on_start(i, p)
            lines: list[str] = []

            def _log(m, _i=i, _lines=lines):
                text = str(m)
                _lines.append(text)
                if on_log is not None:
                    on_log(_i, text)
                else:
                    log(text)

            def _err(m, _i=i, _lines=lines):
                _lines.append("! " + str(m))
                if on_log is not None:
                    on_log(_i, "! " + str(m))
                else:
                    err(m)

            if aborted is not None and aborted():
                break
            try:
                res = convert_one(
                    p, settings, log=_log, err=_err, ctx=ctx,
                    should_abort=aborted,
                    progress_cb=(None if on_progress is None
                                 else (lambda d, _i=i: on_progress(_i, d))))
            except Exception as exc:           # convert_one shouldn't raise
                res = _result(p.name, "FAILED",
                              f"{type(exc).__name__}: {exc}", error=str(exc))
            res["index"] = i
            res.setdefault("log_lines", lines)
            emit(i, res)
        return results

    if n_jobs == 1:
        return run_sequential(ctx)

    # ---- parallel: one process per record, up to n_jobs at a time --------
    import multiprocessing as mp
    from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
    from concurrent.futures.process import BrokenProcessPool

    if ctx is None:
        ctx = load_context(settings, log=log, err=err)
    # Split the machine's threads across the workers before dispatching:
    # otherwise each worker's ffmpeg sizes itself for the whole box.
    if not settings.encoder_threads:
        budget = encoder_thread_budget(n_jobs)
        if budget:
            settings = replace(settings, encoder_threads=budget)
    log(f"parallel: {n_jobs} worker process(es) for {len(paths)} record(s) "
        f"({cpu_jobs()} CPU(s) detected, "
        f"{settings.encoder_threads or 'auto'} encoder thread(s) each)")
    reserved = reserve_output_paths(paths, settings, ctx)

    mp_ctx = mp.get_context("spawn")     # never fork: mGBA/ffmpeg + threads
    pending: dict = {}
    try:
        with mp.Manager() as manager:
            msgq = manager.Queue()

            def drain():
                while True:
                    try:
                        kind, i, payload = msgq.get_nowait()
                    except Exception:
                        return
                    if kind == "log":
                        if on_log is not None:
                            on_log(i, payload)
                    elif kind == "progress" and on_progress is not None:
                        on_progress(i, payload)
                    elif kind == "start" and on_start is not None:
                        # Posted by the worker when it really begins, not at
                        # submit time — a queued record must not look busy.
                        on_start(i, paths[i])

            # Workers cannot see the parent's threading.Event, so Cancel
            # all travels as a manager Event they poll every few frames.
            abort_event = manager.Event()
            with ProcessPoolExecutor(max_workers=n_jobs, mp_context=mp_ctx,
                                     initializer=_worker_init,
                                     initargs=(settings, msgq,
                                               abort_event)) as pool:
                for i, p in enumerate(paths):
                    fut = pool.submit(_worker_convert, i, str(p), reserved[i])
                    pending[fut] = i
                while pending:
                    done, _ = wait(list(pending), timeout=0.2,
                                   return_when=FIRST_COMPLETED)
                    drain()
                    for fut in done:
                        i = pending.pop(fut)
                        try:
                            res = fut.result()
                        except Exception as exc:
                            res = _result(paths[i].name, "FAILED",
                                          f"{type(exc).__name__}: {exc}",
                                          error=str(exc))
                            res["index"] = i
                        if on_log is not None:
                            for line in res.get("log_lines") or []:
                                on_log(i, line)
                        emit(i, res)
                    if aborted is not None and aborted():
                        abort_event.set()          # stop the running replays
                    if (cancelled is not None and cancelled()) or \
                            (aborted is not None and aborted()):
                        for fut in list(pending):
                            if fut.cancel():
                                i = pending.pop(fut)
                                results[i] = None
                drain()
    except RuntimeError as exc:
        # Worker processes could not be started at all. The usual cause is an
        # embedding script with no `if __name__ == "__main__":` guard: on
        # spawn platforms (macOS/Windows) the child re-imports the entry
        # module, and Python refuses rather than recursing. Converting
        # sequentially is always better than failing the batch.
        err(f"could not start worker processes ({exc}) — converting "
            "sequentially instead. If you are calling convert_batch() from "
            "your own script, guard its entry point with "
            "`if __name__ == \"__main__\":` (the rec2mp4 CLI and GUI already "
            "do).")
        return run_sequential(ctx)
    except BrokenProcessPool as exc:
        # A worker died outright (an emulator crash takes its process with
        # it). Report every unfinished record honestly instead of hanging.
        err(f"a conversion worker process died ({exc}) — the remaining "
            "records were not converted; retry with --jobs 1 to see the "
            "failure in-process")
        for i, res in enumerate(results):
            if res is None:
                results[i] = _result(paths[i].name, "FAILED",
                                     "worker process died (try --jobs 1)",
                                     error=str(exc))
                results[i]["index"] = i
                if on_result is not None:
                    on_result(i, results[i])
    return results
