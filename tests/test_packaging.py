#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""Tests for the desktop-icon packaging: the generated icon and the launchers.

Plain python, no pytest — same style as the other suites. Exits non-zero on
failure. Asset-free (no ROM, no emulator, no ffmpeg): the icon sections need
Pillow and skip cleanly without it, the .app section builds a bundle into a
temp dir on macOS and skips elsewhere, and the Windows shortcut script is
checked as text because it cannot be executed here. Exit 2 marks a vacuous run.

Run:  python3 tests/test_packaging.py
"""

import os
import plistlib
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

_checks = 0


def ok(cond, msg):
    # No bare `assert`: it would be stripped under `python3 -O`.
    global _checks
    _checks += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def _pil_available() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def test_icon_drawing():
    print("-- make_icons.draw_icon renders every size natively")
    if not _pil_available():
        print("   SKIP: Pillow not importable in this interpreter")
        return
    import make_icons

    for size in (16, 32, 64, 256, 1024):
        img = make_icons.draw_icon(size)
        ok(img.size == (size, size), f"{size}px icon came out {img.size}")
        ok(img.mode == "RGBA", f"{size}px icon must keep transparency")
        alpha = img.getchannel("A")
        ok(alpha.getextrema()[1] == 255, f"{size}px icon is fully transparent")
        ok(alpha.getextrema()[0] == 0,
           f"{size}px icon fills its whole square — the corners must be round")
        # the gold play triangle must actually be there
        rgb = img.convert("RGB").tobytes()
        golds = sum(1 for i in range(0, len(rgb), 3)
                    if rgb[i] > 200 and rgb[i + 1] > 150 and rgb[i + 2] < 120)
        ok(golds > (size * size) * 0.02,
           f"{size}px icon has almost no gold ({golds}px) — no play triangle?")

    # Native rendering is the point: a 16px draw must NOT equal a downsampled
    # 1024px draw, or every small icon would be the mush this avoids.
    from PIL import Image
    native = make_icons.draw_icon(16)
    shrunk = make_icons.draw_icon(1024).resize((16, 16), Image.LANCZOS)
    ok(native.tobytes() != shrunk.tobytes(),
       "16px icon is just a downsample — the size-specific detail is gone")


def test_ico_container():
    print("-- the .ico container: real multi-size, PNG payloads")
    if not _pil_available():
        print("   SKIP: Pillow not importable in this interpreter")
        return
    import make_icons
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "test.ico"
        make_icons.write_ico(path)
        blob = path.read_bytes()
        reserved, kind, count = struct.unpack("<HHH", blob[:6])
        ok(reserved == 0 and kind == 1, "not an icon-type ICO header")
        ok(count == len(make_icons.ICO_SIZES),
           f"ICO holds {count} images, expected {len(make_icons.ICO_SIZES)}")
        seen = []
        for i in range(count):
            off = 6 + i * 16
            w, h, _pal, _res, planes, bpp, nbytes, data_off = struct.unpack(
                "<BBBBHHII", blob[off:off + 16])
            size = 256 if w == 0 else w
            seen.append(size)
            ok(w == h, f"entry {i} is not square")
            ok(planes == 1 and bpp == 32, f"entry {i} is not 32-bit")
            payload = blob[data_off:data_off + nbytes]
            ok(payload[:8] == b"\x89PNG\r\n\x1a\n",
               f"entry {i} ({size}px) payload is not a PNG")
            from PIL import Image
            import io as _io
            ok(Image.open(_io.BytesIO(payload)).size == (size, size),
               f"entry {i} payload is not {size}x{size}")
        ok(sorted(seen) == sorted(make_icons.ICO_SIZES),
           f"ICO sizes {seen} != {make_icons.ICO_SIZES}")


def test_shipped_assets():
    print("-- the checked-in icon files exist and are what they claim")
    assets = ROOT / "assets"
    if not assets.is_dir():
        print("   SKIP: assets/ not generated yet (run tools/make_icons.py)")
        return
    png = assets / "icon.png"
    ico = assets / "rec2mp4.ico"
    ok(png.is_file() and png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n",
       "assets/icon.png missing or not a PNG")
    ok(ico.is_file() and struct.unpack("<HHH", ico.read_bytes()[:6])[1] == 1,
       "assets/rec2mp4.ico missing or not an ICO")
    icns = assets / "rec2mp4.icns"
    if icns.is_file():
        ok(icns.read_bytes()[:4] == b"icns", "rec2mp4.icns has no icns magic")


def test_macos_app_bundle():
    print("-- the .app bundle: structure, plist, launcher")
    if sys.platform != "darwin":
        print("   SKIP: .app bundles are macOS-only")
        return
    import make_macos_app
    with tempfile.TemporaryDirectory() as td:
        icon = ROOT / "assets" / "rec2mp4.icns"
        app = make_macos_app.build(Path(td), "/usr/bin/python3", ROOT,
                                   icon if icon.is_file() else None)
        ok(app.is_dir() and app.name == "rec2mp4.app", f"bad bundle: {app}")
        exe = app / "Contents" / "MacOS" / "rec2mp4"
        plist_path = app / "Contents" / "Info.plist"
        ok(plist_path.is_file(), "no Info.plist")
        ok(exe.is_file(), "no launcher executable")
        ok(os.access(exe, os.X_OK), "the launcher is not executable — "
                                    "double-clicking it would do nothing")
        with open(plist_path, "rb") as fh:
            info = plistlib.load(fh)
        for key in ("CFBundleName", "CFBundleExecutable", "CFBundleIdentifier",
                    "CFBundlePackageType", "CFBundleShortVersionString"):
            ok(key in info, f"Info.plist is missing {key}")
        ok(info["CFBundleExecutable"] == "rec2mp4",
           "CFBundleExecutable must match the file in MacOS/")
        ok(info["CFBundlePackageType"] == "APPL", "not marked as an app")
        ok(info.get("NSHighResolutionCapable") is True,
           "without NSHighResolutionCapable Tk renders at 1x on Retina")
        if icon.is_file():
            ok(info.get("CFBundleIconFile") == "rec2mp4",
               "the icon is not referenced from Info.plist")
            ok((app / "Contents" / "Resources" / "rec2mp4.icns").is_file(),
               "the icon was not copied into Resources/")

        text = exe.read_text()
        ok(str(ROOT) in text, "the working copy path was not baked in")
        ok("/usr/bin/python3" in text, "the interpreter was not baked in")
        ok("REC2MP4_PYTHON" in text and "REC2MP4_HOME" in text,
           "the launcher must honour both env overrides")
        ok("osascript" in text,
           "a missing interpreter must surface as a dialog, not silence")
        ok("-m rec2mp4.gui" in text, "the launcher does not start the GUI")

        # A second build over the top must not fail or nest bundles.
        app2 = make_macos_app.build(Path(td), "/usr/bin/python3", ROOT, None)
        ok(app2 == app and not (app / "Contents" / "Contents").exists(),
           "rebuilding into the same directory nested the bundle")


def test_windows_launchers_present():
    print("-- the Windows launchers (checked as text; cannot run here)")
    ps1 = ROOT / "tools" / "make_windows_shortcut.ps1"
    cmd = ROOT / "tools" / "rec2mp4-gui.cmd"
    ok(ps1.is_file(), "tools/make_windows_shortcut.ps1 is missing")
    ok(cmd.is_file(), "tools/rec2mp4-gui.cmd is missing")
    text = ps1.read_text()
    for needle in ("CreateShortcut", "TargetPath", "WorkingDirectory",
                   "IconLocation", "-m rec2mp4.gui", "pythonw"):
        ok(needle in text, f"the shortcut script never sets {needle}")
    ok("rec2mp4.ico" in text, "the shortcut does not point at the icon")
    body = cmd.read_text()
    ok("rec2mp4.gui" in body and "REC2MP4_PYTHON" in body,
       "the .cmd launcher is not wired to the GUI / the env override")
    ok(cmd.read_bytes().count(b"\x00") == 0, "the .cmd must stay plain text")


def test_entry_points_are_freeze_safe():
    print("-- entry points call freeze_support (spawn + a frozen build)")
    for mod in ("rec2mp4/gui.py", "rec2mp4/__main__.py"):
        text = (ROOT / mod).read_text()
        ok("freeze_support()" in text,
           f"{mod} does not call multiprocessing.freeze_support() — a frozen "
           "build would spawn new apps instead of conversion workers")


def main() -> int:
    test_icon_drawing()
    test_ico_container()
    test_shipped_assets()
    test_macos_app_bundle()
    test_windows_launchers_present()
    test_entry_points_are_freeze_safe()
    if _checks == 0:
        print("FAIL: vacuous run (no checks executed)")
        return 2
    print(f"\nPASS: {_checks} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
