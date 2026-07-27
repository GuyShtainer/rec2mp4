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
import json
import os
import re
import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__, rec, romdata

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


def _default_err(msg: object) -> None:
    print(msg, file=sys.stderr)


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
    panel: str = "right"              # right | left | off
    panel_info: str = "all"           # CSV of panel.PANEL_SECTIONS


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
    writer_accepts_log = _writer_accepts_kwarg(video_mod.Mp4Writer, "log")

    # --- panel options (validated even when disabled -> early CLI errors)
    if settings.panel not in ("right", "left", "off"):
        raise PipelineError(
            f"--panel must be right, left or off (got {settings.panel!r})")
    from . import panel as panel_mod       # PIL-free import
    try:
        panel_sections = panel_mod.parse_panel_info(settings.panel_info)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc
    panel_enabled = settings.panel in ("right", "left")
    if panel_enabled:
        try:
            import PIL  # noqa: F401
        except ImportError:
            panel_enabled = False
            err("warning: side panel disabled — Pillow is not installed in "
                "this Python.\n"
                "  Install it in the rec2mp4 conda env:  "
                "~/miniconda3/envs/rec2mp4/bin/python "
                "-m pip install pillow\n"
                "  (or run with --panel off to silence this). Videos will "
                "be written without the info panel.")

    outdir.mkdir(parents=True, exist_ok=True)

    return ConvertContext(
        rom_path=rom_path, sav_path=sav_path, outdir=outdir,
        rom_bytes=rom_bytes, rom_crc32=rom_crc32, sav_bytes=sav_bytes,
        driver_mod=driver_mod, video_mod=video_mod,
        writer_kwargs=writer_kwargs, writer_accepts_log=writer_accepts_log,
        panel_enabled=panel_enabled, panel_sections=panel_sections)


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


def build_output_basename(info: dict, stem: str,
                          rom_bytes: bytes | None,
                          plain: bool = False,
                          streak: int | None = None) -> str:
    """'<stem> - <Facility> <Open|Lv50>[ <kind>] vs <Opp>[ and <OppB>]
    [ (streak N)]'.

    Windows-safe ASCII, no extension. plain=True keeps just the stem
    (streak included only in rich mode). Capped at 180 chars so
    '<base> (NN).mp4'/'.json' and Mp4Writer's temp file stay under every
    OS's 255-byte per-name limit.
    """
    if plain:
        base = sanitize_filename(stem)
        return base[:180].rstrip(" .") or "record"
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
    return sanitize_filename(base)[:180].rstrip(" .") or "record"


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
                  export_info: list[str] | None = None) -> dict:
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

def _render_panel_png(info: dict, settings: ConvertSettings,
                      ctx: ConvertContext, replay_result,
                      streak: int | None,
                      export_info: list[str] | None) -> bytes:
    from . import panel as panel_mod
    extras = {
        "rom_bytes": ctx.rom_bytes,
        "outcome_text": getattr(replay_result, "outcome_text", "unknown"),
        "duration_seconds": getattr(replay_result, "seconds", None),
        "streak": streak,
        "export_lines": export_info or [],
        "sections": ctx.panel_sections,
        "opponent_a_label": opponent_label(info, "a", ctx.rom_bytes),
        "opponent_b_label": opponent_label(info, "b", ctx.rom_bytes),
    }
    size = (settings.scale * PANEL_WIDTH_UNITS,
            settings.scale * PANEL_HEIGHT_UNITS)
    return panel_mod.render_panel(info, extras, size)


def _composite_panel(video_path: str, png_bytes: bytes, side: str,
                     video_mod) -> None:
    """hstack the panel PNG beside the finished game video, atomically.

    Video is re-encoded (the geometry changes); audio is stream-copied.
    The temp output lives next to the target so os.replace() is atomic;
    on any ffmpeg failure the original video is left untouched.
    """
    ffmpeg = video_mod._find_ffmpeg()
    out_dir = os.path.dirname(video_path) or "."
    base = os.path.basename(video_path)
    fps = "%d/%d" % video_mod.GBA_FPS
    # shortest=1 makes hstack itself stop when the FIRST input ends; the
    # looped panel PNG is infinite, so without it a no-audio game video
    # (e.g. --no-audio, or the zero-samples fallback) gives '-shortest'
    # nothing finite to bound against and ffmpeg encodes forever.
    if side == "left":
        fc = "[1:v][0:v]hstack=inputs=2:shortest=1[v]"
    else:
        fc = "[0:v][1:v]hstack=inputs=2:shortest=1[v]"

    fd, png_tmp = tempfile.mkstemp(prefix=f".{base}.panel.", suffix=".png",
                                   dir=out_dir)
    with os.fdopen(fd, "wb") as fh:
        fh.write(png_bytes)
    fd, out_tmp = tempfile.mkstemp(prefix=f".{base}.panelmux.",
                                   suffix=".mp4", dir=out_dir)
    os.close(fd)
    try:
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-i", video_path,
               "-loop", "1", "-framerate", fps, "-i", png_tmp,
               "-filter_complex", fc,
               "-map", "[v]", "-map", "0:a?",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
               "-pix_fmt", "yuv420p",
               "-c:a", "copy",
               "-shortest", "-movflags", "+faststart",
               out_tmp]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE)
        if res.returncode != 0:
            tail = res.stderr.decode("utf-8", "replace").strip()[-2000:]
            raise RuntimeError(
                f"ffmpeg panel composite failed (exit {res.returncode}):\n"
                f"{tail or '(no ffmpeg stderr captured)'}")
        os.replace(out_tmp, video_path)
        out_tmp = None
    finally:
        for p in (png_tmp, out_tmp):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# convert_one
# ---------------------------------------------------------------------------

def _result(name: str, status: str, detail: str, **over) -> dict:
    base = {
        "name": name, "status": status, "detail": detail,
        "output": None, "sidecar": None,
        "frames": 0, "seconds": 0.0, "end_reason": None,
        "outcome_text": None, "error": None, "info": None, "streak": None,
    }
    base.update(over)
    return base


def convert_one(rec_path, settings: ConvertSettings, log: Callable = print,
                progress_cb: Callable | None = None, *,
                ctx: ConvertContext | None = None,
                err: Callable | None = None) -> dict:
    """Convert one .rec to .mp4; never raises for a bad record/replay.

    log(str)  — human-readable progress (the CLI's stdout lines).
    err(str)  — problems (the CLI's stderr lines); defaults to stderr.
    progress_cb(dict) — optional, called every ~second of captured video
        with {"phase": "replay", "frames": n, "seconds": s} (for GUIs).
    ctx — a load_context() result; created on the fly when omitted (which
        raises PipelineError if the batch preconditions fail).

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

    try:
        data = rp.read_bytes()
    except OSError as exc:
        err(f"cannot read: {exc}")
        return _result(name, "FAILED", f"read error: {exc}", error=str(exc))

    errors = rec.validate(data)
    if errors:
        log("invalid record — skipping:")
        for e in errors:
            log(f"  - {e}")
        return _result(name, "INVALID", errors[0], error=errors[0])

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

    if ctx is None:
        ctx = load_context(settings, log=log, err=err)

    rec_sha1_bytes = data                   # pre-patch bytes for the sidecar
    base = build_output_basename(info, rp.stem, ctx.rom_bytes,
                                 plain=settings.plain_names, streak=streak)
    out_path = resolve_output_path(ctx.outdir, base, rp.name,
                                   ctx.used_out_paths)
    writer = None
    try:
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
                                    max_seconds=settings.max_seconds)
            final_path = writer.close()
            writer = None
        log(f"replay done: {result.frames} frames, "
            f"{result.seconds:.1f}s, end: {result.end_reason}, "
            f"outcome: {result.outcome_text}")

        # --- side panel: composite at finalize (outcome/duration known) -
        if ctx.panel_enabled:
            try:
                png = _render_panel_png(info, settings, ctx, result,
                                        streak, export_info)
                _composite_panel(str(final_path), png, settings.panel,
                                 ctx.video_mod)
                log(f"panel: {settings.panel} side info panel composited "
                    f"({settings.scale * PANEL_WIDTH_UNITS}x"
                    f"{settings.scale * PANEL_HEIGHT_UNITS})")
            except Exception as exc:
                err(f"panel failed ({type(exc).__name__}: {exc}) — "
                    "video kept without the panel")

        sidecar_path = None
        if settings.sidecar:
            options = {"anims": settings.anims,
                       "text_speed": settings.text_speed,
                       "scale": settings.scale,
                       "audio": settings.audio,
                       "pix_fmt": settings.pix_fmt}
            if settings.panel != "off":
                options["panel"] = (settings.panel if ctx.panel_enabled
                                    else "off (Pillow missing)")
            sidecar = build_sidecar(
                source_rec_name=rp.name, rec_bytes=rec_sha1_bytes,
                info=info, rom_crc32=ctx.rom_crc32,
                options=options, result=result,
                output_name=Path(final_path).name,
                streak=streak, export_info=export_info)
            sidecar_path = Path(final_path).with_suffix(".json")
            sidecar_path.write_text(json.dumps(sidecar, indent=2) + "\n",
                                    encoding="utf-8")
            log(f"sidecar: {sidecar_path}")

        common = dict(output=str(final_path),
                      sidecar=str(sidecar_path) if sidecar_path else None,
                      frames=result.frames, seconds=result.seconds,
                      end_reason=result.end_reason,
                      outcome_text=result.outcome_text,
                      info=info, streak=streak)
        if result.end_reason == "natural":
            log(f"wrote {final_path}")
            return _result(name, "OK",
                           f"{result.frames}f {result.seconds:.1f}s "
                           f"({result.end_reason}) -> {final_path}",
                           **common)
        # timeout / mid-battle stall: keep the partial .mp4 for
        # inspection but never report the record as OK.
        err(f"TRUNCATED ({result.end_reason}) — partial video "
            f"kept at {final_path}")
        return _result(name, "TRUNC",
                       f"{result.frames}f {result.seconds:.1f}s "
                       f"({result.end_reason}) partial -> {final_path}",
                       **common)
    except Exception as exc:                       # keep the batch going
        if writer is not None:
            try:                                    # no orphaned ffmpeg
                writer.close()
            except Exception:
                pass
        err(f"FAILED: {type(exc).__name__}: {exc}")
        return _result(name, "FAILED", f"{type(exc).__name__}: {exc}",
                       error=str(exc), info=info, streak=streak)
