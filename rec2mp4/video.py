# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
#
# rec2mp4/video.py — Mp4Writer: stream raw GBA video frames and s16le PCM audio
# into a finished .mp4 via ffmpeg.
#
# Two-stage design (robust for unbounded streams):
#   stage 1: raw 240x160 4-byte-per-pixel frames are piped to an ffmpeg
#            subprocess that encodes a temporary video-only .mp4 (libx264,
#            nearest-neighbor integer upscale); audio bytes are appended to a
#            temporary raw s16le file as they arrive.
#   stage 2: close() muxes the two — video stream copied, audio encoded AAC
#            192k — with -shortest and -movflags +faststart, then atomically
#            renames onto out_path only on success. Temp files are cleaned up
#            in a finally block either way.
#
# Stdlib only (subprocess / tempfile / shutil / os).

import os
import shutil
import subprocess
import tempfile

# Exact GBA frame rate as a fraction: 16777216 / 280896 ≈ 59.7275005696 fps.
GBA_FPS = (16777216, 280896)

WIDTH = 240
HEIGHT = 160
BYTES_PER_PIXEL = 4
FRAME_BYTES = WIDTH * HEIGHT * BYTES_PER_PIXEL  # 153600

# Byte orders the emulator framebuffer may plausibly use. mGBA 0.10.x on
# macOS-arm64 emits R,G,B,X per pixel -> ffmpeg "rgb0"; the driver passes
# whichever it verified. "rgba"/"bgra" honor alpha, "rgb0"/"bgr0" ignore the
# 4th byte, "argb"/"abgr" are the alpha-first variants.
_ALLOWED_PIX_FMTS = ("rgba", "bgra", "rgb0", "bgr0", "argb", "abgr")

_FFMPEG_DEFAULT = "/opt/homebrew/bin/ffmpeg"

_STDERR_TAIL_BYTES = 4000


def _find_ffmpeg():
    """Resolve the ffmpeg binary: Homebrew path first, then PATH."""
    if os.path.isfile(_FFMPEG_DEFAULT) and os.access(_FFMPEG_DEFAULT, os.X_OK):
        return _FFMPEG_DEFAULT
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError(
        "ffmpeg not found: expected %s or an 'ffmpeg' on PATH "
        "(install with: brew install ffmpeg)" % _FFMPEG_DEFAULT
    )


def _tail(text, limit=2000):
    text = (text or "").strip()
    if len(text) > limit:
        text = "..." + text[-limit:]
    return text


class Mp4Writer:
    """Incremental 240x160 GBA video + stereo PCM -> .mp4 encoder.

    Usage:
        w = Mp4Writer("out/battle.mp4", scale=4, audio_rate=32768)
        for each frame:  w.add_video(rgba_bytes)   # 153600 bytes
        as audio drains: w.add_audio(s16le_stereo_bytes)
        path = w.close()

    close() finalizes and atomically renames the finished file onto out_path;
    it raises RuntimeError (with an ffmpeg stderr tail) on any encoder/muxer
    failure and never leaves a partial file at out_path.
    """

    def __init__(self, out_path, scale=4, audio_rate=32768, audio=True,
                 log=print, pix_fmt="rgb0", threads=0):
        # Default pix_fmt "rgb0": the mGBA framebuffer is R,G,B,X — the 4th
        # byte is padding, not alpha (verified in emulator-stack.md).
        if pix_fmt not in _ALLOWED_PIX_FMTS:
            raise ValueError(
                "pix_fmt %r not supported; expected one of %s"
                % (pix_fmt, ", ".join(_ALLOWED_PIX_FMTS))
            )
        scale = int(scale)
        if scale < 1:
            raise ValueError("scale must be a positive integer, got %d" % scale)
        audio_rate = int(audio_rate)
        if audio and audio_rate <= 0:
            raise ValueError("audio_rate must be > 0, got %d" % audio_rate)

        self._out_path = os.path.abspath(out_path)
        self._scale = scale
        self._audio_rate = audio_rate
        self._audio = bool(audio)
        self._log = log if log is not None else (lambda *a, **k: None)
        self._pix_fmt = pix_fmt
        # 0 = let ffmpeg decide (it picks ~1.5x the core count for x264).
        # A parallel batch MUST cap this: N workers x 55 threads each is how
        # you turn 12 cores into a context-switching heap.
        self._threads = max(0, int(threads or 0))
        self._ffmpeg = _find_ffmpeg()

        self._frames = 0
        self._audio_bytes = 0
        self._closed = False
        self._finalized = False
        self._proc = None
        self._audio_fh = None
        self._stderr_fh = None
        self._video_tmp = None
        self._audio_tmp = None
        self._stderr_path = None
        self._final_tmp = None

        out_dir = os.path.dirname(self._out_path) or "."
        os.makedirs(out_dir, exist_ok=True)
        self._out_dir = out_dir

        # All temp files live next to out_path (dot-prefixed) so every rename
        # in the pipeline stays on one filesystem and os.replace() is atomic.
        base = os.path.basename(self._out_path)
        fd, self._video_tmp = tempfile.mkstemp(
            prefix=".%s.video." % base, suffix=".mp4", dir=out_dir)
        os.close(fd)
        fd, self._stderr_path = tempfile.mkstemp(
            prefix=".%s.fflog." % base, suffix=".log", dir=out_dir)
        os.close(fd)
        if self._audio:
            fd, self._audio_tmp = tempfile.mkstemp(
                prefix=".%s.audio." % base, suffix=".s16le", dir=out_dir)
            self._audio_fh = os.fdopen(fd, "wb")

        cmd = [
            self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo",
            "-pix_fmt", self._pix_fmt,
            "-s", "%dx%d" % (WIDTH, HEIGHT),
            "-framerate", "%d/%d" % GBA_FPS,
            "-i", "pipe:0",
        ]
        if scale != 1:
            cmd += ["-vf", "scale=iw*%d:ih*%d:flags=neighbor" % (scale, scale)]
        if self._threads:
            cmd += ["-threads", str(self._threads),
                    "-filter_threads", str(self._threads)]
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            self._video_tmp,
        ]

        # stderr goes to a file: bounded ffmpeg chatter (-loglevel error) and
        # no pipe-buffer deadlock; read back as the error tail on failure.
        self._stderr_fh = open(self._stderr_path, "wb")
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self._stderr_fh,
            )
        except OSError as exc:
            self._cleanup()
            raise RuntimeError("failed to launch ffmpeg (%s): %s"
                               % (self._ffmpeg, exc))

        self._log("[video] encoder started: %dx%d x%d (%s) -> %s%s%s"
                  % (WIDTH, HEIGHT, scale, pix_fmt, self._out_path,
                     ", audio %d Hz" % audio_rate if self._audio
                     else ", no audio",
                     ", %d thread(s)" % self._threads if self._threads
                     else ""))

    # ---------------------------------------------------------------- feed

    def add_video(self, frame_rgba):
        """Feed exactly one 240x160 frame, 4 bytes/pixel (byte order per the
        pix_fmt chosen at construction)."""
        if self._closed:
            raise RuntimeError("Mp4Writer is closed")
        n = len(frame_rgba)
        if n != FRAME_BYTES:
            raise ValueError(
                "add_video: expected %d bytes (%dx%d, %d bytes/pixel), got %d"
                % (FRAME_BYTES, WIDTH, HEIGHT, BYTES_PER_PIXEL, n))
        try:
            self._proc.stdin.write(frame_rgba)
        except (BrokenPipeError, OSError):
            # ffmpeg died under us -- surface its stderr, not just EPIPE.
            tail = self._collect_encoder_failure()
            raise RuntimeError(
                "ffmpeg video encoder exited early after %d frames:\n%s"
                % (self._frames, tail or "(no ffmpeg stderr captured)"))
        self._frames += 1

    def add_audio(self, samples):
        """Feed interleaved stereo s16le PCM (any chunk size, multiple of 4
        bytes = one L+R sample pair). Silently dropped when audio=False."""
        if self._closed:
            raise RuntimeError("Mp4Writer is closed")
        if not self._audio:
            return
        n = len(samples)
        if n == 0:
            return
        if n % 4 != 0:
            raise ValueError(
                "add_audio: length must be a multiple of 4 bytes "
                "(interleaved stereo s16le), got %d" % n)
        self._audio_fh.write(samples)
        self._audio_bytes += n

    # ------------------------------------------------------------- finalize

    def close(self):
        """Finalize: finish the video encode, mux in the audio, atomically
        rename onto out_path. Returns out_path; raises RuntimeError on any
        ffmpeg failure. Temp files are removed in all cases."""
        if self._finalized:
            return self._out_path
        if self._closed:
            raise RuntimeError("Mp4Writer.close() already failed once")
        self._closed = True
        try:
            # ---- stage 1: drain and finish the video-only encode
            try:
                self._proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            rc = self._proc.wait()
            if self._audio_fh is not None:
                self._audio_fh.close()
                self._audio_fh = None
            if self._stderr_fh is not None:
                try:
                    self._stderr_fh.close()
                except OSError:
                    pass
                self._stderr_fh = None
            if rc != 0:
                raise RuntimeError(
                    "ffmpeg video encode failed (exit %d) after %d frames:\n%s"
                    % (rc, self._frames,
                       _tail(self._read_stderr_log())
                       or "(no ffmpeg stderr captured)"))
            if self._frames == 0:
                raise RuntimeError("no video frames were written")

            # ---- stage 2: mux (or plain rename when there is no audio)
            use_audio = self._audio and self._audio_bytes > 0
            if self._audio and self._audio_bytes == 0:
                self._log("[video] warning: audio enabled but no samples "
                          "received; writing video-only mp4")

            if use_audio:
                base = os.path.basename(self._out_path)
                fd, self._final_tmp = tempfile.mkstemp(
                    prefix=".%s.mux." % base, suffix=".mp4",
                    dir=self._out_dir)
                os.close(fd)
                cmd = [
                    self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", self._video_tmp,
                    "-f", "s16le", "-ar", str(self._audio_rate), "-ac", "2",
                    "-i", self._audio_tmp]
                if self._threads:
                    cmd += ["-threads", str(self._threads)]
                cmd += [
                    "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k",
                    "-shortest",
                    "-movflags", "+faststart",
                    self._final_tmp,
                ]
                res = subprocess.run(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                if res.returncode != 0:
                    raise RuntimeError(
                        "ffmpeg audio mux failed (exit %d):\n%s"
                        % (res.returncode,
                           _tail(res.stderr.decode("utf-8", "replace"))
                           or "(no ffmpeg stderr captured)"))
                os.replace(self._final_tmp, self._out_path)
                self._final_tmp = None
            else:
                # audio mux skipped entirely: stage 1 already produced a
                # +faststart mp4 in the destination directory -> atomic rename.
                os.replace(self._video_tmp, self._out_path)
                self._video_tmp = None

            self._finalized = True
            secs = self._frames * GBA_FPS[1] / GBA_FPS[0]
            self._log("[video] wrote %s (%d frames, %.2f s%s)"
                      % (self._out_path, self._frames, secs,
                         ", %.2f s audio" % (self._audio_bytes / 4.0
                                             / self._audio_rate)
                         if use_audio else ", no audio"))
            return self._out_path
        finally:
            self._cleanup()

    # -------------------------------------------------------------- helpers

    def _read_stderr_log(self):
        if self._stderr_fh is not None:
            try:
                self._stderr_fh.flush()
            except (OSError, ValueError):
                pass
        if not self._stderr_path or not os.path.exists(self._stderr_path):
            return ""
        try:
            with open(self._stderr_path, "rb") as fh:
                try:
                    fh.seek(-_STDERR_TAIL_BYTES, os.SEEK_END)
                except OSError:
                    pass  # file shorter than the tail window
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def _collect_encoder_failure(self):
        """After a broken pipe: reap ffmpeg and return its stderr tail.
        Marks the writer closed and removes temp files."""
        self._closed = True
        try:
            if self._proc is not None:
                try:
                    self._proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                self._proc.wait()
        except OSError:
            pass
        tail = _tail(self._read_stderr_log())
        self._cleanup()
        return tail

    def _cleanup(self):
        """Best-effort: close handles, reap the subprocess, delete temps.
        Never raises."""
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
            except (BrokenPipeError, OSError, AttributeError):
                pass
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
            except OSError:
                pass
        for fh_attr in ("_audio_fh", "_stderr_fh"):
            fh = getattr(self, fh_attr, None)
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
                setattr(self, fh_attr, None)
        for path_attr in ("_video_tmp", "_audio_tmp", "_stderr_path",
                          "_final_tmp"):
            path = getattr(self, path_attr, None)
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                setattr(self, path_attr, None)

    def __del__(self):
        try:
            self._cleanup()
        except Exception:
            pass


# --------------------------------------------------------------- self-test

if __name__ == "__main__":
    import json
    import math
    import struct
    import sys

    def _selftest():
        ok = True

        def check(cond, label):
            nonlocal ok
            print("  %s %s" % ("ok " if cond else "FAIL", label))
            if not cond:
                ok = False

        ffprobe = os.path.join(os.path.dirname(_find_ffmpeg()), "ffprobe")
        if not (os.path.isfile(ffprobe) and os.access(ffprobe, os.X_OK)):
            ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            print("FAIL: ffprobe not found, cannot verify output")
            return 1

        def probe(path):
            res = subprocess.run(
                [ffprobe, "-v", "error", "-print_format", "json",
                 "-show_streams", "-show_format", path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if res.returncode != 0:
                print("FAIL: ffprobe error: %s"
                      % res.stderr.decode("utf-8", "replace").strip())
                return None
            return json.loads(res.stdout.decode("utf-8"))

        tmpdir = tempfile.mkdtemp(prefix="rec2mp4-video-selftest-")
        try:
            # ---- A/V test: 120 frames of moving gradient + 440 Hz tone
            out_path = os.path.join(tmpdir, "selftest_av.mp4")
            scale = 2
            rate = 32768
            n_frames = 120
            w = Mp4Writer(out_path, scale=scale, audio_rate=rate, audio=True,
                          pix_fmt="rgba")

            # wrong-size frame must raise ValueError (and not kill the writer)
            try:
                w.add_video(b"\x00" * 16)
                check(False, "short add_video raises ValueError")
            except ValueError:
                check(True, "short add_video raises ValueError")
            try:
                w.add_audio(b"\x00" * 3)
                check(False, "odd add_audio raises ValueError")
            except ValueError:
                check(True, "odd add_audio raises ValueError")

            emitted = 0
            amp = 9830  # ~0.3 full scale
            for f in range(n_frames):
                frame = bytearray(FRAME_BYTES)
                i = 0
                for y in range(HEIGHT):
                    g = (y + f * 2) & 0xFF
                    for x in range(WIDTH):
                        frame[i] = (x + f * 3) & 0xFF
                        frame[i + 1] = g
                        frame[i + 2] = (x ^ y) & 0xFF
                        frame[i + 3] = 0xFF
                        i += 4
                w.add_video(bytes(frame))
                # keep audio sample count locked to the video clock
                target = (f + 1) * rate * GBA_FPS[1] // GBA_FPS[0]
                chunk = bytearray()
                for s in range(emitted, target):
                    v = int(amp * math.sin(2 * math.pi * 440.0 * s / rate))
                    chunk += struct.pack("<hh", v, v)
                emitted = target
                w.add_audio(bytes(chunk))
            path = w.close()
            check(path == out_path, "close() returns out_path")
            check(os.path.isfile(path), "output file exists")
            check(w.close() == out_path, "close() is idempotent")

            info = probe(path)
            if info is None:
                ok = False
            else:
                vs = [s for s in info["streams"]
                      if s.get("codec_type") == "video"]
                as_ = [s for s in info["streams"]
                       if s.get("codec_type") == "audio"]
                check(len(vs) == 1, "exactly one video stream")
                check(len(as_) == 1, "exactly one audio stream")
                if vs:
                    check(vs[0].get("codec_name") == "h264",
                          "video codec h264 (got %r)" % vs[0].get("codec_name"))
                    check(vs[0].get("width") == WIDTH * scale
                          and vs[0].get("height") == HEIGHT * scale,
                          "scaled to %dx%d (got %sx%s)"
                          % (WIDTH * scale, HEIGHT * scale,
                             vs[0].get("width"), vs[0].get("height")))
                    check(vs[0].get("pix_fmt") == "yuv420p",
                          "pix_fmt yuv420p (got %r)" % vs[0].get("pix_fmt"))
                if as_:
                    check(as_[0].get("codec_name") == "aac",
                          "audio codec aac (got %r)" % as_[0].get("codec_name"))
                expected = n_frames * GBA_FPS[1] / GBA_FPS[0]  # ~2.009 s
                dur = float(info["format"]["duration"])
                check(abs(dur - expected) < 0.35,
                      "duration ~%.2fs (got %.3fs)" % (expected, dur))

            # ---- video-only test: audio=False takes the rename path
            out2 = os.path.join(tmpdir, "selftest_v.mp4")
            w2 = Mp4Writer(out2, scale=1, audio=False, log=lambda *a: None)
            blank = bytes(FRAME_BYTES)
            for _ in range(30):
                w2.add_video(blank)
                w2.add_audio(b"\x00" * 64)  # must be silently dropped
            path2 = w2.close()
            info2 = probe(path2)
            if info2 is None:
                ok = False
            else:
                kinds = sorted(s.get("codec_type") for s in info2["streams"])
                check(kinds == ["video"],
                      "audio=False -> video stream only (got %s)" % kinds)
                v0 = info2["streams"][0]
                check(v0.get("width") == WIDTH and v0.get("height") == HEIGHT,
                      "scale=1 keeps 240x160 (got %sx%s)"
                      % (v0.get("width"), v0.get("height")))

            leftovers = [p for p in os.listdir(tmpdir)
                         if p not in ("selftest_av.mp4", "selftest_v.mp4")]
            check(leftovers == [], "no temp files left (got %s)" % leftovers)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        print("PASS" if ok else "FAIL")
        return 0 if ok else 1

    sys.exit(_selftest())
