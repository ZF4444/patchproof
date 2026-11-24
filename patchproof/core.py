"""Core detection + verification for patchproof.

Pipeline per panel:
  1. Find the content bounding box (non-transparent pixels) -> content padding.
  2. Guess initial 9-slice borders from "stretchability" profiles: a column is
     stretchable when the image is locally linear along x there (flat fills and
     smooth gradients stretch losslessly; ornaments and photos do not).
  3. Settle the borders greedily against a distortion score: shrink while the
     score stays under the threshold (minimal borders), expand while it is over.
  4. Cross-check with a second, independent estimator (mean-colour profile
     step detection). A borderline classic pass the second estimator disputes
     is not trusted. The second estimator's borders can still be PROVEN via a
     texture path: the centre must be stationary in mean colour and free of
     strong periodicity (see the texture section below).
  5. Verify by resynthesis: re-render the panel 9-sliced at several sizes and
     require the corners to be pixel-identical to the original corners.
  6. If no borders get a score under the threshold, the panel is
     UNCERTAIN -- patchproof abstains instead of inventing numbers.

The distortion score is the worst per-row / per-column linear-fit residual
(RMS, 0-255 scale) over the centre and the four edge strips. A region that is
flat or a smooth gradient along its stretch direction has residual ~0, so
stretching it is near-lossless; ornament or photo content trapped in a stretch
region has a large residual.
"""

from dataclasses import dataclass, field

import numpy as np
from PIL import Image

# A column/row counts as "stretchable" for the initial guess when the 90th
# percentile of its second-difference magnitude is below this (0-255 scale).
COST_THRESHOLD = 4.0
COST_PERCENTILE = 90
# A detection is PROVEN when the distortion score is at or below this.
DEFAULT_THRESHOLD = 6.0

ALPHA_OPAQUE = 8  # alpha above this counts as content


@dataclass
class Detection:
    width: int
    height: int
    borders: dict  # {"left","right","top","bottom"} relative to the full image
    padding: dict  # {"left","top","right","bottom"} transparent margins
    verdict: str  # "PROVEN" or "UNCERTAIN"
    score: float  # distortion score (lower is better); -1 when not applicable
    reason: str = ""
    sizes_checked: list = field(default_factory=list)
    corners_identical: bool = False


# --------------------------------------------------------------------------
# basic helpers
# --------------------------------------------------------------------------

def content_bbox(alpha: np.ndarray):
    """Return (x0, y0, x1, y1) inclusive bbox of alpha > ALPHA_OPAQUE, or None."""
    ys, xs = np.where(alpha > ALPHA_OPAQUE)
    if xs.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _fit_rms(region: np.ndarray, axis: int) -> float:
    """Worst linear-fit residual RMS over all series in `region` (HxWx4).

    axis=1: fit each row along x. axis=0: fit each column along y.
    Series shorter than 3 points fit a line exactly, so they contribute 0.
    """
    if region.size == 0:
        return 0.0
    if axis == 1:
        n = region.shape[1]
        if n < 3:
            return 0.0
        series = region.transpose(2, 0, 1).reshape(-1, n)
    else:
        n = region.shape[0]
        if n < 3:
            return 0.0
        series = region.transpose(2, 1, 0).reshape(-1, n)
    x = np.arange(n, dtype=np.float64)
    xc = x - x.mean()
    denom = float((xc ** 2).sum())
    ym = series.mean(axis=1, keepdims=True)
    slope = ((series - ym) * xc).sum(axis=1) / denom
    pred = ym + slope[:, None] * xc[None, :]
    resid = series - pred
    return float(np.sqrt((resid ** 2).mean()))


def distortion_score(arr: np.ndarray, left: int, right: int, top: int, bottom: int) -> float:
    """Distortion score of a candidate slicing of float RGBA array `arr`."""
    h, w = arr.shape[:2]
    cx0, cx1 = left, w - right
    cy0, cy1 = top, h - bottom
    if cx1 <= cx0 or cy1 <= cy0:
        return float("inf")
    centre = arr[cy0:cy1, cx0:cx1]
    score = max(_fit_rms(centre, axis=1), _fit_rms(centre, axis=0))
    if top > 0:
        score = max(score, _fit_rms(arr[0:top, cx0:cx1], axis=1))
    if bottom > 0:
        score = max(score, _fit_rms(arr[h - bottom:h, cx0:cx1], axis=1))
    if left > 0:
        score = max(score, _fit_rms(arr[cy0:cy1, 0:left], axis=0))
    if right > 0:
        score = max(score, _fit_rms(arr[cy0:cy1, w - right:w], axis=0))
    return score


# --------------------------------------------------------------------------
# initial guess from stretchability profiles
# --------------------------------------------------------------------------

def _stretch_costs(arr: np.ndarray, axis: int) -> np.ndarray:
    """Per-column (axis=1) or per-row (axis=0) stretchability cost."""
    if axis == 1:
        sd = np.abs(arr[:, :-2] - 2.0 * arr[:, 1:-1] + arr[:, 2:]).max(axis=2)
        per = np.percentile(sd, COST_PERCENTILE, axis=0)
        n = arr.shape[1]
    else:
        sd = np.abs(arr[:-2, :] - 2.0 * arr[1:-1, :] + arr[2:, :]).max(axis=2)
        per = np.percentile(sd, COST_PERCENTILE, axis=1)
        n = arr.shape[0]
    full = np.empty(n, dtype=np.float64)
    full[1:-1] = per
    full[0] = per[0]
    full[-1] = per[-1]
    return full


def _longest_run(mask: np.ndarray):
    """(start, end_inclusive) of the longest True run, or None."""
    best = None
    start = None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        if (not v or i == len(mask) - 1) and start is not None:
            end = i if v and i == len(mask) - 1 else i - 1
            if best is None or (end - start) > (best[1] - best[0]):
                best = (start, end)
            start = None
    return best


def _min_centre(size: int) -> int:
    return max(4, size // 6)


def _initial_borders(arr: np.ndarray):
    h, w = arr.shape[:2]
    col_cost = _stretch_costs(arr, axis=1)
    row_cost = _stretch_costs(arr, axis=0)
    run_x = _longest_run(col_cost <= COST_THRESHOLD)
    if run_x is None or (run_x[1] - run_x[0] + 1) < _min_centre(w):
        left, right = w // 4, w // 4
    else:
        left, right = run_x[0], w - 1 - run_x[1]
    run_y = _longest_run(row_cost <= COST_THRESHOLD)
    if run_y is None or (run_y[1] - run_y[0] + 1) < _min_centre(h):
        top, bottom = h // 4, h // 4
    else:
        top, bottom = run_y[0], h - 1 - run_y[1]
    return left, right, top, bottom


# --------------------------------------------------------------------------
# greedy settle against the distortion score
# --------------------------------------------------------------------------

def _settle(arr: np.ndarray, borders, threshold: float):
    """Adjust borders until the score is under `threshold` and minimal.

    Returns (borders, score, proven).
    """
    h, w = arr.shape[:2]
    min_w, min_h = _min_centre(w), _min_centre(h)

    def ok(b):
        l, r, t, bo = b
        return (w - l - r) >= min_w and (h - t - bo) >= min_h and min(b) >= 0

    def score(b):
        return distortion_score(arr, *b)

    borders = tuple(borders)
    if not ok(borders):
        borders = (min(borders[0], w - min_w), min(borders[1], w - min_w),
                   min(borders[2], h - min_h), min(borders[3], h - min_h))
        # clamp so the centre constraint holds
        l, r, t, bo = borders
        if w - l - r < min_w:
            r = max(0, w - min_w - l)
        if h - t - bo < min_h:
            bo = max(0, h - min_h - t)
        borders = (l, r, t, bo)
    cur = score(borders)

    # Phase 1: expand while over threshold (each step picks the side whose
    # expansion lowers the score most). Bail out if it stops helping.
    steps, stall = 0, 0
    while cur > threshold and steps < 200:
        cands = []
        for side in range(4):
            cand = list(borders)
            cand[side] += 1
            cand = tuple(cand)
            if ok(cand):
                cands.append((score(cand), cand))
        if not cands:
            break
        cands.sort(key=lambda c: c[0])
        best_score, best = cands[0]
        if best_score >= cur - 1e-9:
            stall += 1
        else:
            stall = 0
        borders, cur = best, best_score
        steps += 1
        if stall >= 6:
            break

    if cur > threshold:
        return borders, cur, False

    # Phase 2: shrink while the score stays under threshold (minimal borders).
    changed = True
    while changed:
        changed = False
        for side in range(4):
            if borders[side] == 0:
                continue
            cand = list(borders)
            cand[side] -= 1
            cand = tuple(cand)
            if ok(cand):
                s = score(cand)
                if s <= threshold:
                    borders, cur = cand, s
                    changed = True
                    break
    return borders, cur, True


# --------------------------------------------------------------------------
# second estimator: mean-profile step detection + texture verification
#
# Benchmark round (see BENCHMARK_INTERNAL.md): the classic path has two
# failure modes on textured centres. (1) On stationary textures (parchment-
# style grain) the linear-fit score hovers near the threshold and the
# settle can drift to borders that pass by luck -- a silent wrong answer.
# (2) The same textures are genuinely stretchable in the statistical
# sense (stretching preserves their mean/variance structure), so
# abstaining on all of them leaves real panels unserved.
#
# Fix, at technique level (no fixture tuning):
#   * An independent estimator locates borders from the step in the
#     per-column / per-row MEAN colour profile (frame bands differ in
#     mean colour from the centre; grain averages out). When the classic
#     path passes only borderline (score > 1.0) and the two estimators
#     disagree by > 2px on any side, the classic pass is not trusted.
#   * A texture verification path can then prove the mean-profile
#     borders: the centre (and the edge strips along their stretch
#     direction) must be stationary in mean (profile deviation under the
#     same threshold), and the centre must NOT have a strong periodicity
#     (autocorrelation veto) -- stretching a checker/diamond pattern
#     would rescale the pattern itself, so those still abstain.
# --------------------------------------------------------------------------

MEAN_STEP_TOLERANCE = 6.0   # mean-colour units, same scale as the threshold
PERIODICITY_VETO = 0.35     # max |autocorrelation| at lags >= 3 in a texture centre


def _axis_means(arr: np.ndarray, axis: int) -> np.ndarray:
    """Mean RGB per column (axis=1) or per row (axis=0), over opaque pixels."""
    alpha = arr[:, :, 3] > ALPHA_OPAQUE
    rgb = arr[:, :, :3]
    if axis == 1:
        mask = alpha.T                      # (W, H)
        vals = np.transpose(rgb, (1, 0, 2))  # (W, H, 3)
    else:
        mask = alpha
        vals = rgb
    counts = mask.sum(axis=1)
    sums = (vals * mask[:, :, None]).sum(axis=1)
    out = np.zeros((vals.shape[0], 3))
    nz = counts > 0
    out[nz] = sums[nz] / counts[nz, None]
    if not nz.all():  # fully transparent lines: fall back to plain mean
        out[~nz] = vals[~nz].mean(axis=1)
    return out


def _run_containing(mask: np.ndarray, idx: int):
    if not mask[idx]:
        return None
    start = idx
    while start > 0 and mask[start - 1]:
        start -= 1
    end = idx
    while end < len(mask) - 1 and mask[end + 1]:
        end += 1
    return start, end


def _mean_profile_borders(arr: np.ndarray):
    """Borders from the step in the mean-colour profile, or None.

    The centre reference is the mean colour of the middle 20% of the
    profile; a column/row counts as centre-like when its mean colour is
    within MEAN_STEP_TOLERANCE of that reference. The centre run must
    contain the image centre and must not touch either edge (a run that
    reaches an edge means no frame step was found on that axis).
    """
    h, w = arr.shape[:2]
    borders = []
    for axis, n in ((1, w), (0, h)):
        means = _axis_means(arr, axis)
        mid = means[int(n * 0.4):max(int(n * 0.6), int(n * 0.4) + 1)].mean(axis=0)
        dist = np.abs(means - mid).mean(axis=1)
        # 3-wide median: one noisy line must not break the centre run;
        # a real frame step is many lines wide and is not shifted by it.
        if n >= 3:
            padded = np.pad(dist, 1, mode="edge")
            dist = np.median(
                np.stack([padded[:-2], padded[1:-1], padded[2:]]), axis=0)
        run = _run_containing(dist <= MEAN_STEP_TOLERANCE, n // 2)
        if run is None or run[0] == 0 or run[1] == n - 1:
            return None
        if (run[1] - run[0] + 1) < _min_centre(n):
            return None
        borders.append((run[0], n - 1 - run[1]))
    (left, right), (top, bottom) = borders
    return left, right, top, bottom


def _profile_deviation(region: np.ndarray, axis: int) -> float:
    """RMS deviation of the per-line mean colour from its median."""
    if region.size == 0:
        return 0.0
    means = _axis_means(region, axis)
    med = np.median(means, axis=0)
    return float(np.sqrt(((means - med) ** 2).mean()))


def _texture_score(arr: np.ndarray, borders) -> float:
    """Stationarity score of a candidate slicing (mean-profile units).

    The centre must be stationary in mean along both axes; each edge
    strip must be stationary along its own stretch direction. Stretching
    a region whose mean profile is flat changes none of its coarse
    structure, whatever its fine grain does.
    """
    left, right, top, bottom = borders
    h, w = arr.shape[:2]
    cx0, cx1, cy0, cy1 = left, w - right, top, h - bottom
    if cx1 <= cx0 or cy1 <= cy0:
        return float("inf")
    centre = arr[cy0:cy1, cx0:cx1]
    score = max(_profile_deviation(centre, 1), _profile_deviation(centre, 0))
    if top > 0:
        score = max(score, _profile_deviation(arr[0:top, cx0:cx1], 1))
    if bottom > 0:
        score = max(score, _profile_deviation(arr[h - bottom:h, cx0:cx1], 1))
    if left > 0:
        score = max(score, _profile_deviation(arr[cy0:cy1, 0:left], 0))
    if right > 0:
        score = max(score, _profile_deviation(arr[cy0:cy1, w - right:w], 0))
    return score


def _axis_periodicity(grey: np.ndarray, axis: int) -> float:
    """Strongest |autocorrelation| at lags 3..24 of the detrended signal,
    averaged over lines (rows for axis=1, columns for axis=0)."""
    sig = grey if axis == 1 else grey.T
    n = sig.shape[1]
    max_lag = min(24, n // 2)
    if max_lag < 3 or sig.shape[0] == 0:
        return 0.0
    x = np.arange(n, dtype=np.float64)
    xc = x - x.mean()
    denom = float((xc ** 2).sum())
    slope = ((sig - sig.mean(axis=1, keepdims=True)) * xc).sum(axis=1) / denom
    trend = sig.mean(axis=1, keepdims=True) + slope[:, None] * xc[None, :]
    d = sig - trend
    energy = (d ** 2).sum(axis=1)
    total_e = float(energy.sum())
    if total_e <= 1e-9:
        return 0.0
    peak = 0.0
    for lag in range(3, max_lag + 1):
        ac = (d[:, :-lag] * d[:, lag:]).sum(axis=1)
        peak = max(peak, abs(float((ac / np.maximum(energy, 1e-9)).mean())))
    return peak


def _has_strong_periodicity(centre: np.ndarray) -> bool:
    grey = centre[:, :, :3].mean(axis=2)
    return max(_axis_periodicity(grey, 1), _axis_periodicity(grey, 0)) > PERIODICITY_VETO


# --------------------------------------------------------------------------
# resynthesis (9-slice render) + verification
# --------------------------------------------------------------------------

def render_9slice(img: Image.Image, borders, out_w: int, out_h: int) -> Image.Image:
    """Render `img` 9-sliced with borders (left, right, top, bottom)."""
    left, right, top, bottom = borders
    w, h = img.size
    cw = out_w - left - right
    ch = out_h - top - bottom
    if cw < 1 or ch < 1:
        raise ValueError("target size too small for these borders")
    src = img.convert("RGBA")
    out = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    bilin = Image.BILINEAR

    def paste(box, dest, size=None):
        x0, y0, x1, y1 = box
        if x1 <= x0 or y1 <= y0:
            return
        piece = src.crop(box)
        if size is not None and size != piece.size:
            if size[0] < 1 or size[1] < 1:
                return
            piece = piece.resize(size, bilin)
        out.paste(piece, dest)

    # corners (copied 1:1, never resampled)
    paste((0, 0, left, top), (0, 0))
    paste((w - right, 0, w, top), (out_w - right, 0))
    paste((0, h - bottom, left, h), (0, out_h - bottom))
    paste((w - right, h - bottom, w, h), (out_w - right, out_h - bottom))
    # edges (stretched along one axis)
    paste((left, 0, w - right, top), (left, 0), (cw, top))
    paste((left, h - bottom, w - right, h), (left, out_h - bottom), (cw, bottom))
    paste((0, top, left, h - bottom), (0, top), (left, ch))
    paste((w - right, top, w, h - bottom), (out_w - right, top), (right, ch))
    # centre (stretched along both axes)
    paste((left, top, w - right, h - bottom), (left, top), (cw, ch))
    return out


def verify_sizes(w: int, h: int, borders):
    left, right, top, bottom = borders
    cand = [
        (w, h),
        (w + 31, h + 17),
        (2 * w + 1, h + 23),
        (w + 17, 2 * h + 1),
        (left + right + 9, top + bottom + 7),
        (w + 101, h + 57),
        (w + 53, h + 41),
    ]
    sizes, seen = [], set()
    for s in cand:
        if s[0] > left + right and s[1] > top + bottom and s not in seen:
            sizes.append(s)
            seen.add(s)
    return sizes


def _corners_identical(src: Image.Image, rendered: Image.Image, borders) -> bool:
    left, right, top, bottom = borders
    w, h = src.size
    ow, oh = rendered.size
    a = np.asarray(src.convert("RGBA"))
    b = np.asarray(rendered.convert("RGBA"))
    blocks = [
        (a[0:top, 0:left], b[0:top, 0:left]),
        (a[0:top, w - right:w], b[0:top, ow - right:ow]),
        (a[h - bottom:h, 0:left], b[oh - bottom:oh, 0:left]),
        (a[h - bottom:h, w - right:w], b[oh - bottom:oh, ow - right:ow]),
    ]
    return all(np.array_equal(x, y) for x, y in blocks)


# --------------------------------------------------------------------------
# top-level detection
# --------------------------------------------------------------------------

def detect(img: Image.Image, threshold: float = DEFAULT_THRESHOLD) -> Detection:
    """Detect 9-slice borders + content padding of one panel image."""
    img = img.convert("RGBA")
    w, h = img.size
    arr = np.asarray(img).astype(np.float64)
    # Analyse visible colour: hidden RGB in transparent pixels must not
    # introduce artificial edges. Keep img unchanged for rendering/export.
    arr[:, :, :3] *= arr[:, :, 3:4] / 255.0
    empty_padding = {"left": 0, "top": 0, "right": 0, "bottom": 0}
    empty_borders = {"left": 0, "right": 0, "top": 0, "bottom": 0}

    bbox = content_bbox(arr[:, :, 3])
    if bbox is None:
        return Detection(w, h, empty_borders, empty_padding, "UNCERTAIN", -1.0,
                         reason="image is fully transparent (no content)")
    x0, y0, x1, y1 = bbox
    padding = {"left": x0, "top": y0, "right": w - 1 - x1, "bottom": h - 1 - y1}
    content = arr[y0:y1 + 1, x0:x1 + 1]
    ch, cw = content.shape[:2]
    if cw < 8 or ch < 8:
        return Detection(w, h, empty_borders, padding, "UNCERTAIN", -1.0,
                         reason=f"content is only {cw}x{ch}px -- too small to slice")

    init = _initial_borders(content)
    (l, r, t, b), score, proven = _settle(content, init, threshold)

    # Cross-check + texture path (see the section above _mean_profile_borders).
    mp = _mean_profile_borders(content)
    if proven and score > 1.0 and mp is not None:
        if max(abs(a - c) for a, c in zip((l, r, t, b), mp)) > 2:
            proven = False  # borderline pass the second estimator disputes
    if not proven and mp is not None:
        ml, mr, mt, mb = mp
        if (cw - ml - mr) >= _min_centre(cw) and (ch - mt - mb) >= _min_centre(ch):
            tscore = _texture_score(content, mp)
            centre = content[mt:ch - mb, ml:cw - mr]
            if tscore <= threshold and not _has_strong_periodicity(centre):
                l, r, t, b, score, proven = ml, mr, mt, mb, tscore, True

    borders = {"left": padding["left"] + l, "right": padding["right"] + r,
               "top": padding["top"] + t, "bottom": padding["bottom"] + b}

    if not proven:
        return Detection(
            w, h, borders, padding, "UNCERTAIN", float(score),
            reason=(f"no cleanly stretchable centre found: best distortion "
                    f"score {score:.1f} is above the threshold {threshold:.1f} "
                    f"(content is likely photo-like, patterned, or ornamented "
                    f"through its middle)"))

    btuple = (borders["left"], borders["right"], borders["top"], borders["bottom"])
    sizes = verify_sizes(w, h, btuple)
    corners_ok = True
    for (ow, oh) in sizes:
        rendered = render_9slice(img, btuple, ow, oh)
        if not _corners_identical(img, rendered, btuple):
            corners_ok = False
            break
    if not corners_ok:
        return Detection(w, h, borders, padding, "UNCERTAIN", float(score),
                         reason="resynthesis failed: corners changed when re-rendered")
    return Detection(w, h, borders, padding, "PROVEN", float(score),
                     sizes_checked=sizes, corners_identical=True)
