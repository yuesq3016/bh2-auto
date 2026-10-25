"""Recognition: template matching (NCC), colour-band counting, bar fill.

This is the piece that replaces OpenCV.  ``ncc`` below is a real
zero-mean normalised cross-correlation — the same thing as
``cv2.matchTemplate(..., TM_CCOEFF_NORMED)`` that MAA uses by default — but
computed with numpy integral images.  That keeps the whole project free of
native dependencies, and it is plenty fast for the few hundred ROIs a script
like this checks per minute.

Score convention: everything returns a score in roughly [-1, 1] where higher
is better, and a threshold of 0.8 means what it means in MAA.  Game UI is
pixel-exact, so 0.85-0.95 is the usual working range; drop it only when the
target animates or is partly transparent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np

from .imaging import ImageError, crop_roi, load_image, parse_scale_list, to_gray, resize_rgb

__all__ = [
    "MatchResult", "ncc", "ncc_masked", "match_template", "match_color", "match_bar",
    "DETECTORS", "run_detector", "suggest_threshold",
]


# Below this grey-level standard deviation a template (or a frame window) is
# treated as featureless.  Keeping this well above the float noise floor is what
# stops flat areas from scoring a spurious 1.0.
_MIN_TEMPLATE_STD = 4.0
_MIN_WINDOW_STD = 4.0


@dataclass
class MatchResult:
    """One recognition outcome.

    ``hit`` is the decision; ``score``/``loc``/``size`` exist so callers can
    log *why* something matched, and so ``--debug`` can draw a box.
    """

    hit: bool
    name: str = ""
    method: str = ""
    score: float = 0.0
    threshold: float = 0.0
    loc: tuple[int, int] | None = None      # centre, in frame coordinates
    box: tuple[int, int, int, int] | None = None  # (x, y, w, h)
    size: tuple[int, int] | None = None
    scale: float = 1.0
    roi: tuple[int, int, int, int] | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.hit

    def describe(self) -> str:
        where = f"@{self.loc}" if self.loc else ""
        extra = " ".join(f"{k}={v}" for k, v in self.metrics.items())
        return (
            f"{'HIT ' if self.hit else 'miss'} {self.name or self.method} "
            f"score={self.score:.4f} thr={self.threshold:.2f} {where} {extra}".rstrip()
        )


# --------------------------------------------------------------------------
# Normalised cross-correlation
# --------------------------------------------------------------------------

def _window_sums(integral: np.ndarray, th: int, tw: int) -> np.ndarray:
    """Sliding-window sums over a padded integral image.

    ``integral`` must be the cumulative sum of a (H+1, W+1)-padded array, so
    ``integral`` has shape (H+1, W+1) and the result is (H-th+1, W-tw+1).
    """
    return (
        integral[th:, tw:] - integral[:-th, tw:]
        - integral[th:, :-tw] + integral[:-th, :-tw]
    )


def _cumsum2d(arr: np.ndarray) -> np.ndarray:
    """Inclusive 2-D cumulative sum: ``out[i, j] == arr[:i+1, :j+1].sum()``."""
    out = np.zeros(arr.shape, dtype=np.float64)
    out[1:, 1:] = arr[1:, 1:].cumsum(axis=0).cumsum(axis=1)
    out[1:, 1:] += out[:-1, 1:] + out[1:, :-1] - out[:-1, :-1]
    out[0, :] = np.cumsum(arr[0, :])
    out[:, 0] = np.cumsum(arr[:, 0])
    return out


def _correlate_valid(img: np.ndarray, tpl: np.ndarray) -> np.ndarray:
    """Exact valid cross-correlation: ``out[t, s] = sum img[t+i, s+j] * tpl[i, j]``.

    Two implementations, same result:

    * small templates (the normal case for UI icons) use an explicit
      accumulation over template pixels, which is straightforward and exact;
    * larger templates use the convolution theorem, because the explicit loop
      costs O(template pixels x image pixels) and a 40x40 template over a full
      1080p frame takes seconds.

    Correlation is convolution with the mirrored kernel, so
    ``correlate_valid(img, tpl) == fftconvolve(img, tpl[::-1, ::-1])[th-1:h, tw-1:w]``
    — the valid region of the full convolution.  Both paths are checked against
    the brute-force definition in the test suite.
    """
    img = np.asarray(img, dtype=np.float64)
    tpl = np.asarray(tpl, dtype=np.float64)
    th, tw = tpl.shape
    h, w = img.shape
    out_h, out_w = h - th + 1, w - tw + 1
    if out_h <= 0 or out_w <= 0:
        raise ValueError(f"template {tw}x{th} larger than image {w}x{h}")

    if th * tw > _FFT_TEMPLATE_PIXELS:
        return _correlate_fft(img, tpl)

    out = np.zeros((out_h, out_w), dtype=np.float64)
    for i in range(th):
        for j in range(tw):
            value = float(tpl[i, j])
            if value == 0.0:
                continue
            # Because the output window is the *valid* region, the image
            # sub-array img[i:i+out_h, j:j+out_w] is exactly the set of values
            # that multiply tpl[i, j], with no bounds fiddling needed.
            out += img[i:i + out_h, j:j + out_w] * value
    return out


# Above this many template pixels the explicit accumulation loses to FFT.
# 16x16 is where the crossover sits on a full 1280x720 frame; ROI-limited
# searches stay fast either way.
_FFT_TEMPLATE_PIXELS = 256


def _correlate_fft(img: np.ndarray, tpl: np.ndarray) -> np.ndarray:
    """Valid cross-correlation via convolution with the mirrored template.

    scipy's ``fftconvolve`` is the fast path (C implementation, next_fast_len
    padding); the numpy FFT version is the fallback so the package still has no
    hard dependency on scipy.
    """
    th, tw = tpl.shape
    kernel = tpl[::-1, ::-1]
    try:
        from scipy.signal import fftconvolve  # type: ignore
        full = fftconvolve(img, kernel, mode="full")
    except ImportError:
        full = _fft_convolve_full(img, kernel)
    return np.ascontiguousarray(full[th - 1: img.shape[0], tw - 1: img.shape[1]])


def _fft_convolve_full(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Full 2-D linear convolution using numpy's FFT (no scipy needed)."""
    out_shape = (a.shape[0] + b.shape[0] - 1, a.shape[1] + b.shape[1] - 1)
    fa = np.fft.rfft2(a, out_shape)
    fb = np.fft.rfft2(b, out_shape)
    return np.fft.irfft2(fa * fb, out_shape)


def ncc(image: np.ndarray, template: np.ndarray) -> np.ndarray:
    """Zero-mean normalised cross-correlation map.

    ``image`` and ``template`` are 2-D float arrays (grey).  The result has
    shape (H-th+1, W-tw+1); entry [y, x] is the NCC of the template against
    the image window whose top-left corner is (x, y).
    """
    img = np.asarray(image, dtype=np.float64)
    tpl = np.asarray(template, dtype=np.float64)
    if img.ndim != 2 or tpl.ndim != 2:
        raise ValueError("ncc expects 2-D arrays")
    th, tw = tpl.shape
    h, w = img.shape
    if th > h or tw > w:
        raise ValueError(f"template {tw}x{th} larger than image {w}x{h}")
    if th < 2 or tw < 2:
        raise ValueError("template must be at least 2x2")

    tn = th * tw
    t_mean = tpl.mean()
    t_centered = tpl - t_mean
    t_norm = float(np.sqrt(np.sum(t_centered * t_centered)))
    # Reject low-contrast templates *relatively*.  A template whose standard
    # deviation is a couple of grey levels has essentially no signal, and
    # normalising by its tiny norm turns floating-point noise into a confident
    # 1.0 score at any flat patch of the frame — the worst possible failure
    # mode, because it looks like a perfect match.
    t_std = float(tpl.std())
    if t_norm <= 1e-9 or t_std < _MIN_TEMPLATE_STD:
        raise ValueError(
            f"template has no usable contrast (std {t_std:.2f} < {_MIN_TEMPLATE_STD}); "
            f"crop a region with visible detail, or use method=color/bar"
        )

    # Sliding window sums of I and I^2 come from integral images.  These are
    # window sums, not correlations, so the integral-image trick is valid.
    # float32 keeps the O(image) part cheap; the relative error is ~1e-7, far
    # below the thresholds this program compares against.
    img32 = img.astype(np.float32, copy=False)
    pad_i = np.zeros((h + 1, w + 1), dtype=np.float32)
    pad_i[1:, 1:] = np.cumsum(np.cumsum(img32, axis=0), axis=1)
    sum_i = _window_sums(pad_i, th, tw).astype(np.float64)

    pad_sq = np.zeros((h + 1, w + 1), dtype=np.float32)
    pad_sq[1:, 1:] = np.cumsum(np.cumsum(img32 * img32, axis=0), axis=1)
    sum_i2 = _window_sums(pad_sq, th, tw).astype(np.float64)

    # The I*T term is a *correlation*, so it needs real alignment; I and I^2
    # above are window sums and are fine as integral images.
    sum_it = _correlate_valid(img, tpl)

    mean_i = sum_i / tn
    # var_i is the *sum of squared deviations* of the window:
    # sum over the window of (I - mean)^2.  NCC's denominator is the product of
    # the two *standard deviations*, so this must be square-rooted before use
    # (dropping the sqrt scales every score down by sqrt(tn)).
    var_i = sum_i2 - sum_i * mean_i
    np.maximum(var_i, 0.0, out=var_i)
    # The low-contrast floor compares against squared units, hence the square.
    good = var_i > (tn * _MIN_WINDOW_STD * _MIN_WINDOW_STD)
    norm_i = np.sqrt(np.where(good, var_i, 0.0))

    numer = sum_it - mean_i * tpl.sum()
    out = np.zeros_like(numer)
    np.divide(numer, norm_i * t_norm, out=out, where=good)
    return np.clip(out, -1.0, 1.0)


def ncc_masked(image: np.ndarray, template: np.ndarray, mask: np.ndarray) -> tuple[float, tuple[int, int]]:
    """NCC restricted to the masked template pixels.

    Used when a template is cropped with transparency (a glow, a highlighted
    button) and only part of it should count.  This is a per-position
    operation, so it is only practical for small templates — which is exactly
    the case it exists for.
    """
    img = np.asarray(image, dtype=np.float64)
    tpl = np.asarray(template, dtype=np.float64)
    m = np.asarray(mask) > 0
    th, tw = tpl.shape
    h, w = img.shape
    if th > h or tw > w:
        raise ValueError("template larger than image")
    t_vals = tpl[m]
    t_c = t_vals - t_vals.mean()
    t_norm = float(np.sqrt(np.sum(t_c * t_c)))
    if t_norm <= 1e-9 or float(t_vals.std()) < _MIN_TEMPLATE_STD:
        raise ValueError(
            f"masked template has no usable contrast (std {float(t_vals.std()):.2f}); "
            f"widen the mask or use method=color"
        )

    # Fancy-index every window at once: (n_positions, n_masked_pixels).
    ys, xs = np.nonzero(m)
    from numpy.lib.stride_tricks import sliding_window_view

    windows = sliding_window_view(img, (th, tw))          # (H-th+1, W-tw+1, th, tw)
    flat = windows[:, :, ys, xs]                          # (nY, nX, nMask)
    flat = flat.reshape(-1, ys.size)
    mean = flat.mean(axis=1, keepdims=True)
    centred = flat - mean
    numer = centred @ t_c
    denom = np.sqrt(np.einsum("ij,ij->i", centred, centred)) * t_norm
    scores = np.zeros_like(numer)
    good = denom > 1e-9
    np.divide(numer, denom, out=scores, where=good)
    best = int(np.argmax(scores))
    n_x = windows.shape[1]
    return float(scores[best]), (best % n_x, best // n_x)


# --------------------------------------------------------------------------
# Template matching over ROI + scale
# --------------------------------------------------------------------------

def _load_template(template: Any) -> tuple[np.ndarray, np.ndarray | None]:
    """``template`` is a path or an already-loaded array (optionally + mask)."""
    if isinstance(template, tuple):
        rgb, mask = template
        return np.asarray(rgb), np.asarray(mask)
    if isinstance(template, np.ndarray):
        return template, None
    return load_image(str(template)), None


def match_template(
    frame: np.ndarray,
    template: Any,
    *,
    roi: tuple[int, int, int, int] | None = None,
    threshold: float = 0.85,
    scales: Iterable[float] = (1.0,),
    name: str = "",
    max_results: int = 0,
    index: int = 0,
    mask: np.ndarray | None = None,
) -> MatchResult:
    """Find ``template`` inside ``frame`` (optionally inside ``roi``).

    Multi-scale: each scale resizes the *template* (not the frame), which is
    cheap and keeps the returned coordinates in frame space.  The best scale
    wins, and its score is what ``threshold`` is compared against.

    ``index`` selects among equally-good matches: 0 (default) is the best one,
    1 is the best one whose box does not overlap it, and so on.  Screens with
    several identical controls (a list of finished expeditions, each with the
    same "claim reward" button) need this - a plain best-match search can only
    ever return the topmost one.
    """
    tpl_rgb, tpl_mask = _load_template(template)
    if mask is not None:
        tpl_mask = mask
    region = crop_roi(frame, roi)
    if region.size == 0:
        return MatchResult(False, name=name, method="template", threshold=threshold,
                           roi=roi, metrics={"error": "empty roi"})

    img_gray = to_gray(region)
    best = MatchResult(False, name=name, method="template", threshold=threshold, roi=roi)
    origin = (roi[0], roi[1]) if roi else (0, 0)
    errors = []
    peaks: list[tuple[float, int, int, int, int, float]] = []   # score,x,y,tw,th,scale

    for scale in scales:
        s = float(scale)
        tw = max(2, int(round(tpl_rgb.shape[1] * s)))
        th = max(2, int(round(tpl_rgb.shape[0] * s)))
        if th > region.shape[0] or tw > region.shape[1]:
            continue
        if s == 1.0:
            t_rgb, t_mask = tpl_rgb, tpl_mask
        else:
            t_rgb = resize_rgb(tpl_rgb, (tw, th))
            t_mask = resize_rgb(tpl_mask, (tw, th)) if tpl_mask is not None else None
        t_gray = to_gray(t_rgb)

        try:
            if t_mask is not None and (t_mask > 0).any() and not (t_mask > 0).all():
                score, (x, y) = ncc_masked(img_gray, t_gray, t_mask)
                scores = None
            else:
                scores = ncc(img_gray, t_gray)
                flat = int(np.argmax(scores))
                y, x = divmod(flat, scores.shape[1])
                score = float(scores[y, x])
        except (ValueError, ImageError) as exc:
            errors.append(f"scale {s}: {exc}")
            continue

        if scores is not None:
            # Collect the high peaks for this scale so `index` has something to
            # choose from.  Implemented as non-maximum suppression: take the
            # global max, blank its neighbourhood, repeat.  A fixed budget
            # (rather than "just enough for this index") keeps the reported
            # candidate count the same whatever index was asked for.
            work = scores.copy()
            budget = max(16, index + 1)
            for _ in range(budget):
                fy, fx = np.unravel_index(int(np.argmax(work)), work.shape)
                peak = float(work[fy, fx])
                if peak < threshold:
                    break
                peaks.append((peak, int(fx), int(fy), tw, th, s))
                y0 = max(0, fy - th // 2)
                y1 = min(work.shape[0], fy + th - th // 2)
                x0 = max(0, fx - tw // 2)
                x1 = min(work.shape[1], fx + tw - tw // 2)
                work[y0:y1, x0:x1] = -1.0
        else:
            peaks.append((score, x, y, tw, th, s))

        if best.box is None or score > best.score:
            best.score = score
            best.scale = s
            best.size = (tw, th)
            best.box = (origin[0] + x, origin[1] + y, tw, th)
            best.loc = (origin[0] + x + tw // 2, origin[1] + y + th // 2)

    if peaks:
        peaks.sort(key=lambda p: p[0], reverse=True)
        picked = peaks[min(index, len(peaks) - 1)]
        score, px, py, tw, th, s = picked
        # Overlapping boxes are usually the *same* control seen at a couple of
        # pixel offsets, so keep only physically distinct peaks when choosing by
        # index; otherwise index=1 would return a 1px-shifted copy of index=0.
        distinct: list[tuple[float, int, int, int, int, float]] = []
        for cand in peaks:
            cscore, cx, cy, ctw, cth, cs = cand
            if all(abs(cx - dx) > ctw * 0.5 or abs(cy - dy) > cth * 0.5
                   for _, dx, dy, dtw, dth, _ in distinct):
                distinct.append(cand)
        if index < len(distinct):
            picked = distinct[index]
        else:
            picked = distinct[-1] if distinct else picked
        score, px, py, tw, th, s = picked
        best.score = score
        best.scale = s
        best.size = (tw, th)
        best.box = (origin[0] + px, origin[1] + py, tw, th)
        best.loc = (origin[0] + px + tw // 2, origin[1] + py + th // 2)
        best.metrics["candidates"] = len(distinct)

    best.hit = best.score >= threshold
    if errors:
        best.metrics["errors"] = "; ".join(errors[:3])
    best.metrics["score"] = round(best.score, 4)
    return best


# --------------------------------------------------------------------------
# Colour-based recognition
# --------------------------------------------------------------------------

def _to_hsv(rgb: np.ndarray) -> np.ndarray:
    """Vectorised RGB->HSV, H in [0, 360), S and V in [0, 1]."""
    arr = np.asarray(rgb, dtype=np.float32) / 255.0
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    mx = arr.max(axis=-1)
    mn = arr.min(axis=-1)
    diff = mx - mn
    hsv = np.zeros(arr.shape, dtype=np.float32)
    hsv[..., 2] = mx
    with np.errstate(invalid="ignore", divide="ignore"):
        hsv[..., 1] = np.where(mx > 0, diff / np.maximum(mx, 1e-6), 0.0)
        rc = np.where(diff > 1e-6, (mx - r) / np.maximum(diff, 1e-6), 0.0)
        gc = np.where(diff > 1e-6, (mx - g) / np.maximum(diff, 1e-6), 0.0)
        bc = np.where(diff > 1e-6, (mx - b) / np.maximum(diff, 1e-6), 0.0)
        hue = np.where(r == mx, bc - gc, np.where(g == mx, 2.0 + rc - bc, 4.0 + gc - rc))
        hsv[..., 0] = (hue * 60.0) % 360.0
    return hsv


def _band_mask(region: np.ndarray, bands, space: str) -> np.ndarray:
    """Boolean mask of pixels inside any colour band.  Single source of truth
    for both :func:`match_color` and :func:`match_bar`.

    For ``space="hsv"`` a band whose low hue is greater than its high hue is
    treated as wrapping through 0/360, which is what a red HP bar needs
    (hue ~357).  A plain ``[0, 25]`` band does *not* include hue 357 — that
    surprise is worth the explicit wrap test rather than silently matching
    nothing.
    """
    if space == "hsv":
        data = _to_hsv(region) * np.array([1.0, 255.0, 255.0], dtype=np.float32)
    elif space == "rgb":
        data = np.asarray(region, dtype=np.float32)
    else:
        raise ValueError(f"unknown colour space {space!r}")

    mask = np.zeros(region.shape[:2], dtype=bool)
    for lo_raw, hi_raw in _normalise_bands(bands):
        lo = np.asarray(lo_raw, dtype=np.float32)
        hi = np.asarray(hi_raw, dtype=np.float32)
        if space == "hsv" and lo[0] > hi[0]:
            sel = (data[..., 0] >= lo[0]) | (data[..., 0] <= hi[0])
        else:
            sel = (data[..., 0] >= lo[0]) & (data[..., 0] <= hi[0])
        sel &= (data[..., 1] >= lo[1]) & (data[..., 1] <= hi[1])
        sel &= (data[..., 2] >= lo[2]) & (data[..., 2] <= hi[2])
        mask |= sel
    return mask


def match_color(
    frame: np.ndarray,
    bands,
    *,
    roi: tuple[int, int, int, int] | None = None,
    threshold: float = 0.5,
    name: str = "",
    space: str = "hsv",
    min_pixels: int = 1,
) -> MatchResult:
    """Fraction of ROI pixels inside any of the colour bands.

    ``bands`` is a list of ``[[lo, hi], ...]`` triples.  For ``space="hsv"``
    the triples are ``[h(0-360), s(0-255), v(0-255)]`` like MAA's
    ``colorScales``; for ``space="rgb"`` they are plain 0-255 triples.

    Use a wrapping band (``[[350, 90, 90], [25, 255, 255]]``) for red or the
    hue near 360 will not match.
    """
    region = crop_roi(frame, roi)
    if region.size == 0:
        return MatchResult(False, name=name, method="color", threshold=threshold, roi=roi)

    inside = _band_mask(region, bands, space)
    count = int(inside.sum())
    total = int(inside.size)
    ratio = count / total if total else 0.0
    origin = (roi[0], roi[1]) if roi else (0, 0)
    ys, xs = np.nonzero(inside)
    box = None
    loc = None
    if count:
        x0, x1 = int(xs.min()), int(xs.max())
        y0, y1 = int(ys.min()), int(ys.max())
        box = (origin[0] + x0, origin[1] + y0, x1 - x0 + 1, y1 - y0 + 1)
        loc = (origin[0] + (x0 + x1) // 2, origin[1] + (y0 + y1) // 2)
    hit = ratio >= threshold and count >= min_pixels
    return MatchResult(
        hit, name=name, method="color", score=ratio, threshold=threshold,
        loc=loc, box=box, roi=roi,
        metrics={"pixels": count, "ratio": round(ratio, 4)},
    )


def match_bar(
    frame: np.ndarray,
    bands,
    *,
    roi: tuple[int, int, int, int] | None = None,
    threshold: float = 0.5,
    name: str = "",
    space: str = "hsv",
    column: int | None = None,
) -> MatchResult:
    """Estimate a bar's fill fraction inside ``roi``.

    Scans a single row (``column``, if given it is a row index; default: the
    middle row of the ROI) and reports the *rightmost* matching pixel as a
    fraction of the ROI width.  This reads an HP or progress bar without OCR:
    the fill colour (usually a saturated red/green) tells you how much is left,
    and the returned ``score`` is that fraction so ``threshold`` reads as
    "alert when below X".
    """
    region = crop_roi(frame, roi)
    if region.ndim != 3 or region.shape[0] == 0 or region.shape[1] == 0:
        return MatchResult(False, name=name, method="bar", threshold=threshold, roi=roi)
    row = region.shape[0] // 2 if column is None else max(0, min(int(column), region.shape[0] - 1))
    line = region[row: row + 1, :, :]
    width = region.shape[1]
    mask = _band_mask(line, bands, space)[0]
    xs = np.nonzero(mask)[0]
    origin = (roi[0], roi[1]) if roi else (0, 0)
    y_abs = origin[1] + row
    if xs.size:
        right = int(xs.max()) + 1
        left = int(xs.min())
        fill = right / width
        return MatchResult(
            fill >= threshold, name=name, method="bar", score=fill, threshold=threshold,
            loc=(origin[0] + (left + right) // 2, y_abs),
            box=(origin[0], y_abs, right, 1), roi=roi,
            metrics={"fill": round(fill, 4)},
        )
    return MatchResult(False, name=name, method="bar", score=0.0, threshold=threshold,
                       roi=roi, metrics={"fill": 0.0})


def _normalise_bands(bands) -> list[tuple[list[float], list[float]]]:
    """Accept [[lo, hi], ...] with lo/hi as 3-element sequences."""
    if bands is None:
        raise ValueError("colour detector needs 'bands'/'color_scales'")
    out = []
    for entry in bands:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise ValueError(f"bad colour band {entry!r}; expected [[lo...], [hi...]]")
        lo, hi = entry
        if len(lo) != 3 or len(hi) != 3:
            raise ValueError(f"bad colour band {entry!r}; expected 3 channels")
        out.append(([float(v) for v in lo], [float(v) for v in hi]))
    return out


# --------------------------------------------------------------------------
# Detector dispatch
# --------------------------------------------------------------------------

def run_detector(frame: np.ndarray, spec: dict, templates: dict[str, Any] | None = None) -> MatchResult:
    """Run one recogniser spec against a frame.

    Recogniser spec fields (all optional except ``method``/``template``):

    ``method``      template | color | bar      (default: template)
    ``template``    path, or a key into ``templates`` (a loaded loader dict)
    ``roi``         [x, y, w, h] in reference-resolution coordinates
    ``threshold``   score cutoff
    ``scales``      e.g. "1.0" or "0.9:1.1:0.05"
    ``bands``       colour bands, alias ``color_scales``
    ``space``       hsv | rgb (color/bar only)
    ``min_pixels``  colour detector minimum matching pixel count
    ``name``        label for logs
    """
    templates = templates or {}
    spec = dict(spec or {})
    method = str(spec.get("method", "template")).lower()
    name = str(spec.get("name") or spec.get("id") or "")
    roi = spec.get("roi")
    roi = tuple(int(v) for v in roi) if roi else None
    default_thr = 0.85 if method == "template" else 0.5
    threshold = float(spec.get("threshold", default_thr))

    if method == "template":
        # Explicit None checks: a caller may pass an already-loaded numpy
        # template array, and `array or default` raises "truth value is
        # ambiguous" instead of doing anything useful.
        tpl_ref = spec.get("template")
        if tpl_ref is None:
            tpl_ref = spec.get("templates")
        if tpl_ref is None:
            raise ValueError(f"recogniser {name!r}: template method needs a 'template'")
        tpls = tpl_ref if isinstance(tpl_ref, (list, tuple)) else [tpl_ref]
        best: MatchResult | None = None
        for one in tpls:
            loaded = templates.get(one, one) if isinstance(one, str) else one
            try:
                res = match_template(
                    frame, loaded, roi=roi, threshold=threshold, name=name,
                    scales=parse_scale_list(spec.get("scales", "1.0")),
                    mask=spec.get("mask"),
                    index=int(spec.get("index", 0) or 0),
                )
            except (ImageError, ValueError) as exc:
                res = MatchResult(False, name=name, method="template",
                                  threshold=threshold, roi=roi,
                                  metrics={"error": str(exc)})
                res.metrics["template"] = str(one)
            if best is None or res.score > best.score:
                best = res
        assert best is not None
        if len(tpls) > 1:
            best.metrics["templates_tried"] = len(tpls)
        return best

    if method == "color":
        return match_color(
            frame, spec.get("bands") or spec.get("color_scales"),
            roi=roi, threshold=threshold, name=name,
            space=str(spec.get("space", "hsv")),
            min_pixels=int(spec.get("min_pixels", 1)),
        )

    if method == "bar":
        return match_bar(
            frame, spec.get("bands") or spec.get("color_scales"),
            roi=roi, threshold=threshold, name=name,
            space=str(spec.get("space", "hsv")),
            column=spec.get("column"),
        )

    if method == "ocr":
        return match_text(
            frame,
            spec.get("text") or spec.get("expected") or spec.get("wanted"),
            roi=roi,
            # OCR confidence lives on a different scale than correlation.
            threshold=float(spec.get("threshold", 0.5)),
            name=name,
            mode=str(spec.get("mode", "contains")),
        )

    raise ValueError(f"unknown method {method!r} (expected template/color/bar/ocr)")


DETECTORS = ("template", "color", "bar", "ocr")

# One shared OCR session; importing lazily keeps the module usable without the
# MaaFramework binding installed.
_ocr_engine = None


def ocr_engine():
    global _ocr_engine
    if _ocr_engine is None:
        from .ocr import OcrEngine
        _ocr_engine = OcrEngine.shared()
    return _ocr_engine


def match_text(
    frame: np.ndarray,
    wanted,
    *,
    roi: tuple[int, int, int, int] | None = None,
    threshold: float = 0.5,
    name: str = "",
    mode: str = "contains",
) -> MatchResult:
    """Recognise text and report the best matching string's box.

    ``threshold`` here is the OCR *confidence* cutoff (0..1), not a correlation
    score - a completely different scale from template matching, so it gets its
    own default of 0.5.
    """
    if isinstance(wanted, str):
        wanted = [wanted]
    wanted = [str(w) for w in (wanted or []) if str(w).strip()]
    if not wanted:
        return MatchResult(False, name=name, method="ocr", threshold=threshold, roi=roi,
                           metrics={"error": "no 'text'/'expected' strings given"})
    try:
        engine = ocr_engine()
        hit = engine.find(frame, wanted, roi=roi, threshold=threshold, mode=mode)
    except Exception as exc:  # noqa: BLE001 - surface, never crash the loop
        return MatchResult(False, name=name, method="ocr", threshold=threshold, roi=roi,
                           metrics={"error": f"{type(exc).__name__}: {exc}"})
    if hit is None:
        # Report what *was* on screen: invaluable when a label gets reworded.
        try:
            seen = [h.text for h in engine.recognize(frame, roi)]
        except Exception:  # noqa: BLE001
            seen = []
        res = MatchResult(False, name=name, method="ocr", threshold=threshold, roi=roi)
        res.metrics["seen"] = seen[:12]
        res.metrics["score"] = 0.0
        return res
    res = MatchResult(True, name=name, method="ocr", score=hit.score, threshold=threshold,
                      loc=hit.loc, box=hit.box, roi=roi)
    res.metrics["text"] = hit.text
    res.metrics["score"] = round(hit.score, 4)
    return res


def ocr_strings(frame: np.ndarray,
                roi: tuple[int, int, int, int] | None = None) -> list[tuple[str, float, tuple]]:
    """Every recognised string as ``(text, confidence, box)`` - for debugging."""
    engine = ocr_engine()
    return [(h.text, h.score, h.box) for h in engine.recognize(frame, roi)]


def suggest_threshold(frame: np.ndarray, template, roi=None) -> dict:
    """Calibration helper: score a template and report a safe threshold.

    The point is to stop users from copying ``0.8`` blindly.  We report the
    best score plus the runner-up *outside* the winning box, which is the
    number that actually tells you how much margin you have: if the second
    best is 0.72 and the true match is 0.99, a threshold of 0.9 is safe; if
    the second best is 0.93, it is not.
    """
    res = match_template(frame, template, roi=roi, threshold=0.0)
    info: dict[str, Any] = {
        "best_score": round(res.score, 4),
        "best_box": res.box,
        "best_scale": res.scale,
    }
    if res.box:
        tpl_rgb, _ = _load_template(template)
        region = crop_roi(frame, roi)
        try:
            scores = ncc(to_gray(region), to_gray(tpl_rgb))
            x, y, w, h = res.box
            ox, oy = (roi[0], roi[1]) if roi else (0, 0)
            x, y = x - ox, y - oy
            padded = scores.copy()
            y0 = max(0, y - h // 2)
            y1 = min(padded.shape[0], y + h + h // 2)
            x0 = max(0, x - w // 2)
            x1 = min(padded.shape[1], x + w + w // 2)
            padded[y0:y1, x0:x1] = -1.0
            info["runner_up"] = round(float(padded.max()), 4)
            if padded.size and padded.max() > -1.0:
                lo, hi = info["runner_up"], info["best_score"]
                info["suggested_threshold"] = round(min(0.95, max(0.6, (lo + hi) / 2)), 3)
        except (ValueError, ImageError) as exc:
            info["runner_up_error"] = str(exc)
    return info
