#!/usr/bin/env python3
"""Offline checks of the segmentation and the plugin's pieces -- everything
that needs no host.

    .venv/bin/python selftest.py

Every expectation is written independently of the implementation: the
polygons are drawn by hand and their insides enumerated by hand, the
components are counted by eye, the medians computed on paper.  Agreeing
with the implementation is evidence; deriving the expectation from it is
not.  ``hostsim.py`` is the other half, over a real process.
"""

from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import interactive_seg  # noqa: E402
from seg.grow import LabelMap, Refusal, components, diagnostic, grow, lut, threshold  # noqa: E402
from seg.lasso import LIVE_MAX_POINTS, decimate, search_window, selected_columns  # noqa: E402
from seg.plot import png, render  # noqa: E402
from vizi_plugin_sdk import pb2  # noqa: E402
from vizi_plugin_sdk.server import _COMMANDS, _PANEL_OPENED, _TOOLS, check_live_overlay  # noqa: E402

_failures: list[str] = []


def check(label: str, fn) -> None:
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001 -- report every failure, don't stop
        _failures.append(label)
        print(f"FAIL  {label}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok    {label}" + (f": {detail}" if detail else ""))


def expect_refusal(fn, needle: str) -> str:
    try:
        fn()
    except Refusal as exc:
        assert needle in str(exc), (needle, str(exc))
        return str(exc)
    raise AssertionError(f"expected a refusal containing {needle!r}")


# --- §4 the selection ---------------------------------------------------------------------


def test_convex_loop():
    """A square with corners at voxel centres (2.5, 2.5)..(6.5, 6.5): the
    centres strictly inside are y, x in 3..5 -- nine columns; the boundary
    ones are settled by the even-odd rule's strict comparisons."""
    path = [(2.5, 2.5), (2.5, 6.5), (6.5, 6.5), (6.5, 2.5)]
    s = selected_columns(path, (10, 10))
    expected = np.zeros((10, 10), dtype=bool)
    expected[3:6, 3:6] = True
    # the top and left edges (y = 2.5 or x = 2.5) are 'inside' by the rule's
    # half-open convention: a.y > py != b.y > py admits py == 2.5 on one side
    expected[2, 2:6] = True
    expected[2:6, 2] = True
    assert np.array_equal(s, expected), np.argwhere(s != expected).tolist()
    return f"{int(s.sum())} columns"


def test_non_convex_loop():
    """A U shape: the notch between the arms is outside."""
    path = [(0.0, 0.0), (0.0, 7.0), (7.0, 7.0), (7.0, 5.0), (2.0, 5.0), (2.0, 2.0), (7.0, 2.0), (7.0, 0.0)]
    s = selected_columns(path, (8, 8))
    assert s[1, 1] and s[1, 6] and s[5, 6] and s[5, 1], "the arms and the bar are inside"
    assert not s[5, 3] and not s[4, 3], "the notch is outside"
    assert not s[7, 3], "below the notch's mouth is outside"
    return f"{int(s.sum())} columns, notch excluded"


def test_self_intersecting_loop():
    """A bow-tie: even-odd fills both lobes and leaves the crossing point
    on the boundary; a winding rule would do the same here, so add a
    pentagram, where the two rules differ -- even-odd leaves the centre
    empty."""
    bow = [(0.0, 0.0), (6.0, 6.0), (0.0, 6.0), (6.0, 0.0)]
    s = selected_columns(bow, (7, 7))
    # (y, x) vertices: the diagonals cross at (3, 3); the lobes are LEFT and RIGHT
    # of the crossing (worked by hand: (3.5, 1.5) toggles three edges, (1.5, 3.5) two)
    assert s[3, 1] and s[3, 5] and not s[1, 3] and not s[4, 3], "the lobes are the left and right triangles"
    r = 10.0
    star = []
    for k in range(5):
        a = -np.pi / 2 + k * 4 * np.pi / 5
        star.append((12.0 + r * np.sin(a), 12.0 + r * np.cos(a)))
    s2 = selected_columns(star, (24, 24))
    assert not s2[12, 12], "even-odd leaves the pentagram's centre outside"
    # the top spike's tip is at (2, 12) with a 36-degree point: 4.5 below the
    # tip its half-width is 4.5 * tan(18 deg) = 1.46, so the centre (6.5, 12.5)
    # -- 0.5 off the spike's axis -- is inside it
    assert s2[6, 12], "the top spike is inside"
    return f"bow-tie {int(s.sum())} columns; pentagram centre empty, {int(s2.sum())} columns"


def test_point_on_edge_and_leaving_volume():
    path = [(1.0, 1.0), (1.0, 4.0), (4.0, 4.0), (4.0, 1.0)]
    s = selected_columns(path, (6, 6))
    # centres at 1.5..3.5 are inside; (0.5, *) and (4.5, *) are outside
    assert s[1:4, 1:4].all() and not s[0].any() and not s[4].any()
    outside = [(-5.0, -5.0), (-5.0, 3.0), (3.0, 3.0), (3.0, -5.0)]
    s2 = selected_columns(outside, (6, 6))
    assert s2[0:3, 0:3].all() and int(s2.sum()) == 9, "a loop leaving the volume is clipped to it"
    entirely = [(-9.0, -9.0), (-9.0, -6.0), (-6.0, -6.0), (-6.0, -9.0)]
    assert not selected_columns(entirely, (6, 6)).any()
    tiny = [(2.0, 2.0), (2.0, 2.0)]
    assert not selected_columns(tiny, (6, 6)).any(), "a bare click selects nothing"
    return "edges by the strict rule; clipped; entirely outside = empty; click = empty"


# --- §2 decimation, §5 the window ---------------------------------------------------------


def test_decimation():
    assert decimate(0) == [] and decimate(1) == [0] and decimate(2) == [0, 1]
    assert decimate(5, 3) == [0, 2, 4]
    assert decimate(4, 3) == [0, 2, 3]  # round(1 * 3 / 2) = 2 (half to even), then 3
    picks = decimate(3000)
    assert len(picks) == LIVE_MAX_POINTS and picks[0] == 0 and picks[-1] == 2999
    assert picks == sorted(set(picks)), "deduplicated, ascending"
    return f"3000 -> {len(picks)} points, first 0, last 2999"


def test_search_window():
    s = np.zeros((40, 60), dtype=bool)
    s[10:14, 20:30] = True  # 4 rows, 10 columns
    lo, hi = search_window(s, (12, 40, 60))
    assert lo == (0, 2, 12) and hi == (12, 22, 38), (lo, hi)  # margins max(8, round(1)) and max(8, round(2.5)) = 8
    s2 = np.zeros((40, 60), dtype=bool)
    s2[2:38, 5:55] = True  # 36 rows -> margin 9; 50 columns -> margin 12 (round(12.5) = 12)
    lo2, hi2 = search_window(s2, (12, 40, 60))
    assert lo2 == (0, 0, 0) and hi2 == (12, 40, 60), (lo2, hi2)
    return f"{lo}-{hi}; a wide loop clips to the volume"


# --- §6 the threshold -------------------------------------------------------------------


def test_threshold_median_and_scale():
    w = np.zeros((3, 2, 4), dtype=np.float64)
    w[:, 0, :] = [[1, 2, 3, 4], [5, 6, 7, 8], [0, 0, 0, 0]]  # column maxima 5, 6, 7, 8
    w[:, 1, :] = 10
    s = np.zeros((2, 4), dtype=bool)
    s[0, :] = True
    assert threshold(w, s, 1.0) == 6.5, threshold(w, s, 1.0)  # even count: mean of 6 and 7
    assert threshold(w, s, 2.0) == 13.0
    s[0, 3] = False
    assert threshold(w, s, 1.0) == 6.0  # odd count: the middle
    return "median of column maxima, even and odd counts, scaled"


def test_threshold_non_finite():
    w = np.full((3, 1, 6), 5.0)
    w[1, 0, 0] = np.nan  # a NaN voxel never contributes; the column's maximum is still 5
    w[:, 0, 1] = np.nan  # an all-NaN column is dropped
    w[2, 0, 2] = np.inf  # +inf is a maximum
    w[0, 0, 3] = -np.inf  # a -inf voxel never contributes either; the column's maximum is still 5
    w[:, 0, 4] = -np.inf  # an all -inf column is dropped, like an all-NaN one
    w[:2, 0, 5] = np.nan  # ...and so is a column of NaN and -inf
    w[2, 0, 5] = -np.inf
    s = np.ones((1, 6), dtype=bool)
    # maxima: 5, (dropped), inf, 5, (dropped), (dropped) -> median of [5, 5, inf] = 5
    assert threshold(w, s, 1.0) == 5.0
    w2 = np.full((2, 1, 2), np.nan)
    assert expect_refusal(lambda: threshold(w2, np.ones((1, 2), dtype=bool), 1.0), "Nothing measurable under the loop.")
    # SPEC.md §6: T is never -inf, never NaN
    w3 = np.full((1, 1, 2), -np.inf)  # every column -inf: nothing measurable, not T = -inf
    assert expect_refusal(lambda: threshold(w3, np.ones((1, 2), dtype=bool), 1.0), "Nothing measurable under the loop.")
    w4 = np.array([[[-np.inf, np.inf]]])  # the -inf column is dropped; the median is +inf, not (-inf + inf) / 2 = NaN
    assert threshold(w4, np.ones((1, 2), dtype=bool), 1.0) == np.inf
    assert threshold(w4, np.ones((1, 2), dtype=bool), 0.5) == np.inf
    return "NaN and -inf dropped per voxel and per column; +inf admitted; T never -inf or NaN"


# --- §7-§8 components, anchor, choice -----------------------------------------------------


def test_components_connectivity():
    """Two voxels touching at an edge or a corner are two components under
    face connectivity, one under 18- or 26-connectivity."""
    m = np.zeros((3, 3, 3), dtype=bool)
    m[1, 1, 1] = True
    m[1, 2, 2] = True  # edge neighbour (shares an edge, not a face)
    m[2, 2, 2] = True  # corner neighbour of (1,1,1); face neighbour of (1,2,2)
    lab = components(m)
    assert lab[1, 1, 1] == 1 and lab[1, 2, 2] == 2 and lab[2, 2, 2] == 2, lab[m].tolist()
    m2 = np.zeros((4, 4), dtype=bool)
    m2[0, 0] = m2[1, 1] = True  # diagonal in 2D: two components under 4-connectivity
    m2[2, 2] = m2[2, 3] = m2[3, 3] = True  # an L: one
    lab2 = components(m2)
    assert lab2[0, 0] == 1 and lab2[1, 1] == 2 and lab2[2, 2] == lab2[2, 3] == lab2[3, 3] == 3, lab2.tolist()
    return "3D: 6-connected; 2D: 4-connected; ids in raster order"


def test_components_long_thin():
    """A snake through the volume converges (pointer jumping) and is one component."""
    m = np.zeros((2, 30, 30), dtype=bool)
    for r in range(0, 30, 2):
        m[0, r, :] = True
        if r + 1 < 30:
            m[0, r + 1, 29 if (r // 2) % 2 == 0 else 0] = True
    lab = components(m)
    assert lab[m].max() == 1 and (lab[m] == 1).all(), np.unique(lab[m]).tolist()
    return f"a {int(m.sum())}-voxel snake is one component"


def _window_with_two_objects():
    """A window (Z=6, 10, 10): object A, 2x3x3 at planes 1-2, centred; object
    B, a 3x2x2 bar at planes 3-5, in the corner and larger than A."""
    w = np.full((6, 10, 10), 10.0)
    w[1:3, 3:6, 3:6] = 100.0  # A: 18 voxels
    w[3:6, 0:2, 0:2] = 100.0  # B: 12 voxels... make B larger:
    w[3:6, 0:3, 0:3] = 100.0  # B: 27 voxels
    return w


def test_anchor_condition():
    """The loop encloses A's footprint and is drawn on plane 1: B is larger
    but touches no selected column on plane 1, so A wins.  Drawn on plane 4,
    where nothing enclosed is above the threshold, the refusal names plane 4
    and the diagnostic's plane 1."""
    w = _window_with_two_objects()
    s = np.zeros((10, 10), dtype=bool)
    s[3:6, 3:6] = True
    faces = ((True, True), (True, True), (True, True))
    t = threshold(w, s, 1.0)
    assert t == 100.0
    grown = grow(w, s, 1, t, grow_3d=True, min_size=1, window_is_volume_face=faces)
    assert int(grown.component.sum()) == 18 and grown.component[1:3, 3:6, 3:6].all(), int(grown.component.sum())
    message = expect_refusal(
        lambda: grow(w, s, 4, t, grow_3d=True, min_size=1, window_is_volume_face=faces),
        "Nothing above the threshold on plane 4.",
    )
    assert "z=1" in message, message
    assert diagnostic(w, s) == (1, 3, 3), diagnostic(w, s)
    return f"A (18 voxels) chosen on its plane; on plane 4: {message!r}"


def test_largest_and_ties():
    """Two components meet the anchor: the larger wins; equal sizes go to
    the one holding the lowest raster-order voxel."""
    w = np.full((1, 6, 12), 0.0)
    w[0, 1:3, 1:3] = 50.0  # C1: 4 voxels
    w[0, 1:4, 7:10] = 50.0  # C2: 9 voxels
    s = np.zeros((6, 12), dtype=bool)
    s[0:5, 0:12] = True
    faces = ((True, True), (True, True), (True, True))
    grown = grow(w, s, 0, 50.0, grow_3d=False, min_size=1, window_is_volume_face=faces)
    assert int(grown.component.sum()) == 9 and grown.component[0, 1:4, 7:10].all()
    w[0, 1:3, 1:3] = 0.0
    w[0, 1:4, 1:4] = 50.0  # C1 now 9 too
    grown = grow(w, s, 0, 50.0, grow_3d=False, min_size=1, window_is_volume_face=faces)
    assert grown.component[0, 1:4, 1:4].all() and not grown.component[0, 1:4, 7:10].any(), "ties go to the lowest raster voxel"
    return "largest wins; ties to the lowest raster-order voxel"


def test_single_plane_and_size_bounds():
    w = _window_with_two_objects()
    s = np.zeros((10, 10), dtype=bool)
    s[3:6, 3:6] = True
    faces = ((True, True), (True, True), (True, True))
    grown = grow(w, s, 2, 100.0, grow_3d=False, min_size=1, window_is_volume_face=faces)
    assert int(grown.component.sum()) == 9 and grown.component[2].any() and not grown.component[1].any()
    expect_refusal(lambda: grow(w, s, 2, 100.0, grow_3d=False, min_size=10, window_is_volume_face=faces), "No component of at least 10 voxels.")
    return "one plane deep with Grow through Z off; min_size refuses by name"


def test_clip_reported():
    w = np.full((2, 5, 5), 100.0)  # everything is above threshold 50
    s = np.ones((5, 5), dtype=bool)
    grown = grow(w, s, 0, 50.0, grow_3d=True, min_size=1, window_is_volume_face=((True, True), (True, False), (True, True)))
    assert grown.clipped, "touching a window face that is not a volume face is a clip"
    grown = grow(w, s, 0, 50.0, grow_3d=True, min_size=1, window_is_volume_face=((True, True), (True, True), (True, True)))
    assert not grown.clipped
    return "clipped only at a window face that is not the volume's"


def test_non_finite_growth():
    w = np.full((1, 3, 3), 10.0)
    w[0, 1, 1] = np.nan
    w[0, 0, 0] = np.inf
    w[0, 2, 2] = -np.inf
    s = np.ones((3, 3), dtype=bool)
    faces = ((True, True), (True, True), (True, True))
    grown = grow(w, s, 0, 10.0, grow_3d=False, min_size=1, window_is_volume_face=faces)
    assert not grown.component[0, 1, 1] and not grown.component[0, 2, 2] and grown.component[0, 0, 0]
    assert int(grown.component.sum()) == 7
    # T = +inf, the one non-finite threshold §6 admits: the +inf voxels are the whole foreground
    grown = grow(w, s, 0, np.inf, grow_3d=False, min_size=1, window_is_volume_face=faces)
    assert int(grown.component.sum()) == 1 and grown.component[0, 0, 0]
    return "NaN and -inf are background, +inf foreground, at a finite T and at T = +inf"


# --- §9 the label volume ------------------------------------------------------------------


def test_lut():
    table = lut()
    assert table.shape == (256, 4) and table.dtype == np.uint8
    assert tuple(table[0]) == (0, 0, 0, 0)
    assert tuple(table[1]) == (230, 25, 75, 180) and tuple(table[8]) == (191, 239, 69, 180)
    assert tuple(table[9]) == (230, 25, 75, 180), "row 9 wraps to the first palette entry"
    return "row 0 transparent; rows 1-8 the palette at alpha 180; row 9 wraps"


def test_label_map_accumulation():
    m = LabelMap()
    a = np.zeros((2, 3, 3), dtype=bool)
    a[:, 0:2, 0:2] = True
    assert m.add((4, 10, 10), a) == 1
    b = np.ones((1, 2, 2), dtype=bool)
    assert m.add((5, 11, 11), b) == 2  # overlaps a's (5, 11, 11): the newer label wins
    labels, origin = m.render()
    assert origin == (4, 10, 10) and labels.shape == (2, 3, 3) and labels.dtype == np.uint8
    assert labels[1, 1, 1] == 2 and labels[0, 0, 0] == 1 and labels[1, 2, 2] == 2
    assert labels[0].any() and labels[-1].any() and labels[:, 0].any() and labels[:, -1].any(), "bbox-tight"
    for _ in range(253):
        m.add((0, 0, 0), np.ones((1, 1, 1), dtype=bool))
    assert m.next_id == 256
    expect_refusal(lambda: m.add((0, 0, 0), np.ones((1, 1, 1), dtype=bool)), "At most 255 labels")
    return "newer label wins; tight box; 255 cap"


def test_live_payload_fits_the_slot():
    """The decimated trail as the SDK encodes it: 512 points of f64 ZYX
    inline, under the slot the driver grants (16 KiB - 8 for the pane pair)."""
    points = np.random.default_rng(1).uniform(0, 1000, size=(LIVE_MAX_POINTS, 3))
    item = pb2.OverlayItem(item_id="trail")
    item.marker.positions_zyx.inline = points.astype("<f8").tobytes()
    item.marker.positions_zyx.shape.extend(points.shape)
    item.marker.positions_zyx.dtype = pb2.DTYPE_F64
    update = pb2.OverlayUpdate(overlay_id="seg.trail", revision=1, items=[item])
    size = len(update.SerializeToString())
    assert size <= (16 << 10) - 8, size
    assert check_live_overlay(update, "seg.trail") == [], check_live_overlay(update, "seg.trail")
    return f"{size} bytes for {LIVE_MAX_POINTS} points, inline, subject matches"


# --- the plot -----------------------------------------------------------------------------


def test_plot_png():
    data = png(render([100.0, 140.0, 120.0]))
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, depth, colour = struct.unpack(">IIBB", data[16:26])
    assert (width, height, depth, colour) == (240, 140, 8, 0)
    idat = data.index(b"IDAT")
    length = struct.unpack(">I", data[idat - 4 : idat])[0]
    raw = zlib.decompress(data[idat + 4 : idat + 4 + length])
    assert len(raw) == height * (width + 1)
    assert png(render([])) and png(render([float("nan")])), "an empty or NaN series still renders axes"
    return f"{len(data)}-byte {width}x{height} greyscale PNG, IDAT inflates to {len(raw)} bytes"


# --- the manifest and the registry --------------------------------------------------------


def _widgets(node, out=None, depth=1):
    out = [] if out is None else out
    out.append((node, depth))
    for child in node.get("children", ()):
        _widgets(child, out, depth + 1)
    return out


def test_manifest():
    manifest = json.loads((HERE / "vizi-plugin.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 1 and manifest["id"] == interactive_seg.PLUGIN_ID
    assert manifest["engines"]["vizi"] == "1.0"
    assert manifest["entry"]["command"] == [".venv/bin/python", "interactive_seg.py"]
    assert sorted(manifest["permissions"]) == ["contribute_rois", "data_access"]
    tools = manifest["contributions"]["tools"]
    assert [t["id"] for t in tools] == [interactive_seg.TOOL] and tools[0]["views"] == ["2d"]
    assert set(_TOOLS) == {interactive_seg.TOOL}
    overlays = {o["id"]: o for o in manifest["contributions"]["overlays"]}
    assert set(overlays) == {interactive_seg.TRAIL, interactive_seg.LABELS}
    assert overlays[interactive_seg.LABELS]["segment_mib"] == 64
    commands = manifest["contributions"]["commands"]
    assert all(c["menu"] is None for c in commands), "every command is menu: null"
    assert {c["id"] for c in commands} == set(_COMMANDS), set(_COMMANDS) ^ {c["id"] for c in commands}
    panels = manifest["contributions"]["panels"]
    assert len(panels) == 1 and panels[0]["id"] == interactive_seg.PANEL and set(_PANEL_OPENED) == {interactive_seg.PANEL}
    nodes = _widgets(panels[0]["root"])
    ids = [n["id"] for n, _ in nodes]
    assert len(ids) == len(set(ids)) and max(d for _, d in nodes) <= 8
    for node, _ in nodes:
        if "command" in node:
            assert node["command"] in _COMMANDS, node
    by_id = {n["id"]: n for n, _ in nodes}
    assert by_id["thresh"]["float"] is True and "float" not in by_id["size"]
    assert by_id["status"]["text"] == interactive_seg.MSG_READY
    return f"{len(commands)} commands, 1 tool, 2 overlays, {len(ids)} widgets"


def main() -> int:
    check("§4 convex loop", test_convex_loop)
    check("§4 non-convex loop", test_non_convex_loop)
    check("§4 self-intersecting loops", test_self_intersecting_loop)
    check("§4 edges, leaving the volume, a click", test_point_on_edge_and_leaving_volume)
    check("§2 decimation", test_decimation)
    check("§5 search window", test_search_window)
    check("§6 threshold: median and scale", test_threshold_median_and_scale)
    check("§6 threshold: non-finite", test_threshold_non_finite)
    check("§8 components: connectivity", test_components_connectivity)
    check("§8 components: a long thin object", test_components_long_thin)
    check("§7 anchor condition and diagnostic", test_anchor_condition)
    check("§8 largest, and ties", test_largest_and_ties)
    check("§8 single plane; minimum size", test_single_plane_and_size_bounds)
    check("§5 clip reported", test_clip_reported)
    check("§8 non-finite growth", test_non_finite_growth)
    check("§9 LUT", test_lut)
    check("§9 label map accumulation", test_label_map_accumulation)
    check("§2 live payload fits the slot", test_live_payload_fits_the_slot)
    check("plot PNG", test_plot_png)
    check("manifest", test_manifest)
    if _failures:
        print(f"\n{len(_failures)} check(s) failed: {', '.join(_failures)}", file=sys.stderr)
        return 1
    print("\nself-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
