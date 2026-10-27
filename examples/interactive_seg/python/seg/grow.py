"""The threshold, the components, the anchor and the choice (`SPEC.md` §6-§8),
plus the accumulation into a label map (§9).  Everything here is arrays in
and arrays out; the window ``W`` has already been read.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MAX_COMPONENT = 8_000_000
MAX_LABELS = 255

#: §9's palette; LUT row ``k >= 1`` is entry ``(k - 1) % 8`` at alpha 180.
PALETTE = (
    (230, 25, 75),
    (60, 180, 75),
    (67, 99, 216),
    (245, 130, 49),
    (145, 30, 180),
    (66, 212, 244),
    (240, 50, 230),
    (191, 239, 69),
)
LABEL_ALPHA = 180


class Refusal(Exception):
    """An outcome to report on the status line, changing nothing."""


def threshold(window: np.ndarray, selected: np.ndarray, scale: float) -> float:
    """§6: ``scale * median`` of the per-column maxima over the selected
    columns, in f64.  A NaN or -inf voxel never contributes to a column
    maximum; a column with no contributing voxel is dropped from the median;
    no column left is a refusal.  +inf does contribute, so the median -- and
    with a positive finite ``scale`` the threshold -- is finite or +inf,
    never -inf and never NaN."""
    w = np.asarray(window, dtype=np.float64)
    if not selected.any():
        raise Refusal("Drag a closed loop around a region.")
    columns = w[:, selected]  # (Z, n)
    maxima = np.max(np.where(np.isnan(columns), -np.inf, columns), axis=0)  # -inf where nothing contributed
    maxima = maxima[maxima > -np.inf]  # a column of NaN and -inf is dropped, not admitted as -inf
    if maxima.size == 0:
        raise Refusal("Nothing measurable under the loop.")
    return float(scale) * float(np.median(maxima))


def components(mask: np.ndarray) -> np.ndarray:
    """Face-connected components (6 in 3D, 4 in 2D) of a bool array, as an
    int32 label array with 0 for background and ids ``1..K`` in order of each
    component's first voxel in raster order -- so the lowest id is the
    component containing the lowest raster-order voxel.

    Vectorized label propagation: every foreground cell starts as its own
    raster index, each pass takes the minimum over face neighbors, and a
    pointer-jumping step follows the chain of minima to its root so a long
    thin object converges in O(log) passes rather than O(length)."""
    mask = np.asarray(mask, dtype=bool)
    if not mask.any():
        return np.zeros(mask.shape, dtype=np.int32)
    flat_index = np.arange(1, mask.size + 1, dtype=np.int64).reshape(mask.shape)
    labels = np.where(mask, flat_index, 0)
    ndim = mask.ndim
    while True:
        previous = labels
        current = labels.copy()
        for axis in range(ndim):
            for direction in (1, -1):
                neighbour = np.roll(labels, direction, axis=axis)
                # the roll wraps the far edge round; blank it
                edge = [slice(None)] * ndim
                edge[axis] = 0 if direction == 1 else -1
                neighbour[tuple(edge)] = 0
                both = mask & (neighbour > 0)
                np.minimum(current, np.where(both, neighbour, current), out=current)
        # pointer jumping: labels.flat[label - 1] is the label of the cell a
        # label points at; iterate to the root
        flat = current.reshape(-1)
        fg = np.nonzero(flat)[0]
        while True:
            hop = flat[flat[fg] - 1]
            if np.array_equal(hop, flat[fg]):
                break
            flat[fg] = hop
        labels = flat.reshape(mask.shape)
        if np.array_equal(labels, previous):
            break
    # compact ids in raster order of first appearance
    roots = labels.reshape(-1)
    unique_roots = np.unique(roots[roots > 0])  # ascending = raster order of the root cell
    lookup = np.zeros(int(unique_roots.max()) + 1, dtype=np.int32)
    lookup[unique_roots] = np.arange(1, len(unique_roots) + 1, dtype=np.int32)
    return lookup[roots].reshape(mask.shape)


@dataclass(frozen=True)
class Grown:
    component: np.ndarray  # bool, the window's shape (Z, h, w) -- one plane deep when not 3D
    threshold: float
    clipped: bool


def diagnostic(window: np.ndarray, selected: np.ndarray) -> tuple[int, int, int]:
    """§7: the position of the maximum of ``W`` over all ``z`` and the
    selected ``(y, x)``, ties to the lowest raster-order index."""
    w = np.asarray(window, dtype=np.float64)
    candidates = np.where(selected[None, :, :], np.where(np.isnan(w), -np.inf, w), -np.inf)
    flat = int(np.argmax(candidates))  # first occurrence in C order = lowest raster index
    return tuple(int(i) for i in np.unravel_index(flat, candidates.shape))  # type: ignore[return-value]


def grow(
    window: np.ndarray,
    selected: np.ndarray,
    z_g: int,
    threshold_value: float,
    *,
    grow_3d: bool,
    min_size: int,
    window_is_volume_face: tuple[tuple[bool, bool], tuple[bool, bool], tuple[bool, bool]],
) -> Grown:
    """§7-§8 on a window ``W`` of shape ``(Z, h, w)`` whose ``selected`` is the
    ``(h, w)`` mask of the lasso's columns in window coordinates and whose
    plane ``z_g`` is the drawn one.  Returns the chosen component as a bool
    array of the window's shape, or raises `Refusal`."""
    w = np.asarray(window, dtype=np.float64)
    with np.errstate(invalid="ignore"):
        above = w >= threshold_value  # NaN compares False: background
    if grow_3d:
        labels = components(above)
        anchor_plane = labels[z_g]
    else:
        plane_labels = components(above[z_g])
        labels = np.zeros_like(above, dtype=np.int32)
        labels[z_g] = plane_labels
        anchor_plane = plane_labels
    # §7: candidates contain a voxel (z_g, y, x) with (y, x) selected
    candidate_ids = np.unique(anchor_plane[selected & (anchor_plane > 0)])
    if candidate_ids.size == 0:
        z_star, _, _ = diagnostic(w, selected)
        raise Refusal(
            f"Nothing above the threshold on plane {z_g}. The brightest voxel you enclosed is at "
            f"z={z_star} — scroll there, or lower Threshold ×."
        )
    counts = np.bincount(labels.reshape(-1), minlength=int(labels.max()) + 1)
    sizes = counts[candidate_ids]
    best = int(candidate_ids[np.flatnonzero(sizes == sizes.max())[0]])  # ties: lowest id = lowest raster voxel
    size = int(counts[best])
    if size < min_size:
        raise Refusal(f"No component of at least {min_size} voxels.")
    if size > MAX_COMPONENT:
        raise Refusal("Selection too permissive — try a tighter loop or a higher Threshold ×.")
    component = labels == best
    clipped = False
    for axis in range(3):
        for side, is_face in enumerate(window_is_volume_face[axis]):
            if is_face:
                continue
            index = 0 if side == 0 else -1
            face = np.take(component, index, axis=axis)
            if face.any():
                clipped = True
    return Grown(component=component, threshold=threshold_value, clipped=clipped)


def lut() -> np.ndarray:
    """§9's ``(256, 4)`` u8 LUT: row 0 transparent, row k the palette at alpha 180."""
    table = np.zeros((256, 4), dtype=np.uint8)
    for k in range(1, 256):
        r, g, b = PALETTE[(k - 1) % len(PALETTE)]
        table[k] = (r, g, b, LABEL_ALPHA)
    return table


class LabelMap:
    """§9's accumulation: labels ``1..255``, each a bool array at an origin;
    the newer label wins overlapping voxels.  Scoped by the caller to one
    ``(t, c, gen)``."""

    def __init__(self) -> None:
        self.entries: list[tuple[int, tuple[int, int, int], np.ndarray]] = []

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def next_id(self) -> int:
        return len(self.entries) + 1

    def add(self, origin_zyx: tuple[int, int, int], component: np.ndarray) -> int:
        if self.next_id > MAX_LABELS:
            raise Refusal(f"At most {MAX_LABELS} labels — commit or clear first.")
        label = self.next_id
        self.entries.append((label, tuple(int(v) for v in origin_zyx), np.asarray(component, dtype=bool)))
        return label

    def render(self) -> tuple[np.ndarray, tuple[int, int, int]]:
        """The bbox-tight ``uint8`` union and its origin; later labels overwrite."""
        assert self.entries, "empty label map"
        lo = np.array([min(o[i] for _, o, _ in self.entries) for i in range(3)])
        hi = np.array([max(o[i] + a.shape[i] for _, o, a in self.entries) for i in range(3)])
        # tight around the union of the components, not around their boxes
        out = np.zeros(tuple(int(v) for v in hi - lo), dtype=np.uint8)
        for label, origin, array in self.entries:
            off = np.array(origin) - lo
            view = out[off[0] : off[0] + array.shape[0], off[1] : off[1] + array.shape[1], off[2] : off[2] + array.shape[2]]
            view[array] = label
        nz = np.nonzero(out)
        tight_lo = np.array([int(a.min()) for a in nz])
        tight_hi = np.array([int(a.max()) + 1 for a in nz])
        out = out[tight_lo[0] : tight_hi[0], tight_lo[1] : tight_hi[1], tight_lo[2] : tight_hi[2]]
        origin = tuple(int(v) for v in lo + tight_lo)
        return np.ascontiguousarray(out), origin  # type: ignore[return-value]
