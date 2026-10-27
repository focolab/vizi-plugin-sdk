#!/usr/bin/env python3
"""Offline verification for the Python peak-finder plugin.

Run it with the plugin's own interpreter:

    .venv/bin/python selftest.py

No host, no socket, no shared segment — this covers everything that can be
checked without a live vizi: the blur and peak rules from SPEC.md §3–§4, the
ellipsoid arrangement the host misreads rather than rejects, the manifest, and
the wire round-trip of what the plugin actually sends.

Expectations here are written from the specification, not derived from the
tables the implementation uses.  A check that reuses the builder's own constant
proves self-consistency and nothing else.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import peak_roi  # noqa: E402
from peak.analysis import blur, blur_axis, find_peak, gaussian_taps, peak_of  # noqa: E402
from peak.ellipsoid import ARC_WEIGHT, ellipsoid  # noqa: E402
from vizi_plugin_sdk import pb2  # noqa: E402
from vizi_plugin_sdk.server import _COMMANDS, _PANEL_OPENED  # noqa: E402

TOLERANCE = 1e-12
_failures: list[str] = []


def check(label: str, fn) -> None:
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001 — report every failure, don't stop
        _failures.append(label)
        print(f"FAIL  {label}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok    {label}" + (f": {detail}" if detail else ""))


def close(a, b, tolerance: float = TOLERANCE) -> bool:
    return abs(float(a) - float(b)) <= tolerance


# --- an independent blur, written from SPEC.md §3 ---------------------------
#
# Deliberately not the implementation's approach: explicit per-position
# accumulation with an explicit index clamp, so agreeing with it is evidence.


def reference_blur_axis(values, sigma: float, axis: int):
    if sigma <= 0.0:
        return values
    radius = int(math.ceil(3.0 * sigma))
    offsets = list(range(-radius, radius + 1))
    taps = [math.exp(-(i * i) / (2.0 * sigma * sigma)) for i in offsets]
    total = sum(taps)
    taps = [tap / total for tap in taps]

    moved = np.moveaxis(np.asarray(values, dtype=np.float64), axis, -1)
    length = moved.shape[-1]
    out = np.zeros_like(moved)
    for position in range(length):
        accumulator = np.zeros(moved.shape[:-1], dtype=np.float64)
        for offset, tap in zip(offsets, taps):
            source = min(max(position + offset, 0), length - 1)  # clamp
            accumulator = accumulator + tap * moved[..., source]
        out[..., position] = accumulator
    return np.moveaxis(out, -1, axis)


def reference_blur(volume, sigmas):
    values = np.asarray(volume, dtype=np.float64)
    for axis, sigma in enumerate(sigmas):
        values = reference_blur_axis(values, sigma, axis)
    return values


def sample_volume(shape) -> np.ndarray:
    """A deterministic, non-symmetric volume — no RNG, no seed to drift."""
    z, y, x = shape
    grid = np.arange(z * y * x, dtype=np.float64).reshape(shape)
    return np.sin(grid * 0.7) + 0.25 * np.cos(grid * 1.0)


# --- kernel -----------------------------------------------------------------


def test_kernel_shape():
    taps = gaussian_taps(1.0)
    assert len(taps) == 7, f"sigma=1 gives radius ceil(3) = 3, so 7 taps, not {len(taps)}"
    expected = [math.exp(-(i * i) / 2.0) for i in range(-3, 4)]
    scale = sum(expected)
    for index, value in enumerate(expected):
        assert close(taps[index], value / scale), f"tap {index - 3}"
    assert close(taps.sum(), 1.0), "taps must sum to 1"
    return "sigma=1 -> 7 normalized taps"


def test_kernel_radius():
    # r = ceil(3σ): the boundary cases are where an off-by-one hides.
    for sigma, taps in ((0.5, 2), (1.0, 3), (1.5, 5), (2.0, 6), (4.0, 12)):
        assert len(gaussian_taps(sigma)) == 2 * taps + 1, f"sigma={sigma}"
    assert list(gaussian_taps(0.0)) == [1.0], "sigma=0 is the identity kernel"
    return "radius = ceil(3*sigma); sigma=0 is identity"


def test_no_blur_at_zero_sigma():
    volume = sample_volume((3, 4, 5))
    assert np.array_equal(blur_axis(volume, 0.0, 1), volume)
    assert np.array_equal(blur(volume, (0.0, 0.0, 0.0)), volume)
    return "sigma=0 leaves the axis untouched"


# --- blur -------------------------------------------------------------------


def test_constant_volume_survives():
    volume = np.full((4, 5, 6), 7.25, dtype=np.float64)
    result = blur(volume, (1.0, 2.0, 0.5))
    assert np.allclose(result, 7.25, atol=1e-12), "clamped edges + unit-sum taps"
    return "a constant volume is unchanged"


def test_edge_clamping():
    # An impulse at x=0 in a 1x1x5 volume.  With clamping, position 0 sees the
    # edge voxel for every negative offset, so it accumulates taps -3..0.
    volume = np.zeros((1, 1, 5), dtype=np.float64)
    volume[0, 0, 0] = 1.0
    taps = [math.exp(-(i * i) / 2.0) for i in range(-3, 4)]
    scale = sum(taps)
    # Position 0 sees the impulse at offsets -3, -2, -1 (all clamped to it) and 0.
    expected_at_zero = sum(taps[:4]) / scale
    # Position 1 sees it at offsets -3 and -2 (clamped) and -1 (in range); the
    # offset-0 sample is its own zero.
    expected_at_one = sum(taps[:3]) / scale
    result = blur(volume, (0.0, 0.0, 1.0))
    assert close(result[0, 0, 0], expected_at_zero), f"{result[0, 0, 0]} != {expected_at_zero}"
    assert close(result[0, 0, 1], expected_at_one), f"{result[0, 0, 1]} != {expected_at_one}"
    return "edges replicate the nearest voxel"


def test_matches_reference_blur():
    volume = sample_volume((5, 7, 6))
    for sigmas in ((1.0, 1.0, 1.0), (2.0, 4.0, 4.0), (0.0, 1.5, 0.5), (0.25, 0.0, 3.0)):
        mine = blur(volume, sigmas)
        theirs = reference_blur(volume, sigmas)
        assert np.allclose(mine, theirs, atol=1e-12), f"sigmas={sigmas}"
    return "agrees with an independently written blur"


def test_integer_input_promotes():
    volume = (sample_volume((3, 3, 3)) * 100 + 200).astype(np.uint16)
    result = blur(volume, (1.0, 1.0, 1.0))
    assert result.dtype == np.float64, "conversion to f64 happens before accumulation"
    assert np.allclose(result, reference_blur(volume, (1.0, 1.0, 1.0)), atol=1e-12)
    return "u16 input accumulates in f64"


# --- peak -------------------------------------------------------------------


def test_tie_break():
    volume = np.zeros((3, 3, 3), dtype=np.float64)
    for index in ((2, 2, 2), (1, 0, 2), (1, 0, 0), (0, 2, 1)):
        volume[index] = 5.0
    assert find_peak(volume) == (0, 2, 1), "lowest raster-order index wins"
    volume[0, 0, 2] = 5.0
    assert find_peak(volume) == (0, 0, 2)
    return "ties resolve to smallest z, then y, then x"


def test_non_finite_voxels():
    # SPEC.md §3: NaN and -inf become the minimum finite value before the
    # blur, so a stray artifact is never the peak and never lifts or sinks its
    # neighborhood; +inf is a legitimate maximum; all-bad fails.
    volume = np.full((3, 5, 5), 10.0)
    volume[1, 2, 2] = 40.0
    volume[0, 0, 0] = np.nan
    volume[2, 4, 4] = -np.inf
    assert peak_of(volume, (0, 0, 0)) == (1, 2, 2)
    assert peak_of(volume, (2, 2, 2)) == (1, 2, 2), "the NaN's neighborhood must not outrank the blob"
    floored = blur(volume, (0, 0, 0))
    assert floored[0, 0, 0] == 10.0 and floored[2, 4, 4] == 10.0 and np.isfinite(floored).all()
    volume[0, 4, 0] = np.inf
    assert peak_of(volume, (0, 0, 0)) == (0, 4, 0), "+inf stands"
    try:
        peak_of(np.full((2, 2, 2), np.nan), (0, 0, 0))
    except ValueError as exc:
        assert "no finite voxel" in str(exc)
    else:
        raise AssertionError("an all-NaN volume was searched")
    return "NaN / -inf floored to the finite minimum; +inf stands; all-NaN refused"


def test_known_blob():
    # SPEC.md acceptance §6: a synthetic Gaussian blob places the ROI center at
    # the blob's peak voxel.
    shape = (9, 21, 23)
    centre = (3, 12, 7)
    zz, yy, xx = np.meshgrid(*[np.arange(n, dtype=np.float64) for n in shape], indexing="ij")
    blob = np.exp(
        -(
            ((zz - centre[0]) ** 2) / 4.0
            + ((yy - centre[1]) ** 2) / 18.0
            + ((xx - centre[2]) ** 2) / 18.0
        )
    )
    volume = (600.0 * blob + 40.0).astype(np.uint16)
    assert peak_of(volume, (4.0, 8.0, 8.0)) == centre, "blurred peak is the blob center"
    # A single-voxel spike brighter than anything in the blob must not win once
    # blurred at these extents; rejecting it is the whole point of blurring
    # first, and the raw argmax below shows the test would fail without it.
    spike_at = (7, 3, 19)
    spiked = volume.copy()
    spiked[spike_at] = 5000
    raw = np.unravel_index(int(np.argmax(spiked)), shape)
    assert tuple(int(i) for i in raw) == spike_at, "the unblurred maximum is the spike"
    assert peak_of(spiked, (4.0, 8.0, 8.0)) == centre, "the blur rejects a lone hot voxel"
    return f"blob at {centre} found, spike at {spike_at} rejected"


# --- ellipsoid --------------------------------------------------------------


def expected_display_positions(centre, extents):
    """The 18 display positions, written out from the geometry.

    Six axis tips `c ± a_k`, then four ring corners `c ± a_i ± a_j` for the ZY,
    ZX and YX rings — the order SPEC.md recommends.  Nothing here consults the
    builder's tables.
    """
    cz, cy, cx = centre
    ez, ey, ex = extents
    a0, a1, a2 = (ez, 0.0, 0.0), (0.0, ey, 0.0), (0.0, 0.0, ex)

    def point(*terms):
        return (
            cz + sum(term[0] for term in terms),
            cy + sum(term[1] for term in terms),
            cx + sum(term[2] for term in terms),
        )

    def neg(axis):
        return (-axis[0], -axis[1], -axis[2])

    return [
        point(a0), point(neg(a0)),
        point(a1), point(neg(a1)),
        point(a2), point(neg(a2)),
        point(a0, a1), point(neg(a0), a1), point(neg(a0), neg(a1)), point(a0, neg(a1)),
        point(a0, a2), point(neg(a0), a2), point(neg(a0), neg(a2)), point(a0, neg(a2)),
        point(a1, a2), point(neg(a1), a2), point(neg(a1), neg(a2)), point(a1, neg(a2)),
    ]


CENTRE = (3.5, 12.5, 7.5)
EXTENTS = (4.0, 8.0, 6.0)


def test_controls():
    roi = ellipsoid(CENTRE, EXTENTS)
    assert roi.type_name == "ellipsoid"
    assert roi.root == 0, "the root is the center"
    assert len(roi.vertices) == 4, (
        f"vertices holds the 4 controls and nothing else, not {len(roi.vertices)}"
    )
    expected = [
        CENTRE,
        (CENTRE[0] + EXTENTS[0], CENTRE[1], CENTRE[2]),
        (CENTRE[0], CENTRE[1] + EXTENTS[1], CENTRE[2]),
        (CENTRE[0], CENTRE[1], CENTRE[2] + EXTENTS[2]),
    ]
    for index, (vertex, want) in enumerate(zip(roi.vertices, expected)):
        got = (vertex.position.z, vertex.position.y, vertex.position.x)
        assert all(close(a, b) for a, b in zip(got, want)), f"control {index}: {got} != {want}"
    return "c, c+a0, c+a1, c+a2 in order"


def test_compile_operator():
    from peak.ellipsoid import compile_display

    roi = ellipsoid(CENTRE, EXTENTS)
    assert roi.compile.num_display == 18, roi.compile.num_display
    assert list(roi.compile.controls) == [0, 1, 2, 3]
    got = compile_display(roi)
    want = expected_display_positions(CENTRE, EXTENTS)
    assert len(got) == len(want)
    for index, (a, b) in enumerate(zip(got, want)):
        assert all(close(u, v) for u, v in zip(a, b)), f"display {index}: {a} != {b}"
    assert len({tuple(p) for p in got}) == 18, "display positions must be distinct"
    return "18 display positions reproduced from the controls"


def test_arcs():
    from peak.ellipsoid import compile_display

    roi = ellipsoid(CENTRE, EXTENTS)
    assert len(roi.edges) == 12, f"3 rings x 4 quarter arcs, not {len(roi.edges)}"
    positions = compile_display(roi)
    centre = (roi.vertices[0].position.z, roi.vertices[0].position.y, roi.vertices[0].position.x)

    rings: dict[frozenset, int] = {}
    for edge in roi.edges:
        for index in (edge.source, edge.target, edge.control):
            assert 0 <= index < 18, f"display index {index} out of range"
        assert close(edge.weight, ARC_WEIGHT), f"quarter arc weight {edge.weight}"
        assert edge.source < 6 and edge.target < 6, "an arc joins two axis tips"
        assert edge.control >= 6, "an arc's control is a ring corner"
        assert edge.source // 2 != edge.target // 2, "an arc joins two DIFFERENT axes"
        # The tangent-intersection corner between tips c+u and c+v is c+u+v,
        # which is (c+u) + (c+v) - c.  Derived from the geometry, not the table.
        want = tuple(
            positions[edge.source][k] + positions[edge.target][k] - centre[k] for k in range(3)
        )
        got = positions[edge.control]
        assert all(close(a, b) for a, b in zip(got, want)), (
            f"edge {edge.source}->{edge.target} control {got} != corner {want}"
        )
        rings.setdefault(frozenset({edge.source // 2, edge.target // 2}), 0)
        rings[frozenset({edge.source // 2, edge.target // 2})] += 1

    assert len(rings) == 3, f"three principal rings, not {len(rings)}"
    assert all(count == 4 for count in rings.values()), rings
    return "12 quarter arcs, 3 closed rings, controls at the ring corners"


def test_timeless_by_default():
    roi = ellipsoid(CENTRE, EXTENTS)
    assert not roi.HasField("t_start") and not roi.HasField("t_end"), "timeless"
    assert len(roi.pose_translation) == 0
    return "no time fields set"


def test_dense_pose():
    peaks = [(3, 12, 7), (4, 12, 9), (2, 15, 7), (3, 12, 7)]
    first = peaks[0]
    offsets = [tuple(float(a - b) for a, b in zip(p, first)) for p in peaks]
    roi = ellipsoid(_centre_of(first), EXTENTS, pose_translation=offsets)
    assert roi.t_start == 0 and roi.t_end == len(peaks) - 1
    assert roi.out_of_span == 0, "HOLD"
    assert len(roi.pose_translation) == 3 * len(peaks), "one row per frame, dense"
    assert list(roi.pose_translation[:3]) == [0.0, 0.0, 0.0], "frame 0 is the origin"
    assert list(roi.pose_translation[3:6]) == [1.0, 0.0, 2.0]
    assert list(roi.pose_translation[6:9]) == [-1.0, 3.0, 0.0]
    return f"{len(peaks)} frames, 3T doubles"


def _centre_of(index):
    return tuple(float(value) + 0.5 for value in index)


def test_voxel_centre():
    assert peak_roi._voxel_centre((0, 4, 9)) == (0.5, 4.5, 9.5)
    roi = ellipsoid(peak_roi._voxel_centre((3, 12, 7)), (1.0, 1.0, 1.0))
    assert close(roi.vertices[0].position.z, 3.5)
    return "index (z,y,x) -> center (z+.5, y+.5, x+.5)"


# --- what actually goes on the wire -----------------------------------------


def test_contribution_round_trip():
    roi = ellipsoid(CENTRE, EXTENTS, pose_translation=[(0.0, 0.0, 0.0), (1.0, -2.0, 3.0)])
    contribution = pb2.RoiContribution(
        contrib_id=peak_roi.CONTRIB_ID,
        roi=roi,
        name=peak_roi.ROI_NAME,
        style=pb2.RoiStyle(color=peak_roi.ROI_COLOR),
    )
    message = pb2.PluginMsg(
        id="q0", contribute_rois=pb2.ContributeRois(rois=[contribution], replace_all=False)
    )
    parsed = pb2.PluginMsg.FromString(message.SerializeToString())
    assert parsed == message, "PluginMsg did not survive serialize/parse"
    assert parsed.contribute_rois.rois[0].roi.type_name == "ellipsoid"
    assert len(parsed.contribute_rois.rois[0].roi.vertices) == 4
    return f"{len(message.SerializeToString())} bytes round-trip"


# --- manifest ---------------------------------------------------------------


def _widgets(node, out=None, depth=1):
    out = [] if out is None else out
    out.append((node, depth))
    for child in node.get("children", ()):
        _widgets(child, out, depth + 1)
    return out


def test_manifest():
    manifest = json.loads((HERE / "vizi-plugin.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 1
    assert manifest["id"] == peak_roi.PLUGIN_ID
    # `control_plane_protocol.md` §3.2: a MAJOR bump breaks and a minor bump
    # does not, so this declares the major it is built against.  It does not
    # run against a `0.x` host and does not try.
    assert manifest["engines"]["vizi"] == "1.0", "the major is the gate"
    assert manifest["entry"]["command"] == [".venv/bin/python", "peak_roi.py"]
    assert sorted(manifest["permissions"]) == ["contribute_rois", "data_access"]

    commands = manifest["contributions"]["commands"]
    declared = {entry["id"] for entry in commands}
    for entry in commands:
        assert "menu" in entry and entry["menu"] is None, (
            f"{entry['id']} must set menu: null — a bare menu invocation sends no args"
        )
    assert declared == set(_COMMANDS), f"{declared} != {set(_COMMANDS)}"

    panels = manifest["contributions"]["panels"]
    assert len(panels) == 1 and panels[0]["id"] == peak_roi.PANEL_ID
    assert set(_PANEL_OPENED) == {peak_roi.PANEL_ID}

    nodes = _widgets(panels[0]["root"])
    ids = [node["id"] for node, _ in nodes]
    assert len(ids) == len(set(ids)), "widget ids must be unique"
    assert len(ids) <= 256 and max(depth for _, depth in nodes) <= 8
    for node, _ in nodes:
        if "command" in node:
            assert node["command"] in declared, f"{node['id']} binds undeclared {node['command']}"

    # Every widget the plugin patches has to exist, or the patch is silently
    # dropped by the host.
    patched = set(peak_roi._EXTENT_WIDGETS.values()) | {"mode", peak_roi.STATUS_WIDGET}
    assert patched <= set(ids), f"patched widgets missing from the panel: {patched - set(ids)}"

    mode = next(node for node, _ in nodes if node["id"] == "mode")
    assert tuple(mode["options"]) == peak_roi.MODES, "dropdown options must match the model"
    assert mode["value"] in mode["options"]

    defaults = {"ez": "z", "ey": "y", "ex": "x"}
    for widget_id, axis in defaults.items():
        node = next(node for node, _ in nodes if node["id"] == widget_id)
        assert float(node["value"]) == peak_roi._EXTENTS[axis], (
            f"{widget_id} default {node['value']} != seeded state {peak_roi._EXTENTS[axis]}"
        )
        assert any(
            child["kind"] == "label"
            for parent, _ in nodes
            if parent.get("kind") == "row"
            for child in parent.get("children", ())
            if any(sibling["id"] == widget_id for sibling in parent["children"])
        ), f"{widget_id} needs a label — a placeholder vanishes once the field has a value"
    return f"{len(commands)} commands, {len(ids)} widgets"


def test_extent_parsing():
    assert peak_roi._parse_extent(" 4 ") == 4.0
    assert peak_roi._parse_extent("2.5") == 2.5
    for bad in ("", "x", "0", "-1", "nan", "inf"):
        try:
            peak_roi._parse_extent(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should not parse as an extent")
    return "positive finite numbers only"


check("kernel shape", test_kernel_shape)
check("kernel radius", test_kernel_radius)
check("zero sigma", test_no_blur_at_zero_sigma)
check("constant volume", test_constant_volume_survives)
check("edge clamping", test_edge_clamping)
check("blur vs reference", test_matches_reference_blur)
check("integer promotion", test_integer_input_promotes)
check("peak tie-break", test_tie_break)
check("non-finite voxels", test_non_finite_voxels)
check("known blob", test_known_blob)
check("ellipsoid controls", test_controls)
check("compile operator", test_compile_operator)
check("quarter arcs", test_arcs)
check("timeless default", test_timeless_by_default)
check("dense pose", test_dense_pose)
check("voxel center", test_voxel_centre)
check("contribution round-trip", test_contribution_round_trip)
check("manifest", test_manifest)
check("extent parsing", test_extent_parsing)

if _failures:
    print(f"\n{len(_failures)} check(s) failed: {', '.join(_failures)}", file=sys.stderr)
    raise SystemExit(1)
print("\nself-test passed")
