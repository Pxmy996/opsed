# Fetch the external binaries this tool drives (ffmpeg / ffprobe + MKVToolNix).
#
#   pwsh -File scripts/fetch_tools.ps1
#
# Both are third-party programs under their own licences; nothing is vendored
# in this repository. Downloads land in tools/ and are git-ignored.
#
# Requires 7-Zip (https://7-zip.org) for the .7z archives. Alternatively grab
# the archives by hand and extract them into tools/ yourself -- the only thing
# that matters is where the executables end up:
#
#   tools/ffmpeg/bin/ffmpeg.exe
#   tools/ffmpeg/bin/ffprobe.exe
#   tools/mkvtoolnix/mkvpropedit.exe
#   tools/mkvtoolnix/mkvmerge.exe
#
# On Linux/macOS just install ffmpeg and mkvtoolnix from your package manager;
# both are found on PATH and tools/ is not needed at all.

[CmdletBinding()]
param(
    [string]$FfmpegUrl = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.7z',
    [string]$MkvToolNixUrl = 'https://mkvtoolnix.download/windows/releases/102.0/mkvtoolnix-64-bit-102.0.7z'
)

$ErrorActionPreference = 'Stop'
$root  = Split-Path -Parent $PSScriptRoot
$tools = Join-Path $root 'tools'
New-Item -ItemType Directory -Force -Path $tools | Out-Null

function Find-SevenZip {
    $candidates = @(
        "$env:ProgramFiles\7-Zip\7z.exe",
        "${env:ProgramFiles(x86)}\7-Zip\7z.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    $onPath = Get-Command 7z -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    throw "7-Zip not found. Install it from https://7-zip.org or extract the archives into tools/ manually."
}

function Fetch([string]$url, [string]$out) {
    if (Test-Path $out) { Write-Host "cached: $out"; return }
    Write-Host "downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing
}

$sevenZip = Find-SevenZip

# --- ffmpeg ---------------------------------------------------------------
if (Test-Path (Join-Path $tools 'ffmpeg\bin\ffmpeg.exe')) {
    Write-Host 'ffmpeg: already present'
} else {
    $arc = Join-Path $tools 'ffmpeg.7z'
    Fetch $FfmpegUrl $arc
    Write-Host 'extracting ffmpeg'
    # The archive holds a single versioned top-level folder (ffmpeg-*-essentials_build).
    & $sevenZip x $arc "-o$tools" -y | Out-Null
    $inner = Get-ChildItem $tools -Directory | Where-Object { $_.Name -like 'ffmpeg-*' } | Select-Object -First 1
    if (-not $inner) { throw 'unexpected ffmpeg archive layout' }
    if (Test-Path (Join-Path $tools 'ffmpeg')) { Remove-Item -Recurse -Force (Join-Path $tools 'ffmpeg') }
    Move-Item $inner.FullName (Join-Path $tools 'ffmpeg')
    Remove-Item $arc -Force
    Write-Host 'ffmpeg: ok'
}

# --- MKVToolNix -----------------------------------------------------------
if (Test-Path (Join-Path $tools 'mkvtoolnix\mkvpropedit.exe')) {
    Write-Host 'mkvtoolnix: already present'
} else {
    $arc = Join-Path $tools 'mkvtoolnix.7z'
    Fetch $MkvToolNixUrl $arc
    Write-Host 'extracting mkvtoolnix'
    # This archive already extracts into a "mkvtoolnix" folder.
    & $sevenZip x $arc "-o$tools" -y | Out-Null
    Remove-Item $arc -Force
    Write-Host 'mkvtoolnix: ok'
}

Write-Host ''
Write-Host 'done. verify with:'
Write-Host '  tools\ffmpeg\bin\ffmpeg.exe -version'
Write-Host '  tools\mkvtoolnix\mkvpropedit.exe --version'
