"""Shared paths, tool discovery and subprocess helpers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "tools"
WORK_DIR = PROJECT_ROOT / "work"
CACHE_DIR = WORK_DIR / "cache"
PCM_DIR = CACHE_DIR / "pcm"
FEAT_DIR = CACHE_DIR / "feat"
LOG_DIR = WORK_DIR / "logs"
ART_DIR = WORK_DIR / "artifacts"
REPORT_PATH = WORK_DIR / "report.json"
MANIFEST_PATH = WORK_DIR / "manifest.jsonl"

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".mov", ".webm", ".avi"}

for _d in (WORK_DIR, CACHE_DIR, PCM_DIR, FEAT_DIR, LOG_DIR, ART_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def find_tool(name: str) -> Path:
    """Locate a bundled tool, falling back to PATH."""
    candidates = [
        TOOLS_DIR / "ffmpeg" / "bin" / f"{name}.exe",
        TOOLS_DIR / "mkvtoolnix" / f"{name}.exe",
        TOOLS_DIR / "bin" / f"{name}.exe",
    ]
    for c in candidates:
        if c.is_file():
            return c
    found = shutil.which(name)
    if found:
        return Path(found)
    raise FileNotFoundError(f"tool not found: {name} (looked in {TOOLS_DIR} and PATH)")


_FFMPEG: Path | None = None
_FFPROBE: Path | None = None
_MKVPROPEDIT: Path | None = None
_MKVMERGE: Path | None = None


def ffmpeg() -> Path:
    global _FFMPEG
    if _FFMPEG is None:
        _FFMPEG = find_tool("ffmpeg")
    return _FFMPEG


def ffprobe() -> Path:
    global _FFPROBE
    if _FFPROBE is None:
        _FFPROBE = find_tool("ffprobe")
    return _FFPROBE


def mkvpropedit() -> Path:
    global _MKVPROPEDIT
    if _MKVPROPEDIT is None:
        _MKVPROPEDIT = find_tool("mkvpropedit")
    return _MKVPROPEDIT


def mkvmerge() -> Path:
    global _MKVMERGE
    if _MKVMERGE is None:
        _MKVMERGE = find_tool("mkvmerge")
    return _MKVMERGE


def run(cmd: list, *, timeout: float | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command, capturing output as text (utf-8, lossy)."""
    proc = subprocess.run(
        [str(c) for c in cmd],
        capture_output=True,
        timeout=timeout,
    )
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    proc = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(str(c) for c in cmd)}\n"
            f"--- stderr (tail) ---\n{err[-3000:]}"
        )
    return proc


# ----------------------------------------------------------------------------
# logging
# ----------------------------------------------------------------------------

class Log:
    """Minimal thread-safe logger writing to stdout and a run log file."""

    def __init__(self, name: str = "opsed"):
        self._lock = threading.Lock()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = LOG_DIR / f"{name}-{stamp}.log"
        self._fh = open(self.path, "w", encoding="utf-8")

    def __call__(self, msg: str = "") -> None:
        with self._lock:
            line = f"[{time.strftime('%H:%M:%S')}] {msg}"
            try:
                print(line, flush=True)
            except UnicodeEncodeError:
                print(line.encode("ascii", "replace").decode(), flush=True)
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def read_json(path: Path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def fmt_time(t: float | None) -> str:
    if t is None:
        return "  --:--  "
    t = max(0.0, float(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    if h:
        return f"{h:d}:{m:02d}:{s:05.2f}"
    return f"{m:02d}:{s:05.2f}"


def fmt_bytes(n: float) -> str:
    """Human-readable size, e.g. '1.1 GB'."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def dir_stats(path: Path) -> tuple[int, int]:
    """(file count, total bytes) of a directory tree; (0, 0) when missing."""
    if not path.is_dir():
        return 0, 0
    count = 0
    total = 0
    for p in path.rglob("*"):
        if not p.is_file():
            continue
        count += 1
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return count, total


def wipe_dir(path: Path) -> tuple[int, int]:
    """Delete everything under `path`, keeping the directory itself."""
    count, total = dir_stats(path)
    if path.is_dir():
        for p in sorted(path.rglob("*"), key=lambda q: len(q.parts), reverse=True):
            try:
                p.unlink() if p.is_file() else p.rmdir()
            except OSError:
                pass
    return count, total
