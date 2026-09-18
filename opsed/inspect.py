"""Visual verification: frame grabbing and contact sheets.

Boundaries are verified by looking at frames around them.  A correct OP start
shows the frame change from story content to the OP title sequence, which is
unmistakable in a contact sheet.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .util import ART_DIR, ffmpeg, run

CELL_W = 384
LABEL_H = 20
PAD = 4
FRAME_W = 96
FRAME_H = 54
FRAME_FPS = 2.0


def read_chpl(path: Path) -> list[dict] | None:
    """Read the Nero `chpl` chapter box of an MP4 directly.

    ffprobe prefers the QuickTime chapter *track* that ffmpeg writes alongside
    chpl, and for tracks whose first chapter does not start at 0 that readback
    reports a wrong start for the first chapter.  chpl holds the absolute times
    and is what players actually show, so it is the authoritative source here.

    Layout: box header, version (1 byte), flags (3 bytes), reserved (4 bytes),
    chapter count (1 byte), then per chapter: start in 100 ns units (8 bytes),
    title length (1 byte), UTF-8 title.
    """
    import struct

    buf = Path(path).read_bytes()

    def boxes(start: int, end: int):
        off = start
        while off + 8 <= end:
            size = struct.unpack(">I", buf[off:off + 4])[0]
            typ = buf[off + 4:off + 8].decode("latin1", "replace")
            if size == 0:
                size = end - off
            if size < 8:
                return
            yield off, typ, size
            off += size

    for off, typ, size in boxes(0, len(buf)):
        if typ != "moov":
            continue
        for o2, t2, s2 in boxes(off + 8, off + size):
            if t2 != "udta":
                continue
            for o3, t3, s3 in boxes(o2 + 8, o2 + s2):
                if t3 != "chpl":
                    continue
                count = buf[o3 + 16]
                p = o3 + 17
                out = []
                for _ in range(count):
                    if p + 9 > o3 + s3:
                        break
                    stamp = struct.unpack(">Q", buf[p:p + 8])[0]
                    p += 8
                    ln = buf[p]
                    p += 1
                    out.append({"start": stamp / 1e7, "end": None,
                                "title": buf[p:p + ln].decode("utf-8", "replace")})
                    p += ln
                return out
    return None


def frame_series(video: Path, start: float, duration: float, *,
                 w: int = FRAME_W, h: int = FRAME_H,
                 fps: float = FRAME_FPS) -> np.ndarray | None:
    """Tiny grayscale frames at a fixed rate, as (n, w*h) uint8.

    Used to test whether two episodes show the *same* picture at the same offset
    -- the decisive signal for where an OP/ED sequence ends, since the sequence
    animation is identical in every episode while the surrounding footage is not.
    """
    cmd = [
        ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-ss", f"{max(0.0, float(start)):.3f}", "-i", str(video),
        "-t", f"{max(0.05, float(duration)):.3f}",
        "-an", "-sn", "-dn",
        "-vf", f"fps={fps},scale={w}:{h},format=gray",
        "-f", "rawvideo", "-",
    ]
    proc = subprocess.run([str(c) for c in cmd], capture_output=True)
    if proc.returncode != 0:
        return None
    buf = np.frombuffer(proc.stdout, dtype=np.uint8)
    n = buf.size // (w * h)
    if n == 0:
        return None
    return buf[: n * w * h].reshape(n, w * h)


def video_agreement(frames: list[np.ndarray]) -> np.ndarray:
    """Mean pairwise frame similarity (1.0 = identical pictures) per frame slot."""
    if len(frames) < 2:
        return np.zeros(0, dtype=np.float32)
    n = min(f.shape[0] for f in frames)
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    stack = np.stack([f[:n].astype(np.float32) for f in frames])
    acc = np.zeros(n, dtype=np.float64)
    pairs = 0
    for i in range(stack.shape[0]):
        for j in range(i + 1, stack.shape[0]):
            diff = np.abs(stack[i] - stack[j]).mean(axis=1) / 255.0
            acc += 1.0 - diff
            pairs += 1
    return (acc / max(pairs, 1)).astype(np.float32)


def grab(video: Path, times: list[float], out_dir: Path, *,
         width: int = CELL_W, prefix: str = "") -> list[Path | None]:
    """Extract one frame per timestamp (fast seek, no full decode)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[Path | None] = []
    for t in times:
        out = out_dir / f"{prefix}{t:09.2f}.png".replace(" ", "_")
        if not out.is_file():
            cmd = [
                ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                "-ss", f"{max(0.0, t):.3f}", "-i", str(video),
                "-frames:v", "1", "-vf", f"scale={width}:-2",
                str(out),
            ]
            res = run(cmd, check=False)
            if res.returncode != 0 or not out.is_file():
                results.append(None)
                continue
        results.append(out)
    return results


def _font(size: int = 14):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def sheet(rows: list[tuple[str, list[Path | None]]], out_png: Path, *,
          row_label_w: int = 132, sub_labels: list[str] | None = None) -> Path:
    """Compose a labelled grid: one row per boundary, one column per sample."""
    font = _font(14)
    cols = max((len(fr) for _, fr in rows), default=0)
    if cols == 0:
        raise ValueError("nothing to draw")
    probe = None
    for _, frames in rows:
        for f in frames:
            if f is not None:
                probe = Image.open(f)
                break
        if probe:
            break
    if probe is None:
        raise ValueError("no frames could be read")
    cw, ch = probe.size
    row_h = ch + LABEL_H
    W = row_label_w + cols * (cw + PAD) + PAD
    H = len(rows) * (row_h + PAD) + PAD + (LABEL_H if sub_labels else 0)
    img = Image.new("RGB", (W, H), (24, 24, 28))
    draw = ImageDraw.Draw(img)

    y = 0
    if sub_labels:
        for c, txt in enumerate(sub_labels[:cols]):
            x = row_label_w + c * (cw + PAD)
            draw.text((x + 2, 2), txt, fill=(200, 200, 120), font=font)
        y = LABEL_H
    for label, frames in rows:
        draw.text((6, y + row_h // 2 - 8), label, fill=(230, 230, 230), font=font)
        for c, f in enumerate(frames):
            x = row_label_w + c * (cw + PAD)
            if f is None:
                draw.rectangle([x, y, x + cw, y + ch], fill=(80, 30, 30))
                continue
            with Image.open(f) as im:
                img.paste(im.convert("RGB").resize((cw, ch)), (x, y))
        y += row_h + PAD
    img.save(out_png)
    return out_png


def timeline_sheet(video: Path, duration: float, out_png: Path, *,
                   marks: dict | None = None, n: int = 30,
                   width: int = 256) -> Path:
    """Evenly spaced frames across the whole episode, as a coarse overview."""
    step = duration / n
    times = [round(i * step, 2) for i in range(n)]
    frames = grab(video, times, ART_DIR / "frames" / out_png.stem,
                  width=width, prefix="tl_")
    per_row = 10
    rows = []
    for r0 in range(0, n, per_row):
        chunk = frames[r0:r0 + per_row]
        rows.append((f"{times[r0]:.0f}s", chunk))
    return sheet(rows, out_png, row_label_w=64)


def boundary_sheet(video: Path, spans: dict, out_png: Path, *,
                   offsets: tuple[float, ...] = (-3.0, -1.5, 0.0, 1.5, 3.0),
                   width: int = CELL_W) -> Path:
    """Frames straddling each detected boundary, so the transition is visible."""
    rows: list[tuple[str, list]] = []
    sub = [f"{o:+.1f}s" for o in offsets]
    plan = [
        ("OP start", spans.get("op", {}).get("start") if spans.get("op") else None, 0.0),
        ("OP end", spans.get("op", {}).get("end") if spans.get("op") else None, 0.0),
        ("ED start", spans.get("ed", {}).get("start") if spans.get("ed") else None, 0.0),
        ("ED end", spans.get("ed", {}).get("end") if spans.get("ed") else None, 0.0),
    ]
    for label, t, _ in plan:
        if t is None:
            rows.append((f"{label} (none)", [None] * len(offsets)))
            continue
        times = [max(0.0, t + o) for o in offsets]
        frames = grab(video, times, ART_DIR / "frames" / out_png.stem, width=width,
                      prefix=f"{label.replace(' ', '')}_")
        rows.append((f"{label} {t:.1f}s", frames))
    mid_row_times = []
    if spans.get("op"):
        s, e = spans["op"]["start"], spans["op"]["end"]
        mid_row_times += [s + (e - s) * f for f in (0.25, 0.5, 0.75)]
    if spans.get("ed"):
        s, e = spans["ed"]["start"], spans["ed"]["end"]
        mid_row_times += [s + (e - s) * f for f in (0.25, 0.5, 0.75)]
    if mid_row_times:
        frames = grab(video, mid_row_times, ART_DIR / "frames" / out_png.stem,
                      width=width, prefix="mid_")
        rows.append(("inside", frames))
    return sheet(rows, out_png, sub_labels=sub)
