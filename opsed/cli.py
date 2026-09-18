"""Command line interface.

    analyze   decode audio, detect OP/ED, write work/report.json + contact sheets
    show      print the detection report
    bench     compare detection against the embedded chapter ground truth and AniSkip
    apply     write chapters into the files (needs --write; defaults to a dry run)
    verify    re-probe every processed file and compare against the manifest
    rollback  remove the chapters again
    clean     delete the analysis cache (features + decoded audio)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import aniskip as ask
from . import apply as ap
from . import chapters as ch
from . import detect as det
from . import features as ft
from . import inspect as ins
from .library import MediaFile, Group, parse_episode, probe, scan
from .util import (ART_DIR, FEAT_DIR, PCM_DIR, PROJECT_ROOT, REPORT_PATH, Log,
                   dir_stats, ffmpeg, fmt_bytes, fmt_time, mkvpropedit,
                   read_json, wipe_dir, write_json)

CONFIG = PROJECT_ROOT / "config" / "series.json"


def _configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _load_series_config() -> dict:
    return read_json(CONFIG, {}) or {}


def _section(group_name: str, cfg: dict) -> dict:
    if group_name in cfg:
        return cfg[group_name]
    for k, v in cfg.items():
        if k and k in group_name:
            return v
    return {}


# ----------------------------------------------------------------------------
# scan
# ----------------------------------------------------------------------------

def cmd_scan(args) -> int:
    log = Log("scan")
    log(f"folder: {args.library}")
    groups = scan(args.library, probe_files=not args.fast, log=log)
    total = 0
    for g in groups:
        eps = g.episodes
        total += len(g.files)
        log(f"\n{g.name}")
        log(f"  slug={g.slug}  files={len(g.files)}  probed={len(eps)}")
        for mf in sorted(g.files, key=lambda f: (f.episode or 999)):
            ep = f"{mf.episode:02d}" if mf.episode is not None else " ?? "
            log(f"    E{ep} {fmt_time(mf.duration)} {mf.container:4s} "
                f"{mf.video_codec:5s} {mf.width}x{mf.height} "
                f"a={mf.audio_tracks} ch={mf.existing_chapters} "
                f"{mf.size/1e6:7.1f}MB  {mf.path.name}")
        missing = [mf.path.name for mf in g.files if mf.episode is None]
        if missing:
            log(f"  ! unparsed episode numbers: {missing}")
    log(f"\ntotal files: {total}")
    log.close()
    return 0


# ----------------------------------------------------------------------------
# analyze
# ----------------------------------------------------------------------------

def _select(groups: list[Group], want: list[str] | None) -> list[Group]:
    if not want:
        return groups
    sel = []
    for g in groups:
        if any(w.lower() in g.name.lower() or w.lower() == g.slug.lower() for w in want):
            sel.append(g)
    return sel


def cmd_analyze(args) -> int:
    log = Log("analyze")
    log(f"folder: {args.library}")
    groups = _select(scan(args.library, probe_files=True, log=log), args.group)
    log(f"groups: {[g.name for g in groups]}")
    if not groups:
        log("! no video files found here; nothing to do")
        log.close()
        return 1
    if len(groups) > 1:
        # one folder per run, always.  Several groups means the path is a
        # collection rather than a series folder (or a season split into
        # sub-folders, where --group picks one).
        log(f"! {len(groups)} groups under this folder; refusing to analyze.")
        log(f"  groups: {[g.name for g in groups]}")
        log("  point --library at a single series folder, or add --group <关键词>")
        log.close()
        return 1

    report = {"library": str(args.library), "groups": {}}
    if REPORT_PATH.is_file() and not args.force:
        prev = read_json(REPORT_PATH, report)
        if str(prev.get("library")) == str(args.library):
            report = prev
        else:
            log(f"note: the existing report belongs to another folder "
                f"({prev.get('library')}); starting a fresh one for this one")

    pairs = [(mf.path, mf.rel) for g in groups for mf in g.episodes]
    try:
        for g in groups:
            eps = g.episodes
            if not eps:
                log(f"\n{g.name}: no usable episodes")
                continue
            log(f"\n{g.name}  ({len(eps)} episodes)")
            feats = ft.compute_many([mf.path for mf in eps],
                                   rels=[mf.rel for mf in eps],
                                   force=args.recompute,
                                   workers=args.workers, log=log)
            keys = [mf.rel for mf in eps]
            durations = {mf.rel: mf.duration for mf in eps}
            group_report = det.detect_group(keys, durations,
                                            [feats[mf.path] for mf in eps],
                                            p=det.Params(adapt=not args.no_split),
                                            log=log)
            group_report["name"] = g.name
            group_report["slug"] = g.slug
            for k in group_report["episodes"]:
                group_report["episodes"][k]["episode"] = parse_episode(Path(k).stem)[1]
            report["groups"][g.slug] = group_report
            _print_group(group_report, log)
            _make_artifacts(g, group_report, log)
    finally:
        # Features and decoded PCM are scratch for this run only; the report is
        # what later steps read, so the cache is dropped unless asked to keep it.
        if not args.keep_cache:
            n, freed = ft.discard(pairs)
            if n:
                log(f"cache: dropped {n} file(s), freed {fmt_bytes(freed)}"
                    f" (--keep-cache to keep)")
    write_json(REPORT_PATH, report)
    log(f"\nreport -> {REPORT_PATH}")
    log.close()
    return 0


def _print_group(gr: dict, log) -> None:
    eps = gr["episodes"]
    log(f"  reference: {gr.get('reference')}")
    for name in ("op", "ed"):
        starts, durs, miss = [], [], []
        for k, rec in eps.items():
            span = rec.get(name)
            if span:
                starts.append(span["start"])
                durs.append(span["duration"])
            else:
                miss.append(rec.get("episode"))
        if not starts:
            log(f"  {name.upper()}: not detected")
            continue
        import statistics as st
        log(f"  {name.upper()}: {len(starts)}/{len(eps)} detected  "
            f"start {min(starts):7.2f}..{max(starts):7.2f} (med {st.median(starts):7.2f})  "
            f"dur {min(durs):6.2f}..{max(durs):6.2f} (med {st.median(durs):6.2f})")
        if miss:
            log(f"       no {name.upper()}: episodes {sorted(x for x in miss if x)}")
    for note in gr.get("notes", []):
        log(f"  note: {note}")
    for v in gr.get("variants", []):
        where = f" depth={v.get('depth')} n={v.get('unit_episodes')}" if v.get("depth") else ""
        log(f"  variant {v['label']} iter{v.get('iteration')} @{v['ref_start']:.1f}s "
            f"dur={v['ref_duration']:.1f} agree_peak={v.get('agree_peak')} "
            f"inliers={v.get('inlier_count')} assigned={len(v.get('assigned') or [])}"
            f"{where}")


def _make_artifacts(g: Group, gr: dict, log) -> None:
    ref_rel = gr.get("reference")
    if not ref_rel:
        return
    ref = next((mf for mf in g.files if mf.rel == ref_rel), None)
    if ref is None:
        return
    rec = gr["episodes"].get(ref_rel, {})
    spans = {"op": rec.get("op"), "ed": rec.get("ed")}
    if not any(r.get("op") or r.get("ed") for r in gr["episodes"].values()):
        log("    (nothing detected: no boundary sheet to draw)")
        return
    try:
        out = ART_DIR / f"{g.slug}__boundaries.png"
        if not out.is_file():
            size = _cell_width(ref)
            ins.boundary_sheet(ref.path, spans, out, width=size)
            log(f"  artifact -> {out.name}")
    except Exception as exc:  # noqa: BLE001
        log(f"  ! boundary sheet failed: {exc}")


def _cell_width(mf: MediaFile) -> int:
    return 384 if (mf.width or 0) >= 1600 else 256


# ----------------------------------------------------------------------------
# show
# ----------------------------------------------------------------------------

def cmd_show(args) -> int:
    report = read_json(REPORT_PATH)
    if not report:
        print("no report; run: analyze")
        return 1
    for slug, gr in report["groups"].items():
        if args.group and not any(w.lower() in slug.lower() for w in args.group):
            continue
        print(f"\n=== {gr.get('name')} ===")
        for k, rec in sorted(gr["episodes"].items(),
                             key=lambda kv: (kv[1].get("episode") or 999)):
            ep = rec.get("episode")
            op, ed = rec.get("op"), rec.get("ed")
            osc = f"{op['match_score']:.2f}" if op else " -- "
            esc = f"{ed['match_score']:.2f}" if ed else " -- "
            print(f"  E{ep if ep else '??':>2}  dur={fmt_time(rec['duration'])}  "
                  f"OP {fmt_time(op['start'] if op else None)}-"
                  f"{fmt_time(op['end'] if op else None)} [{osc}]  "
                  f"ED {fmt_time(ed['start'] if ed else None)}-"
                  f"{fmt_time(ed['end'] if ed else None)} [{esc}]")
    return 0


# ----------------------------------------------------------------------------
# bench
# ----------------------------------------------------------------------------

def cmd_bench(args) -> int:
    report = read_json(REPORT_PATH)
    if not report:
        print("no report; run: analyze")
        return 1
    cfg = _load_series_config()
    log = Log("bench")

    ground = _ground_truth_chapters(report)
    if ground:
        log("\n== embedded chapter ground truth ==")
        for rel, spans in ground.items():
            gr = _find_group(report, rel)
            if not gr:
                continue
            rec = gr["episodes"].get(rel)
            if not rec:
                continue
            for name, truth in spans.items():
                got = rec.get(name)
                if not got:
                    log(f"  {Path(rel).name} {name.upper()}: not detected "
                        f"(truth {truth['start']:.3f})")
                    continue
                log(f"  {Path(rel).name} {name.upper()}: detected {got['start']:.3f} "
                    f"vs truth {truth['start']:.3f}  delta {got['start']-truth['start']:+.3f}s  "
                    f"dur {got['duration']:.2f} vs {truth['duration']:.2f}")

    if args.no_network:
        log("\n(AniSkip cross-check skipped: --no-network)")
        log.close()
        return 0

    log("\n== AniSkip cross-check ==")
    for slug, gr in report["groups"].items():
        sec = _section(gr.get("name", ""), cfg)
        mal = sec.get("mal_id")
        if mal is None:
            title = sec.get("title")
            if not title:
                log(f"  {gr.get('name')}: no title configured, skipped")
                continue
            res = ask.resolve_mal_id(title)
            cands = (res or {}).get("candidates") or []
            mal = next((c["mal_id"] for c in cands if c.get("mal_id")), None)
            log(f"  {gr.get('name')}: resolved '{title}' -> mal {mal} "
                f"({'/'.join(c.get('title') or '' for c in cands[:3])})")
        if mal is None:
            continue
        deltas = {"op": [], "ed": [], "miss_remote": 0, "miss_local": 0}
        for k, rec in gr["episodes"].items():
            ep = rec.get("episode")
            if not ep:
                continue
            remote = ask.skip_times(int(mal), ep, rec["duration"])
            if not remote or not remote.get("found"):
                deltas["miss_remote"] += 1
                continue
            for name in ("op", "ed"):
                r = remote.get(name)
                loc = rec.get(name)
                if not r:
                    continue
                if not loc:
                    deltas["miss_local"] += 1
                    continue
                deltas[name].append(loc["start"] - r["start"])
        for name in ("op", "ed"):
            ds = deltas[name]
            if ds:
                import statistics as st
                good = [d for d in ds if abs(d) <= 3.0]
                log(f"  {gr.get('name')[:38]:38s} {name.upper()}: n={len(ds)} "
                    f"median {st.median(ds):+.2f}s  |d|<=3s: {len(good)}/{len(ds)}  "
                    f"min {min(ds):+.2f} max {max(ds):+.2f}")
        if deltas["miss_remote"] or deltas["miss_local"]:
            log(f"      AniSkip无数据 {deltas['miss_remote']} 集；"
                f"本地未检出但AniSkip有 {deltas['miss_local']} 集")
    log.close()
    return 0


def _find_group(report: dict, rel: str) -> dict | None:
    for gr in report["groups"].values():
        if rel in gr["episodes"]:
            return gr
    return None


def _ground_truth_chapters(report: dict) -> dict:
    """Episodes that already carry chapters: use them as a reference."""
    out = {}
    for slug, gr in report["groups"].items():
        for k in gr["episodes"]:
            path = Path(report["library"]) / k
            if not path.is_file():
                continue
            try:
                info = probe(path, chapters=True)
            except Exception:  # noqa: BLE001
                continue
            cs = ap.read_chapters(info)
            if not cs:
                continue
            spans = {}
            for c in cs:
                t = (c.get("title") or "").lower()
                if not spans.get("op") and any(w in t for w in ("op", "オープニング", "opening", "片头", "片頭")):
                    spans["op"] = {"start": c["start"], "end": c["end"],
                                   "duration": c["end"] - c["start"]}
                if not spans.get("ed") and any(w in t for w in ("ed", "エンディング", "ending", "片尾", "片尾曲")):
                    spans["ed"] = {"start": c["start"], "end": c["end"],
                                   "duration": c["end"] - c["start"]}
            if spans:
                out[k] = spans
    return out


# ----------------------------------------------------------------------------
# apply / verify / rollback
# ----------------------------------------------------------------------------

def _chapters_for(gr: dict, rel: str, mode: str) -> list[dict]:
    rec = gr["episodes"][rel]
    return ch.build(rec.get("op"), rec.get("ed"), rec["duration"], mode)


def cmd_apply(args) -> int:
    report = read_json(REPORT_PATH)
    if not report:
        print("no report; run: analyze")
        return 1
    log = Log("apply")
    out_dir = Path(args.out_dir) if args.out_dir else None
    done = skipped = failed = attempted = 0
    for slug, gr in report["groups"].items():
        if args.group and not any(w.lower() in slug.lower()
                                  or w.lower() in (gr.get("name") or "").lower()
                                  for w in args.group):
            continue
        for rel, rec in sorted(gr["episodes"].items(),
                               key=lambda kv: (kv[1].get("episode") or 999)):
            if args.limit and attempted >= args.limit:
                break
            path = Path(report["library"]) / rel
            if not path.is_file():
                log(f"  ! missing: {rel}")
                failed += 1
                continue
            if not rec.get("op") and not rec.get("ed"):
                log(f"  - skip (no OP/ED detected): {path.name}")
                skipped += 1
                continue
            chapters = _chapters_for(gr, rel, args.chapters)
            if not chapters:
                log(f"  - skip (empty chapter list): {path.name}")
                skipped += 1
                continue
            mf = MediaFile(path=path, group=gr.get("name", slug), rel=rel,
                           size=path.stat().st_size)
            info = probe(path)
            mf.existing_chapters = len(info.get("chapters") or [])
            if mf.existing_chapters and not args.force:
                log(f"  - skip (already has {mf.existing_chapters} chapters): {path.name}")
                skipped += 1
                continue
            kind = "mkv" if path.suffix.lower() == ".mkv" else "mp4"
            attempted += 1
            fn = ap.apply_mkv if kind == "mkv" else ap.apply_mp4
            kwargs = {"write": args.write, "log": log}
            if kind == "mp4":
                kwargs["out_dir"] = out_dir
            result = fn(mf, chapters, **kwargs)
            result["episode"] = rec.get("episode")
            result["series"] = gr.get("name")
            result["mode"] = args.chapters
            if result.get("ok"):
                done += 1
                tag = "dry-run" if result.get("dry_run") else "written"
                log(f"  + {tag}: {path.name}  "
                    f"[{', '.join(c['title'] for c in chapters)}]")
                if args.write:
                    ap.append_manifest(result)
            else:
                failed += 1
                log(f"  ! FAILED: {path.name}: {result.get('error')}")
                if args.write:
                    ap.append_manifest(result)
    log(f"\nprocessed={done} skipped={skipped} failed={failed} "
        f"{'(dry run: nothing was modified)' if not args.write else ''}")
    log.close()
    return 1 if failed else 0


def _manifest_records(want: list[str] | None) -> list[dict]:
    """Current state per file, optionally filtered by series.

    The manifest is an append-only log, so one file can appear several times (a
    failed attempt, then a success, then a rollback).  Only the LAST record per
    path is the current truth — otherwise `verify` would keep demanding chapters
    from files the user deliberately rolled back.
    """
    latest: dict[str, dict] = {}
    for r in ap.load_manifest():
        path = r.get("path")
        if path:
            latest[path] = r
    recs = [r for r in latest.values()
            if r.get("ok") and r.get("wrote") and not r.get("dry_run")
            and not r.get("rolled_back")]
    if not want:
        return recs
    keep = []
    for r in recs:
        hay = f"{r.get('series') or ''} {r.get('path') or ''}".lower()
        if any(w.lower() in hay for w in want):
            keep.append(r)
    return keep


def cmd_verify(args) -> int:
    recs = _manifest_records(getattr(args, "group", None))
    if not recs:
        print("no matching records in the manifest; nothing to verify")
        return 1
    log = Log("verify")
    bad = 0
    for r in recs:
        path = Path(r["path"])
        if not path.is_file():
            log(f"  ! missing: {path}")
            bad += 1
            continue
        try:
            info = probe(path)
        except Exception as exc:  # noqa: BLE001
            log(f"  ! unreadable {path.name}: {exc}")
            bad += 1
            continue
        container = "mkv" if path.suffix.lower() == ".mkv" else "mp4"
        got = ap.readback_chapters(path, info, container)
        problems = ap.check_chapters(r.get("chapters") or [], got)
        if problems:
            log(f"  ! {path.name}: {'; '.join(problems[:4])}")
            bad += 1
        else:
            names = ", ".join(f"{c['title']}@{fmt_time(c['start'])}" for c in got)
            log(f"  ok {path.name}  [{names}]")
    log(f"\nverified {len(recs)-bad}/{len(recs)} ok")
    log.close()
    return 1 if bad else 0


def cmd_rollback(args) -> int:
    recs = _manifest_records(getattr(args, "group", None))
    if not recs:
        print("no matching records in the manifest; nothing to roll back")
        return 1
    log = Log("rollback")
    if not args.write:
        log(f"dry run: would remove chapters from {len(recs)} file(s) "
            f"(pass --write to do it)")
    bad = 0
    for r in recs:
        out = ap.rollback(r, write=args.write)
        if out.get("ok"):
            log(f"  - {Path(r['path']).name}")
            if args.write:
                # record the removal so verify/rollback stop expecting chapters
                ap.append_manifest({
                    "path": r["path"], "container": r.get("container"),
                    "series": r.get("series"), "episode": r.get("episode"),
                    "ok": True, "wrote": False, "rolled_back": True,
                    "chapters": [],
                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
        else:
            log(f"  ! {Path(r['path']).name}: {out.get('error')}")
            bad += 1
    log.close()
    return 1 if bad else 0


def cmd_clean(args) -> int:
    log = Log("clean")
    targets = [("pcm", PCM_DIR), ("feat", FEAT_DIR)]
    if args.all:
        targets += [("aniskip", ask.CACHE), ("artifacts", ART_DIR)]
    total_files = 0
    total_bytes = 0
    for name, path in targets:
        if args.dry_run:
            n, size = dir_stats(path)
            log(f"  {name}: {n} file(s), {fmt_bytes(size)}")
        else:
            n, size = wipe_dir(path)
            log(f"  {name}: removed {n} file(s), freed {fmt_bytes(size)}")
        total_files += n
        total_bytes += size
    what = "would free" if args.dry_run else "freed"
    log(f"{what} {fmt_bytes(total_bytes)} ({total_files} file(s))")
    log("report.json and manifest.jsonl are left alone")
    log.close()
    return 0


# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    p = argparse.ArgumentParser(prog="opsed", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--library", metavar="<文件夹>",
                   help="folder to operate on -- required for every command "
                        "except clean; one series folder per run")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="list groups, episodes, durations")
    s.add_argument("--fast", action="store_true", help="skip ffprobe (names only)")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("analyze", help="detect OP/ED and write the report")
    s.add_argument("--group", action="append", help="only this group (repeatable)")
    s.add_argument("--force", action="store_true", help="ignore existing report")
    s.add_argument("--recompute", action="store_true", help="re-decode audio")
    s.add_argument("--keep-cache", action="store_true",
                   help="keep features/PCM after the run (default: delete them)")
    s.add_argument("--no-split", action="store_true",
                   help="do not re-run detection on uncovered episodes "
                        "(mid-season OP/ED changes then stay undetected)")
    s.add_argument("--workers", type=int, default=8)
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("show", help="print the report")
    s.add_argument("--group", action="append")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("bench", help="compare against embedded chapters and AniSkip")
    s.add_argument("--no-network", action="store_true")
    s.set_defaults(func=cmd_bench)

    s = sub.add_parser("apply", help="write chapters (dry run unless --write)")
    s.add_argument("--group", action="append")
    s.add_argument("--write", action="store_true", help="actually modify files")
    s.add_argument("--force", action="store_true", help="overwrite existing chapters")
    s.add_argument("--limit", type=int, default=0, help="stop after N files")
    s.add_argument("--out-dir", default=None,
                   help="write MP4 output here instead of replacing in place")
    s.add_argument("--chapters", default=ch.MODE_DEFAULT,
                   choices=[ch.MODE_MINIMAL, ch.MODE_DEFAULT, ch.MODE_FULL])
    s.set_defaults(func=cmd_apply)

    s = sub.add_parser("verify", help="re-probe every written file")
    s.add_argument("--group", action="append", help="only this group (repeatable)")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("rollback", help="remove written chapters again")
    s.add_argument("--group", action="append", help="only this group (repeatable)")
    s.add_argument("--write", action="store_true")
    s.set_defaults(func=cmd_rollback)

    s = sub.add_parser("clean", help="delete the analysis cache (features + decoded audio)")
    s.add_argument("--dry-run", action="store_true", help="only report what would be freed")
    s.add_argument("--all", action="store_true",
                   help="also drop the AniSkip cache and boundary contact sheets")
    s.set_defaults(func=cmd_clean)

    args = p.parse_args(argv)
    if args.cmd != "clean":
        # Every run names exactly one folder: there is no default library and no
        # way to sweep the whole collection by accident.
        if not args.library:
            p.error("--library <文件夹> 是必需的——每次只能处理指定的一个文件夹。\n"
                    "例如: -m opsed.cli --library \"/path/to/某番\" analyze")
        if not Path(args.library).is_dir():
            p.error(f"not a folder: {args.library}")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
