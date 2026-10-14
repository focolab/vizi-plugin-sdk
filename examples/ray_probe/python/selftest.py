#!/usr/bin/env python3
"""Offline checks of the probe's arithmetic and the plugin's pieces --
everything that needs no host.

    .venv/bin/python selftest.py

Every expectation is written independently of the implementation: the level
maps against `control_plane_protocol.md` §8.11's own worked numbers, the
traversal against dense sampling and hand-drawn segments, the tie rules by
hand.  Agreeing with the implementation is evidence; deriving the expectation
from it is not.  ``hostsim.py`` is the other half, over a real process.
"""

from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ray_probe  # noqa: E402
from probe.ellipsoid import compile_display, ellipsoid  # noqa: E402
from probe.geometry import (  # noqa: E402
    aabb,
    block_of,
    centres,
    clip,
    column_cells,
    crop_at_level,
    edge_at_level,
    level0_of,
    traverse,
)
from probe.pick import block_max, distances, first_max, sample_indices  # noqa: E402
from probe.plot import png, render  # noqa: E402

_failures: list[str] = []


def check(name: str, fn) -> None:
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001
        _failures.append(name)
        print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
        return
    print(f"ok    {name}: {detail}")


def closed_cells(p0, p1) -> set[tuple[int, int, int]]:
    """The closed-cell rule computed without any traversal: every cell in the
    segment's bounding box whose closed cube ``[k - 0.5, k + 0.5]^3`` meets
    the closed segment, by the slab test with inclusive comparisons.  The
    expectation `SPEC.md` §3 states, written independently of the DDA."""
    p0, p1 = np.array(p0, float), np.array(p1, float)
    d = p1 - p0
    lo = [int(math.floor(min(a, b) + 0.5)) - 1 for a, b in zip(p0, p1)]
    hi = [int(math.floor(max(a, b) + 0.5)) + 1 for a, b in zip(p0, p1)]
    out = set()
    for k in range(lo[0], hi[0] + 1):
        for j in range(lo[1], hi[1] + 1):
            for i in range(lo[2], hi[2] + 1):
                t_in, t_out = 0.0, 1.0
                ok = True
                for a, c in enumerate((k, j, i)):
                    lo_a, hi_a = c - 0.5, c + 0.5
                    if d[a] == 0:
                        if not (lo_a <= p0[a] <= hi_a):
                            ok = False
                            break
                        continue
                    ta, tb = sorted(((lo_a - p0[a]) / d[a], (hi_a - p0[a]) / d[a]))
                    t_in, t_out = max(t_in, ta), min(t_out, tb)
                if ok and t_in <= t_out + 1e-9:
                    out.add((k, j, i))
    return out


def entry_parameter(p0, p1, cell) -> float:
    """Where along the segment a cell's closed cube is first met."""
    p0, p1 = np.array(p0, float), np.array(p1, float)
    d = p1 - p0
    t_in = 0.0
    for a, c in enumerate(cell):
        if d[a] == 0:
            continue
        ta, tb = sorted(((c - 0.5 - p0[a]) / d[a], (c + 0.5 - p0[a]) / d[a]))
        t_in = max(t_in, ta)
    return t_in


def dense_cells(p0, p1, n: int = 100000) -> list[tuple[int, int, int]]:
    """An independent traversal: sample the segment densely, keep each new cell."""
    p0, p1 = np.array(p0, float), np.array(p1, float)
    seen, cells = set(), []
    for u in np.linspace(0.0, 1.0, n):
        cell = tuple(int(math.floor(v + 0.5)) for v in p0 + u * (p1 - p0))
        if cell not in seen:
            seen.add(cell)
            cells.append(cell)
    return cells


# --- §8.11's two maps ------------------------------------------------------------


def test_level_maps():
    # the worked example: pixels 25..1519 are edges {24.5, 1519.5}; at factor 2 the
    # included centres are 12 ..= 759 -- NOT 13 ..= 759, which `e / s` gives
    assert edge_at_level(24.5, 2) == 12.0 and edge_at_level(1519.5, 2) == 759.5
    assert centres(edge_at_level(24.5, 2), edge_at_level(1519.5, 2)) == (12, 759)
    assert centres(24.5 / 2, 1519.5 / 2) == (13, 759), "the proportional map is the wrong one, and it differs"
    # a level-N pixel j is centred at level-0 j*s + (s-1)/2
    assert level0_of(10, 4) == 41.5 and level0_of(0, 2) == 0.5 and level0_of(3, 1) == 3.0
    # the maps invert each other, for a factor that is NOT a power of two
    for e in (-0.5, 4.0, 30.5, 1000.25):
        assert abs(level0_of(edge_at_level(e, 3), 3) - e) < 1e-12
    # an exactly-integral bound is a hit, inclusive at both ends
    assert centres(3.0, 7.0) == (3, 7) and centres(3.2, 3.9) is None and centres(2.5, 2.5) is None
    # the crop: convert to the level FIRST, then to centres; clamp to the level's shape
    full = (-0.5, 31.5, -0.5, 47.5, -0.5, 63.5)
    assert crop_at_level(full, (4, 4, 4), (8, 12, 16)) == ((0, 7), (0, 11), (0, 15))
    assert crop_at_level((3.5, 9.5, -0.5, 47.5, -0.5, 63.5), (2, 2, 2), (16, 24, 32)) == ((2, 4), (0, 23), (0, 31))
    assert crop_at_level((3.5, 4.5, -0.5, 47.5, -0.5, 63.5), (4, 4, 4), (8, 12, 16)) is None, "no level-2 centre inside z in [3.5, 4.5]"
    return "§8.11's 12..=759 at factor 2; inverse maps at factor 3; the crop converted before rounding"


# --- the traversal ------------------------------------------------------------------


def test_traversal_axis_aligned():
    cells = traverse((5, 7, 2.2), (5, 7, 9.7))
    assert cells == [(5, 7, x) for x in range(2, 11)], cells  # 9.7 lies in cell 10's [9.5, 10.5]
    return f"{len(cells)} cells along x, in order"


def test_traversal_oblique():
    for p0, p1 in (((0.2, 0.3, 0.1), (3.7, 2.9, 5.6)), ((10.4, 3.1, 7.9), (1.2, 9.8, 0.3)), ((0.13, 0.21, 0.37), (2.71, 2.83, 2.59))):
        got, want = traverse(p0, p1), dense_cells(p0, p1)
        assert got == want, (p0, p1, got, want)
    return "three oblique segments agree with dense sampling, in order"


def test_traversal_closed_rule():
    """§3's rule -- every cell the CLOSED segment intersects -- against a slab
    test that is not a traversal, on segments with simultaneous crossings:
    an edge, a corner, a diagonal through three corners, both directions."""
    cases = [
        ((0.0, 0.0, 0.0), (1.0, 1.0, 0.0)),  # an edge crossing: four cells
        ((1.0, 1.0, 0.0), (0.0, 0.0, 0.0)),  # the same, reversed
        ((0.0, 0.0, 0.0), (2.999, 2.999, 2.999)),  # three corners: 22 cells
        ((0.2, 0.3, 0.1), (3.7, 2.9, 5.6)),  # no tie
        ((0.5, 0.5, 0.5), (0.5, 0.5, 2.0)),  # along an edge: four columns
        ((2.0, 0.0, 0.0), (0.0, 2.0, 0.0)),  # a corner met mid-segment, direction mixed
        # ties exact in real arithmetic that an ACCUMULATED crossing parameter loses
        ((0.0, 0.0, 0.0), (7.0, 3.0, 5.0)),
        ((1.0, 2.0, 3.0), (10.0, 5.0, 9.0)),
        ((0.0, 0.0, 0.0), (9.0, 3.0, 3.0)),
        ((0.5, 0.5, 0.5), (12.5, 4.5, 8.5)),  # starts on a corner: eight cells there
        ((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),  # a point on a corner: eight cells, nothing else
    ]
    for p0, p1 in cases:
        got = traverse(p0, p1)
        want = closed_cells(p0, p1)
        assert set(got) == want, (p0, p1, sorted(set(got) ^ want))
        assert len(got) == len(set(got)), "a cell listed twice"
        ts = [entry_parameter(p0, p1, c) for c in got]
        assert all(a <= b + 1e-9 for a, b in zip(ts, ts[1:])), "not in order along the ray"
    assert traverse((0.0, 0.0, 0.0), (1.0, 1.0, 0.0)) == [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)]
    assert len(traverse((0, 0, 0), (2.999, 2.999, 2.999))) == 22
    assert len(traverse((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))) == 8
    return f"{len(cases)} segments with edges and corners, ties that drift under accumulation, a corner start: the traversal is exactly the closed-cell set, in order"


def test_traversal_edges():
    assert traverse((1.1, 1.2, 1.3), (1.4, 0.9, 1.2)) == [(1, 1, 1)], "a segment inside one voxel"
    # a start exactly on a face: both cells there, the one behind first
    assert traverse((0, 0, 2.5), (0, 0, 4.0)) == [(0, 0, 2), (0, 0, 3), (0, 0, 4)]
    # an end exactly on a face: both cells there too
    assert traverse((0, 0, 1.0), (0, 0, 2.5)) == [(0, 0, 1), (0, 0, 2), (0, 0, 3)]
    # a segment lying in a boundary plane walks both columns
    both = traverse((0, 1.5, 0), (0, 1.5, 2))
    assert both == [(0, 1, 0), (0, 2, 0), (0, 1, 1), (0, 2, 1), (0, 1, 2), (0, 2, 2)], both
    return "one cell; a face at the start and at the end; a segment in a boundary plane"


def test_clip_and_box():
    cells = traverse((18, 22, -0.5), (18, 22, 63.5))
    assert cells[0] == (18, 22, -1) and cells[-1] == (18, 22, 64), "the boundary endpoints' outer cells, before the clip"
    ranges = crop_at_level((-0.5, 31.5, -0.5, 47.5, -0.5, 63.5), (1, 1, 1), (32, 48, 64))
    kept = clip(cells, ranges)
    assert kept == [(18, 22, x) for x in range(64)], (kept[0], kept[-1], len(kept))
    assert aabb(kept) == ((18, 22, 0), (19, 23, 64))
    return "the crop removes the outer cells; the box is min inclusive, max exclusive"


def test_column():
    cells = column_cells(0, {1: 22.2, 2: 41.7}, 3.0, 9.0)
    assert cells == [(z, 22, 42) for z in range(3, 9)], cells
    cells = column_cells(2, {0: 4.5, 1: 6.4}, 0.0, 5.0)
    assert cells == [(5, 6, x) for x in range(0, 5)], cells
    return "a YX column over [3, 9) and an ZY column over [0, 5), fixed coordinates rounded"


# --- the pick rules ------------------------------------------------------------------


def test_first_max():
    cells = [(0, 0, i) for i in range(6)]
    assert first_max(cells, [1, 5, 5, 2, float("nan"), 5]) == (1, 5.0), "ties to the first"
    assert first_max(cells, [float("nan"), float("-inf"), -3, float("nan"), -3, -1]) == (5, -1.0)
    assert first_max(cells, [float("-inf")] * 6) == (0, float("-inf")), "-inf is a value"
    assert first_max(cells, [float("nan")] * 6) is None
    return "first along the ray; NaN skipped; -inf a value; all-NaN is nothing"


def test_block():
    assert block_of((4, 5, 10), (4, 4, 4), (32, 48, 64)) == ((16, 20, 40), (20, 24, 44))
    assert block_of((7, 11, 15), (4, 4, 4), (30, 46, 62)) == ((28, 44, 60), (30, 46, 62)), "clamped at the high face"
    assert block_of((3, 1, 0), (2, 2, 2), (7, 9, 11)) == ((6, 2, 0), (7, 4, 2))
    block = np.array([[[1, 9], [9, 2]], [[9, 3], [4, 9]]], dtype=np.uint16)
    assert block_max(block, (10, 20, 30)) == ((10, 20, 31), 9.0), "ties to the smallest z, then y, then x"
    nan = np.full((2, 2, 2), np.nan)
    assert block_max(nan, (0, 0, 0)) is None
    nan[1, 0, 1] = 2.0
    assert block_max(nan, (0, 0, 0)) == ((1, 0, 1), 2.0)
    return "blocks at factors 2 and 4, clamped; the raster-order tie rule"


def test_distances():
    cells = [(0, 0, 0), (0, 0, 1), (0, 1, 1), (1, 1, 1)]
    assert distances(cells, None) == [0.0, 1.0, 2.0, 3.0]
    d = distances(cells, (2.0, 0.5, 0.5))
    assert d == [0.0, 0.5, 1.0, 3.0], d
    assert sample_indices(10) == list(range(10)) and sample_indices(2049, 1024)[-1] == 2048
    assert len(sample_indices(2049, 1024)) <= 1025
    return "voxel and physical distances; the plot's sample set keeps the last index"


# --- the plugin's pieces -------------------------------------------------------------


def test_ellipsoid():
    roi = ellipsoid((18.0, 22.0, 42.0), (2.0, 2.0, 2.0))
    assert roi.type_name == "ellipsoid"
    v = roi.vertices[0].position
    assert (v.z, v.y, v.x) == (18.0, 22.0, 42.0)
    tips = np.array(compile_display(roi))[:6] - np.array([18.0, 22.0, 42.0])
    assert np.allclose(np.abs(tips).max(axis=0), [2.0, 2.0, 2.0])
    assert not roi.HasField("t_start") and not roi.HasField("t_end")
    return "centre at the pick, equal voxel radii, timeless"


def test_plot_png():
    raster = render([1.0, 2.0, 5.0, 3.0], [0.0, 0.5, 1.0, 3.0], pick=2, x_name="d um")
    data = png(raster)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (240, 140)
    assert raster.max() == 255, "the pick is marked"
    return f"{width}x{height} PNG, the pick marked"


def test_pick_label():
    pick = ray_probe.Pick(3, (18.0, 22.0, 42.0), 2901.0, 2, 2.5, 0, 2, True)
    text = pick.label((2.0, 0.5, 0.5), "um")
    assert text.startswith("Pick 3: (18, 22, 42) = (36, 11, 21) um") and "c=0" in text and text.endswith("t=2 (2.5 s)"), text
    assert ray_probe.Pick(1, (0.5, 1.5, 2.5), 7.0, 0, None, 1, 0, False).label(None, "") == "Pick 1: (0.5, 1.5, 2.5), value 7, c=1, t=0"
    m = ray_probe.Model()
    assert m.count_text() == "No picks"
    m.picks.append(pick)
    assert m.count_text() == "1 pick"
    m.picks.append(pick)
    assert m.count_text() == "2 picks"
    return "the pinned label and count forms"


def test_manifest():
    m = json.loads((HERE / "vizi-plugin.json").read_text(encoding="utf-8"))
    assert m["manifest_version"] == 1 and m["engines"] == {"vizi": "1.0"} and m["id"] == ray_probe.PLUGIN_ID
    tool = m["contributions"]["tools"][0]
    assert tool["id"] == ray_probe.TOOL and tool["cancel_on_camera_move"] is False and tool["mode"] == "gesture"
    assert [a["id"] for a in tool["actions"]] == ["undo", "clear"]
    for icon in [tool["pane"]["icon"]] + [a["pane"]["icon"] for a in tool["actions"]]:
        path = HERE / icon
        assert path.is_file() and b"<svg" in path.read_bytes()[:256], icon
    assert {o["id"] for o in m["contributions"]["overlays"]} == {ray_probe.PICKS, ray_probe.AIM}
    assert {p["id"] for p in m["contributions"]["panels"]} == {ray_probe.PANEL, ray_probe.PROFILE}
    return "version 1, engines 1.0, the tool block, three SVG icons, two overlays, two panels"


def main() -> int:
    check("§8.11 level maps", test_level_maps)
    check("traversal: axis-aligned", test_traversal_axis_aligned)
    check("traversal: oblique vs dense sampling", test_traversal_oblique)
    check("traversal: the closed-cell rule at edges and corners", test_traversal_closed_rule)
    check("traversal: one cell, faces, a boundary plane", test_traversal_edges)
    check("crop clip and bounding box", test_clip_and_box)
    check("column cells", test_column)
    check("first maximum and ties", test_first_max)
    check("block of a coarse voxel; block maximum", test_block)
    check("distances and the plot's samples", test_distances)
    check("ellipsoid ROI", test_ellipsoid)
    check("plot PNG", test_plot_png)
    check("pick label and count forms", test_pick_label)
    check("manifest", test_manifest)
    if _failures:
        print(f"\n{len(_failures)} check(s) failed: {', '.join(_failures)}", file=sys.stderr)
        return 1
    print("\nself-test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
