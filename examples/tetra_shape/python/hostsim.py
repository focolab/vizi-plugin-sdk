#!/usr/bin/env python3
"""Drive the tetra_shape example over the real transport, with no vizi.

    .venv/bin/python hostsim.py                       # this implementation
    .venv/bin/python hostsim.py --plugin-dir ../rust  # any other implementation

The scenarios are ``../SPEC.md``'s acceptance list (§5): four contributed
tetrahedra, two declared regions the host enacts, an asked one whose shaper
keeps the volume, and an asked type with no shaper.  Every expected value is
computed here from the SPEC, never by the plugin's code; the asked shapes are
checked by their properties (the volume, the corner at the projected point,
the face scaled about its centroid), so any correct shaper passes.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # examples/, for vizi_hostsim

from vizi_hostsim import Dataset, Host, check, failures, regions  # noqa: E402
from vizi_plugin_sdk import pb2, roi_pb2  # noqa: E402

PLUGIN_ID = "org.example.tetra_shape"
CORNERS = np.array([[10, 10, 10], [10, 30, 12], [28, 18, 14], [16, 20, 32]], dtype=np.float64)
CENTRE = CORNERS.mean(axis=0)
OFFSETS = {"free": (0, 0, 0), "scale": (0, 0, 40), "volume": (0, 0, 80), "unshaped": (0, 0, 120)}
TYPES = {"free": "tetra.free", "scale": "tetra.scale", "volume": "tetra.volume", "unshaped": "tetra.unshaped"}
NAMES = {"free": "Free corners", "scale": "Scale", "volume": "Keeps its volume", "unshaped": "No shaper"}
EPS = 1e-9

DATASET = Dataset(
    shape_tczyx=(1, 1, 40, 40, 160),
    np_dtype=np.dtype(np.uint16),
    wire_dtype=pb2.DTYPE_U16,
    open_generation=3,
    volume=lambda t, c: np.zeros((40, 40, 160), dtype=np.uint16),
    t=0,
    visible_channels=(0,),
)


def rest(name: str) -> np.ndarray:
    """SPEC §2: the five rest positions of one region."""
    return np.vstack([CORNERS, CENTRE]) + np.asarray(OFFSETS[name], dtype=np.float64)


def volume(p: np.ndarray) -> float:
    return abs(float(np.linalg.det(np.array([p[1] - p[0], p[2] - p[0], p[3] - p[0]])))) / 6


def scale_m(i: int, j: int) -> np.ndarray:
    r = np.linalg.norm(CORNERS[i] - CENTRE)
    return np.outer(CORNERS[j] - CENTRE, (CORNERS[i] - CENTRE) / r) / r


def kept_volume(before: np.ndarray, after: np.ndarray, h: int, p1: np.ndarray) -> None:
    """SPEC §3's shape, by its properties: the corner at ``p1``, each face
    corner on its line from the face's centroid by one common factor, the
    center unchanged, and the volume kept."""
    face = [j for j in range(4) if j != h]
    g = before[face].mean(axis=0)
    assert np.allclose(after[h], p1, atol=EPS), ("the dragged corner is at the projected point", after[h], p1)
    assert np.allclose(after[4], before[4], atol=EPS), "the center stays"
    ks = []
    for j in face:
        d0, d1 = before[j] - g, after[j] - g
        k = float(np.dot(d1, d0) / np.dot(d0, d0))
        assert np.allclose(d1, k * d0, atol=1e-7), (f"face corner {j} stays on its line from the face centroid", d0, d1)
        ks.append(k)
    assert max(ks) - min(ks) < 1e-7, ("one factor for the face", ks)
    assert abs(volume(after) - volume(before)) <= 1e-9 * volume(before), ("the volume is kept", volume(before), volume(after))


def wait_for(pred, what: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = pred()
        if got:
            return got
        time.sleep(0.02)
    raise AssertionError(f"{what} within {timeout:g}s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-dir", type=Path, default=HERE)
    args = parser.parse_args()
    host = Host(args.plugin_dir.resolve(), DATASET)
    ids: dict[str, str] = {}

    def record(name: str):
        return host.rois[ids[name]]

    def now(name: str) -> np.ndarray:
        return regions.positions(record(name).roi)

    opened: list = []

    def begin(name: str, corner: int):
        drag = host.begin_shape_drag(ids[name], corner)
        opened.append(drag)
        return drag

    def settled(fn):
        """A check that leaves no drag open when it fails, so the next
        check fails, if at all, for its own reason."""
        def run():
            try:
                return fn()
            finally:
                for drag in opened:
                    if drag.ended is None:
                        drag.cancel()
                opened.clear()
        return run

    def ray_point(name: str, corner: int, factor: float, off_axis=(0.0, 0.0, 0.0)) -> np.ndarray:
        p = now(name)
        return p[4] + factor * (p[corner] - p[4]) + np.asarray(off_axis, dtype=np.float64)

    with host:
        wait_for(lambda: host.ready, "Ready")

        def test_launch():
            assert host.region_types == {"tetra.volume", "tetra.unshaped"}, host.region_types
            return f"region_types {sorted(host.region_types)}"

        check("1. launch: Ready; the host read the two region types", test_launch)

        def contribute() -> None:
            done = host.invoke("tetra.contribute")
            assert done.ok, done.message
            mine = {r.contrib_id: rid for rid, r in host.rois.items() if r.origin == f"plugin:{PLUGIN_ID}"}
            assert set(mine) == set(OFFSETS), sorted(mine)
            ids.update(mine)

        def test_contribute():
            contribute()
            assert not host.refusals, host.refusals
            for name in OFFSETS:
                owner = record(name).type_owner
                want = PLUGIN_ID if name in ("volume", "unshaped") else ""
                assert owner == want, (name, owner)
                assert record(name).name == NAMES[name], (name, record(name).name)
            return "four regions; type_owner on volume and unshaped only"

        check("2. contribute: four regions, nothing refused, type_owner on the asked two", test_contribute)

        def test_geometry():
            for name in OFFSETS:
                roi = record(name).roi
                assert roi.type_name == TYPES[name], (name, roi.type_name)
                assert roi.root == 4 and len(roi.vertices) == 5, name
                assert np.allclose(regions.positions(roi), rest(name), atol=EPS), name
                assert not roi.vertices[4].HasField("handle") and not roi.vertices[4].enclosing, (name, "the center")
                assert not roi.edges and not roi.facets and not roi.patches, name
                for i in range(4):
                    v = roi.vertices[i]
                    assert v.enclosing and v.HasField("handle"), (name, i)
                    assert list(v.handle.frame) == [1, 0, 0, 0, 1, 0, 0, 0, 1], (name, i, list(v.handle.frame))
                    if name in ("free", "unshaped"):
                        assert v.handle.mode == roi_pb2.ALL and v.handle.dof_mask == 7, (name, i)
                    else:
                        assert v.handle.mode == roi_pb2.PRIMARY and v.handle.axis_source == roi_pb2.AXIS_RADIAL and v.handle.extent_anchor == 4, (name, i)
                        want_min = name == "scale"
                        assert v.handle.HasField("min_extent") == want_min, (name, i, "min_extent")
                        if want_min:
                            assert v.handle.min_extent == 1.0, (name, i)
                assert roi.auto_surface == (roi_pb2.AUTO_SURFACE_STAR if name == "scale" else roi_pb2.AUTO_SURFACE_HULL), name
                if name != "scale":
                    assert not roi.bindings, name
            binds = record("scale").roi.bindings
            order = [(i, j) for i in range(4) for j in range(4) if j != i]
            assert [(b.target, b.moved) for b in binds] == order, [(b.target, b.moved) for b in binds]
            for b, (i, j) in zip(binds, order):
                assert np.allclose(np.asarray(b.m).reshape(3, 3), scale_m(i, j), atol=EPS), (i, j)
            return "positions, handles, root, auto_surface and the 12 scale bindings"

        check("3. the geometry is SPEC §2's", test_geometry)

        def test_free():
            before = now("free")
            target = before[2] + np.array([3.0, -2.0, 5.0])
            assert host.drag_handle(ids["free"], 2, target) == roi_pb2.OK
            after = now("free")
            assert np.allclose(after[2], target, atol=EPS), after[2]
            assert np.allclose(np.delete(after, 2, axis=0), np.delete(before, 2, axis=0), atol=EPS), "only corner 2 moves"
            return "corner 2 moved by (3, -2, 5), nothing else"

        check("4. free: the host moves one corner", test_free)

        def test_scale():
            before = now("scale")
            # 3 voxels off the ray, PERPENDICULAR to it, so the projection lands
            # at exactly 1.5x.
            ray = before[1] - before[4]
            side = np.cross(ray, (1.0, 0.0, 0.0))
            side *= 3.0 / np.linalg.norm(side)
            target = ray_point("scale", 1, 1.5, off_axis=side)
            assert host.drag_handle(ids["scale"], 1, target) == roi_pb2.OK
            after = now("scale")
            factor = np.linalg.norm(after[1] - after[4]) / np.linalg.norm(before[1] - before[4])
            assert abs(factor - 1.5) < 1e-9, ("the pointer projects to 1.5x", factor)
            assert np.allclose(after[4], before[4], atol=EPS), "the center stays"
            assert np.allclose(after[:4] - after[4], factor * (before[:4] - before[4]), atol=1e-7), ("uniform scale", factor)
            return f"every corner scaled by {factor:.4f} about the center (a pointer 3 voxels off the ray, projected onto it)"

        check("5. scale: the bindings scale the whole tetrahedron", test_scale)

        def test_clamp():
            target = ray_point("scale", 0, -2.0)
            assert host.drag_handle(ids["scale"], 0, target) == roi_pb2.CLAMPED
            after = now("scale")
            distance = float(np.linalg.norm(after[0] - after[4]))
            assert abs(distance - 1.0) < 1e-7, distance
            return "CLAMPED, corner 0 at 1.0 from the center"

        check("6. scale clamp: min_extent holds a corner off the center", test_clamp)

        def test_volume():
            start = now("volume")
            drag = begin("volume", 3)
            for factor in (1.1, 1.2, 1.3, 1.4, 1.5):
                p1 = drag.move(start[4] + factor * (start[3] - start[4]))
                shown = drag.await_proposal()
                kept_volume(start, shown, 3, p1)
            ended = drag.release()
            assert ended.outcome == pb2.ShapeEditEnded.COMMITTED, ended
            kept_volume(start, now("volume"), 3, drag.sent[drag.seq])
            assert len(host.shape_answers) == 1, host.shape_answers
            return f"5 proposals and the commit keep the volume {volume(start):.3f}"

        check("7. volume: an asked drag keeps the volume, and the answer is the commit", settled(test_volume))

        def test_rejected():
            start = now("volume")
            drag = begin("volume", 0)
            g = start[1:4].mean(axis=0)
            n = np.cross(start[2] - start[1], start[3] - start[1])
            n /= np.linalg.norm(n)
            s0 = float(n @ (start[0] - g))
            # The point on corner 0's ray whose height over the face is s0/32.
            ray = start[0] - start[4]
            t = (s0 / 32 - float(n @ (start[4] - g))) / float(n @ ray)
            drag.move(start[4] + t * ray)
            wait_for(lambda: any(r.drag_id == drag.drag_id for r in host.shape_proposals), "a proposal for the drag")
            last = [r for r in host.shape_proposals if r.drag_id == drag.drag_id][-1]
            assert last.resolution == roi_pb2.REJECTED and not last.positions, last
            assert drag.on_screen_seq == 0 and np.allclose(drag.on_screen, start), "the shape on screen stays"
            ended = drag.release()
            assert ended.outcome == pb2.ShapeEditEnded.REVERTED and ended.detail, ended
            assert np.allclose(now("volume"), start, atol=EPS), "the region is unchanged"
            return f"REJECTED, then REVERTED: {ended.detail!r}"

        check("8. volume, rejected: too close to the face is refused and reverted", settled(test_rejected))

        def test_spinbox():
            start = now("volume")
            before = len(host.shape_proposals)
            drag, ended = host.spinbox_edit(ids["volume"], 2, start[4] + 1.2 * (start[2] - start[4]))
            assert ended.outcome == pb2.ShapeEditEnded.COMMITTED, ended
            assert drag.seq == 0 and len(host.shape_proposals) == before, "a commit with no drag before it"
            kept_volume(start, now("volume"), 2, drag.p0 + 0.2 * (drag.p0 - start[4]))
            return "COMMITTED with no shape_drag; volume kept"

        check("9. spinbox: a commit with no drag", settled(test_spinbox))

        def test_cancel():
            start = now("volume")
            drag = begin("volume", 1)
            for factor in (1.1, 1.2):
                drag.move(start[4] + factor * (start[1] - start[4]))
            drag.await_proposal()
            ended = drag.cancel()
            assert ended.outcome == pb2.ShapeEditEnded.CANCELLED, ended
            assert np.allclose(now("volume"), start, atol=EPS), "the region is unchanged"
            return "CANCELLED, region unchanged"

        check("10. cancel: the person cancels a drag", settled(test_cancel))

        def test_drops():
            start = now("volume")
            drag = begin("volume", 3)
            drag.move(start[4] + 1.1 * (start[3] - start[4]))
            drag.await_proposal()
            assert drag.release().outcome == pb2.ShapeEditEnded.COMMITTED
            drag.send_late_drag(start[4] + 1.3 * (start[3] - start[4]))
            unseen = begin("volume", 0)
            assert unseen.cancel().outcome == pb2.ShapeEditEnded.CANCELLED
            time.sleep(0.3)
            done = host.invoke("tetra.contribute")
            assert done.ok, done.message
            fresh = begin("volume", 3)
            fresh.move(rest("volume")[4] + 1.2 * (rest("volume")[3] - rest("volume")[4]))
            fresh.await_proposal()
            assert fresh.release().outcome == pb2.ShapeEditEnded.COMMITTED
            assert not host.violations and not host.refusals, (host.violations, host.refusals)
            return "a late shape_drag and an unseen drag's end: nothing reported; a command and a new drag answered after"

        check("11. silent drops: a late drag and an unseen drag's end", settled(test_drops))

        def test_unshaped():
            start = now("unshaped")
            drag = begin("unshaped", 0)
            drag.move(start[0] + np.array([1.0, 1.0, 1.0]))
            ended = drag.release(timeout=10.0)
            assert ended.outcome == pb2.ShapeEditEnded.REVERTED and ended.detail, ended
            assert np.allclose(now("unshaped"), start, atol=EPS)
            return f"REVERTED: {ended.detail!r}"

        check("12. no shaper: the commit is answered REJECTED", settled(test_unshaped))

        def test_again():
            done = host.invoke("tetra.contribute")
            assert done.ok, done.message
            for name in OFFSETS:
                assert np.allclose(now(name), rest(name), atol=EPS), name
            return "every region back at rest"

        check("13. again: tetra.contribute restores the rest shapes", test_again)

        def test_clean():
            assert not host.refusals, host.refusals
            assert not host.violations, host.violations
            assert not host.unhandled, host.unhandled
            return "no refusal, no violation, nothing unhandled"

        check("14. nothing refused", test_clean)

    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("\nhost emulation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
