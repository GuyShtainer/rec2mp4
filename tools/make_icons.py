#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""Draw the rec2mp4 application icon and write .png / .ico / .icns.

    python tools/make_icons.py            # -> assets/

The artwork is generated, not stored: one function draws it at any size, so
every icon size is rendered NATIVELY instead of downsampling a big PNG (which
is what turns a 16 px taskbar icon into mush). Sizes at or below 32 px drop
the fine detail automatically — at that size scanlines and sprocket holes are
noise, not detail.

Design notes: a dark screen with a gold play triangle, film sprockets down
both margins and a red REC dot — "a recording, played back". It is deliberately
ORIGINAL: no Poké Ball, no console silhouette, no game-derived shape or
colour scheme, so the icon carries none of the IP exposure the rest of the
project is careful about (docs/../ip-publishing-policy). Colours match the
info panel rec2mp4 draws (gold #FFCB4F on #10141A).

Needs Pillow. .icns is built with macOS's own `iconutil`, so it is produced
only on macOS; the .png and .ico are written everywhere.
"""

from __future__ import annotations

import struct
import subprocess
import sys
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

# Palette — the panel's, so the icon and the videos look related.
BG_TOP = (30, 38, 60)
BG_BOTTOM = (12, 15, 22)
SCREEN = (8, 11, 17)
EDGE = (46, 56, 72)
GOLD = (255, 203, 79)
RED = (235, 110, 110)
SPROCKET = (54, 65, 84)

# Sizes Windows wants in a .ico, and the macOS .iconset roster.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
ICNS_SPEC = (  # (pixel size, iconset filename)
    (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
)


def _gradient(size: int, top, bottom):
    """A vertical gradient the size of the icon (Pillow only, no numpy)."""
    from PIL import Image
    strip = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        strip.putpixel((0, y), tuple(int(a + (b - a) * t)
                                     for a, b in zip(top, bottom)))
    return strip.resize((size, size), Image.BICUBIC)


def draw_icon(size: int):
    """Render the icon at `size` px, returning an RGBA PIL image.

    Everything is expressed as a fraction of `size`, so the same code draws a
    crisp 16 px favicon and a 1024 px Retina icon. `detail` gates the parts
    that only read above ~32 px.
    """
    from PIL import Image, ImageDraw, ImageFilter
    s = size
    # Two thresholds, both found by looking at the contact sheet: below 48 px
    # the screen frame and the REC dot stop reading and only cost contrast, and
    # sprocket holes need 64 px before they are holes rather than smudges.
    detail = s >= 48
    fine = s >= 64
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    def px(f):                       # fraction of the icon -> whole pixels
        return max(1, int(round(f * s)))

    # ---- body: a rounded square, inset so macOS' grid spacing looks right
    inset = px(0.055)
    body_box = [inset, inset, s - 1 - inset, s - 1 - inset]
    radius = px(0.22)

    if detail:                                   # soft drop shadow
        shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            [body_box[0], body_box[1] + px(0.02),
             body_box[2], body_box[3] + px(0.02)],
            radius=radius, fill=(0, 0, 0, 90))
        img.alpha_composite(shadow.filter(
            ImageFilter.GaussianBlur(max(1, px(0.02)))))

    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(body_box, radius=radius, fill=255)
    img.paste(_gradient(s, BG_TOP, BG_BOTTOM), (0, 0), mask)

    draw = ImageDraw.Draw(img)
    if detail:                                   # top inner highlight
        draw.rounded_rectangle(body_box, radius=radius,
                               outline=(70, 84, 108, 140), width=px(0.006))

    # ---- screen (large sizes only: at 32 px it is just a dark ring)
    m = px(0.20) if fine else px(0.16)
    scr = [inset + m, inset + px(0.155), s - 1 - inset - m, s - 1 - inset
           - px(0.155)]
    if detail:
        draw.rounded_rectangle(scr, radius=px(0.05), fill=SCREEN + (255,),
                               outline=EDGE + (255,), width=max(1, px(0.008)))

    if fine:                                     # scanlines, barely there
        step = max(3, px(0.034))
        line = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ld = ImageDraw.Draw(line)
        for y in range(int(scr[1]) + step, int(scr[3]), step):
            ld.line([(scr[0] + px(0.01), y), (scr[2] - px(0.01), y)],
                    fill=(255, 255, 255, 7), width=1)
        # a soft diagonal glare across the top-left of the screen
        glare = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(glare).polygon(
            [(scr[0], scr[1]), (scr[0] + (scr[2] - scr[0]) * 0.62, scr[1]),
             (scr[0], scr[1] + (scr[3] - scr[1]) * 0.52)],
            fill=(255, 255, 255, 10))
        line.alpha_composite(glare.filter(
            ImageFilter.GaussianBlur(max(1, px(0.02)))))
        img.alpha_composite(line)
        draw = ImageDraw.Draw(img)

    # ---- film sprockets down both margins
    if fine:
        holes = 4
        hw, hh = px(0.055), px(0.085)
        span = scr[3] - scr[1]
        gap = (span - holes * hh) / (holes + 1)
        for i in range(holes):
            y0 = scr[1] + gap * (i + 1) + hh * i
            for x0 in (inset + px(0.045), s - 1 - inset - px(0.045) - hw):
                draw.rounded_rectangle([x0, y0, x0 + hw, y0 + hh],
                                       radius=px(0.018),
                                       fill=SPROCKET + (255,))

    # ---- play triangle
    # Optically centred, not mathematically: a triangle's mass sits toward its
    # base, so a geometrically centred one always looks shoved right.
    cx = (scr[0] + scr[2]) / 2 - (scr[2] - scr[0]) * 0.035
    cy = (scr[1] + scr[3]) / 2
    r = (scr[3] - scr[1]) * (0.29 if detail else 0.44)
    tri = [(cx - r * 0.66, cy - r), (cx - r * 0.66, cy + r),
           (cx + r * 0.92, cy)]
    if fine:                                     # a little glow under it
        glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(glow).polygon(tri, fill=GOLD + (70,))
        img.alpha_composite(glow.filter(
            ImageFilter.GaussianBlur(max(1, px(0.03)))))
        draw = ImageDraw.Draw(img)
    draw.polygon(tri, fill=GOLD + (255,))
    if detail:
        # Re-stroking the outline with a curved joint rounds the corners —
        # a sharp-cornered triangle looks dated next to modern app icons.
        # Repeat the first TWO points: ending the polyline exactly on its
        # start leaves that vertex an end cap, not a join, and it shows up as
        # a notch in the corner.
        draw.line(tri + [tri[0], tri[1]], fill=GOLD + (255,),
                  width=px(0.035), joint="curve")

    # ---- REC dot, top-left inside the screen
    if fine:
        d = px(0.052)
        x0, y0 = scr[0] + px(0.045), scr[1] + px(0.045)
        draw.ellipse([x0, y0, x0 + d, y0 + d], fill=RED + (255,))
    return img


def write_ico(path: Path, sizes=ICO_SIZES) -> None:
    """Write a multi-size .ico with a NATIVELY rendered image per size.

    Pillow's own ICO writer downsamples one image to every size; drawing each
    size instead is the whole point here. Entries are PNG-compressed, which
    every Windows since Vista reads.
    """
    images = []
    for size in sizes:
        buf = BytesIO()
        draw_icon(size).save(buf, "PNG")
        images.append(buf.getvalue())
    header = struct.pack("<HHH", 0, 1, len(images))     # reserved, type=icon
    offset = len(header) + 16 * len(images)
    entries, payloads = b"", b""
    for size, data in zip(sizes, images):
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,     # width  (0 means 256)
            0 if size >= 256 else size,     # height
            0, 0,                           # palette, reserved
            1, 32,                          # colour planes, bits per pixel
            len(data), offset)
        payloads += data
        offset += len(data)
    path.write_bytes(header + entries + payloads)


def write_icns(path: Path, iconset_dir: Path) -> bool:
    """Build a .icns via macOS' iconutil. False (with a note) elsewhere."""
    iconset_dir.mkdir(parents=True, exist_ok=True)
    for size, name in ICNS_SPEC:
        draw_icon(size).save(iconset_dir / name, "PNG")
    if sys.platform != "darwin":
        print(f"   .icns skipped (needs macOS iconutil); "
              f"iconset written to {iconset_dir}")
        return False
    res = subprocess.run(["iconutil", "-c", "icns", str(iconset_dir),
                          "-o", str(path)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0:
        print("   iconutil failed: "
              + res.stderr.decode("utf-8", "replace").strip())
        return False
    return True


def main(argv=None) -> int:
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("error: drawing the icon needs Pillow "
              "(python -m pip install pillow)", file=sys.stderr)
        return 2
    ASSETS.mkdir(parents=True, exist_ok=True)

    png = ASSETS / "icon.png"
    draw_icon(1024).save(png, "PNG")
    print(f"   {png}  (1024x1024)")

    ico = ASSETS / "rec2mp4.ico"
    write_ico(ico)
    print(f"   {ico}  ({', '.join(str(s) for s in ICO_SIZES)} px, "
          "each drawn natively)")

    icns = ASSETS / "rec2mp4.icns"
    if write_icns(icns, ASSETS / "rec2mp4.iconset"):
        print(f"   {icns}")
    # A contact sheet makes it obvious when a small size stops reading.
    from PIL import Image
    sheet_sizes = (16, 32, 48, 64, 128, 256)
    pad = 12
    w = sum(sheet_sizes) + pad * (len(sheet_sizes) + 1)
    sheet = Image.new("RGBA", (w, max(sheet_sizes) + 2 * pad), (32, 34, 40, 255))
    x = pad
    for size in sheet_sizes:
        sheet.alpha_composite(draw_icon(size), (x, (sheet.height - size) // 2))
        x += size + pad
    sheet.save(ASSETS / "icon-sizes.png")
    print(f"   {ASSETS / 'icon-sizes.png'}  (every size, side by side)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
