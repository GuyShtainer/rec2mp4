# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Guy Shtainer
#
# Create clickable rec2mp4 shortcuts on Windows (Desktop + Start Menu), with
# the rec2mp4 icon. Run once, from the repo root:
#
#     powershell -ExecutionPolicy Bypass -File tools\make_windows_shortcut.ps1
#
# Options:
#     -Python  C:\path\to\pythonw.exe   the interpreter to launch
#     -NoDesktop / -NoStartMenu         skip one of the two shortcuts
#
# Why a shortcut and not a .exe: this launches the rec2mp4 in THIS working
# copy with an interpreter that has the stack (Pillow + the vendored mGBA
# bindings + ffmpeg on PATH). A frozen .exe would have to ship those too —
# see README "Desktop icon".
#
# The shortcut targets pythonw.exe (no console window) with the repo as its
# working directory. That is enough for imports: `python -m rec2mp4.gui` puts
# the current directory first on sys.path, so no PYTHONPATH is needed.

[CmdletBinding()]
param(
    [string] $Python,
    [switch] $NoDesktop,
    [switch] $NoStartMenu
)

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$icon = Join-Path $repo 'assets\rec2mp4.ico'

if (-not (Test-Path (Join-Path $repo 'rec2mp4\gui.py'))) {
    Write-Error "This does not look like the rec2mp4 repo: $repo"
}
if (-not (Test-Path $icon)) {
    Write-Warning "assets\rec2mp4.ico is missing — run 'python tools\make_icons.py' first. Using the Python icon for now."
    $icon = $null
}

# --- pick the interpreter: -Python, then REC2MP4_PYTHON, then PATH ---------
if (-not $Python) { $Python = $env:REC2MP4_PYTHON }
if (-not $Python) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $Python = $cmd.Source }
}
if (-not $Python -or -not (Test-Path $Python)) {
    Write-Error "No pythonw.exe found. Pass -Python C:\path\to\pythonw.exe (use the env that has Pillow + vendor\mgba)."
}
# pythonw runs without a console window; python.exe would flash one.
if ((Split-Path -Leaf $Python) -ieq 'python.exe') {
    $candidate = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
    if (Test-Path $candidate) {
        $Python = $candidate
        Write-Host "   using pythonw.exe (no console window)"
    }
}

function New-Rec2mp4Shortcut([string] $Path) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Path)
    $lnk.TargetPath       = $Python
    $lnk.Arguments        = '-m rec2mp4.gui'
    $lnk.WorkingDirectory = $repo
    $lnk.Description      = 'rec2mp4 — turn Pokemon Emerald Battle Records into MP4 videos'
    if ($icon) { $lnk.IconLocation = "$icon,0" }
    $lnk.Save()
    Write-Host "   created $Path"
}

Write-Host "rec2mp4 shortcuts"
Write-Host "   python: $Python"
Write-Host "   repo:   $repo"

if (-not $NoDesktop) {
    New-Rec2mp4Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'rec2mp4.lnk')
}
if (-not $NoStartMenu) {
    $programs = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
    New-Item -ItemType Directory -Force -Path $programs | Out-Null
    New-Rec2mp4Shortcut (Join-Path $programs 'rec2mp4.lnk')
}

Write-Host ""
Write-Host "Done. If a conversion fails with 'No module named mgba', that interpreter"
Write-Host "is missing the stack — run 'python tools\fetch_bindings.py' with it, and"
Write-Host "make sure ffmpeg is on PATH."
