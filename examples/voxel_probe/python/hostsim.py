#!/usr/bin/env python3
"""Drive the voxel-probe plugin end to end over the real transport, with no vizi.

    .venv/bin/python hostsim.py

This is the fast path's emulator run: what
``examples/peak_roi/python/hostsim.py`` cannot do because that plugin has no
tool and no overlay.  It grants **two `O` segments and a `P` segment** as the
`1.0` host lays them out, runs the arrangement
sequence a session needs before any input (`ToolBinding`, `TimeMapping`, one
`EpochChange` per pane), composes `E` records per `doorbell_E_input.md`, reads
the plugin's `D` hints off the doorbell and the Pulsed lane they point at
(`shared_memory_protocol.md` §6.5), applies §3.2's pane rule and §3.3's two
rules as the receiver, reads every loan a control-plane push names out of the
plugin's arena and releases it, and takes a PNG out of the panel arena.

The emulator is ``examples/vizi_hostsim``; what is here is this fixture's
scenarios.  The plugin is the developer guide's tool sample plus a trail
overlay and a panel image, so a scenario here is also a check on the guide.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # examples/, for vizi_hostsim

from vizi_hostsim import (  # noqa: E402
    SHUTDOWN_WINDOW_S,
    VIEW_2D,
    VIEW_3D,
    Dataset,
    Host,
    Ray,
    RayAndPicked,
    Segment,
    Voxel,
    check,
    check_encoder,
    failures,
    segment_id,
)
from vizi_plugin_sdk import pb2  # noqa: E402

SHAPE_TCZYX = (3, 2, 16, 32, 40)
OPEN_GENERATION = (5 << 32) | 9  # more than 32 bits: `E.gen` carries the low ones
MARKER_SEG = segment_id(1)
TRAIL_SEG = segment_id(2)
PANEL_SEG = segment_id(3)
LANE_SLOT = 16 << 10


def flat_volume(t: int, c: int) -> np.ndarray:
    _, _, depth, height, width = SHAPE_TCZYX
    return np.full((depth, height, width), 100 * (t + 1) + c, dtype=np.uint16)


DATASET = Dataset(
    shape_tczyx=SHAPE_TCZYX,
    np_dtype=np.dtype(np.uint16),
    wire_dtype=pb2.DTYPE_U16,
    open_generation=OPEN_GENERATION,
    volume=flat_volume,
    t=2,
    z=7,
    c=1,
    dataset_uri="hostsim://flat",
)


def positions_of(buf: pb2.GeomBuffer) -> np.ndarray:
    """An inline (N, 3) f64 GeomBuffer as an array."""
    assert buf.WhichOneof("source") == "inline", buf.WhichOneof("source")
    assert buf.dtype == pb2.DTYPE_F64, buf.dtype
    return np.frombuffer(buf.inline, dtype="<f8").reshape(tuple(buf.shape))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-dir", type=Path, default=HERE)
    plugin_dir = parser.parse_args().plugin_dir

    check("record encoder", check_encoder)
    if failures:
        print("\nthe encoder disagrees with the corpus; nothing else was run", file=sys.stderr)
        return 1

    host = Host(plugin_dir, DATASET)
    host.add(Segment.overlay(MARKER_SEG, "probe_marker", 1 << 20, host.directory, slot_bytes=LANE_SLOT))
    host.add(Segment.overlay(TRAIL_SEG, "probe_trail", 4 << 20, host.directory, slot_bytes=LANE_SLOT))
    host.add(Segment.plugin_arena(PANEL_SEG, "P", 1 << 20, host.directory, subject="probe"))

    with host:
        state = {"tool": 0, "epoch": 0}

        def test_handshake():
            return host.check_handshake()

        def test_grants():
            """Two `O` segments told apart by `subject`, and a `P` for the
            panel: each acknowledged with the proof read out of its mapping."""
            for seg in (MARKER_SEG, TRAIL_SEG, PANEL_SEG):
                ack = host.grant(seg)
                assert ack.WhichOneof("outcome") == "installed", (seg, ack)
                assert ack.installed.identity_proof == host.segments[seg].proof
            return "O(probe_marker), O(probe_trail), P(probe) installed"

        def test_arrangements():
            """The arrangement sequence: `ToolBinding`, `TimeMapping`,
            one `EpochChange` per pane, each acknowledged."""
            # A modal tool: no trigger (engaged from the menu), and one
            # declared action the host kept a key for.
            state["tool"] = host.arrange("probe", actions=[("mark", "M")])
            state["epoch"] = host.epochs[VIEW_2D]
            assert state["tool"] == 1 and host.tool_acks[1] == 1
            assert host.mapping_acks[OPEN_GENERATION] == 1
            assert host.epoch_acks == {VIEW_2D: 1, VIEW_3D: 1}, host.epoch_acks
            return f"tool handle {state['tool']}, mapping rev 1, panes {sorted(host.epochs)} at epoch 1"

        def test_panel_opened():
            host.open_panel("probe")
            status = host.await_status("probe", "gestures so far")
            assert "0 gestures so far" in status, status
            return status

        def test_press_publishes_live():
            """A `PRESS` in 2D: the plugin publishes into the marker lane and
            rings `D`; the host reads the newest value, decodes a bare
            `OverlayUpdate` with inline geometry at the voxel, stamped with
            the pane pair the record carried."""
            gesture = host.press(state["tool"], VIEW_2D, Voxel(7.0, 10.0, 12.0))
            state["gesture"] = gesture
            live = host.await_live(1)
            assert live, "no live value was published"
            value = live[-1]
            assert value.segment == MARKER_SEG and value.region == 0, (value.segment, value.region)
            assert value.applied, value.reason
            assert value.pane == (VIEW_2D, state["epoch"]), value.pane
            assert value.update.overlay_id == "probe_marker"
            assert len(value.update.items) == 1
            item = value.update.items[0]
            assert item.WhichOneof("geometry") == "marker", item
            pos = positions_of(item.marker.positions_zyx)
            assert pos.shape == (1, 3) and np.array_equal(pos[0], (7.0, 10.0, 12.0)), pos
            hints = [h for h in host.hints if h.segment == MARKER_SEG]
            assert hints and hints[-1].regions == {0} and hints[-1].pane == value.pane, hints
            return f"lane revision {value.revision}, pane {value.pane}, marker at {tuple(float(v) for v in pos[0])}"

        def test_drags_coalesce_or_arrive():
            """Every `DRAG` publishes; a publish that finds every buffer busy
            is coalesced, not failed, so what the host must see is the NEWEST
            value, not every one."""
            gesture = state["gesture"]
            before = len(host.live)
            for x in (13.0, 14.0, 15.0):
                gesture.drag(Voxel(7.0, 10.0, x))
                time.sleep(0.02)
            # The newest value is what must arrive; the intermediate ones may
            # have been coalesced away.  Scan until it does.
            deadline = time.monotonic() + 5.0
            newest = []
            while time.monotonic() < deadline:
                host.scan(MARKER_SEG)
                newest = [v for v in host.live[before:] if v.segment == MARKER_SEG]
                if newest and positions_of(newest[-1].update.items[0].marker.positions_zyx)[0][2] == 15.0:
                    break
                time.sleep(0.02)
            assert newest, "no drag reached the lane"
            pos = positions_of(newest[-1].update.items[0].marker.positions_zyx)
            assert pos[0][2] == 15.0, pos
            assert all(v.applied for v in newest), [v.reason for v in newest]
            return f"{len(newest)} value(s) consumed of 3 drags; newest at x={pos[0][2]}"

        def test_release_pushes_two_loans():
            """`RELEASE`: the marker persists as a control-plane push with
            segment-backed geometry, and the trail arrives on the second
            overlay -- two loans out of two arenas, each read and released."""
            gesture = state["gesture"]
            gesture.release(Voxel(7.0, 10.0, 16.0))
            pushes = host.await_pushes(2, kind="update")
            by_id = {p.overlay_id: p for p in pushes}
            assert {"probe_marker", "probe_trail"} <= set(by_id), sorted(by_id)
            marker = by_id["probe_marker"]
            assert len(marker.loans) == 1 and marker.loans[0].segment == MARKER_SEG, marker.loans
            assert marker.loans[0].region == 1, "the marker's geometry comes from the O arena, region 1"
            got = np.frombuffer(marker.loans[0].payload, dtype="<f8").reshape(-1, 3)
            assert np.array_equal(got, [[7.0, 10.0, 16.0]]), got
            trail = by_id["probe_trail"]
            assert len(trail.loans) == 1 and trail.loans[0].segment == TRAIL_SEG, trail.loans
            path = np.frombuffer(trail.loans[0].payload, dtype="<f8").reshape(-1, 3)
            assert path.shape == (5, 3) and path[0][2] == 12.0 and path[-1][2] == 16.0, path
            assert trail.update.items[0].WhichOneof("geometry") == "line_set"
            released = {(s, r) for s, r, _, _ in host.loans_released}
            assert {(MARKER_SEG, 1), (TRAIL_SEG, 1)} <= released, released
            # The overlay revision is monotonic ACROSS lanes (§3.3): the
            # control-plane push comes after every live value's.
            lane_revs = [v.update.revision for v in host.live if v.segment == MARKER_SEG]
            assert marker.revision > max(lane_revs), (marker.revision, lane_revs)
            return f"marker loan {len(marker.loans[0].payload)} B, trail loan {len(trail.loans[0].payload)} B ({path.shape[0]} points), both released"

        def test_miss_and_3d_are_ignored():
            """A `MISS` is a real coordinate in a place the tool does not want,
            and a 3D pane resolves a ray: the guide's sample drops both, so
            nothing is published and nothing is pushed."""
            live_before, pushes_before = len(host.live), len(host.pushes)
            g = host.press(state["tool"], VIEW_2D, Voxel(7.0, -1.0, -1.0), miss=True)
            g.release(Voxel(7.0, -1.0, -1.0), miss=True)
            ray = Ray(Voxel(-10.0, 16.0, 20.0), Voxel(30.0, 16.0, 20.0), 0.25)
            g3 = host.press(state["tool"], VIEW_3D, RayAndPicked(ray, Voxel(3.0, 16.0, 20.0), back_frac=0.5))
            g3.drag(ray, miss=True)
            g3.release(ray)
            time.sleep(0.3)
            host.scan()
            assert len(host.live) == live_before, host.live[live_before:]
            assert len(host.pushes) == pushes_before, host.pushes[pushes_before:]
            return "MISS press/release and a 3D RAY_AND_PICKED gesture: nothing published, nothing pushed"

        def test_cancel_clears():
            """A gesture the host ends with `CANCEL` (a camera move, a pane
            change): the tool retracts the marker and pushes no trail."""
            pushes_before = len(host.pushes)
            g = host.press(state["tool"], VIEW_2D, Voxel(2.0, 3.0, 4.0))
            g.drag(Voxel(2.0, 3.0, 5.0))
            host.await_live(len(host.live) + 1)
            g.cancel()
            removes = host.await_pushes(1, kind="remove")
            assert removes and removes[-1].overlay_id == "probe_marker", removes
            assert not [p for p in host.pushes[pushes_before:] if p.kind == "update"], "a cancelled gesture pushed"
            return "OverlayRemove on probe_marker, no trail"

        def test_stale_pane_is_not_drawn():
            """§3.2: a value carrying a pane pair
            is applied only while that pair is the pane's current epoch.
            Publish under epoch 1, advance the pane to epoch 2 before the host
            looks, and the value is consumed and counted, not drawn."""
            host.scan_on_hint = False
            try:
                g = host.press(state["tool"], VIEW_2D, Voxel(1.0, 1.0, 1.0))
                time.sleep(0.2)  # published, hinted, not yet scanned
                new_epoch = host.arrange_pane(VIEW_2D)
                assert new_epoch == 2, new_epoch
                found = host.scan(MARKER_SEG)
            finally:
                host.scan_on_hint = True
            assert found and found[-1].pane == (VIEW_2D, 1) and not found[-1].applied, found
            assert found[-1].reason == "stale_pane", found[-1].reason
            assert host.segments[MARKER_SEG].lane.stale_pairs == 1
            g.cancel()  # under the old epoch; the SDK ignores a stale record
            state["epoch"] = new_epoch
            return "value stamped (0, 1) read after the pane moved to epoch 2: consumed, not applied"

        def test_gesture_under_new_epoch():
            g = host.press(state["tool"], VIEW_2D, Voxel(8.0, 8.0, 8.0))
            live = host.await_live(len(host.live) + 1)
            assert live[-1].applied and live[-1].pane == (VIEW_2D, 2), (live[-1].applied, live[-1].pane)
            g.release(Voxel(8.0, 8.0, 9.0))
            host.await_pushes(len([p for p in host.pushes if p.kind == "update"]) + 2, kind="update")
            return f"applied under epoch {live[-1].pane[1]}"

        def test_snapshot_pushes_a_png():
            """`push_image`: the PNG goes through the panel's `P` arena as a
            loan, the patch carries only the reference, the host reads the
            bytes and releases them."""
            done = host.invoke("probe.snapshot")
            assert done.ok, done.message
            assert "snapshot pushed" in done.message, done.message
            deadline = time.monotonic() + 5.0
            while not host.images and time.monotonic() < deadline:
                time.sleep(0.02)
            assert host.images, "no set_image arrived"
            panel, widget, loan = host.images[-1]
            assert (panel, widget) == ("probe", "snapshot"), (panel, widget)
            assert loan.segment == PANEL_SEG and loan.region == 0, (loan.segment, loan.region)
            assert loan.payload[:8] == b"\x89PNG\r\n\x1a\n", loan.payload[:8]
            width, height = struct.unpack(">II", loan.payload[16:24])
            assert (width, height) == (64, 64)
            assert (PANEL_SEG, 0, loan.offset, loan.length) in host.loans_released
            status = host.await_status("probe", "Snapshot of")
            assert "Snapshot of 2 points" in status, status
            return f"{loan.length}-byte PNG {width}x{height} out of the P arena, released; {status!r}"

        def test_action_fire_lands_inside_the_open_gesture():
            """`doorbell_E_input.md` §1: a `FIRE` whose `action`
            names one of the tool's declared actions lands inside whatever
            gesture is open and touches its state not at all -- the drag that
            follows is still the same gesture and still publishes live.  A
            receiver that faults on a non-zero byte at [42..44) is wrong."""
            violations_before = len(host.violations)
            g = host.press(state["tool"], VIEW_2D, Voxel(4.0, 5.0, 6.0))
            live = host.await_live(len(host.live) + 1)
            fired = host.fire(state["tool"], VIEW_2D, Voxel(4.0, 5.0, 6.0), action=1)
            g.drag(Voxel(4.0, 5.0, 7.0))
            live = host.await_live(len(live) + 1)
            assert live[-1].applied and live[-1].pane == (VIEW_2D, state["epoch"]), live[-1]
            pos = positions_of(live[-1].update.items[0].marker.positions_zyx)
            assert np.array_equal(pos[-1], (4.0, 5.0, 7.0)), pos
            pushes_before = len([p for p in host.pushes if p.kind == "update"])
            g.release(Voxel(4.0, 5.0, 8.0))
            host.await_pushes(pushes_before + 1, kind="update")
            assert len(host.violations) == violations_before, [(pb2.ViolationSite.Name(v.where), v.detail) for v in host.violations[violations_before:]]
            assert host.final_report is None, "the action FIRE faulted the doorbell"
            return f"FIRE action=1 (interaction {fired}) delivered mid-gesture; the drag and release completed the same gesture; no violation, no fault"

        def test_unusable_context_is_reported_and_delivered():
            """`doorbell_E_input.md` §3's third terminal row: a
            record whose `level` the epoch's `Geometry.levels` does not
            declare cannot be reconciled with arranged state.  It is still
            delivered and the interaction closes; the supervisor marks it
            unusable locally and reports `unusable_context` (site 27).  A
            plain gesture afterwards behaves exactly as before."""
            before = len(host.violations)
            g = host.press(state["tool"], VIEW_2D, Voxel(2.0, 3.0, 4.0), level=3)
            g.release(Voxel(2.0, 3.0, 4.0), level=3)
            reported = host.await_violations("unusable_context")
            assert len(reported) >= 1, reported
            assert "level 3" in reported[-1].detail, reported[-1].detail
            unexpected = [v for v in host.violations[before:] if v.where != reported[-1].where]
            assert not unexpected, [(pb2.ViolationSite.Name(v.where), v.detail) for v in unexpected]
            # the terminal closed: a fresh gesture on the same pane is live again
            live_before = len(host.live)
            g2 = host.press(state["tool"], VIEW_2D, Voxel(2.0, 3.0, 5.0))
            live = host.await_live(live_before + 1)
            assert live[-1].applied and live[-1].pane == (VIEW_2D, state["epoch"]), live[-1]
            removes_before = len([p for p in host.pushes if p.kind == "remove"])
            g2.cancel()
            host.await_pushes(removes_before + 1, kind="remove")
            host.expected_violations = {reported[-1].where}
            return f"level 3 record delivered, {pb2.ViolationSite.Name(reported[-1].where)} reported ({reported[-1].count}x), next gesture live"

        def test_no_violations():
            expected = getattr(host, "expected_violations", set())
            unexpected = [v for v in host.violations if v.where not in expected]
            assert not unexpected, [(pb2.ViolationSite.Name(v.where), v.detail) for v in unexpected]
            assert not host.unhandled, host.unhandled
            quarantined = [s.lane.quarantined for s in host.segments.values() if s.purpose == "O" and s.lane.quarantined]
            assert not quarantined, quarantined
            return "no ContractViolation, no undecodable record, no quarantined lane"

        def test_teardown():
            closed = host.shutdown()
            assert closed, f"the stream did not end within {SHUTDOWN_WINDOW_S:g} s"
            assert host.stream_error is None, host.stream_error
            report = host.shutdown_report
            assert report is not None and report.outcome == pb2.ShutdownReport.NO_HOOK, report
            return "ShutdownReport NO_HOOK, then a clean end of stream"

        check("handshake", test_handshake)
        if failures:
            print("\nthe handshake failed; nothing else was run", file=sys.stderr)
            return 1
        check("grants: two O and a P", test_grants)
        check("arrangements", test_arrangements)
        check("panel opened", test_panel_opened)
        check("press -> live lane", test_press_publishes_live)
        check("drags coalesce", test_drags_coalesce_or_arrive)
        check("release -> two loans", test_release_pushes_two_loans)
        check("miss and 3D ignored", test_miss_and_3d_are_ignored)
        check("cancel clears", test_cancel_clears)
        check("stale pane not drawn", test_stale_pane_is_not_drawn)
        check("gesture under the new epoch", test_gesture_under_new_epoch)
        check("snapshot -> P arena", test_snapshot_pushes_a_png)
        check("action FIRE inside the open gesture", test_action_fire_lands_inside_the_open_gesture)
        check("unusable context: delivered, reported", test_unusable_context_is_reported_and_delivered)
        check("no violations", test_no_violations)
        check("teardown", test_teardown)

    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("\nhost emulation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
