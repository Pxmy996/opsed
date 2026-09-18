"""Library scanning, series grouping, episode-number parsing and probing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .util import VIDEO_EXTS, ffprobe, run


@dataclass
class MediaFile:
    path: Path
    group: str            # parent folder name == series group key
    rel: str              # path relative to library root
    size: int
    episode: int | None = None
    season: int | None = None
    duration: float | None = None
    container: str = ""   # "mkv" | "mp4"
    audio_tracks: int = 0
    video_codec: str = ""
    width: int = 0
    height: int = 0
    existing_chapters: int = 0
    probe_error: str | None = None

    @property
    def slug(self) -> str:
        return slugify(self.group)

    def tag(self) -> str:
        ep = f"{self.episode:02d}" if self.episode is not None else "??"
        return f"{self.slug}/E{ep}"


@dataclass
class Group:
    name: str
    slug: str
    files: list[MediaFile] = field(default_factory=list)

    @property
    def episodes(self) -> list[MediaFile]:
        return sorted(
            [f for f in self.files if f.duration],
            key=lambda f: (f.episode if f.episode is not None else 999, f.path.name),
        )


_CN = r"\u4e00-\u9fff"


def slugify(name: str) -> str:
    """Stable, filesystem-safe identifier for a series group."""
    s = name.strip()
    s = re.sub(r"[\s　]+", "_", s)
    s = re.sub(r"[^\w\-\u4e00-\u9fff.]", "", s, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_.")
    return s[:80] or "series"


# Ordered, most-specific-first episode-number patterns.
_EP_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee][Pp]?(\d{1,3})(?!\d)"), "SxE"),
    (re.compile(r"第\s*(\d{1,3})\s*[集话話]"), "cn_ep"),
    # "[29] [1080p]" / "[46v2] [1080p]": the episode sits in its own bracket
    # group, which is how several Chinese release groups number a season
    (re.compile(r"\[(\d{1,3})(?:v\d)?\]\s*\["), "bracket_ep"),
    (re.compile(r"[Ee][Pp]?(\d{1,3})(?!\d)"), "E"),
    (re.compile(rf"\]\s*-\s*(\d{{1,3}})(?!\d)"), "dash"),
    (re.compile(r"-\s*(\d{1,3})\s*\["), "dash_bracket"),
    (re.compile(r"(\d{1,3})\s*(?:4[kK]|2160p|1080p|720p|480p)"), "bare_res"),
    (re.compile(rf"[{_CN}]\s*(\d{{1,3}})\s*$"), "cn_glued"),
    (re.compile(r"(?:^|[\s._\-])(\d{1,3})(?:\s*v\d)?\s*$"), "trailing"),
]


def parse_episode(stem: str) -> tuple[int | None, int | None]:
    """Best-effort (season, episode) extraction from a filename stem."""
    for pat, kind in _EP_PATTERNS:
        m = pat.search(stem)
        if not m:
            continue
        if kind == "SxE":
            return int(m.group(1)), int(m.group(2))
        ep = int(m.group(1))
        if ep == 0:
            continue
        return None, ep
    return None, None


def probe(path: Path, *, chapters: bool = True) -> dict:
    """ffprobe a file; returns format/streams/chapters as parsed JSON."""
    cmd = [
        ffprobe(), "-v", "error",
        "-show_format", "-show_streams",
        "-print_format", "json",
    ]
    if chapters:
        cmd.append("-show_chapters")
    cmd.append(str(path))
    res = run(cmd, check=False)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[-500:] or "ffprobe failed")
    import json
    return json.loads(res.stdout or "{}")


def apply_probe(mf: MediaFile, info: dict) -> None:
    fmt = info.get("format", {})
    dur = fmt.get("duration")
    mf.duration = float(dur) if dur not in (None, "N/A") else None
    mf.container = (fmt.get("format_name") or "").split(",")[0]
    streams = info.get("streams", [])
    v = [s for s in streams if s.get("codec_type") == "video"]
    a = [s for s in streams if s.get("codec_type") == "audio"]
    mf.audio_tracks = len(a)
    if v:
        mf.video_codec = v[0].get("codec_name", "")
        mf.width = int(v[0].get("width") or 0)
        mf.height = int(v[0].get("height") or 0)
    mf.existing_chapters = len(info.get("chapters") or [])


def scan(root: Path, *, probe_files: bool = True, log=None) -> list[Group]:
    """Walk the given folder and group media files by their parent directory.

    `root` is always explicit: there is deliberately no default library, so a
    run can never silently touch folders the user did not name.
    """
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(root)
    by_group: dict[str, list[MediaFile]] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
            continue
        mf = MediaFile(
            path=p,
            group=p.parent.name,
            rel=str(p.relative_to(root)),
            size=p.stat().st_size,
        )
        mf.season, mf.episode = parse_episode(p.stem)
        by_group.setdefault(mf.group, []).append(mf)

    groups: list[Group] = []
    for name, files in sorted(by_group.items()):
        g = Group(name=name, slug=slugify(name), files=files)
        groups.append(g)

    if probe_files:
        for g in groups:
            for mf in g.files:
                try:
                    apply_probe(mf, probe(mf.path))
                except Exception as exc:  # noqa: BLE001
                    mf.probe_error = str(exc)
                    if log:
                        log(f"  ! probe failed: {mf.rel}: {exc}")

    return groups
