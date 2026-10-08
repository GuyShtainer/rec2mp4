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
    DEFAULT_OUTDIR, DEFAULT_PIX_FMT, DEFAULT_ROM, DEFAULT_SAV, PANEL_SIDES,
    PREVIEW_COUNT, PREVIEW_SPACING_SECONDS, PREVIEW_START_SECONDS,
    ConvertSettings, PipelineError, build_output_basename, build_sidecar,
    convert_batch, convert_one, load_context, opponent_label,
    parse_export_stem, preview_frames, resolve_jobs, resolve_output_path,
    sanitize_filename,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rec2mp4",
        description="Replay Pokemon Emerald Battle Record (.rec) exports "
                    "in a headless mGBA and encode them to .mp4.",
    )
    p.add_argument("input",
                   help=".rec file, a 128 KiB .sav whose sector 31 holds a "
                        "Battle Record (the record is extracted to a .rec "
                        "first, see --rec-dir), or a folder — every *.rec "
                        "and *.sav inside it (sorted) is converted")
    p.add_argument("--rec-dir", default=None, metavar="DIR",
                   help="where to write the .rec extracted from a .sav input "
                        "(default: next to the save, as '<save stem>.rec'). "
                        "An existing identical .rec is reused; a different "
                        "one is never overwritten")
    p.add_argument("--extract-only", action="store_true",
                   help="extract the record from each .sav input to a .rec "
                        "and stop — no emulator, ffmpeg or ROM needed. "
                        "(.rec inputs are just summarized)")
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
    p.add_argument("--panel", choices=PANEL_SIDES + ("off",),
                   default="right",
                   help="composite a battle-info panel onto the video "
                        "(default: right). Text only, rendered from the "
                        "record + YOUR ROM; needs Pillow. 'top'/'bottom' are "
                        "full-width bands (they use a generated block layout "
                        "— see --layout). 'off' produces the plain game video")
    p.add_argument("--layout", default=None, metavar="FILE",
                   help="a panel layout .json designed in the GUI's panel "
                        "designer (rec2mp4-designer): free-form information "
                        "blocks, per-block fonts/colours, background images, "
                        "and up to four panels (left/right/top/bottom) at "
                        "once. Replaces the built-in stacked panel; ignored "
                        "when --panel off")
    p.add_argument("--panel-info", default="all", metavar="CSV",
                   help="comma-separated panel sections: header, players, "
                        "trainer, opponents, teams, moves, evs, ivs, export, "
                        "footer — or 'all' (default). 'trainer' shows the "
                        "save state from PokeDNA's '<stem>.txt' sidecar "
                        "(playtime, Pokedex, BP, Frontier symbols) and draws "
                        "nothing when there is none. 'moves' lists each "
                        "mon's moves "
                        "(names read from YOUR ROM); 'evs'/'ivs' show the six "
                        "values plus a bold Sum (EV /510, IV /186, a star on "
                        "perfect IVs); 'export' shows the first lines of a "
                        "PokeDNA '<stem>.txt' info sidecar when present")
    p.add_argument("--panel-cycle", type=float, default=0.0, metavar="SECONDS",
                   help="cycle the stat views (moves/evs/ivs) over time: show "
                        "each page for SECONDS, looping for the whole video "
                        "(so a non-interactive viewer sees every page). "
                        "0 (default) keeps a single static panel. Needs Pillow")
    p.add_argument("--panel-cycle-pages", default="moves,evs,ivs",
                   metavar="CSV",
                   help="which stat pages --panel-cycle rotates through: any "
                        "of moves, evs, ivs (default all three)")
    p.add_argument("--intro-card", type=float, default=3.0, metavar="SECONDS",
                   help="open the video with SECONDS (default 3) of a card "
                        "carrying the opponent's pre-battle line — the Easy "
                        "Chat taunt the Frontier trainer says, read from "
                        "YOUR ROM by the opponent id in the record. The "
                        "record itself starts at \"<TRAINER> would like to "
                        "battle!\" and never had that line. 0 disables it; "
                        "opponents with no ROM speech (record-mix friends, "
                        "apprentices) get no card")
    p.add_argument("--end-card", type=float, default=3.0, metavar="SECONDS",
                   help="hold a trainer-state card on the last SECONDS of "
                        "the video (default 3): playtime, Pokedex, Battle "
                        "Points and the seven Frontier symbols, read from "
                        "PokeDNA's '<stem>.txt' sidecar. 0 disables it; a "
                        "record whose sidecar has no 'state.' block never "
                        "gets one (see docs/REC-SIDECAR.md)")
    p.add_argument("--pov", choices=("player", "opponent"), default="player",
                   help="whose side the camera is on. 'player' (default) is "
                        "the normal replay; 'opponent' flips the camera to "
                        "the opponent's side using the game's own link-replay "
                        "path (their team at the bottom, yours as the enemy). "
                        "EXPERIMENTAL: faithful only for genuine link-battle "
                        "records; for Frontier (vs-AI) records it is a "
                        "'what-if' — the opponent's moves are re-decided and "
                        "diverge after ~turn 1, so the video may end early")
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
    p.add_argument("--preview", nargs="?", type=int, const=PREVIEW_COUNT,
                   default=None, metavar="N",
                   help="do not convert: replay only the first seconds of "
                        f"each record and write N (default {PREVIEW_COUNT}) "
                        "composited preview PNGs — the real game frame with "
                        "the real panel/layout beside it — into the output "
                        "folder, so you can check the look before spending a "
                        "full conversion")
    p.add_argument("--preview-every", type=float,
                   default=PREVIEW_SPACING_SECONDS, metavar="SECONDS",
                   help="seconds of battle between preview frames "
                        f"(default {PREVIEW_SPACING_SECONDS:g})")
    p.add_argument("--preview-start", type=float,
                   default=PREVIEW_START_SECONDS, metavar="SECONDS",
                   help="seconds into the battle for the FIRST preview frame "
                        f"(default {PREVIEW_START_SECONDS:g})")
    p.add_argument("-j", "--jobs", type=int, default=0, metavar="N",
                   help="convert N records at once, each in its own process "
                        "(default 0 = one per CPU). A replay is strictly "
                        "sequential, so parallelism is per record: N records "
                        "on N cores. Use --jobs 1 for the classic in-process "
                        "loop (live interleaved logs, easier debugging)")
    p.add_argument("--no-facility-folders", action="store_true",
                   help="write every video straight into the output folder "
                        "instead of grouping them by facility "
                        "(out/Battle Arena/..., out/Battle Dome/...)")
    p.add_argument("--no-outcome-in-name", action="store_true",
                   help="do not append the battle's outcome to the file name "
                        "('... vs PSYCHIC NORTON [WON].mp4'). The outcome is "
                        "only known once the replay ends, so the finished "
                        "file is renamed at that point")
    p.add_argument("--encoder-threads", type=int, default=0, metavar="N",
                   help="threads each ffmpeg may use (default 0 = one CPU's "
                        "worth per parallel job, i.e. cores/jobs; with "
                        "--jobs 1 ffmpeg gets the whole machine). Left "
                        "uncapped, EVERY worker's x264 sizes itself for all "
                        "cores — 12 workers asking for 55 threads each is "
                        "kernel time, not throughput")
    p.add_argument("--max-seconds", type=float, default=1800, metavar="N",
                   help="give up on a replay after N seconds of emulated "
                        "time (default 1800)")
    p.add_argument("--pix-fmt", default=DEFAULT_PIX_FMT, metavar="FMT",
                   help="raw framebuffer pixel format handed to the "
                        f"encoder (default {DEFAULT_PIX_FMT})")
    return p


def _collect_recs(arg: str) -> list[Path]:
    """input argument -> list of .rec / .sav paths (empty list = usage error)."""
    p = Path(arg)
    if p.is_dir():
        return sorted(p.glob("*.rec")) + sorted(p.glob("*.sav"))
    if p.is_file():
        return [p]
    return []


def _is_save(p: Path) -> bool:
    """A .sav by name, or any non-.rec file big enough to hold sector 31."""
    if p.suffix.lower() == ".rec":
        return False
    if p.suffix.lower() == ".sav":
        return True
    try:
        return p.stat().st_size >= rec.SAV_MIN_SIZE
    except OSError:
        return False


def _extract_saves(paths: list[Path], rec_dir: str | None
                   ) -> tuple[list[Path], list[tuple[str, str, str]]]:
    """Replace every .sav in `paths` by the .rec extracted from its sector 31.

    The .rec is written as '<save stem>.rec' next to the save (or into
    rec_dir). An existing identical file is reused; a different one is left
    alone and the save is reported FAILED instead of clobbering it. Returns
    (paths with saves swapped for their .rec, summary rows for the saves).
    """
    out: list[Path] = []
    rows: list[tuple[str, str, str]] = []
    for p in paths:
        if not _is_save(p):
            out.append(p)
            continue
        try:
            sav = p.read_bytes()
            data = rec.extract(sav)
        except (OSError, rec.RecError) as exc:
            print(f"{p.name}: {exc}", file=sys.stderr)
            rows.append((p.name, "FAILED", str(exc)))
            continue
        errors = rec.validate(data)
        if errors:
            detail = "no usable Battle Record in sector 31: " + "; ".join(errors)
            print(f"{p.name}: {detail}", file=sys.stderr)
            rows.append((p.name, "FAILED", detail))
            continue
        target = (Path(rec_dir) if rec_dir else p.parent) / (p.stem + ".rec")
        if target.exists():
            if target.read_bytes() == data:
                print(f"{p.name}: sector 31 already exported as {target}")
            else:
                detail = (f"refusing to overwrite {target} — it holds a "
                          "different record (move it or use --rec-dir)")
                print(f"{p.name}: {detail}", file=sys.stderr)
                rows.append((p.name, "FAILED", detail))
                continue
        else:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except OSError as exc:
                print(f"{p.name}: cannot write {target}: {exc}", file=sys.stderr)
                rows.append((p.name, "FAILED", f"cannot write {target}: {exc}"))
                continue
            print(f"{p.name}: extracted sector 31 -> {target}")
        rows.append((p.name, "OK", f"record exported to {target.name}"))
        out.append(target)
    return out, rows


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


def _preview_one(rp: Path, settings: ConvertSettings, ctx,
                 args) -> tuple[str, str, str]:
    """--preview for one record: write composited preview PNGs, no video."""
    res = preview_frames(rp, settings, ctx=ctx, count=args.preview,
                         spacing_seconds=args.preview_every,
                         start_seconds=args.preview_start)
    if res["status"] != "OK":
        print(f"preview failed: {res['detail']}", file=sys.stderr)
        return (rp.name, res["status"], res["detail"])
    base = sanitize_filename(rp.stem)[:160] or "record"
    written = []
    for n, frame in enumerate(res["frames"], start=1):
        label = frame.get("label")
        path = ctx.outdir / (f"{base} - {label}.png" if label
                             else f"{base} - preview {n}.png")
        try:
            path.write_bytes(frame["png"])
        except OSError as exc:
            print(f"cannot write {path}: {exc}", file=sys.stderr)
            return (rp.name, "FAILED", f"write error: {exc}")
        written.append(path)
        print(f"preview {n}/{len(res['frames'])} "
              f"{label or ('@ %.1fs' % frame['seconds'])} -> {path}")
    size = res["frames"][0]["size"]
    return (rp.name, "OK",
            f"{len(written)} preview PNG(s) {size[0]}x{size[1]} "
            f"-> {written[0].parent}")


def main(argv: list[str] | None = None) -> int:
    # Needed before any process pool if this ever runs frozen; a no-op
    # otherwise (see convert_batch's spawn requirements).
    import multiprocessing
    multiprocessing.freeze_support()
    args = _build_parser().parse_args(argv)

    rec_paths = _collect_recs(args.input)
    if not rec_paths:
        print(f"error: {args.input!r} is not a .rec/.sav file or a folder "
              "containing .rec/.sav files", file=sys.stderr)
        return 2
    n_inputs = len(rec_paths)
    rec_paths, save_rows = _extract_saves(rec_paths, args.rec_dir)
    save_failures = sum(1 for r in save_rows if r[1] != "OK")
    if args.extract_only:
        for n, status, detail in save_rows:
            print(f"{n:<40}  {status:<7}  {detail}")
        n_ok = len(save_rows) - save_failures
        print(f"\n{n_ok}/{len(save_rows)} save(s) exported, "
              f"{save_failures} failed.")
        return 1 if save_failures or not save_rows else 0
    if not rec_paths:
        print("error: no usable record among the inputs", file=sys.stderr)
        return 1

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
            panel_info=args.panel_info, pov=args.pov,
            panel_cycle=args.panel_cycle,
            panel_cycle_pages=tuple(
                x.strip() for x in args.panel_cycle_pages.split(",")
                if x.strip()),
            layout=args.layout, jobs=args.jobs, end_card=args.end_card,
            intro_card=args.intro_card,
            encoder_threads=args.encoder_threads,
            facility_folders=not args.no_facility_folders,
            outcome_in_name=not args.no_outcome_in_name)
        try:
            ctx = load_context(settings)
        except PipelineError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    # ------------------------------------------------------------------
    # Per-record work: validate -> summarize -> (inject -> replay -> mp4).
    # A bad record or a failed replay never stops the batch.
    # ------------------------------------------------------------------
    results: list[tuple[str, str, str]] = []    # (name, status, detail)
    failures = 0
    # Saves whose record could not be exported are already counted as failed.
    results.extend(r for r in save_rows if r[1] != "OK")
    failures += save_failures

    if args.info_only:
        for rp in rec_paths:
            print(f"\n=== {rp.name} ===")
            row = _info_only_one(rp)
            results.append(row)
            if row[1] != "OK":
                failures += 1
    elif args.preview is not None:
        for rp in rec_paths:
            print(f"\n=== {rp.name} ===")
            row = _preview_one(rp, settings, ctx, args)
            results.append(row)
            if row[1] != "OK":
                failures += 1
    else:
        # jobs > 1 interleaves records across processes, so each record's log
        # is buffered and printed as one block when it finishes; the classic
        # sequential mode keeps printing live.
        n_jobs = resolve_jobs(settings.jobs, len(rec_paths))
        rows: dict = {}

        def on_start(i, path):
            if n_jobs == 1:
                print(f"\n=== {path.name} ===")

        def on_result(i, res):
            if n_jobs > 1:
                print(f"\n=== {rec_paths[i].name} ===")
                for line in res.get("log_lines") or []:
                    if line.startswith("! "):
                        print(line[2:], file=sys.stderr)
                    else:
                        print(line)
            rows[i] = (res["name"], res["status"], res["detail"])

        try:
            convert_batch(rec_paths, settings, ctx=ctx, on_start=on_start,
                          on_result=on_result)
        except PipelineError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        for i, rp in enumerate(rec_paths):
            row = rows.get(i, (rp.name, "FAILED", "not converted"))
            results.append(row)
            if row[1] != "OK":
                failures += 1

    # ------------------------------------------------------------------
    # Batch summary
    # ------------------------------------------------------------------
    if n_inputs > 1:
        width = max(len(n) for n, _, _ in results)
        print("\n" + "=" * 72)
        for n, status, detail in results:
            print(f"{n:<{width}}  {status:<7}  {detail}")
        print("=" * 72)
    ok = len(results) - failures
    print(f"\n{ok}/{len(results)} record(s) OK, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
