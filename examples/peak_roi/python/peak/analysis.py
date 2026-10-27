"""Separable Gaussian blur and peak finding — SPEC.md §3 and §4.

No image-processing library: SPEC.md pins the kernel, the edge rule, the
accumulation width and the tie-break precisely so the implementations
can be compared against each other, and importing SciPy would make this file
trivial in one language and a research project in another.

Nothing here imports the SDK, so it runs under ``selftest.py`` with no host.
"""

from __future__ import annotations

import itertools
import math

import numpy as np


def gaussian_taps(sigma: float) -> np.ndarray:
    """The normalized 1-D kernel for ``sigma``.

    Radius ``r = ceil(3σ)``; taps ``k[i] = exp(-i² / 2σ²)`` for ``i ∈ [-r, r]``,
    scaled so ``Σk = 1``.  ``σ = 0`` means no blur, which is the single-tap
    identity kernel.
    """
    if sigma <= 0.0:
        return np.ones(1, dtype=np.float64)
    radius = int(math.ceil(3.0 * sigma))
    offsets = np.arange(-radius, radius + 1, dtype=np.float64)
    taps = np.exp(-(offsets**2) / (2.0 * sigma * sigma))
    return taps / taps.sum()


def blur_axis(volume, sigma: float, axis: int) -> np.ndarray:
    """Correlate ``volume`` with the 1-D Gaussian along ``axis``, clamping edges.

    Clamping is `np.pad(mode="edge")`: sampling outside the volume replicates
    the nearest edge voxel, so a constant volume survives unchanged.
    """
    values = np.asarray(volume, dtype=np.float64)
    if sigma <= 0.0:
        return values
    taps = gaussian_taps(sigma)
    radius = (len(taps) - 1) // 2

    # Blocked over the other axes, so each block's padded copy and its
    # accumulator stay near cache size.  Accumulating per tap over the WHOLE
    # volume allocated a volume-sized temporary per tap -- 63 of them at the
    # default extents, 1.6 GB each for a 400 MB u16 volume -- and ran at memory
    # speed.  Each voxel still sums its taps in the same order, so the result
    # is bit-identical.
    others = [a for a in range(values.ndim) if a != axis]
    step = list(values.shape)
    budget = max(1, _BLOCK_BYTES // (values.itemsize * (values.shape[axis] + 2 * radius)))
    for other in reversed(others):  # innermost first: longer contiguous runs
        step[other] = min(values.shape[other], budget)
        budget = max(1, budget // step[other])

    result = np.empty_like(values)
    starts = itertools.product(*(range(0, values.shape[a], step[a]) for a in others))
    for corner in starts:
        block = [slice(None)] * values.ndim
        for a, start in zip(others, corner):
            block[a] = slice(start, start + step[a])
        result[tuple(block)] = _blur_block(values[tuple(block)], taps, radius, axis)
    return result


#: About an L2 cache: the padded input and the accumulator of one block.
_BLOCK_BYTES = 1 << 21


def _blur_block(values: np.ndarray, taps: np.ndarray, radius: int, axis: int) -> np.ndarray:
    """One block of `blur_axis`: edge-clamped, one multiply-accumulate per tap.

    Tap `n` of the padded array is offset `n - r` in the original, and the
    kernel is symmetric, so correlation and convolution coincide."""
    padding = [(0, 0)] * values.ndim
    padding[axis] = (radius, radius)
    padded = np.pad(values, padding, mode="edge")
    result = np.zeros_like(values)
    product = np.empty_like(values)
    window = [slice(None)] * values.ndim
    for offset, weight in enumerate(taps):
        window[axis] = slice(offset, offset + values.shape[axis])
        np.multiply(padded[tuple(window)], weight, out=product)
        result += product
    return result


def finite_floor(volume) -> np.ndarray:
    """SPEC.md §3's non-finite rule: every NaN and ``-inf`` becomes the
    volume's minimum finite value, so a bad voxel can neither be the peak nor
    lift or sink its neighborhood; ``+inf`` stands.  A volume with no finite
    voxel cannot be searched."""
    values = np.asarray(volume, dtype=np.float64)
    bad = np.isnan(values) | np.isneginf(values)
    if not bad.any():
        return values
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        raise ValueError("the volume has no finite voxel")
    values = values.copy()
    values[bad] = finite.min()
    return values


def blur(volume, sigmas_zyx) -> np.ndarray:
    """Separable Gaussian over a ``(Z, Y, X)`` volume: f64 throughout, Z then Y then X."""
    result = finite_floor(volume)
    for axis, sigma in enumerate(sigmas_zyx):
        result = blur_axis(result, float(sigma), axis)
    return result


def find_peak(blurred) -> tuple[int, int, int]:
    """``(z, y, x)`` of the maximum value.

    Ties resolve to the lowest index in raster order — smallest ``z``, then
    ``y``, then ``x``.  ``np.argmax`` returns the first maximum of the
    flattened array, and the flattening is C-order, which is exactly that rule.
    """
    flat = int(np.argmax(blurred))
    return tuple(int(index) for index in np.unravel_index(flat, np.shape(blurred)))


def peak_of(volume, extents_zyx) -> tuple[int, int, int]:
    """SPEC.md §3–§4 end to end: blur with ``σ = extent / 2`` per axis, return the peak."""
    return find_peak(blur(volume, [float(extent) / 2.0 for extent in extents_zyx]))
