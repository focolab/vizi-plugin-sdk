"""The lasso: a path of ``(z, y, x)`` records to a selected set of voxel
columns, its search window, and the decimated preview (`SPEC.md` §2, §4, §5).
"""

from __future__ import annotations

import numpy as np

LIVE_MAX_POINTS = 512


def decimate(n: int, k_max: int = LIVE_MAX_POINTS) -> list[int]:
    """§2: the indices of the preview points for a path of ``n`` -- uniform
    index sampling, ``k = min(n, k_max)``, ``round(i * (n - 1) / (k - 1))``,
    deduplicated; ``k = 1`` is the first point alone."""
    if n <= 0:
        return []
    k = min(n, k_max)
    if k == 1:
        return [0]
    out: list[int] = []
    for i in range(k):
        j = int(round(i * (n - 1) / (k - 1)))
        if not out or out[-1] != j:
            out.append(j)
    return out


def selected_columns(path_yx, shape_yx: tuple[int, int]) -> np.ndarray:
    """§4: the columns ``(y, x)`` whose centre ``(y + 0.5, x + 0.5)`` is inside
    the closed polygon by the even-odd rule, as a ``(Y, X)`` bool mask clipped
    to the volume.  The crossing-number form `SPEC.md` pins, accumulated in
    f64, vectorised over the polygon's bounding box."""
    height, width = shape_yx
    mask = np.zeros((height, width), dtype=bool)
    verts = np.asarray(path_yx, dtype=np.float64).reshape(-1, 2)
    n = len(verts)
    if n < 3 or height == 0 or width == 0:
        return mask
    y_lo = max(0, int(np.floor(verts[:, 0].min() - 0.5)))
    y_hi = min(height, int(np.ceil(verts[:, 0].max() - 0.5)) + 1)
    x_lo = max(0, int(np.floor(verts[:, 1].min() - 0.5)))
    x_hi = min(width, int(np.ceil(verts[:, 1].max() - 0.5)) + 1)
    if y_lo >= y_hi or x_lo >= x_hi:
        return mask
    py = np.arange(y_lo, y_hi, dtype=np.float64)[:, None] + 0.5
    px = np.arange(x_lo, x_hi, dtype=np.float64)[None, :] + 0.5
    inside = np.zeros((y_hi - y_lo, x_hi - x_lo), dtype=bool)
    for j in range(n):
        ay, ax = verts[j]
        by, bx = verts[(j + 1) % n]
        straddles = (ay > py) != (by > py)  # (h, 1)
        if not straddles.any():
            continue
        cross_x = ax + (py - ay) * (bx - ax) / (by - ay)  # (h, 1); by != ay where straddles
        inside ^= straddles & (px < cross_x)
    mask[y_lo:y_hi, x_lo:x_hi] = inside
    return mask


def bbox_of(mask: np.ndarray) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    """Exclusive bounding box ``(lo, hi)`` of the true cells, or ``None``."""
    if not mask.any():
        return None
    axes = np.nonzero(mask)
    return tuple(int(a.min()) for a in axes), tuple(int(a.max()) + 1 for a in axes)


def search_window(selected: np.ndarray, shape_zyx: tuple[int, int, int]) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """§5: the exclusive bbox of the selection expanded by
    ``max(8, round(0.25 * extent))`` on each side, clipped, all of Z."""
    box = bbox_of(selected)
    assert box is not None, "no selection"
    (y0, x0), (y1, x1) = box
    margin_y = max(8, int(round(0.25 * (y1 - y0))))
    margin_x = max(8, int(round(0.25 * (x1 - x0))))
    depth, height, width = shape_zyx
    lo = (0, max(0, y0 - margin_y), max(0, x0 - margin_x))
    hi = (depth, min(height, y1 + margin_y), min(width, x1 + margin_x))
    return lo, hi
