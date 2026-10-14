"""Levels, the crop, and the voxels a segment passes through — `SPEC.md`
§"Levels", §"The tool" 3.  Every function here is pure arithmetic on
`control_plane_protocol.md` §8.11's two exact maps and `doorbell_E_input.md`
§3's coordinate rule (a coordinate is a voxel CENTRE in the index space of
the record's level; voxel ``k`` spans ``[k - 0.5, k + 0.5]``).
"""

from __future__ import annotations

import itertools
import math
from typing import Iterable, Sequence

Cell = tuple[int, int, int]
Triple = tuple[float, float, float]


# --- the two level maps (§8.11) ---------------------------------------------


def level0_of(coordinate: float, scale: float) -> float:
    """A level-``L`` coordinate (a voxel centre, or any point in that
    lattice's space) as a level-0 lattice coordinate: ``(c + 0.5) * s - 0.5``.
    Level-``L`` pixel ``j`` covers level-0 pixels ``[j*s, j*s + s - 1]`` and
    its centre sits at ``j*s + (s - 1)/2``, which is this map at ``j``."""
    return (coordinate + 0.5) * scale - 0.5


def edge_at_level(edge: float, scale: float) -> float:
    """A level-0 EDGE coordinate at level ``L``: ``(e + 0.5) / s - 0.5`` —
    affine, never proportional; the offset is the grid's phase."""
    return (edge + 0.5) / scale - 0.5


def centres(lo: float, hi: float) -> tuple[int, int] | None:
    """The included voxel centres of an edge interval, inclusive at both
    ends: ``ceil(lo) ..= floor(hi)``; an exactly-integral bound is a hit.
    ``None`` when the interval holds no centre."""
    first, last = math.ceil(lo), math.floor(hi)
    return (first, last) if first <= last else None


def crop_at_level(
    crop_edges: Sequence[float], scale: Triple, shape: Sequence[int]
) -> tuple[tuple[int, int], ...] | None:
    """The clip box — ``(z0, z1, y0, y1, x0, x1)`` in level-0 edges — as
    included centres at a level, ``((z_lo, z_hi), (y_lo, y_hi), (x_lo,
    x_hi))`` inclusive, clamped to the level's ``shape``.  Convert the edges
    to the level FIRST, then to centres (§8.11: the other order rounds
    twice).  ``None`` when any axis is empty."""
    out = []
    for axis in range(3):
        e0, e1 = crop_edges[2 * axis], crop_edges[2 * axis + 1]
        span = centres(edge_at_level(e0, scale[axis]), edge_at_level(e1, scale[axis]))
        if span is None:
            return None
        lo, hi = max(span[0], 0), min(span[1], int(shape[axis]) - 1)
        if lo > hi:
            return None
        out.append((lo, hi))
    return tuple(out)


# --- the traversal -----------------------------------------------------------


#: How close two crossing parameters, or a coordinate and a boundary, may be
#: and still count as one event.  A tie that is exact in real arithmetic can
#: be an ulp apart after a division, and a segment passing within a billionth
#: of its length of an edge touches it for every purpose this tool has.
#: Comparing exactly and ACCUMULATING the crossing parameter drifts and
#: loses such ties.
EPS = 1e-9


def _on_boundary(coordinate: float) -> bool:
    return abs((coordinate + 0.5) - round(coordinate + 0.5)) <= EPS


def traverse(p0: Sequence[float], p1: Sequence[float]) -> list[Cell]:
    """Every voxel whose CLOSED cube the CLOSED segment ``p0 -> p1`` meets,
    in order from ``p0`` by the parameter at which each cube is first met
    (a supercover Amanatides-Woo).  Coordinates are voxel centres, so cell
    ``k`` is ``[k - 0.5, k + 0.5]``.  So a segment that merely touches a
    face, an edge or a corner counts every cell touching it: an endpoint on
    one or more boundaries belongs to every cell around that point (two at
    a face, four at an edge, eight at a corner), a simultaneous crossing
    yields every cell reached by stepping any non-empty subset of the tied
    axes, and a segment running WITHIN a boundary plane (a zero direction
    component on a boundary coordinate) walks both columns.  Cells first
    met at the same parameter stand in a fixed but arbitrary order among
    themselves.  The crossing parameter is recomputed from the cell at each
    step, never accumulated, so a tie exact in real arithmetic is still one
    within ``EPS``.  ``selftest.py`` checks all of it against a slab test."""
    p0 = tuple(float(v) for v in p0)
    p1 = tuple(float(v) for v in p1)
    d = tuple(b - a for a, b in zip(p0, p1))
    cell = [math.floor(v + 0.5) for v in p0]
    both: list[int] = []  # axes to walk on both sides of a boundary plane
    behind: list[int] = []  # axes whose other side also touches p0
    step = [0, 0, 0]
    for a in range(3):
        if _on_boundary(p0[a]):
            cell[a] = round(p0[a] + 0.5)  # exactly on the boundary: the upper cell's index
        if d[a] > 0:
            step[a] = 1
        elif d[a] < 0:
            step[a] = -1
            if _on_boundary(p0[a]):
                cell[a] -= 1  # heading down from a boundary: the lower cell is the one entered
        if _on_boundary(p0[a]):
            (both if step[a] == 0 else behind).append(a)

    def crossing(a: int) -> float:
        return ((cell[a] + 0.5 * step[a]) - p0[a]) / d[a]

    seen: set[Cell] = set()
    cells: list[Cell] = []

    def emit(c: Cell) -> None:
        if c not in seen:
            seen.add(c)
            cells.append(c)

    # Every cell around the start point: the product over the boundary axes
    # of {the cell behind, the cell ahead}, the walk's own start cell last.
    for size in range(len(behind), 0, -1):
        for subset in itertools.combinations(behind, size):
            touched = list(cell)
            for a in subset:
                touched[a] -= step[a]
            emit(tuple(touched))
    emit(tuple(cell))
    while True:
        t = min((crossing(a) for a in range(3) if step[a]), default=math.inf)
        if t > 1.0 + EPS:
            break
        tied = [a for a in range(3) if step[a] and crossing(a) <= t + EPS]
        # A simultaneous crossing: the segment passes through an edge or a
        # corner, and the closed rule counts every cell that touches it --
        # each cell reached by stepping any non-empty subset of the tied
        # axes.  Smaller subsets first, the full step last: that is the cell
        # the segment enters after the corner.
        for size in range(1, len(tied)):
            for subset in itertools.combinations(tied, size):
                touched = list(cell)
                for a in subset:
                    touched[a] += step[a]
                emit(tuple(touched))
        for a in tied:
            cell[a] += step[a]
        emit(tuple(cell))
    if not both:
        return cells
    # A segment lying in a boundary plane belongs to the cells on both sides.
    expanded: list[Cell] = []
    seen2: set[Cell] = set()
    for c in cells:
        variants = [c]
        for a in both:
            variants = [v[:a] + (v[a] - k,) + v[a + 1 :] for v in variants for k in (1, 0)]
        for v in variants:
            if v not in seen2:
                seen2.add(v)
                expanded.append(v)
    return expanded


def column_cells(kind_free_axis: int, fixed: dict[int, float], lo: float, hi: float) -> list[Cell]:
    """A projection column's voxels: the two fixed coordinates rounded to
    their centres, the free axis over ``[lo, hi)`` ascending.  ``fixed``
    maps axis index (0 = z, 1 = y, 2 = x) to its coordinate."""
    first, last = math.ceil(lo), math.ceil(hi) - 1
    out: list[Cell] = []
    for k in range(first, last + 1):
        cell = [0, 0, 0]
        for a in range(3):
            cell[a] = k if a == kind_free_axis else math.floor(fixed[a] + 0.5)
        out.append(tuple(cell))
    return out


def clip(cells: Iterable[Cell], ranges: tuple[tuple[int, int], ...]) -> list[Cell]:
    """Keep the cells inside inclusive per-axis ranges, order preserved."""
    return [c for c in cells if all(ranges[a][0] <= c[a] <= ranges[a][1] for a in range(3))]


def aabb(cells: Sequence[Cell]) -> tuple[Cell, Cell]:
    """The bounding box of a non-empty cell set: min inclusive, max exclusive."""
    lo = tuple(min(c[a] for c in cells) for a in range(3))
    hi = tuple(max(c[a] for c in cells) + 1 for a in range(3))
    return lo, hi


def block_of(cell: Cell, scale: Triple, extent: Sequence[int]) -> tuple[Cell, Cell]:
    """The level-0 block a level-``L`` voxel covers: ``[j*s, j*s + s)`` per
    axis, clamped to the level-0 extent; min inclusive, max exclusive."""
    lo = tuple(int(cell[a] * scale[a]) for a in range(3))
    hi = tuple(min(int(cell[a] * scale[a] + scale[a]), int(extent[a])) for a in range(3))
    return lo, hi
