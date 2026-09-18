"""AniSkip cross-check.

Read-only GETs to the public AniSkip API, used purely to sanity-check the local
audio analysis (and as a fallback when local detection fails for a group).  All
responses are cached on disk; `--no-network` disables it entirely.

API: GET https://api.aniskip.com/v2/skip-times/{malId}/{episode}
         ?types[]=op&types[]=ed&episodeLength={seconds}
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .util import CACHE_DIR

API = "https://api.aniskip.com/v2/skip-times"
ANILIST = "https://graphql.anilist.co"
CACHE = CACHE_DIR / "aniskip"
CACHE.mkdir(parents=True, exist_ok=True)
UA = "opsed/0.1 (local anime chapter tool)"


def _get(url: str, *, timeout: float = 20.0) -> tuple[int, dict | list | None]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return e.code, None
    except Exception:  # noqa: BLE001
        return 0, None


def skip_times(mal_id: int, episode: float, episode_length: float,
               *, cache_only: bool = False) -> dict | None:
    """Return {'op': {'start','end'}, 'ed': {...}} or None (no data / offline)."""
    key = CACHE / f"{mal_id}-{int(episode)}-{int(episode_length)}.json"
    if key.is_file():
        try:
            return json.loads(key.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    if cache_only:
        return None
    q = urllib.parse.urlencode({
        "types[]": ["op", "ed"],
        "episodeLength": int(episode_length),
    }, doseq=True)
    status, body = _get(f"{API}/{mal_id}/{int(episode)}?{q}")
    if status == 404 or not isinstance(body, dict) or not body.get("found"):
        result = {"found": False, "op": None, "ed": None}
    else:
        result = {"found": True, "op": None, "ed": None, "raw": body.get("results")}
        for r in body.get("results") or []:
            iv = r.get("interval") or {}
            st = r.get("skipType")
            if st in ("op", "ed") and iv:
                result[st] = {"start": float(iv.get("startTime", 0.0)),
                              "end": float(iv.get("endTime", 0.0))}
    key.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    time.sleep(0.25)
    return result


def resolve_mal_id(title: str, *, cache_only: bool = False) -> dict | None:
    """Look up a MAL id from an anime title via the public AniList GraphQL API."""
    key = CACHE / f"mal-{abs(hash(title)) % (10 ** 12)}.json"
    if key.is_file():
        try:
            return json.loads(key.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    if cache_only:
        return None
    query = """
    query ($s: String) {
      Page(perPage: 6) {
        media(search: $s, type: ANIME) {
          id idMal format episodes seasonYear
          title { romaji native english }
        }
      }
    }"""
    payload = json.dumps({"query": query, "variables": {"s": title}}).encode("utf-8")
    req = urllib.request.Request(
        ANILIST, data=payload,
        headers={"User-Agent": UA, "Content-Type": "application/json",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None
    cands = ((body.get("data") or {}).get("Page") or {}).get("media") or []
    result = {"query": title, "candidates": [
        {"mal_id": c.get("idMal"), "anilist_id": c.get("id"),
         "episodes": c.get("episodes"), "year": c.get("seasonYear"),
         "format": c.get("format"),
         "title": (c.get("title") or {}).get("romaji"),
         "native": (c.get("title") or {}).get("native")}
        for c in cands
    ]}
    key.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def compare(local: dict | None, remote: dict | None, *, tol: float = 3.0) -> dict:
    """Compare one local span against the AniSkip one."""
    if not remote:
        return {"status": "no-remote"}
    if not local:
        return {"status": "no-local"}
    return {
        "status": "ok" if abs(local["start"] - remote["start"]) <= tol else "mismatch",
        "d_start": round(local["start"] - remote["start"], 3),
        "d_end": round(local["end"] - remote["end"], 3),
    }
