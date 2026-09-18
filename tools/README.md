# tools/ — external binaries (not in version control)

This folder is where the external command-line tools live. **Nothing here is
committed**: `tools/*` is git-ignored except this file and `.gitkeep`.

Expected layout after `scripts/fetch_tools.ps1`:

```
tools/
├── ffmpeg/
│   └── bin/
│       ├── ffmpeg.exe
│       └── ffprobe.exe
└── mkvtoolnix/
    ├── mkvpropedit.exe
    └── mkvmerge.exe
```

`opsed/util.py:find_tool()` looks in exactly those two places, then falls back
to `PATH`. So if you already have both tools installed system-wide you can skip
this folder entirely.

## Windows

```powershell
pwsh -File scripts/fetch_tools.ps1
```

Needs [7-Zip](https://7-zip.org) for the `.7z` archives, and downloads:

* ffmpeg — <https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.7z>
  (or any other build from <https://ffmpeg.org/download.html>)
* MKVToolNix — <https://mkvtoolnix.download/downloads.html>

## Linux / macOS

Install from your package manager; no local copy needed:

```bash
sudo apt install ffmpeg mkvtoolnix     # Debian/Ubuntu
brew install ffmpeg mkvtoolnix         # macOS
```

## Licences

These programs are **not** part of this project and are distributed under their
own terms. If you do redistribute a copy, keep their licence files with them:

* ffmpeg / ffprobe — LGPL/GPL depending on the build; see
  <https://ffmpeg.org/legal.html>
* MKVToolNix — GPL-2.0-or-later; see <https://mkvtoolnix.download/>

This project only invokes them as separate processes and does not link against
them.
