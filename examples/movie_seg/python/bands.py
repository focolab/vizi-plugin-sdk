"""The arithmetic of the movie-seg plugin, with no protocol in it.

A voxel at or above the threshold is foreground, and foreground is cut into
intensity BANDS of a fixed width above the threshold: band 1 is
``[T, T + W)``, band 2 is ``[T + W, T + 2W)``, and the last band takes
everything from its floor up.  A voxel below the threshold -- or a NaN,
which compares false -- is label 0.  The result is one ``uint8`` label
volume, made bbox-tight before it is sent (`SPEC.md` §2).
"""

from __future__ import annotations

import numpy as np

#: The bands, by label id: names, and LUT rows (RGBA).  Three, so a
#: `label_visible` that hides one and not another is exercised.
BAND_NAMES: dict[int, str] = {1: "dim", 2: "mid", 3: "bright"}
BAND_COLORS: dict[int, tuple[int, int, int, int]] = {
    1: (60, 120, 255, 255),
    2: (255, 200, 0, 255),
    3: (255, 60, 60, 255),
}
#: The footprint overlay's one label.
FOOTPRINT_NAME = "ever"
FOOTPRINT_COLOR = (200, 200, 200, 255)

BAND_WIDTH = 200.0
DEFAULT_THRESHOLD = 400.0


def band_labels(volume, threshold: float, band_width: float = BAND_WIDTH, bands: int = len(BAND_NAMES)) -> np.ndarray:
    """Label every voxel of ``volume`` (any shape, any dtype) with its band:
    ``0`` below ``threshold`` (and for NaN), else
    ``min(bands, 1 + floor((v - threshold) / band_width))`` as ``uint8``."""
    v = np.asarray(volume, dtype=np.float64)
    labels = np.zeros(v.shape, dtype=np.uint8)
    with np.errstate(invalid="ignore"):
        foreground = v >= threshold
    if foreground.any():
        band = 1 + np.floor((v[foreground] - threshold) / band_width)
        labels[foreground] = np.minimum(band, bands).astype(np.uint8)
    return labels


def footprint_labels(volumes, threshold: float) -> np.ndarray:
    """The static overlay's one volume per level: label 1 wherever ANY of
    ``volumes`` (one per timepoint, one shape) is at or above ``threshold``,
    0 elsewhere.  A NaN is never foreground."""
    ever = None
    for volume in volumes:
        with np.errstate(invalid="ignore"):
            mask = np.asarray(volume, dtype=np.float64) >= threshold
        ever = mask if ever is None else (ever | mask)
    if ever is None:
        raise ValueError("no volumes")
    return ever.astype(np.uint8)


def bbox_tight(labels: np.ndarray) -> tuple[np.ndarray, tuple[int, int, int]] | None:
    """The smallest box holding every nonzero label, and its origin in the
    volume's own indices; ``None`` when nothing is labelled."""
    nonzero = np.nonzero(labels)
    if nonzero[0].size == 0:
        return None
    lo = tuple(int(axis.min()) for axis in nonzero)
    hi = tuple(int(axis.max()) + 1 for axis in nonzero)
    tight = labels[lo[0] : hi[0], lo[1] : hi[1], lo[2] : hi[2]]
    return np.ascontiguousarray(tight), (lo[0], lo[1], lo[2])


def band_lut(hidden: set[int] | frozenset[int] = frozenset()) -> tuple[np.ndarray, dict[int, str], dict[int, bool]]:
    """The bands overlay's table: the (256, 4) u8 LUT with row 0 clear and
    one row per band, the names by id, and the visibility of EVERY band.

    A hidden band's row has alpha 0: the host draws the voxels from the LUT,
    and ``label_visible`` governs only the compile (`SPEC.md` §3)."""
    lut = np.zeros((256, 4), dtype=np.uint8)
    for label, rgba in BAND_COLORS.items():
        lut[label] = (*rgba[:3], 0) if label in hidden else rgba
    visible = {label: label not in hidden for label in BAND_NAMES}
    return lut, dict(BAND_NAMES), visible


def footprint_lut() -> tuple[np.ndarray, dict[int, str]]:
    lut = np.zeros((256, 4), dtype=np.uint8)
    lut[1] = FOOTPRINT_COLOR
    return lut, {1: FOOTPRINT_NAME}


def format_threshold(value: float) -> str:
    """The threshold as the reply states it: an integral value without a
    decimal point, otherwise the shortest round-tripping decimal."""
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)
