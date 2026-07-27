#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
"""Fetch the prebuilt mGBA Python bindings into vendor/ (gitignored).

Source: hanzi/libmgba-py release 0.2.0-2 (MPL-2.0). Asset list verified
against the live GitHub release on 2026-07-27:

  libmgba-py_0.2.0_macos-arm64.zip   _pylib.abi3.so; dynamically links
                                     libmgba.0.10.dylib -> needs brew mgba
                                     plus the rpath fixes done below
  libmgba-py_0.2.0_macos-x86_64.zip  same, Intel (brew prefix /usr/local)
  libmgba-py_0.2.0_ubuntu-lunar.zip  _pylib.abi3.so (built on Ubuntu 23.04)
  libmgba-py_0.2.0_win64.zip         _pylib.pyd + mgba.dll + EVERY dependency
                                     DLL (SDL2, ffmpeg libs, zlib, ...) bundled
                                     next to the .pyd — fully self-contained.
                                     This is the exact zip 40Cakes/pokebot-gen3
                                     downloads and runs on Windows; no extra
                                     install step is needed there.

Idempotent: re-running skips the download when vendor/mgba/ already holds the
platform binary (use --force to re-fetch), never duplicates macOS rpaths, and
always ends with an import smoke test of `mgba.core` under this interpreter.

Stdlib only, Python >= 3.10. Run from anywhere:  python tools/fetch_bindings.py
"""

from __future__ import annotations

import argparse
import glob
import os
import platform
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(ROOT, "vendor")
MGBA_DIR = os.path.join(VENDOR, "mgba")
ZIP_PATH = os.path.join(VENDOR, "libmgba-py.zip")

RELEASE_BASE = ("https://github.com/hanzi/libmgba-py/releases/download/"
                "0.2.0-2/")

# platform key -> (release asset, extension-module filename inside mgba/)
ASSETS = {
    ("darwin", "arm64"): ("libmgba-py_0.2.0_macos-arm64.zip",
                          "_pylib.abi3.so"),
    ("darwin", "x86_64"): ("libmgba-py_0.2.0_macos-x86_64.zip",
                           "_pylib.abi3.so"),
    ("win32", "amd64"): ("libmgba-py_0.2.0_win64.zip", "_pylib.pyd"),
    ("linux", "x86_64"): ("libmgba-py_0.2.0_ubuntu-lunar.zip",
                          "_pylib.abi3.so"),
}

# Where a Homebrew libmgba can live (Apple Silicon, Intel).
BREW_LIB_DIRS = ("/opt/homebrew/lib", "/usr/local/lib")


def _plat_key() -> tuple[str, str]:
    machine = platform.machine().lower()
    if machine in ("x64", "em64t"):
        machine = "amd64"
    if sys.platform.startswith("linux"):
        return ("linux", machine)
    if sys.platform == "win32":
        return ("win32", machine)
    return (sys.platform, machine)


def _download(url: str, dest: str) -> None:
    print(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "rec2mp4-setup"})
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(req) as resp, open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    except urllib.error.URLError as exc:
        # python.org macOS builds ship without a CA trust store until the
        # user runs "Install Certificates.command" — fall back to curl
        # (present on every macOS and on Windows 10+) instead of failing.
        if not isinstance(getattr(exc, "reason", None),
                          ssl.SSLCertVerificationError):
            raise
        curl = shutil.which("curl")
        if not curl:
            sys.exit(
                "ERROR: this Python cannot verify TLS certificates (no CA "
                "trust store — python.org build without 'Install "
                "Certificates.command'?) and no curl fallback was found.\n"
                "Fix the interpreter's certificates or install curl, then "
                "re-run.")
        print("  Python has no usable CA trust store — falling back to curl")
        subprocess.run([curl, "-fsSL", "--retry", "3", "-o", tmp, url],
                       check=True)
    os.replace(tmp, dest)
    print(f"  -> {dest} ({os.path.getsize(dest):,} bytes)")


def _existing_rpaths(binary: str) -> list[str]:
    out = subprocess.run(["otool", "-l", binary], check=True,
                         capture_output=True, text=True).stdout
    rpaths, lines = [], out.splitlines()
    for i, line in enumerate(lines):
        if "LC_RPATH" in line:
            for follow in lines[i + 1:i + 4]:
                if "path " in follow:
                    rpaths.append(follow.split("path ", 1)[1].split(" (")[0])
                    break
    return rpaths


def _add_rpath_once(binary: str, rpath: str) -> None:
    if rpath in _existing_rpaths(binary):
        print(f"  rpath {rpath}: already present")
        return
    subprocess.run(["install_name_tool", "-add_rpath", rpath, binary],
                   check=True)
    print(f"  rpath {rpath}: added")


def _macos_post_steps() -> None:
    """rpath fixes + local copy of the brew libmgba dylib (idempotent).

    The macOS bindings link against libmgba.0.10.dylib (soname of mGBA
    0.10.x). We add the brew lib dir to the rpath AND copy the dylib next to
    the extension module (@loader_path) so a later brew upgrade to a
    different soname cannot break the vendored setup.
    """
    binary = os.path.join(MGBA_DIR, "_pylib.abi3.so")
    candidates: list[str] = []
    for d in BREW_LIB_DIRS:
        candidates += sorted(glob.glob(os.path.join(d, "libmgba.0.10*.dylib")))
    if not candidates:
        sys.exit(
            "ERROR: no Homebrew libmgba 0.10.x dylib found in "
            + " or ".join(BREW_LIB_DIRS)
            + "\nThe macOS bindings dynamically link libmgba.0.10.dylib."
            "\nInstall it first:  brew install mgba"
            "\n(If brew only offers a non-0.10 mGBA, these 0.2.0-2 bindings "
            "cannot use it — see docs/research/emulator-stack.md.)")
    src = candidates[0]
    local = os.path.join(MGBA_DIR, "libmgba.0.10.dylib")
    if os.path.isfile(local) and os.path.getsize(local) == os.path.getsize(src):
        print(f"  {os.path.basename(local)}: already copied")
    else:
        shutil.copy2(src, local)
        print(f"  copied {src} -> {local}")
    _add_rpath_once(binary, os.path.dirname(src))
    _add_rpath_once(binary, "@loader_path")


def _smoke_test() -> int:
    """Import mgba.core from vendor/ in a fresh interpreter; return exit code."""
    code = (
        "import os, sys\n"
        f"sys.path.insert(0, {VENDOR!r})\n"
        "if sys.platform == 'win32':\n"
        f"    os.add_dll_directory({MGBA_DIR!r})\n"
        "import mgba.core\n"
        "import mgba\n"
        "print('import mgba.core OK from', mgba.__file__)\n"
    )
    print("running import smoke test ...")
    sys.stdout.flush()          # keep parent/child output ordered in CI logs
    res = subprocess.run([sys.executable, "-c", code])
    print("SMOKE TEST: " + ("PASS — the emulator bindings import cleanly"
                            if res.returncode == 0 else
                            "FAIL — `import mgba.core` did not succeed "
                            "(see the traceback above)"))
    return res.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="re-download and re-extract even if vendor/mgba "
                         "already holds the platform binary")
    args = ap.parse_args()

    key = _plat_key()
    if key not in ASSETS:
        sys.exit(
            f"ERROR: no prebuilt libmgba-py 0.2.0-2 asset for {key[0]}/"
            f"{key[1]}.\nAvailable: macOS arm64/x86_64, Windows x64, Linux "
            "x86_64 (built on Ubuntu 23.04).\nOther platforms must build "
            "hanzi/libmgba-py from source — see its README. Note that "
            "everything except the replay itself (parser, --info-only, the "
            "ffmpeg encoder) works without the bindings.")
    asset, binary_name = ASSETS[key]
    binary = os.path.join(MGBA_DIR, binary_name)
    print(f"platform {key[0]}/{key[1]} -> {asset}")

    os.makedirs(VENDOR, exist_ok=True)
    if os.path.isfile(binary) and not args.force:
        print(f"already fetched: {binary} exists — skipping download "
              "(use --force to re-fetch)")
    else:
        _download(RELEASE_BASE + asset, ZIP_PATH)
        with zipfile.ZipFile(ZIP_PATH) as zf:
            zf.extractall(VENDOR)      # zip root is mgba/ -> vendor/mgba/
        print(f"extracted into {MGBA_DIR}")
        if not os.path.isfile(binary):
            sys.exit(f"ERROR: {binary_name} missing after extraction — "
                     f"unexpected zip layout in {asset}")

    if sys.platform == "darwin":
        _macos_post_steps()
    elif sys.platform == "win32":
        # Nothing to do: the win64 zip bundles mgba.dll and every dependency
        # DLL next to _pylib.pyd (self-contained; same zip pokebot-gen3 uses).
        print("  win64 zip is self-contained (mgba.dll + deps bundled) — "
              "no post-processing needed")

    return _smoke_test()


if __name__ == "__main__":
    sys.exit(main())
