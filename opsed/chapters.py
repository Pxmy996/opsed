"""Chapter list assembly and container-specific serialisation."""

from __future__ import annotations

from pathlib import Path

MODE_MINIMAL = "minimal"    # OP, ED
MODE_DEFAULT = "default"    # OP, main, ED, preview
MODE_FULL = "full"          # + a chapter at 0

MIN_CHAPTER_S = 1.0


def build(op: dict | None, ed: dict | None, duration: float,
          mode: str = MODE_DEFAULT) -> list[dict]:
    """Assemble the chapter list for one episode.

    Chapters mark where each section *starts*, so jumping to a chapter skips
    the preceding section: jumping to "main" skips the OP, jumping to "preview"
    skips the ED.
    """
    chapters: list[dict] = []

    def add(start: float, end: float, title: str) -> None:
        start = max(0.0, min(float(start), duration))
        end = max(0.0, min(float(end), duration))
        if end - start < MIN_CHAPTER_S:
            return
        chapters.append({"start": round(start, 3), "end": round(end, 3),
                         "title": title})

    if mode == MODE_MINIMAL:
        if op:
            add(op["start"], op["end"], "OP")
        if ed:
            add(ed["start"], ed["end"], "ED")
    else:
        if op and ed:
            if mode == MODE_FULL and op["start"] > 5.0:
                add(0.0, op["start"], "前情")
            add(op["start"], op["end"], "OP")
            add(op["end"], ed["start"], "正片")
            add(ed["start"], ed["end"], "ED")
            if duration - ed["end"] > 5.0:
                add(ed["end"], duration, "预告")
        elif op:
            if mode == MODE_FULL and op["start"] > 5.0:
                add(0.0, op["start"], "前情")
            add(op["start"], op["end"], "OP")
            add(op["end"], duration, "正片")
        elif ed:
            add(0.0, ed["start"], "正片")
            add(ed["start"], ed["end"], "ED")
            if duration - ed["end"] > 5.0:
                add(ed["end"], duration, "预告")

    chapters.sort(key=lambda c: c["start"])
    return chapters


# ----------------------------------------------------------------------------
# FFMETADATA1 (ffmpeg, used for MP4 and any remux path)
# ----------------------------------------------------------------------------

_ESC = str.maketrans({"\\": "\\\\", "=": "\\=", ";": "\\;", "#": "\\#", "\n": "\\\n"})


def _esc(value: str) -> str:
    return value.translate(_ESC)


def write_ffmetadata(chapters: list[dict], path: Path, *, duration: float | None = None,
                     title: str | None = None) -> Path:
    lines = [";FFMETADATA1"]
    if title:
        lines.append(f"title={_esc(title)}")
    for c in chapters:
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={int(round(c['start'] * 1000))}",
            f"END={int(round(c['end'] * 1000))}",
            f"title={_esc(c['title'])}",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ----------------------------------------------------------------------------
# Matroska chapters XML (mkvpropedit)
# ----------------------------------------------------------------------------

def _matroska_time(t: float) -> str:
    t = max(0.0, float(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:012.9f}"


def write_matroskachapters(chapters: list[dict], path: Path,
                           language: str = "und") -> Path:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<Chapters>", "  <EditionEntry>"]
    for c in chapters:
        parts += [
            "    <ChapterAtom>",
            f"      <ChapterTimeStart>{_matroska_time(c['start'])}</ChapterTimeStart>",
            f"      <ChapterTimeEnd>{_matroska_time(c['end'])}</ChapterTimeEnd>",
            "      <ChapterDisplay>",
            f"        <ChapterString>{_xml(c['title'])}</ChapterString>",
            f"        <ChapterLanguage>{language}</ChapterLanguage>",
            "      </ChapterDisplay>",
            "    </ChapterAtom>",
        ]
    parts += ["  </EditionEntry>", "</Chapters>", ""]
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def _xml(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))
