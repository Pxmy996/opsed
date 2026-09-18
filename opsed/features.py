"""Audio extraction and log-mel feature computation.

The whole detection pipeline runs on mono 16 kHz audio.  Same-season OP/ED
audio is byte-identical across episodes, so mel-band energy over time is more
than discriminative enough; no ML model or external fingerprint library is
needed.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import unicodedata
from pathlib import Path

import numpy as np

from .util import FEAT_DIR, PCM_DIR, ffmpeg, run

SR = 16000
N_FFT = 1024          # 64 ms window
HOP = 320             # 20 ms -> 50 fps
N_MELS = 32
FMIN = 40.0
FMAX = 7600.0
ZSCORE_WIN_S = 15.0   # per-band sliding normalisation window
SD_FLOOR = 0.5        # local std floor, as a fraction of the band's global std


def cache_key(path: Path, rel: str | None = None) -> str:
    """Stable cache key for a media file.

    Keyed on the path relative to the library root when available: writing
    chapters rewrites the container, which changes the file's size and mtime,
    and a key built from those would invalidate the cache for every file just
    processed -- forcing a full re-decode on the next run.  The relative path
    survives a remux, so the analysis cache does too.
    """
    if rel:
        safe = unicodedata.normalize("NFKC", rel)
        safe = re.sub(r'[\\/:*?"<>|\s]+', "_", safe).strip("_")
        return safe[:150] or "ep"
    try:
        st = path.stat()
        basis = f"{path.name}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        basis = str(path)
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]
    stem = unicodedata.normalize("NFKD", path.stem)
    stem = "".join(c for c in stem if c.isascii() and (c.isalnum() or c in "-_"))[:28]
    return f"{stem or 'ep'}-{digest}"


# ----------------------------------------------------------------------------
# audio -> features
# ----------------------------------------------------------------------------

def pcm_path(path: Path, rel: str | None = None) -> Path:
    return PCM_DIR / f"{cache_key(path, rel)}.s16le"


def extract_pcm(path: Path, *, rel: str | None = None, force: bool = False) -> Path:
    """Decode the first audio track to raw mono 16 kHz PCM (no video decode)."""
    out = pcm_path(path, rel)
    if out.is_file() and out.stat().st_size > SR * 2 * 10 and not force:
        return out
    tmp = out.with_suffix(".s16le.part")
    cmd = [
        ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(path),
        "-vn", "-sn", "-dn",
        "-map", "0:a:0",
        "-ac", "1", "-ar", str(SR),
        "-f", "s16le", str(tmp),
    ]
    run(cmd, timeout=1800)
    tmp.replace(out)
    return out


def read_pcm(path: Path) -> np.ndarray:
    raw = np.fromfile(path, dtype="<i2")
    if raw.size == 0:
        raise ValueError(f"empty pcm: {path}")
    return raw.astype(np.float32) / 32768.0


def mel_filterbank(sr: int = SR, n_fft: int = N_FFT, n_mels: int = N_MELS,
                   fmin: float = FMIN, fmax: float = FMAX) -> np.ndarray:
    def hz2mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel2hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    edges = mel2hz(np.linspace(hz2mel(fmin), hz2mel(min(fmax, sr / 2)), n_mels + 2))
    n_bins = n_fft // 2 + 1
    bins = np.clip(np.floor((n_fft + 1) * edges / sr).astype(int), 0, n_bins - 1)
    fb = np.zeros((n_mels, n_bins), dtype=np.float32)
    for i in range(n_mels):
        lo, mid, hi = int(bins[i]), int(bins[i + 1]), int(bins[i + 2])
        mid = max(mid, lo + 1)
        hi = max(hi, mid + 1)
        if hi > n_bins:
            hi = n_bins
        if mid >= hi:
            continue
        fb[i, lo:mid] = (np.arange(lo, mid) - lo) / (mid - lo)
        fb[i, mid:hi] = (hi - np.arange(mid, hi)) / (hi - mid)
    return fb


_FB: np.ndarray | None = None
_WINDOW: np.ndarray | None = None


def _tables() -> tuple[np.ndarray, np.ndarray]:
    global _FB, _WINDOW
    if _FB is None:
        _FB = mel_filterbank()
    if _WINDOW is None:
        _WINDOW = np.hanning(N_FFT).astype(np.float32)
    return _FB, _WINDOW


def logmel(x: np.ndarray) -> np.ndarray:
    """(T_samples,) mono float32 -> (T_frames, N_MELS) log-mel, 50 fps."""
    fb, win = _tables()
    pad = N_FFT // 2
    xp = np.pad(x, (pad, pad + N_FFT), mode="constant")
    frames = np.lib.stride_tricks.sliding_window_view(xp, N_FFT)[::HOP]
    n_frames = frames.shape[0]
    out = np.empty((n_frames, N_MELS), dtype=np.float32)
    chunk = 8192
    for i in range(0, n_frames, chunk):
        blk = frames[i:i + chunk].astype(np.float32) * win
        spec = np.abs(np.fft.rfft(blk, axis=1))
        out[i:i + chunk] = np.log(spec @ fb.T + 1e-6)
    return out


def sliding_zscore(F: np.ndarray, win_frames: int,
                   sd_floor: float = SD_FLOOR) -> np.ndarray:
    """Per-band z-score over a centred sliding window.

    Without a floor, a band that happens to be near-silent inside the window gets
    divided by a tiny standard deviation, which amplifies dither into huge
    values and lets noise dominate the feature vector.  Flooring the local
    standard deviation at a fraction of the band's global standard deviation
    keeps quiet passages quiet.
    """
    T = F.shape[0]
    w = int(min(max(win_frames, 8), T))
    zero = np.zeros((1, F.shape[1]), dtype=np.float64)
    c1 = np.vstack((zero, np.cumsum(F, axis=0, dtype=np.float64)))
    c2 = np.vstack((zero, np.cumsum(F * F, axis=0, dtype=np.float64)))
    n = T - w + 1
    s1 = (c1[w:] - c1[:n]) / w
    s2 = (c2[w:] - c2[:n]) / w
    sd = np.sqrt(np.maximum(s2 - s1 * s1, 1e-6))

    g_mean = F.mean(axis=0, dtype=np.float64)
    g_sd = F.std(axis=0, dtype=np.float64)
    sd = np.maximum(sd, np.maximum(sd_floor * g_sd, 1e-3))

    body = (F[w // 2: w // 2 + n] - s1) / sd
    out = np.empty_like(F)
    out[w // 2: w // 2 + n] = body
    out[: w // 2] = body[0]
    out[w // 2 + n:] = body[-1]
    return out.astype(np.float32)


def feature_path(video: Path, rel: str | None = None) -> Path:
    return FEAT_DIR / f"{cache_key(video, rel)}.npy"


def compute(video: Path, *, rel: str | None = None, force: bool = False,
            log=None) -> np.ndarray:
    """Full cached feature pipeline for one video file."""
    fp = feature_path(video, rel)
    if fp.is_file() and not force:
        return np.load(fp)
    if log:
        log(f"    decode {video.name}")
    pcm = extract_pcm(video, rel=rel, force=force)
    x = read_pcm(pcm)
    F = logmel(x)
    F = sliding_zscore(F, int(ZSCORE_WIN_S * SR / HOP))
    np.save(fp, F)
    return F


def compute_many(videos: list[Path], *, rels: list[str] | None = None,
                 force: bool = False, workers: int = 8, log=None) -> dict:
    """Decode audio for many files in parallel (AAC decode is single-threaded)."""
    from concurrent.futures import ThreadPoolExecutor

    rel_of = dict(zip(videos, rels)) if rels else {}
    todo = [v for v in videos if force or not feature_path(v, rel_of.get(v)).is_file()]
    if todo:
        if log:
            log(f"    decoding {len(todo)} file(s) with {workers} workers")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(lambda v: compute(v, rel=rel_of.get(v), force=force), todo))
    return {v: np.load(feature_path(v, rel_of.get(v))) for v in videos}


def discard(pairs: list[tuple[Path, str | None]]) -> tuple[int, int]:
    """Delete cached features and decoded PCM for the given (video, rel) pairs.

    Both files are pure scratch: detection holds the features in memory for the
    whole run, so once the report is written nothing reads them again.  A later
    run simply re-decodes (about two seconds per episode) instead of hitting the
    cache.  Returns (files removed, bytes freed).
    """
    removed = 0
    freed = 0
    for video, rel in pairs:
        for p in (feature_path(video, rel), pcm_path(video, rel)):
            try:
                size = p.stat().st_size
                p.unlink()
            except OSError:
                continue
            removed += 1
            freed += size
    return removed, freed
