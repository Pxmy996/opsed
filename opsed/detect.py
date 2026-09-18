"""OP/ED detection by cross-episode audio recurrence.

Idea
----
Within one season the OP and ED audio are identical in every episode, while the
story audio is unique per episode.  So: take one episode as a time axis, find
the audio segments it shares with many other episodes.  Those are the OP/ED.
No ML model, no reference audio download, no video decode.

Passes
------
1. coarse  - FFT cross-correlation of a reference episode against every other
             episode yields the relative shifts where shared audio lines up; a
             short normalised window scan turns each shift into concrete shared
             spans on the reference timeline.
2. cluster - spans from all pairs are clustered on the reference timeline.  Each
             cluster is one recurring segment (OP, ED, preview, ...) plus the set
             of episodes containing it.  Two clusters of the same role with
             disjoint episode sets = the OP/ED changed mid-season.
3. refine  - align every episode onto a consensus template to get a per-episode
             anchor, then measure cross-episode agreement versus offset from that
             anchor.  Agreement is ~1 inside a shared segment and ~1/N outside,
             which pins the exact boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

FINE_FPS = 50.0


@dataclass
class Params:
    coarse_stride: int = 5          # 50 fps -> 10 fps for the lag search
    lag_max_peaks: int = 8
    lag_min_sep_s: float = 4.0
    span_win_s: float = 6.0
    span_thr: float = 0.5
    span_merge_gap_s: float = 3.0
    min_det_s: float = 5.0
    coarse_shrink_s: float = 1.0  # coarse spans only need to locate the segment
    seg_min_dur: float = 15.0
    seg_max_dur: float = 240.0
    member_frac: float = 0.3        # some shows omit the OP in several episodes
    member_min: int = 3
    cluster_gap_s: float = 6.0
    trim_s: float = 4.0
    match_thr: float = 0.5
    refine_search_s: float = 15.0
    agree_peak_frac: float = 0.8  # boundary = this fraction of the segment plateau
    agree_floor: float = 0.15
    hyst_s: float = 3.5          # tolerate brief coherence dips inside a segment
    min_template_s: float = 8.0
    coh_win_s: float = 4.0       # window for the cross-episode coherence statistic
    pre_margin_s: float = 15.0   # analysis window before/after the segment
    post_margin_s: float = 30.0
    dedilate: float = 0.0        # extra boundary correction (see calibration)
    harmonise_merge_s: float = 3.0  # variant lengths within this are one version
    harmonise_pct: float = 25.0  # boundary overshoot is one-sided -> low percentile
    max_iterations: int = 8      # one variant (OP/ED version) discovered per round
    adapt: bool = True           # re-run on uncovered episodes (see _adapt_fill)
    adapt_min_episodes: int = 6  # never re-run detection on fewer episodes
    adapt_max_depth: int = 2     # re-split rounds (a 100-episode season -> quarters)


@dataclass
class Variant:
    """One recurring segment found on the reference timeline."""
    label: str
    ref_start: float
    ref_end: float
    member_keys: list = field(default_factory=list)
    corrs: list = field(default_factory=list)
    boundaries: tuple | None = None          # (d_start, d_end) rel. to anchor
    anchors: dict = field(default_factory=dict)
    scores: dict = field(default_factory=dict)
    energy: object = None                    # agreement profile (diagnostics)
    energy_d0: int = 0
    energy_fps: float = 50.0

    @property
    def duration(self) -> float:
        return self.ref_end - self.ref_start

    def summary(self) -> dict:
        return {
            "label": self.label,
            "ref_start": round(self.ref_start, 3),
            "ref_end": round(self.ref_end, 3),
            "ref_duration": round(self.duration, 3),
            "boundaries": [round(x, 3) for x in self.boundaries] if self.boundaries else None,
            "inlier_count": len(self.member_keys),
            "inlier_keys": list(self.member_keys),
        }


# ----------------------------------------------------------------------------
# correlation primitives
# ----------------------------------------------------------------------------

def _next_pow2(n: int) -> int:
    return 1 << int(np.ceil(np.log2(max(n, 2))))


def dot_profile(T: np.ndarray, J: np.ndarray) -> np.ndarray:
    """num[o] = sum_{u,b} T[u,b] * J[o+u,b] for o = 0 .. Tj-Lt.

    irfft(conj(rfft(T)) * rfft(J))[k] is already sum_u T[u] J[u+k], i.e. a
    correlation -- reversing T here would turn it into a convolution.
    """
    Lt, Tj = T.shape[0], J.shape[0]
    if Tj < Lt:
        return np.zeros(0, dtype=np.float32)
    n = _next_pow2(Tj + Lt)
    A = np.fft.rfft(T, n=n, axis=0)
    B = np.fft.rfft(J, n=n, axis=0)
    cc = np.fft.irfft(np.conj(A) * B, n=n, axis=0).sum(axis=1)
    return cc[: Tj - Lt + 1].astype(np.float32)


def frame_energy_cum(J: np.ndarray) -> np.ndarray:
    e = (J.astype(np.float64) ** 2).sum(axis=1)
    return np.concatenate(([0.0], np.cumsum(e)))


def match_range(T: np.ndarray, J: np.ndarray, cum: np.ndarray,
                o_from: int, o_to: int) -> tuple[np.ndarray, np.ndarray]:
    """Normalised correlation of template T at every offset in [o_from, o_to]."""
    Lt, Tj = T.shape[0], J.shape[0]
    o_from = max(0, int(o_from))
    o_to = min(int(o_to), Tj - Lt)
    if o_to < o_from:
        return np.zeros(0, np.int64), np.zeros(0, np.float32)
    idx = np.arange(o_from, o_to + 1)
    num = dot_profile(T, J)[idx]
    ej = cum[idx + Lt] - cum[idx]
    et = float((T.astype(np.float64) ** 2).sum())
    den = np.sqrt(np.maximum(ej, 1e-9) * max(et, 1e-9))
    return idx, (num / den).astype(np.float32)


def norm_win_corr(R: np.ndarray, J: np.ndarray, lag: int, L: int):
    """Sliding normalised correlation of length-L windows at a fixed lag."""
    T1, T2 = R.shape[0], J.shape[0]
    t0 = max(0, -lag)
    t1 = min(T1, T2 - lag)
    if t1 - t0 < L:
        return np.zeros(0, np.int64), np.zeros(0, np.float32)
    a = R[t0:t1].astype(np.float64)
    b = J[t0 + lag:t1 + lag].astype(np.float64)
    cg = np.concatenate(([0.0], np.cumsum((a * b).sum(axis=1))))
    cec = np.concatenate(([0.0], np.cumsum((a * a).sum(axis=1))))
    cef = np.concatenate(([0.0], np.cumsum((b * b).sum(axis=1))))
    num = cg[L:] - cg[:-L]
    den = np.sqrt(np.maximum(cec[L:] - cec[:-L], 1e-9)
                  * np.maximum(cef[L:] - cef[:-L], 1e-9))
    return t0 + np.arange(num.size), (num / den).astype(np.float32)


def lag_peaks(R: np.ndarray, J: np.ndarray, p: Params, fps: float) -> list[int]:
    """Candidate relative shifts (in frames) where R and J share audio."""
    T1, T2 = R.shape[0], J.shape[0]
    if T1 < 2 or T2 < 2:
        return []
    n = _next_pow2(T1 + T2 + 1)
    A = np.fft.rfft(R, n=n, axis=0)
    B = np.fft.rfft(J, n=n, axis=0)
    cc = np.fft.irfft(np.conj(A) * B, n=n, axis=0).sum(axis=1)
    lags = np.arange(-(T2 - 1), T1)
    vals = cc[np.where(lags >= 0, lags, lags + n)]
    min_overlap = int(min(T1, T2) * 0.25)
    overlap = np.minimum(T1, T2 + lags) - np.maximum(0, lags)
    vals = np.where((overlap >= min_overlap) & (vals > 0), vals, -np.inf)
    if not np.isfinite(vals).any():
        return []
    sep = max(1, int(p.lag_min_sep_s * fps))
    order = np.argsort(vals)[::-1]
    chosen: list[int] = []
    for i in order:
        if not np.isfinite(vals[i]):
            break
        if all(abs(int(i) - j) >= sep for j in chosen):
            chosen.append(int(i))
        if len(chosen) >= p.lag_max_peaks:
            break
    return [int(lags[i]) for i in chosen]


def spans_above(times: np.ndarray, corr: np.ndarray, thr: float, fps: float,
                min_len_s: float, merge_gap_s: float, shrink_s: float = 0.0):
    """Contiguous high-correlation spans as (start_s, end_s, mean_corr)."""
    if times.size == 0:
        return []
    hot = corr > thr
    if not hot.any():
        return []
    edges = np.diff(hot.astype(np.int8))
    starts = list(np.where(edges == 1)[0] + 1)
    ends = list(np.where(edges == -1)[0] + 1)
    if hot[0]:
        starts.insert(0, 0)
    if hot[-1]:
        ends.append(hot.size)
    gap = int(merge_gap_s * fps)
    merged: list[list[int]] = []
    for s, e in zip(starts, ends):
        if merged and s - merged[-1][1] <= gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    out = []
    for s, e in merged:
        a = times[s] / fps + shrink_s
        b = times[e - 1] / fps - shrink_s
        if (times[e - 1] - times[s]) / fps < min_len_s or b - a < min_len_s:
            continue
        out.append((a, b, float(corr[s:e].mean())))
    return out


# ----------------------------------------------------------------------------
# pass 1: coarse shared spans
# ----------------------------------------------------------------------------

def _coarse_detections(feats, keys, r, p: Params) -> list[dict]:
    fps = FINE_FPS / p.coarse_stride
    R = feats[r][:: p.coarse_stride]
    L = max(8, int(p.span_win_s * fps))
    dets: list[dict] = []
    for j, F in enumerate(feats):
        if j == r:
            continue
        J = F[:: p.coarse_stride]
        for lag in lag_peaks(R, J, p, fps):
            times, corr = norm_win_corr(R, J, lag, L)
            for a, b, mc in spans_above(times, corr, p.span_thr, fps,
                                        p.min_det_s, p.span_merge_gap_s,
                                        shrink_s=p.coarse_shrink_s):
                dets.append({
                    "ref_start": a, "ref_end": b, "key": keys[j], "corr": mc,
                    "start": a + lag / fps, "end": b + lag / fps,
                })
    return dets


def _cluster(dets: list[dict], p: Params) -> list[list[dict]]:
    if not dets:
        return []
    dets = sorted(dets, key=lambda d: d["ref_start"])
    clusters, cur = [], [dets[0]]
    cur_end = dets[0]["ref_end"]
    for d in dets[1:]:
        if d["ref_start"] <= cur_end + p.cluster_gap_s:
            cur.append(d)
            cur_end = max(cur_end, d["ref_end"])
        else:
            clusters.append(cur)
            cur = [d]
            cur_end = d["ref_end"]
    clusters.append(cur)
    return clusters


def _cluster_extent(members: list[dict]) -> tuple[float, float]:
    s = np.array([m["ref_start"] for m in members])
    e = np.array([m["ref_end"] for m in members])
    return float(np.percentile(s, 25)), float(np.percentile(e, 75))


# ----------------------------------------------------------------------------
# pass 3: refinement
# ----------------------------------------------------------------------------

def _consensus_template(feats, anchors, length: int) -> np.ndarray | None:
    B = feats[0].shape[1]
    acc = np.zeros((length, B), dtype=np.float64)
    cnt = np.zeros(length, dtype=np.float64)
    for F, o in zip(feats, anchors):
        s, e = o, o + length
        s2, e2 = max(s, 0), min(e, F.shape[0])
        if e2 - s2 < length * 0.5:
            continue
        acc[s2 - s: e2 - s] += F[s2:e2]
        cnt[s2 - s: e2 - s] += 1
    good = cnt > 0
    if good.sum() < length * 0.5:
        return None
    acc[good] /= cnt[good][:, None]
    m = acc.mean(axis=0, keepdims=True)
    sd = acc.std(axis=0, keepdims=True) + 1e-6
    return ((acc - m) / sd).astype(np.float32)


def _coherence(feats, anchors, d0: int, d1: int, win: int,
               min_frac: float = 0.6):
    """Mean pairwise similarity between episodes over a sliding window.

        coherence = (||sum_j x_j||^2 - sum_j ||x_j||^2) / ((N-1) * sum_j ||x_j||^2)

    which is exactly the average normalised correlation over episode pairs.  It
    is ~1 where every episode carries the same audio and ~0 where they carry
    independent story content, and being a ratio it is immune to the loudness
    and spectral-scale differences that make a raw energy threshold fragile.

    Episodes whose audio runs out before the end of the window are tolerated as
    long as enough episodes still cover it, so the profile does not get blanked
    out by one short track.
    """
    D = d1 - d0
    B = feats[0].shape[1]
    S = np.zeros((D, B), dtype=np.float64)
    cnt = np.zeros(D, dtype=np.int32)
    entries: list[tuple[np.ndarray, int]] = []
    for F, o in zip(feats, anchors):
        s, e = o + d0, o + d1
        s2, e2 = max(s, 0), min(e, F.shape[0])
        if e2 <= s2:
            continue
        S[s2 - s: e2 - s] += F[s2:e2]
        cnt[s2 - s: e2 - s] += 1
        entries.append((frame_energy_cum(F), s))
    n_ep = len(entries)
    m = D - win + 1
    if n_ep < 2 or m <= 0:
        return np.zeros(max(0, m), dtype=np.float32), n_ep

    sq = (S * S).sum(axis=1)
    csq = np.concatenate(([0.0], np.cumsum(sq)))
    s2w = csq[win:] - csq[:-win]

    # Per episode, the window [i, i+win) of the delta grid maps to feature
    # indices [s+i, s+i+win).  Only windows fully inside the track count, so the
    # numerator (which sums every episode) and the denominator see the same set.
    q = np.zeros(m, dtype=np.float64)
    cover = np.zeros(m, dtype=np.int32)
    for cum, s in entries:
        lo = s + np.arange(m)
        hi = lo + win
        ok = (lo >= 0) & (hi <= cum.size - 1)
        cover += ok
        lo_c = np.clip(lo, 0, cum.size - 1)
        hi_c = np.clip(hi, 0, cum.size - 1)
        q += np.where(ok, cum[hi_c] - cum[lo_c], 0.0)

    full = cover == n_ep
    den = (n_ep - 1) * q
    coh = np.where(full & (den > 1e-9), (s2w - q) / np.maximum(den, 1e-9), 0.0)
    return np.clip(coh, -1.0, 1.0).astype(np.float32), n_ep


def _smooth(x: np.ndarray, k: int) -> np.ndarray:
    """Moving average, used to damp the noise of a per-frame statistic."""
    if k <= 1 or x.size < k:
        return x
    ker = np.ones(k, dtype=np.float64) / k
    pad = k // 2
    xp = np.pad(x.astype(np.float64), (pad, pad), mode="edge")
    return np.convolve(xp, ker, mode="valid").astype(np.float32)


def _walk_boundaries(energy: np.ndarray, d0: int, core: tuple[int, int],
                     thr: float, fps: float, hyst_s: float) -> tuple[int, int]:
    hyst = max(1, int(hyst_s * fps))
    lo, hi = core
    i = lo
    while i > 0:
        if energy[i - 1] >= thr:
            i -= 1
            continue
        back = max(0, i - hyst)
        if (energy[back:i - 1] >= thr).any():
            i = back
            continue
        break
    j = hi
    n = energy.size
    while j < n - 1:
        if energy[j + 1] >= thr:
            j += 1
            continue
        fwd = min(n, j + hyst)
        if (energy[j + 1:fwd] >= thr).any():
            j = fwd - 1
            continue
        break
    return i, j


def refine_variant(v: Variant, feats, keys, r, durations, p: Params) -> Variant:
    """Compute per-episode anchors and exact boundaries for one variant."""
    fps = FINE_FPS
    n = len(feats)
    idx_of = {k: i for i, k in enumerate(keys)}
    R = feats[r]
    t0 = max(0, int((v.ref_start + p.trim_s) * fps))
    t1 = min(R.shape[0], int((v.ref_end - p.trim_s) * fps))
    if t1 - t0 < int(p.min_template_s * fps):
        return v
    tmpl = R[t0:t1].astype(np.float32).copy()
    tmpl -= tmpl.mean(axis=0, keepdims=True)
    tmpl /= (tmpl.std(axis=0, keepdims=True) + 1e-6)
    Lt = tmpl.shape[0]

    # stage A: rough anchor per episode by full-timeline template match
    anchors, scores = {}, {}
    for k in keys:
        F = feats[idx_of[k]]
        idx, sc = match_range(tmpl, F, frame_energy_cum(F), 0, F.shape[0] - Lt)
        if idx.size == 0:
            continue
        b = int(np.argmax(sc))
        anchors[k] = int(idx[b])
        scores[k] = float(sc[b])

    # stage B: consensus template from the inliers -> re-anchor locally
    inliers = [k for k in keys if scores.get(k, -1) >= p.match_thr]
    if len(inliers) >= 2:
        cons = _consensus_template([feats[idx_of[k]] for k in inliers],
                                  [anchors[k] for k in inliers], Lt)
        if cons is not None:
            search = int(p.refine_search_s * fps)
            for k in keys:
                F = feats[idx_of[k]]
                c = anchors.get(k, 0)
                idx, sc = match_range(cons, F, frame_energy_cum(F),
                                      c - search, c + search)
                if idx.size == 0:
                    continue
                b = int(np.argmax(sc))
                anchors[k] = int(idx[b])
                scores[k] = float(sc[b])
            tmpl = cons

    v.anchors, v.scores = anchors, scores

    # stage C: cross-episode coherence -> exact boundaries relative to the anchor.
    # An episode qualifies if its audio covers the segment itself plus a few
    # seconds past it; releases that cut the audio before the video ends still
    # contribute, and _coherence tolerates the resulting partial coverage.
    d0, d1 = -int(p.pre_margin_s * fps), Lt + int(p.post_margin_s * fps)
    min_cover = Lt + int(5 * fps)
    good = [k for k in keys if scores.get(k, -1) >= p.match_thr
            and feats[idx_of[k]].shape[0] >= anchors[k] + min_cover]
    if len(good) < 2:
        good = sorted((k for k in keys if scores.get(k, -1) >= p.match_thr),
                      key=lambda k: -feats[idx_of[k]].shape[0])[:2]
    if len(good) < 2:
        return v
    win = max(4, int(p.coh_win_s * fps))
    coh, n_ep = _coherence([feats[idx_of[k]] for k in good],
                           [anchors[k] for k in good], d0, d1, win)
    core_lo = max(0, -d0)
    core_hi = min(coh.size, Lt - d0)
    if core_hi - core_lo < int(5 * fps) or n_ep < 2:
        return v
    peak = float(np.median(coh[core_lo:core_hi]))
    thr = max(p.agree_floor, p.agree_peak_frac * peak)
    sm = _smooth(coh, max(1, int(0.5 * fps)))
    i, j = _walk_boundaries(sm, d0, (core_lo, core_hi - 1), thr, fps, p.hyst_s)
    # a windowed statistic crosses 0.5 half a window early on both sides
    shift = p.coh_win_s / 2.0 * p.dedilate
    v.boundaries = ((i + d0) / fps + shift, (j + 1 + d0) / fps + shift)
    v.member_keys = good
    v.agree_peak = peak
    v.agree_thr = thr
    v.energy = coh
    v.energy_d0 = d0
    v.energy_fps = fps
    v.coh_win_s = win / fps
    return v


def episode_span(v: Variant, key: str, duration: float) -> dict | None:
    """Final (start, end) for one episode, or None if it lacks the segment."""
    if v.boundaries is None:
        return None
    if v.scores.get(key, -1) < 0:
        return None
    if key not in v.anchors:
        return None
    fps = FINE_FPS
    d_start, d_end = v.boundaries
    start = v.anchors[key] / fps + d_start
    end = v.anchors[key] / fps + d_end
    start = max(0.0, start)
    end = min(duration, end)
    if end - start < 5.0:
        return None
    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "duration": round(end - start, 3),
        "match_score": round(v.scores.get(key, 0.0), 4),
    }


# ----------------------------------------------------------------------------
# per-group driver
# ----------------------------------------------------------------------------

def _pick_reference(keys: list[str], durations: dict, best: dict,
                    used: set[str]) -> str | None:
    """Next reference: an unused episode still missing a role, best first.

    Iterating the reference is what discovers OP/ED *variants*: a season can use
    two different ED songs, and a template built from one of them simply will
    not match the episodes carrying the other.  Re-running the whole coarse +
    refine pass from an uncovered episode finds the next version.
    """
    durs = np.array([durations[k] for k in keys])
    med = np.median(durs)
    cands = []
    for i, k in enumerate(keys):
        if k in used:
            continue
        missing = (best[k]["op"] is None) + (best[k]["ed"] is None)
        if missing == 0:
            continue
        cands.append((-missing, abs(durations[k] - med), i, k))
    if not cands:
        return None
    cands.sort()
    return cands[0][3]


def detect_group(keys: list[str], durations: dict, feats: list[np.ndarray],
                 p: Params | None = None, log=None) -> dict:
    """Run detection for one series group; returns its report section.

    The group-level sweep is followed by an adaptive pass that re-runs the same
    detection on whatever it could not cover, so a season that changes OP/ED
    more than once is still covered in a single run (see `_adapt_fill`).
    """
    p = p or Params()
    out = _detect_unit(keys, durations, feats, p, log, depth=0)
    if p.adapt:
        _adapt_fill(out, keys, durations, feats, p, log, depth=0)
    return out


def _detect_unit(keys: list[str], durations: dict, feats: list[np.ndarray],
                 p: Params, log=None, *, depth: int = 0, unit: str = "root") -> dict:
    """One coarse + cluster + refine sweep over exactly these episodes.

    `feats` must line up with `keys` index by index.  `unit` tags the sweep so
    that variants found in different sweeps never share an identity.
    """
    n = len(feats)
    out = {"episodes": {}, "variants": [], "notes": [],
           "params": {k: getattr(p, k) for k in vars(p)}}
    best: dict = {k: {"op": None, "ed": None} for k in keys}
    for k in keys:
        out["episodes"][k] = {"duration": durations[k], "op": None, "ed": None}
    if n < 3:
        _note(out, f"only {n} episode(s): cross-episode detection needs >= 3")
        return out

    tag = f" depth={depth} n={n}" if depth else ""
    used: set[str] = set()
    # Candidate diagnostics are collected, not emitted: a cluster dropped for
    # being too short or too thinly shared only matters if the role ends up
    # incomplete, and a healthy group should stay quiet.
    diag: list[tuple[str | None, str]] = []
    found: list[tuple[str, str, set[str]]] = []
    references = []
    assignments: dict[int, list[str]] = {}
    for it in range(p.max_iterations):
        r = _pick_reference(keys, durations, best, used)
        if r is None:
            break
        used.add(r)
        ref_dur = durations[r]
        ri = keys.index(r)
        clusters = _cluster(_coarse_detections(feats, keys, ri, p), p)
        raw: list[Variant] = []
        need = max(p.member_min, int(p.member_frac * (n - 1)))
        for cl in clusters:
            a, b = _cluster_extent(cl)
            label = "op" if ((a + b) / 2) / ref_dur < 0.5 else "ed"
            if not (p.seg_min_dur <= b - a <= p.seg_max_dur):
                diag.append((label, f"{label} cluster @{a:.1f}s (ref {Path(r).name}): "
                                    f"span {b - a:.1f}s outside {p.seg_min_dur:.0f}-"
                                    f"{p.seg_max_dur:.0f}s"))
                continue
            members = sorted({m["key"] for m in cl})
            if len(members) < need:
                diag.append((label, f"{label} cluster @{a:.1f}s (ref {Path(r).name}): "
                                    f"{len(members)} member(s) < {need} needed "
                                    f"({p.member_frac:.0%} of {n - 1}, floor "
                                    f"{p.member_min})"))
                continue
            raw.append(Variant(label=label, ref_start=a, ref_end=b,
                               member_keys=members, corrs=[m["corr"] for m in cl]))
        gained = 0
        variants = []
        for v in raw:
            v = refine_variant(v, feats, keys, ri, durations, p)
            if v.boundaries is None:
                diag.append((v.label, f"{v.label} candidate @{v.ref_start:.1f}s "
                                      f"(ref {Path(r).name}): refinement found no "
                                      f"reliable boundaries"))
                continue
            span0 = episode_span(v, keys[keys.index(r)], ref_dur)
            if span0 is None or not (p.seg_min_dur <= span0["duration"] <= p.seg_max_dur):
                diag.append((v.label,
                             f"{v.label} candidate @{v.ref_start:.1f}s "
                             f"(ref {Path(r).name}): refined duration "
                             f"{span0['duration'] if span0 else float('nan'):.1f}s "
                             f"out of range"))
                continue
            variants.append(v)
            vid = f"{unit}:{v.label}#{len(found)}"
            found.append((vid, v.label, set(v.member_keys)))
            assigned_here: list[str] = []
            for k in keys:
                rec = episode_span(v, k, durations[k])
                if rec is None or rec["match_score"] < p.match_thr:
                    continue
                cur = best[k][v.label]
                if cur is None or rec["match_score"] > cur["match_score"]:
                    rec["variant"] = vid
                    best[k][v.label] = rec
                    gained += 1
                    assigned_here.append(k)
            assignments[id(v)] = assigned_here
        if log:
            log(f"    iter{it}{tag}: ref={Path(r).name[-34:]} candidates={len(raw)} "
                f"accepted={len(variants)} assignments+={gained}")
        for v in variants:
            s = v.summary()
            s["reference"] = r
            s["iteration"] = it
            s["depth"] = depth
            s["unit_episodes"] = n
            s["agree_peak"] = round(getattr(v, "agree_peak", 0.0), 4)
            s["agree_thr"] = round(getattr(v, "agree_thr", 0.0), 4)
            s["assigned"] = assignments.get(id(v), [])
            out.setdefault("variants", []).append(s)
        references.append(r)
        if not variants:
            diag.append((None, f"iteration {it}: no candidate from ref "
                               f"{Path(r).name} survived the filters; stopping "
                               f"this sweep"))
            break
    out["reference"] = references[0] if references else None
    out["references"] = references
    if found:
        id_of = _version_groups(found)
        for k in keys:
            for role in ("op", "ed"):
                rec = best[k][role]
                if rec and rec.get("variant") in id_of:
                    rec["variant"] = f"{unit}:{id_of[rec['variant']]}"
    _harmonise_durations(best, durations, keys, p, out)
    # diagnostics are emitted last so the version notes above survive the cap
    unfilled = {r for r in ("op", "ed") if any(best[k][r] is None for k in keys)}
    for role, msg in diag:
        if unfilled and (role is None or role in unfilled):
            _note(out, msg)
    for k in keys:
        out["episodes"][k]["op"] = best[k]["op"]
        out["episodes"][k]["ed"] = best[k]["ed"]
    return out


# ----------------------------------------------------------------------------
# adaptive re-split: cover what one sweep cannot
# ----------------------------------------------------------------------------


def _version_groups(found: list[tuple[str, str, set[str]]]) -> dict[str, str]:
    """Map variant id -> version id, by overlap of their episode sets.

    This is the mid-season-change signature the sweep above relies on: two
    clusters of the same role with *disjoint* episode sets are two versions of
    the OP/ED (a different song, or a different edit of the same one), while
    overlapping sets are the same sequence rediscovered from another reference
    episode.  Only the first case may split the length harmonisation -- merging
    the second is what keeps one season at one length.
    """
    parent = {vid: vid for vid, _, _ in found}

    def find(a: str) -> str:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, (vid_a, label_a, set_a) in enumerate(found):
        for vid_b, label_b, set_b in found[i + 1:]:
            if label_a != label_b or not set_a or not set_b:
                continue
            inter = len(set_a & set_b)
            if inter and inter / min(len(set_a), len(set_b)) >= 0.4:
                parent[find(vid_b)] = find(vid_a)

    ids: dict[str, str] = {}
    seq: dict[str, int] = {}
    for vid, label, _ in found:
        root = find(vid)
        if root not in ids:
            ids[root] = f"{label}-v{seq.get(label, 0)}"
            seq[label] = seq.get(label, 0) + 1
    return {vid: ids[find(vid)] for vid, _, _ in found}


NOTE_CAP = 30


def _note(out: dict, msg: str, cap: int = NOTE_CAP) -> None:
    """Append a diagnostic note, keeping the list bounded.

    Candidates get dropped in several places (span outside 15-240s, too few
    members, refinement without boundaries), and those drops used to be silent,
    which left a partly covered group with no explanation at all.
    """
    notes = out.setdefault("notes", [])
    if len(notes) < cap:
        notes.append(msg)
    elif len(notes) == cap:
        notes.append(f"... (further notes suppressed at {cap})")


def _names(keys: list[str], limit: int = 3) -> str:
    shown = ", ".join(Path(k).name[:24] for k in keys[:limit])
    extra = len(keys) - limit
    return f"{shown}, +{extra} more" if extra > 0 else shown


def _adapt_fill(out: dict, keys: list[str], durations: dict,
                feats: list[np.ndarray], p: Params, log, *, depth: int) -> None:
    """Cover what a sweep missed by re-running detection on sub-units.

    The acceptance bar is `max(member_min, member_frac * (n - 1))`: 30% of the
    unit with a floor of three episodes.  A version carried by a minority of a
    long season never reaches that bar, so its episodes come back uncovered.
    Re-running the *same* detection on just those episodes lowers the bar back to
    the three-episode floor without touching a single threshold; when a role was
    not found at all, halving the unit does the same (a 25% share becomes 50%).

    Whatever stays uncovered is reported and never guessed -- `apply` skips
    episodes without OP/ED, so nothing gets written for them.
    """
    pos = {k: i for i, k in enumerate(keys)}
    for role in ("op", "ed"):
        covered = [k for k in keys if out["episodes"][k].get(role) is not None]
        gap = [k for k in keys if out["episodes"][k].get(role) is None]
        if not gap:
            continue
        if depth >= p.adapt_max_depth:
            _note(out, f"{role}: {len(gap)} episode(s) still uncovered at depth "
                       f"{depth}; re-split limit reached -> reported, nothing written")
            continue
        if not covered:
            if len(keys) < 2 * p.adapt_min_episodes:
                _note(out, f"{role}: not found in any of {len(keys)} episode(s); below "
                           f"the {2 * p.adapt_min_episodes}-episode floor for a re-split "
                           f"-> reported, nothing written")
                continue
            half = len(keys) // 2
            units = [keys[:half], keys[half:]]
            _note(out, f"{role}: not found in the {len(keys)}-episode unit; re-running "
                       f"detection in 2 sub-units of {len(units[0])}/{len(units[1])}")
        else:
            if len(gap) < p.adapt_min_episodes:
                _note(out, f"{role}: {len(gap)} episode(s) left [{_names(gap)}]; below "
                           f"the {p.adapt_min_episodes}-episode floor for a re-split "
                           f"-> reported, nothing written")
                continue
            units = [gap]
            _note(out, f"{role}: {len(gap)} episode(s) uncovered [{_names(gap)}]; "
                       f"re-running detection on those alone")
        gained = 0
        for i, part in enumerate(units):
            sub = _detect_unit(part, durations, [feats[pos[k]] for k in part], p,
                               log, depth=depth + 1, unit=f"d{depth + 1}.{i}")
            for k in part:
                if out["episodes"][k].get(role) is not None:
                    continue
                rec = sub["episodes"][k].get(role)
                if rec is None:
                    continue
                rec = dict(rec)
                rec["via"] = f"subset depth={depth + 1} n={len(part)}"
                out["episodes"][k][role] = rec
                gained += 1
            out["variants"].extend(sub.get("variants", []))
            for nt in sub.get("notes", []):
                _note(out, f"[depth {depth + 1}] {nt}")
        if gained:
            _note(out, f"{role}: +{gained} episode(s) from the sub-unit pass (those "
                       f"carry the sub-unit's own duration harmonisation)")
            _adapt_fill(out, keys, durations, feats, p, log, depth=depth + 1)
        else:
            _note(out, f"{role}: the sub-unit pass found nothing new; {len(gap)} "
                       f"episode(s) stay uncovered -> reported, nothing written")


def _harmonise_durations(best: dict, durations: dict, keys: list[str],
                         p: Params, out: dict) -> None:
    """Force one sequence length per version, anchored on the detected start.

    The OP/ED animation is identical in every episode of a version, so its
    length is a constant; measuring it per episode only adds noise.  The start is
    sharp, so it is kept and the duration is replaced by a robust low percentile
    over the episodes carrying that sequence: the boundary walk only ever stops
    late (shared material after the sequence delays it), so the error is
    one-sided.

    Episodes are grouped by the *variant* that matched them, never by raw
    duration: a season that changes OP/ED mid-way has versions of different
    lengths, and per-episode durations overlap by several seconds thanks to the
    overshoot, so a spread test merges them and shortens the longer version by
    that overlap.  Per-variant estimates are themselves merged when they agree to
    within `harmonise_merge_s`, which is how the same song found from two
    different reference episodes collapses back into one version.
    """
    for role in ("op", "ed"):
        by_variant: dict[str, list[str]] = {}
        for k in keys:
            rec = best[k][role]
            if rec:
                by_variant.setdefault(rec.get("variant") or "-", []).append(k)
        if not by_variant:
            continue

        def estimate(members: list[str]) -> float:
            return float(np.percentile([best[k][role]["duration"] for k in members],
                                       p.harmonise_pct))

        # single-linkage over per-variant length estimates, sorted so that only
        # material differences start a new version
        versions: list[tuple[list[str], float]] = []
        for vid, members in sorted(by_variant.items(), key=lambda kv: estimate(kv[1])):
            val = estimate(members)
            if versions and val - versions[-1][1] <= p.harmonise_merge_s:
                vids = versions[-1][0] + [vid]
                versions[-1] = (vids, estimate([k for v in vids for k in by_variant[v]]))
            else:
                versions.append(([vid], val))
        multi = len(versions) > 1
        detail = []
        for vids, _ in versions:
            members = [k for v in vids for k in by_variant[v]]
            chosen = estimate(members)
            for k in members:
                rec = best[k][role]
                rec["end"] = round(min(durations[k], rec["start"] + chosen), 3)
                rec["duration"] = round(rec["end"] - rec["start"], 3)
                rec["duration_source"] = "variant_p25" if multi else "season_p25"
            detail.append(f"{len(members)} ep @ {chosen:.2f}s")
        out.setdefault("harmonised", {})[role] = [round(v, 3) for _, v in versions]
        if multi:
            _note(out, f"{role}: {len(versions)} versions kept separate "
                       f"({'; '.join(detail)})")
