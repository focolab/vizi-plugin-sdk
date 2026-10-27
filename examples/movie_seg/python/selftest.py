#!/usr/bin/env python3
"""The movie-seg arithmetic, checked against hand-worked expectations.

    .venv/bin/python selftest.py

No protocol here: `bands.py` alone -- the band rule at its edges, NaN and
-inf, the tight box, the footprint's union, the tables, the threshold's
spelling in a reply.  The emulator run is ``hostsim.py``.
"""

from __future__ import annotations

import sys

import numpy as np

from bands import (
    BAND_NAMES,
    BAND_WIDTH,
    DEFAULT_THRESHOLD,
    band_labels,
    band_lut,
    bbox_tight,
    footprint_labels,
    footprint_lut,
    format_threshold,
)

failures: list[str] = []


def check(label: str, fn) -> None:
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001
        failures.append(label)
        print(f"FAIL  {label}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok    {label}" + (f": {detail}" if detail else ""))


def test_band_edges():
    """SPEC §2: band 1 is [T, T+W), band 2 [T+W, T+2W), band 3 from T+2W up;
    below T is 0; the edges belong to the upper band."""
    T, W = DEFAULT_THRESHOLD, BAND_WIDTH
    v = np.array([[[0, T - 1e-9, T, T + W - 1, T + W, T + 2 * W - 1, T + 2 * W, 10 * T]]])
    got = band_labels(v, T)
    assert got.dtype == np.uint8 and got.shape == v.shape
    assert got.ravel().tolist() == [0, 0, 1, 1, 2, 2, 3, 3], got.ravel().tolist()
    return "0 0 1 1 2 2 3 3 across the edges; uint8"


def test_nan_and_infinities():
    v = np.array([[[np.nan, -np.inf, np.inf, 500.0]]], dtype=np.float32)
    got = band_labels(v, 400.0)
    assert got.ravel().tolist() == [0, 0, 3, 1], got.ravel().tolist()
    return "NaN and -inf are background; +inf is the top band"


def test_integer_input_and_threshold_zero():
    v = np.arange(8, dtype=np.uint16).reshape(2, 2, 2) * 100
    got = band_labels(v, 0.0)
    assert got.ravel().tolist() == [1, 1, 2, 2, 3, 3, 3, 3], got.ravel().tolist()
    return "u16 input; T = 0 makes every voxel foreground"


def test_bbox_tight():
    labels = np.zeros((4, 5, 6), dtype=np.uint8)
    labels[1, 2, 3] = 2
    labels[2, 4, 1] = 1
    tight, origin = bbox_tight(labels)
    assert origin == (1, 2, 1) and tight.shape == (2, 3, 3), (origin, tight.shape)
    assert tight[0, 0, 2] == 2 and tight[1, 2, 0] == 1 and tight.sum() == 3
    assert tight.flags.c_contiguous
    assert bbox_tight(np.zeros((2, 2, 2), dtype=np.uint8)) is None
    return "box (2, 3, 3) at (1, 2, 1); contiguous; empty is None"


def test_bbox_whole_volume():
    labels = np.ones((3, 4, 5), dtype=np.uint8)
    tight, origin = bbox_tight(labels)
    assert origin == (0, 0, 0) and tight.shape == (3, 4, 5)
    return "a full volume is its own box at the origin"


def test_footprint_union():
    a = np.array([[[500, 0], [0, 0]]], dtype=np.float64)
    b = np.array([[[0, 0], [0, 450]]], dtype=np.float64)
    c = np.array([[[0, np.nan], [0, 0]]], dtype=np.float64)
    got = footprint_labels(iter([a, b, c]), 400.0)
    assert got.dtype == np.uint8 and got.ravel().tolist() == [1, 0, 0, 1], got.ravel().tolist()
    return "union over three frames, label 1, NaN never foreground"


def test_tables():
    lut, names, visible = band_lut()
    assert lut.shape == (256, 4) and lut.dtype == np.uint8
    assert not lut[0].any() and lut[1].any() and lut[2].any() and lut[3].any() and not lut[4:].any()
    assert names == BAND_NAMES and visible == {1: True, 2: True, 3: True}
    hlut, _, hidden = band_lut({2})
    assert hidden == {1: True, 2: False, 3: True}
    assert hlut[2, 3] == 0 and tuple(hlut[2, :3]) == tuple(lut[2, :3]), "a hidden band's row: its color, alpha 0"
    assert np.array_equal(hlut[[1, 3]], lut[[1, 3]])
    flut, fnames = footprint_lut()
    assert flut[1].any() and not flut[0].any() and not flut[2:].any() and fnames == {1: "ever"}
    return "bands: rows 1-3, a hidden band's alpha 0, names by id, every band's visibility; footprint: one row"


def test_threshold_spelling():
    assert format_threshold(400) == "400" and format_threshold(400.0) == "400"
    assert format_threshold(12.5) == "12.5" and format_threshold(0) == "0"
    return "400.0 -> '400', 12.5 -> '12.5'"


def main() -> int:
    check("band edges", test_band_edges)
    check("NaN and infinities", test_nan_and_infinities)
    check("integer input, threshold 0", test_integer_input_and_threshold_zero)
    check("bbox tight", test_bbox_tight)
    check("bbox of a full volume", test_bbox_whole_volume)
    check("footprint union", test_footprint_union)
    check("tables", test_tables)
    check("threshold spelling", test_threshold_spelling)
    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("\nself-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
