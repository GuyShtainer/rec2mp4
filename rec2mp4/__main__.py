# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""rec2mp4 command-line interface.

    python3 -m rec2mp4 <input.rec|folder> [options]

Turns Pokemon Emerald Battle Record exports (.rec, save sector 31) into
.mp4 videos by replaying them in the real engine under headless mGBA.

The emulator and encoder modules are imported lazily, only when a video
is actually produced — `--info-only` runs on the pure-stdlib parser and
needs no mGBA bindings, no ffmpeg, no ROM and no save.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, rec, romdata

# Default paths resolve relative to the repo root (= two parents up from
# this file: <root>/rec2mp4/__main__.py). User-supplied paths are taken
# as-is (absolute, or relative to the current directory).
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROM = REPO_ROOT / "local" / "rom.gba"
DEFAULT_SAV = REPO_ROOT / "local" / "template.sav"
DEFAULT_OUTDIR = REPO_ROOT / "out"

# mGBA's framebuffer is 4 bytes/pixel in R,G,B,X order -> ffmpeg "rgb0"
# (verified in docs/research/emulator-stack.md).
DEFAULT_PIX_FMT = "rgb0"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rec2mp4",
        description="Replay Pokemon Emerald Battle Record (.rec) exports "
                    "in a headless mGBA and encode them to .mp4.",
    )
    p.add_argument("input",
                   help=".rec file, or a folder — every *.rec inside it "
                        "(sorted) is converted")
    p.add_argument("-o", "--outdir", default=None, metavar="DIR",
                   help="output folder for .mp4 files "
                        f"(default: {DEFAULT_OUTDIR})")
    p.add_argument("--rom", default=None, metavar="PATH",
                   help="US Emerald ROM (BPEE rev0) "
                        f"(default: {DEFAULT_ROM})")
    p.add_argument("--sav", default=None, metavar="PATH",
                   help="128 KiB save with the Frontier Pass; sector 31 is "
                        f"replaced by the record (default: {DEFAULT_SAV})")
    p.add_argument("--headed", action="store_true",
                   help="refresh a preview PNG every 60 frames while "
                        "replaying (no real window exists in this stack; "
                        "the PNG path is logged, needs Pillow in the "
                        "emulator env)")
    p.add_argument("--scale", type=int, default=4, metavar="N",
                   help="integer upscale of the 240x160 GBA frame "
                        "(default 4 = 960x640)")
    p.add_argument("--no-audio", action="store_true",
                   help="encode video only, no audio track")
    p.add_argument("--anims", choices=("on", "off", "record"), default="on",
                   help="battle animations in the replay: 'on' (default) "
                        "forces move effects and the shiny sparkle visible "
                        "even if the recorder played with BATTLE SCENE OFF; "
                        "'off' hides them; 'record' keeps the recorder's own "
                        "setting. Presentation-only — cannot desync the "
                        "replay (animations use the game's separate visual "
                        "RNG stream)")
    p.add_argument("--text-speed", choices=("slow", "mid", "fast", "record"),
                   default="record",
                   help="dialogue text speed during the replay "
                        "(default: as recorded)")
    p.add_argument("--plain-names", action="store_true",
                   help="name outputs '<stem>.mp4' instead of the default "
                        "'<stem> - <facility> <level> vs <opponent>.mp4' "
                        "(opponent names are read from YOUR ROM at runtime)")
    p.add_argument("--no-sidecar", action="store_true",
                   help="do not write the '<basename>.json' metadata sidecar "
                        "next to each converted video")
    p.add_argument("--info-only", action="store_true",
                   help="validate + summarize the record(s), then exit "
                        "without emulating")
    p.add_argument("--max-seconds", type=float, default=1800, metavar="N",
                   help="give up on a replay after N seconds of emulated "
                        "time (default 1800)")
    p.add_argument("--pix-fmt", default=DEFAULT_PIX_FMT, metavar="FMT",
                   help="raw framebuffer pixel format handed to the "
                        f"encoder (default {DEFAULT_PIX_FMT})")
    return p


def _collect_recs(arg: str) -> list[Path]:
    """input argument -> list of .rec paths (empty list = usage error)."""
    p = Path(arg)
    if p.is_dir():
        return sorted(p.glob("*.rec"))
    if p.is_file():
        return [p]
    return []


def _log(msg: object) -> None:
    print(f"    {msg}", flush=True)


def _writer_accepts_pix_fmt(mp4writer_cls) -> bool:
    """True if Mp4Writer.__init__ takes a pix_fmt kwarg (or **kwargs)."""
    try:
        params = inspect.signature(mp4writer_cls.__init__).parameters
    except (TypeError, ValueError):        # C-implemented / no signature
        return True
    return ("pix_fmt" in params
            or any(prm.kind is inspect.Parameter.VAR_KEYWORD
                   for prm in params.values()))


# ---------------------------------------------------------------------------
# Rich output naming + JSON sidecar
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
                          plain: bool = False) -> str:
    """'<stem> - <Facility> <Open|Lv50>[ <kind>] vs <Opp>[ and <OppB>]'.

    Windows-safe ASCII, no extension. plain=True keeps just the stem.
    Capped at 180 chars so '<base> (NN).mp4'/'.json' and Mp4Writer's temp
    file stay under every OS's 255-byte per-name limit.
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
                  output_name: str) -> dict:
    """All the data we know about one conversion, JSON-serializable."""
    return {
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    rec_paths = _collect_recs(args.input)
    if not rec_paths:
        print(f"error: {args.input!r} is not a .rec file or a folder "
              "containing .rec files", file=sys.stderr)
        return 2

    # ------------------------------------------------------------------
    # Emulation preflight (skipped entirely for --info-only, which must
    # work without the vendored mGBA bindings, ffmpeg, ROM or save).
    # ------------------------------------------------------------------
    driver_mod = video_mod = None
    sav_bytes = b""
    rom_bytes = b""
    rom_crc32 = 0
    writer_kwargs: dict = {}
    rom_path = Path(args.rom) if args.rom else DEFAULT_ROM
    outdir = Path(args.outdir) if args.outdir else DEFAULT_OUTDIR

    if not args.info_only:
        sav_path = Path(args.sav) if args.sav else DEFAULT_SAV
        if not rom_path.is_file():
            print(f"error: ROM not found: {rom_path}\n"
                  "  supply your own US Emerald ROM with --rom "
                  "(never distributed with this tool)", file=sys.stderr)
            return 2
        if not sav_path.is_file():
            print(f"error: save not found: {sav_path}\n"
                  "  supply a 128 KiB Emerald save with --sav",
                  file=sys.stderr)
            return 2
        # Read the ROM ONCE: naming resolves frontier-trainer display names
        # from these bytes at runtime (no name tables ship with the tool),
        # and the sidecar records the ROM's crc32.
        rom_bytes = rom_path.read_bytes()
        rom_crc32 = zlib.crc32(rom_bytes) & 0xFFFFFFFF
        sav_bytes = sav_path.read_bytes()
        if len(sav_bytes) < rec.SAV_MIN_SIZE:
            print(f"error: {sav_path} is {len(sav_bytes)} bytes — a full "
                  f"128 KiB (0x{rec.SAV_MIN_SIZE:X}-byte) .sav is required "
                  "(64 KiB dumps have no sector 31)", file=sys.stderr)
            return 2
        if sav_bytes == b"\xff" * len(sav_bytes):
            print(f"error: {sav_path} is blank (every byte 0xFF — an "
                  "erased-flash dump, no save data).\n"
                  "  The replay path needs a real post-game save with the "
                  "Frontier Pass; see README 'Requirements'\n"
                  "  (e.g. --sav local/alt-saves/all-shiny.sav).",
                  file=sys.stderr)
            return 2

        try:
            from . import driver as driver_mod     # noqa: F811
            from . import video as video_mod       # noqa: F811
        except ImportError as exc:
            print(f"error: emulator/encoder stack unavailable ({exc})\n"
                  "  Run the setup in README 'Setup (macOS)' — the mGBA "
                  "bindings live in vendor/ (fetched, not committed);\n"
                  "  details in docs/research/emulator-stack.md. "
                  "--info-only works without any of this.", file=sys.stderr)
            return 2

        writer_kwargs = {"scale": args.scale, "audio": not args.no_audio}
        if _writer_accepts_pix_fmt(video_mod.Mp4Writer):
            writer_kwargs["pix_fmt"] = args.pix_fmt
        elif args.pix_fmt != DEFAULT_PIX_FMT:
            print("error: this Mp4Writer does not accept --pix-fmt",
                  file=sys.stderr)
            return 2

        outdir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Per-record loop: validate -> summarize -> (inject -> replay -> mp4).
    # A bad record or a failed replay never stops the batch.
    # ------------------------------------------------------------------
    results: list[tuple[str, str, str]] = []    # (name, status, detail)
    failures = 0
    used_out_paths: set[str] = set()            # batch-local collision guard

    for rp in rec_paths:
        name = rp.name
        print(f"\n=== {name} ===")
        try:
            data = rp.read_bytes()
        except OSError as exc:
            print(f"cannot read: {exc}", file=sys.stderr)
            results.append((name, "FAILED", f"read error: {exc}"))
            failures += 1
            continue

        errors = rec.validate(data)
        if errors:
            print("invalid record — skipping:")
            for e in errors:
                print(f"  - {e}")
            results.append((name, "INVALID", errors[0]))
            failures += 1
            continue

        info = rec.parse(data)
        print(rec.summarize(info))

        if args.info_only:
            results.append((name, "OK",
                            f"{info['facility']}, {info['level_mode']}"))
            continue

        rec_sha1_bytes = data                   # pre-patch bytes for the sidecar
        base = build_output_basename(info, rp.stem, rom_bytes,
                                     plain=args.plain_names)
        out_path = resolve_output_path(outdir, base, rp.name, used_out_paths)
        writer = None
        try:
            # Presentation overrides are patched into the record itself
            # (the game reads BATTLE SCENE / text speed from the record).
            if args.anims != "record" or args.text_speed != "record":
                want_anims = None if args.anims == "record" \
                    else args.anims == "on"
                want_speed = None if args.text_speed == "record" \
                    else {"slow": 0, "mid": 1, "fast": 2}[args.text_speed]
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
                            args.text_speed != info["text_speed"]:
                        changes.append(f"text {args.text_speed} "
                                       f"(recorded {info['text_speed']})")
                    if changes:
                        print("  override: " + ", ".join(changes))
                    data = patched
            injected = rec.inject(data, sav_bytes)
            with driver_mod.EmulatorDriver(str(rom_path), injected,
                                           headed=args.headed,
                                           log=_log) as drv:
                writer = video_mod.Mp4Writer(str(out_path), **writer_kwargs)
                result = drv.run_replay(writer.add_video, writer.add_audio,
                                        max_seconds=args.max_seconds)
                final_path = writer.close()
                writer = None
            print(f"replay done: {result.frames} frames, "
                  f"{result.seconds:.1f}s, end: {result.end_reason}, "
                  f"outcome: {result.outcome_text}")
            if not args.no_sidecar:
                sidecar = build_sidecar(
                    source_rec_name=rp.name, rec_bytes=rec_sha1_bytes,
                    info=info, rom_crc32=rom_crc32,
                    options={"anims": args.anims,
                             "text_speed": args.text_speed,
                             "scale": args.scale,
                             "audio": not args.no_audio,
                             "pix_fmt": args.pix_fmt},
                    result=result, output_name=Path(final_path).name)
                sidecar_path = Path(final_path).with_suffix(".json")
                sidecar_path.write_text(json.dumps(sidecar, indent=2) + "\n",
                                        encoding="utf-8")
                print(f"sidecar: {sidecar_path}")
            if result.end_reason == "natural":
                print(f"wrote {final_path}")
                results.append((name, "OK",
                                f"{result.frames}f {result.seconds:.1f}s "
                                f"({result.end_reason}) -> {final_path}"))
            else:
                # timeout / mid-battle stall: keep the partial .mp4 for
                # inspection but never report the record as OK.
                print(f"TRUNCATED ({result.end_reason}) — partial video "
                      f"kept at {final_path}", file=sys.stderr)
                results.append((name, "TRUNC",
                                f"{result.frames}f {result.seconds:.1f}s "
                                f"({result.end_reason}) partial -> "
                                f"{final_path}"))
                failures += 1
        except Exception as exc:                       # keep the batch going
            if writer is not None:
                try:                                    # no orphaned ffmpeg
                    writer.close()
                except Exception:
                    pass
            print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            results.append((name, "FAILED",
                            f"{type(exc).__name__}: {exc}"))
            failures += 1

    # ------------------------------------------------------------------
    # Batch summary
    # ------------------------------------------------------------------
    if len(rec_paths) > 1:
        width = max(len(n) for n, _, _ in results)
        print("\n" + "=" * 72)
        for n, status, detail in results:
            print(f"{n:<{width}}  {status:<7}  {detail}")
        print("=" * 72)
    ok = len(results) - failures
    print(f"\n{ok}/{len(rec_paths)} record(s) OK, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
