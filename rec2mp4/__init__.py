# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""rec2mp4 — turn Pokemon Emerald Battle Record exports (.rec) into .mp4 videos.

A .rec file is a raw dump of save sector 31 (4096 bytes) — the Frontier Pass
"Battle Record" that Emerald stores after a recordable battle. This package
parses/validates those records (rec2mp4.rec), injects them into a save,
replays them in the real engine under mGBA (rec2mp4.driver / rec2mp4.states)
and encodes the captured frames + audio with ffmpeg (rec2mp4.video).
"""

__version__ = "0.1.0"
