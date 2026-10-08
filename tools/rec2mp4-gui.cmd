@echo off
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Copyright (C) 2026 Guy Shtainer
rem
rem Double-clickable rec2mp4 launcher for Windows, for people who would rather
rem run a file in the repo than make a shortcut. For a proper Desktop/Start
rem Menu icon use tools\make_windows_shortcut.ps1 instead — this file cannot
rem carry an icon of its own.
rem
rem Set REC2MP4_PYTHON to choose the interpreter (it needs Pillow, the
rem vendored mGBA bindings and ffmpeg on PATH).

setlocal
cd /d "%~dp0.."

set "PY=%REC2MP4_PYTHON%"
if not defined PY (
    where pythonw.exe >nul 2>&1 && set "PY=pythonw.exe"
)
if not defined PY (
    where python.exe >nul 2>&1 && set "PY=python.exe"
)
if not defined PY (
    echo No Python found. Install Python 3.10+ or set REC2MP4_PYTHON.
    pause
    exit /b 2
)

rem `start ""` returns immediately so no console window lingers; -m puts the
rem current directory (the repo root, from the cd above) first on sys.path.
start "" "%PY%" -m rec2mp4.gui %*
endlocal
