"""The pick rules — `SPEC.md` §"The tool" 3 and 4: the first maximum along the
traversal, the block maximum with peak-ROI's tie rule, and the distances a
profile is plotted against."""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np

from .geometry import Cell, Triple


def first_max(cells: Sequence[Cell], values: Sequence[float]) -> tuple[int, float] | None:
    """The index into ``cells`` of the greatest value, ties to the FIRST
    (nearest the camera, or lowest along a column); ``NaN`` skipped, ``±inf``
    a number.  ``None`` when nothing is comparable."""
    best: tuple[int, float] | None = None
    for i, v in enumerate(values):
        v = float(v)
        if math.isnan(v):
            continue
        if best is None or v > best[1]:
            best = (i, v)
    return best


def block_max(block: np.ndarray, origin: Cell) -> tuple[Cell, float] | None:
    """The greatest value of a level-0 block, ties to the smallest ``z``,
    then ``y``, then ``x`` (raster order — what ``nanargmax`` returns first);
    the cell is ``origin`` plus the index.  ``None`` when every value is
    ``NaN``."""
    data = np.asarray(block, dtype=np.float64)
    if data.size == 0 or np.all(np.isnan(data)):
        return None
    flat = int(np.nanargmax(data))
    k, j, i = np.unravel_index(flat, data.shape)
    return (origin[0] + int(k), origin[1] + int(j), origin[2] + int(i)), float(data[k, j, i])


def distances(cells: Sequence[Cell], spacing: Triple | None) -> list[float]:
    """Cumulative distance from the first cell's centre along the traversal:
    physical when ``spacing`` is known (``sqrt(sum((dk * u_k)^2))`` between
    consecutive centres), else in voxels of the traversal's level."""
    out = [0.0]
    for a, b in zip(cells, cells[1:]):
        if spacing is None:
            out.append(out[-1] + math.dist(a, b))
        else:
            out.append(out[-1] + math.sqrt(sum(((b[k] - a[k]) * spacing[k]) ** 2 for k in range(3))))
    return out


def sample_indices(n: int, limit: int = 1024) -> list[int]:
    """Which of ``n`` samples to PLOT (never to search): every
    ``ceil(n / limit)``-th index, the last one always kept."""
    if n <= limit:
        return list(range(n))
    stride = math.ceil(n / limit)
    picked = list(range(0, n, stride))
    if picked[-1] != n - 1:
        picked.append(n - 1)
    return picked
