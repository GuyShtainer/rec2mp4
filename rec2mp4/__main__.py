# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""rec2mp4 command-line interface.

    python3 -m rec2mp4 <input.rec|folder> [options]

Turns Pokemon Emerald Battle Record exports (.rec, save sector 31) into
.mp4 videos by replaying them in the real engine under headless mGBA.

This module is only argument parsing + the batch loop + summary printing;
the per-record conversion flow lives in rec2mp4.pipeline (shared with any
GUI). The emulator and encoder modules are imported lazily, only when a
video is actually produced — `--info-only` runs on the pure-stdlib parser
and needs no mGBA bindings, no ffmpeg, no ROM and no save.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import rec
from .pipeline import (                                    # noqa: F401
    DEFAULT_OUTDIR, DEFAULT_PIX_FMT, DEFAULT_ROM, DEFAULT_SAV,
    ConvertSettings, PipelineError, build_output_basename, build_sidecar,
    convert_one, load_context, opponent_label, parse_export_stem,
    resolve_output_path, sanitize_filename,
)


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
    p.add_argument("--panel", choices=("right", "left", "off"),
                   default="right",
                   help="composite a battle-info side panel onto the video "
                        "(default: right). Text only, rendered from the "
                        "record + YOUR ROM; needs Pillow. 'off' produces "
                        "the plain game video")
    p.add_argument("--panel-info", default="all", metavar="CSV",
                   help="comma-separated panel sections: header, players, "
                        "opponents, teams, export, footer — or 'all' "
                        "(default). 'export' shows the first lines of a "
                        "PokeDNA '<stem>.txt' info sidecar when present")
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


def _info_only_one(rp: Path) -> tuple[str, str, str]:
    """--info-only for one record: summarize without emulating.

    Returns the (name, status, detail) summary row, printing exactly what
    the pre-pipeline CLI printed.
    """
    try:
        data = rp.read_bytes()
    except OSError as exc:
        print(f"cannot read: {exc}", file=sys.stderr)
        return (rp.name, "FAILED", f"read error: {exc}")
    errors = rec.validate(data)
    if errors:
        print("invalid record — skipping:")
        for e in errors:
            print(f"  - {e}")
        return (rp.name, "INVALID", errors[0])
    info = rec.parse(data)
    print(rec.summarize(info))
    return (rp.name, "OK", f"{info['facility']}, {info['level_mode']}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    rec_paths = _collect_recs(args.input)
    if not rec_paths:
        print(f"error: {args.input!r} is not a .rec file or a folder "
              "containing .rec files", file=sys.stderr)
        return 2

    # ------------------------------------------------------------------
    # Batch preflight (skipped entirely for --info-only, which must work
    # without the vendored mGBA bindings, ffmpeg, ROM or save).
    # ------------------------------------------------------------------
    ctx = None
    settings = None
    if not args.info_only:
        settings = ConvertSettings(
            rom=args.rom, sav=args.sav, outdir=args.outdir,
            scale=args.scale, audio=not args.no_audio,
            anims=args.anims, text_speed=args.text_speed,
            plain_names=args.plain_names, sidecar=not args.no_sidecar,
            pix_fmt=args.pix_fmt, max_seconds=args.max_seconds,
            headed=args.headed, panel=args.panel,
            panel_info=args.panel_info)
        try:
            ctx = load_context(settings)
        except PipelineError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    # ------------------------------------------------------------------
    # Per-record loop: validate -> summarize -> (inject -> replay -> mp4).
    # A bad record or a failed replay never stops the batch.
    # ------------------------------------------------------------------
    results: list[tuple[str, str, str]] = []    # (name, status, detail)
    failures = 0

    for rp in rec_paths:
        print(f"\n=== {rp.name} ===")
        if args.info_only:
            row = _info_only_one(rp)
        else:
            res = convert_one(rp, settings, ctx=ctx)
            row = (res["name"], res["status"], res["detail"])
        results.append(row)
        if row[1] != "OK":
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
