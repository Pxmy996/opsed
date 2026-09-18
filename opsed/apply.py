"""Writing chapters into files, with verification and rollback.

MP4
    Chapters live in a `chpl` box (plus a QuickTime chapter track) inside `moov`,
    so the file must be rewritten.  The rewrite is a stream copy: no decoding, no
    re-encoding, and the original A/V packet bytes are carried over verbatim.
    Output is written next to the source as a temporary file, verified with
    ffprobe against the original, and only then swapped in.

MKV
    Matroska chapters are a header element, so `mkvpropedit` rewrites only the
    header in place -- no remux at all, milliseconds even for multi-GB files.
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from pathlib import Path

from . import chapters as ch
from . import inspect
from .library import MediaFile, probe
from .util import (MANIFEST_PATH, WORK_DIR, ffmpeg, fmt_time, mkvpropedit, run,
                   write_json)
import json

TMP_DIR = WORK_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)


def signature(info: dict) -> dict:
    """Container-level fingerprint used to prove a remux changed nothing."""
    streams = []
    for s in info.get("streams", []):
        codec_type = s.get("codec_type")
        streams.append({
            "type": codec_type,
            "media": codec_type in ("video", "audio", "subtitle"),
            "codec": s.get("codec_name"),
            "profile": s.get("profile"),
            "w": s.get("width"),
            "h": s.get("height"),
            "ch": s.get("channels"),
            "sr": s.get("sample_rate"),
            "lang": (s.get("tags") or {}).get("language"),
            "title": (s.get("tags") or {}).get("title"),
            "attached_pic": (s.get("disposition") or {}).get("attached_pic", 0),
        })
    dur = info.get("format", {}).get("duration")
    return {
        "duration": float(dur) if dur not in (None, "N/A") else None,
        "size": int(info.get("format", {}).get("size") or 0),
        "streams": streams,
    }


def diff_signature(before: dict, after: dict, *, dur_tol: float = 1.0) -> list[str]:
    """Compare media streams only.

    Writing MP4 chapters makes ffmpeg add a QuickTime chapter track, which shows
    up as an extra `data` stream.  That addition is expected and harmless; losing
    or altering a video/audio/subtitle stream is not.
    """
    problems = []
    a_media = [s for s in before["streams"] if s.get("media")]
    b_media = [s for s in after["streams"] if s.get("media")]
    extra = [s for s in after["streams"] if not s.get("media")]
    if len(a_media) != len(b_media):
        problems.append(f"media stream count {len(a_media)} -> {len(b_media)}")
    for i, (a, b) in enumerate(zip(a_media, b_media)):
        for key in ("type", "codec", "w", "h", "ch", "sr", "lang", "attached_pic"):
            if a.get(key) != b.get(key):
                problems.append(f"stream {i} {key}: {a.get(key)!r} -> {b.get(key)!r}")
    for s in extra:
        if s.get("type") not in ("data", None):
            problems.append(f"unexpected extra stream of type {s.get('type')!r}")
    for which, sig in (("before", before), ("after", after)):
        if sig["duration"] is None:
            problems.append(f"{which}: no duration")
    if before["duration"] and after["duration"]:
        if abs(before["duration"] - after["duration"]) > dur_tol:
            problems.append(
                f"duration {before['duration']:.3f} -> {after['duration']:.3f}")
    return problems


def stream_hashes(path: Path, info: dict) -> dict:
    """MD5 of each elementary stream, demuxed with -c copy (no decoding).

    Identical hashes before and after prove the remux only rearranged the
    container and did not touch a single packet of video or audio.
    """
    out = {}
    for idx, s in enumerate(info.get("streams", [])):
        if s.get("codec_type") not in ("video", "audio"):
            continue
        if (s.get("disposition") or {}).get("attached_pic"):
            continue
        res = run([ffmpeg(), "-hide_banner", "-nostdin", "-v", "error",
                   "-i", str(path), "-map", f"0:{idx}", "-c", "copy",
                   "-f", "md5", "-"], check=False, timeout=3600)
        text = (res.stdout or "").strip()
        if res.returncode == 0 and text.startswith("MD5="):
            out[f"{s.get('codec_type')}:{idx}"] = text.split("=", 1)[1]
        else:
            out[f"{s.get('codec_type')}:{idx}"] = f"error:{res.returncode}"
    return out


def readback_chapters(path: Path, info: dict, container: str) -> list[dict]:
    """Read chapters back the way a player would see them."""
    if container == "mp4":
        ch = inspect.read_chpl(path)
        if ch is not None:
            return ch
    return read_chapters(info)


def read_chapters(info: dict) -> list[dict]:
    out = []
    for c in info.get("chapters") or []:
        out.append({
            "start": float(c.get("start_time", 0.0)),
            "end": float(c.get("end_time", 0.0)),
            "title": (c.get("tags") or {}).get("title"),
        })
    return out


def check_chapters(written: list[dict], readback: list[dict], *,
                   tol: float = 0.05) -> list[str]:
    """Compare the chapters we asked for with what a player would read back.

    `chpl` carries start times only (no end), so ends are only compared when the
    readback provides them.
    """
    problems = []
    if len(written) != len(readback):
        problems.append(f"chapter count written={len(written)} read={len(readback)}")
        return problems
    for i, (w, r) in enumerate(zip(written, readback)):
        if (w.get("title") or "") != (r.get("title") or ""):
            problems.append(f"chapter {i} title {w.get('title')!r} -> {r.get('title')!r}")
        if abs(w["start"] - r["start"]) > tol:
            problems.append(f"chapter {i} start {w['start']:.3f} -> {r['start']:.3f}")
        if r.get("end") is not None and abs(w["end"] - r["end"]) > tol:
            problems.append(f"chapter {i} end {w['end']:.3f} -> {r['end']:.3f}")
    return problems


def _force_writable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
    except OSError:
        pass


def _ffmetadata(chapters: list[dict], tag: str) -> Path:
    p = TMP_DIR / f"{tag}.ffmeta.txt"
    ch.write_ffmetadata(chapters, p)
    return p


def apply_mp4(mf: MediaFile, chapters: list[dict], *, out_dir: Path | None = None,
              write: bool = False, log=print) -> dict:
    """Losslessly remux an MP4 with chapters, verify, then swap in."""
    src = mf.path
    rec: dict = {"path": str(src), "container": "mp4", "chapters": chapters,
                 "ok": False, "wrote": write}
    if not chapters:
        rec["error"] = "no chapters"
        return rec
    before_info = probe(src)
    before = signature(before_info)
    rec["before"] = before
    rec["stream_hashes_before"] = stream_hashes(src, before_info)

    if out_dir is not None:
        rel = Path(mf.rel) if mf.rel else Path(src.name)
        dst = out_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
    else:
        dst = src
    # keep the temporary on the destination volume so the swap is atomic
    tmp = dst.parent / f".{dst.stem}.opsed-tmp{dst.suffix}"

    meta = _ffmetadata(chapters, f"mp4-{abs(hash(str(src))) % 10 ** 10}")
    cmd = [
        ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(src),
        "-f", "ffmetadata", "-i", str(meta),
        "-map", "0", "-map_metadata", "0", "-map_chapters", "1",
        "-c", "copy",
        str(tmp),
    ]
    rec["command"] = " ".join(str(c) for c in cmd)
    if not write:
        rec["dry_run"] = True
        rec["ok"] = True
        _cleanup(meta)
        return rec
    try:
        run(cmd, timeout=3600)
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"ffmpeg failed: {exc}"
        _cleanup(tmp)
        return rec
    finally:
        # ffmpeg has already read it, and a dry run never runs ffmpeg at all;
        # leaving these behind accumulated one file per applied episode
        _cleanup(meta)

    try:
        after_info = probe(tmp)
        after = signature(after_info)
        rec["after"] = after
        problems = diff_signature(before, after)
        problems += check_chapters(chapters, readback_chapters(tmp, after_info, "mp4"))
        hashes_after = stream_hashes(tmp, after_info)
        rec["stream_hashes_after"] = hashes_after
        for key, value in rec["stream_hashes_before"].items():
            if hashes_after.get(key) != value:
                problems.append(f"{key} payload changed ({value} -> {hashes_after.get(key)})")
        rec["problems"] = problems
        if problems:
            _cleanup(tmp)
            rec["error"] = "verification failed: " + "; ".join(problems[:6])
            return rec
        if out_dir is None:
            _force_writable(src)
            os.replace(tmp, src)
            rec["bytes_written"] = after["size"]
        else:
            if dst.exists():
                _force_writable(dst)
            os.replace(tmp, dst)
            rec["bytes_written"] = after["size"]
        rec["ok"] = True
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"post-check failed: {exc}"
        _cleanup(tmp)
        return rec
    rec["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return rec


def apply_mkv(mf: MediaFile, chapters: list[dict], *, write: bool = False,
              log=print) -> dict:
    """Set Matroska chapters in place (header-only rewrite)."""
    src = mf.path
    rec: dict = {"path": str(src), "container": "mkv", "chapters": chapters,
                 "ok": False, "wrote": write}
    if not chapters:
        rec["error"] = "no chapters"
        return rec
    before_info = probe(src)
    before = signature(before_info)
    rec["before"] = before
    rec["stream_hashes_before"] = stream_hashes(src, before_info)
    xml = TMP_DIR / f"mkv-{abs(hash(str(src))) % 10 ** 10}.xml"
    ch.write_matroskachapters(chapters, xml)
    cmd = [mkvpropedit(), str(src), "--chapters", str(xml)]
    rec["command"] = " ".join(str(c) for c in cmd)
    if not write:
        rec["dry_run"] = True
        rec["ok"] = True
        _cleanup(xml)
        return rec
    try:
        res = run(cmd, check=False, timeout=600)
    finally:
        _cleanup(xml)
    rec["mkvpropedit_stdout"] = res.stdout.strip()[-2000:]
    if res.returncode != 0:
        rec["error"] = f"mkvpropedit exit {res.returncode}: {res.stderr.strip()[-800:]}"
        return rec
    if "Warning" in res.stdout or "warning" in res.stdout:
        rec["warnings"] = [l for l in res.stdout.splitlines() if "arning" in l]
    after_info = probe(src)
    after = signature(after_info)
    rec["after"] = after
    problems = diff_signature(before, after)
    problems += check_chapters(chapters, readback_chapters(src, after_info, "mkv"))
    hashes_after = stream_hashes(src, after_info)
    rec["stream_hashes_after"] = hashes_after
    for key, value in rec["stream_hashes_before"].items():
        if hashes_after.get(key) != value:
            problems.append(f"{key} payload changed ({value} -> {hashes_after.get(key)})")
    rec["problems"] = problems
    if problems:
        rec["error"] = "verification failed: " + "; ".join(problems[:6])
        return rec
    rec["ok"] = True
    rec["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return rec


def _cleanup(p: Path) -> None:
    try:
        if p.is_file():
            _force_writable(p)
            p.unlink()
    except OSError:
        pass


def append_manifest(rec: dict) -> None:
    with open(MANIFEST_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_manifest() -> list[dict]:
    if not MANIFEST_PATH.is_file():
        return []
    out = []
    for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def rollback(rec: dict, *, write: bool = False) -> dict:
    """Undo chapter writing for one manifest record."""
    src = Path(rec["path"])
    out = {"path": str(src), "container": rec.get("container"), "ok": False}
    if not src.is_file():
        out["error"] = "file missing"
        return out
    if rec.get("container") == "mkv":
        cmd = [mkvpropedit(), str(src), "--chapters", ""]
    else:
        tmp = src.parent / f".{src.stem}.opsed-rollback{src.suffix}"
        # -map -0:d also drops the (now empty) QuickTime chapter track, so the
        # file returns to the same stream layout it had before the chapters
        # were written, not just to a chapter-less file.
        cmd = [ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
               "-i", str(src), "-map", "0", "-map", "-0:d", "-map_metadata", "0",
               "-map_chapters", "-1", "-c", "copy", str(tmp)]
    out["command"] = " ".join(str(c) for c in cmd)
    if not write:
        out["dry_run"] = True
        out["ok"] = True
        return out
    res = run(cmd, check=False, timeout=3600)
    if res.returncode != 0:
        out["error"] = f"exit {res.returncode}: {res.stderr.strip()[-600:]}"
        return out
    if rec.get("container") != "mkv":
        info = probe(tmp)
        problems = check_chapters([], readback_chapters(tmp, info, "mp4"))
        # A rollback must not touch the media either: compare against the hashes
        # recorded when the chapters were written, so "rollback is lossless" is
        # checked rather than assumed.
        wanted = rec.get("stream_hashes_before") or {}
        if wanted:
            got = stream_hashes(tmp, info)
            for key, value in wanted.items():
                if got.get(key) != value:
                    problems.append(f"{key} payload changed ({value} -> {got.get(key)})")
        if problems:
            _cleanup(tmp)
            out["error"] = "rollback verification failed: " + "; ".join(problems[:4])
            return out
        _force_writable(src)
        os.replace(tmp, src)
    else:
        if readback_chapters(src, probe(src), "mkv"):
            out["error"] = "chapters still present"
            return out
    out["ok"] = True
    return out
