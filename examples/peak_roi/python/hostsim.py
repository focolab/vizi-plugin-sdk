#!/usr/bin/env python3
"""Drive the peak-ROI plugin end to end over the real transport, with no vizi.

    .venv/bin/python hostsim.py                       # this implementation
    .venv/bin/python hostsim.py --plugin-dir ../rust  # any other implementation

``selftest.py`` checks the parts that are pure computation.  This checks the
parts that are a protocol: it launches the plugin exactly as the Broker would
— the manifest's ``entry.command`` in the plugin's own directory, gRPC on a
Unix domain socket, an inherited `SOCK_SEQPACKET` doorbell, a segment granted
over `SCM_RIGHTS` — then plays host for a few scenarios and asserts on what
comes back.

**It drives any implementation of ``SPEC.md``, not only this one.**  The
scenarios are the specification's behavior, so ``--plugin-dir ../<lang>`` is
the same conformance run against that implementation: what it reads is its
``vizi-plugin.json``, and what it launches is that manifest's command.

The emulator itself is ``examples/vizi_hostsim`` -- the launch, the two
channels, the record encoders checked against the cross-check corpus, the
grants, the arrangements and the lane reader -- shared with every example's
``hostsim.py``.  What is here is this example's dataset and scenarios.  Its
segment carries purpose **`V`** — `shared_memory_protocol.md` §3's
host-written volume data a plugin asked for — and the producer column is the
load-bearing half of that row, since `G` carries the same voxels the other way
and a receiver reading the two as one gets the release message backwards.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))  # examples/, for vizi_hostsim

from peak.analysis import peak_of  # noqa: E402
from peak.ellipsoid import compile_display  # noqa: E402
from vizi_hostsim import (  # noqa: E402
    SHUTDOWN_WINDOW_S,
    Dataset,
    Host,
    Segment,
    check,
    check_encoder,
    failures,
    segment_id,
)
from vizi_plugin_sdk import pb2  # noqa: E402

#: §9's bound on each ROI pose array (`Roi.pose_*`), in frames: a `RoiList`
#: carrying one ROI at this bound is 24 MiB, the case the 32 MiB ceiling is
#: sized for.
MAX_POSE_FRAMES = 1 << 20
SHAPE_TCZYX = (4, 2, 9, 21, 23)
CHANNEL = 1
OPEN_GENERATION = 77
EXTENTS = (4.0, 8.0, 8.0)
SEGMENT_A = segment_id(1)
SEGMENT_B = segment_id(2)
SEGMENT_C = segment_id(3)
SEGMENT_BYTES = 8 << 20


# --- the dataset the emulated host serves -----------------------------------


def blob_volume(t: int, c: int) -> np.ndarray:
    """A Gaussian blob that walks with ``t``, on a channel-dependent floor."""
    _, _, depth, height, width = SHAPE_TCZYX
    centre = peak_for(t)
    zz, yy, xx = np.meshgrid(
        np.arange(depth, dtype=np.float64),
        np.arange(height, dtype=np.float64),
        np.arange(width, dtype=np.float64),
        indexing="ij",
    )
    blob = np.exp(
        -(
            ((zz - centre[0]) ** 2) / 4.0
            + ((yy - centre[1]) ** 2) / 18.0
            + ((xx - centre[2]) ** 2) / 18.0
        )
    )
    return (600.0 * blob + 40.0 * (c + 1)).astype(np.uint16)


def peak_for(t: int) -> tuple[int, int, int]:
    return (3, 12 - 2 * t, 7 + 3 * t)


DATASET = Dataset(
    shape_tczyx=SHAPE_TCZYX,
    np_dtype=np.dtype(np.uint16),
    wire_dtype=pb2.DTYPE_U16,
    open_generation=OPEN_GENERATION,
    volume=blob_volume,
    t=1,
    z=4,
    # The host's `c` is deprecated and always 0; the channel to search
    # is `visible_channels[0]` (SPEC.md §1).  They differ here on
    # purpose, so a plugin still reading `c` searches channel 0 and fails.
    c=0,
    visible_channels=(CHANNEL,),
    crop_min_zyx=(0, 0, 0),
    crop_max_zyx=(1, 1, 1),
    dataset_uri="hostsim://blob",
)


def await_status(host: Host, needle: str, timeout: float = 5.0) -> str:
    return host.await_status("peak", needle, timeout)


def await_contribution(host: Host, timeout: float = 60.0) -> pb2.ContributeRois:
    return host.contributions.get(timeout=timeout)


def start_run(host: Host, timeout: float = 5.0):
    """Invoke `peak.run` until it is ACCEPTED.  A run's contribution reaches
    this driver before the plugin has finished with the run -- SPEC.md §7
    reports the outcome on the panel after it, and a plugin's own busy
    guard stays held until then -- so the first `peak.run` after a run can
    meet the previous run's tail and be refused, correctly (§7 refuses "a
    run already in progress").  Seen once on a loaded CI runner; the retry
    is bounded and the last refusal is the failure."""
    deadline = time.monotonic() + timeout
    while True:
        done = host.invoke("peak.run")
        if done.ok or time.monotonic() > deadline:
            assert done.ok, f"peak.run was not accepted within {timeout:g} s: {done.message!r}"
            return done
        time.sleep(0.02)


# --- scenarios --------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--plugin-dir",
        type=Path,
        default=HERE,
        help="directory holding the vizi-plugin.json to launch (default: this one)",
    )
    plugin_dir = parser.parse_args().plugin_dir

    check("record encoder", check_encoder)
    if failures:
        print("\nthe encoder disagrees with the corpus; nothing else was run", file=sys.stderr)
        return 1

    host = Host(plugin_dir, DATASET)
    for segment in (SEGMENT_A, SEGMENT_B, SEGMENT_C):
        host.add(Segment.host_ring(segment, "V", SEGMENT_BYTES, host.directory))
    host.volume_segment = SEGMENT_A
    with host:
        def test_handshake():
            return host.check_handshake()

        def test_grant_capability_first():
            """§9a step 1: the two halves arrive on two channels and EITHER may
            be first.  This one sends the doorbell's `M` before the control
            plane's region table, and the ack must wait for the second."""
            host.grant_capability(SEGMENT_A)
            time.sleep(0.1)
            assert SEGMENT_A not in host.acks, "acknowledged with only one half"
            host.grant_install(SEGMENT_A)
            ack = host.await_ack(SEGMENT_A)
            assert ack.WhichOneof("outcome") == "installed", ack
            # §8.12: the proof is read back OUT OF the mapping, never echoed
            # from the record -- which is why the record does not carry it.
            assert ack.installed.identity_proof == host.segments[SEGMENT_A].proof, (
                "the acknowledgement does not carry the mapping's own proof"
            )
            return "M, then InstallSegment, then one GrantAck with the proof"

        def test_panel_opened():
            """The panel opens.  The host KEEPS a
            declared panel's state patched before it opens, so re-sending
            state on `PanelOpened` is optional (§8.4): a plugin that re-sends
            is right, and so is one that sends nothing.  Whatever arrives
            must name a declared widget."""
            before = len(host.patches)
            host.open_panel("peak")
            time.sleep(0.5)
            pushed = {widget for _, widget, _ in host.patches[before:]}
            assert pushed <= set(host._panel_widgets["peak"]), pushed
            assert not host.unhandled and not host.refusals, (host.unhandled, host.refusals)
            return f"PanelOpened -> {len(pushed)} widget(s) re-sent (optional)"

        def test_widget_args():
            # line_edit sends {"value": <str>}; a dropdown sends index + value.
            for command_id, value in (
                ("peak.set_z", "4"),
                ("peak.set_y", "8"),
                ("peak.set_x", "8"),
            ):
                done = host.invoke(command_id, value=value)
                assert done.ok, f"{command_id}: {done.message}"
            done = host.invoke("peak.set_mode", index=0, value="Current timepoint")
            assert done.ok, done.message
            return "extents and mode accepted"

        def test_bad_extent():
            previous_status = await_status(host, "")
            before = len(host.patches)
            done = host.invoke("peak.set_z", value="not a number")
            assert not done.ok, "an unparseable extent must fail the invocation"
            assert "not a number" in done.message, done.message
            # SPEC.md requires panel feedback, not a particular sentence or
            # a strictly-positive extent policy (zero sigma is specified).
            deadline = time.monotonic() + 5.0
            while not any(
                panel == "peak" and widget == "status"
                for panel, widget, _ in host.patches[before:]
            ) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert any(
                panel == "peak" and widget == "status"
                for panel, widget, _ in host.patches[before:]
            ), "invalid extent did not update the panel status"
            status = await_status(host, "")
            assert status.strip() and status != previous_status, status
            # Restore the good value for the runs below.
            assert host.invoke("peak.set_z", value="4").ok
            return f"rejected with {done.message!r}"

        def test_run_and_release():
            start_run(host)
            batch = await_contribution(host)
            assert len(batch.rois) == 1 and not batch.replace_all
            contribution = batch.rois[0]
            assert contribution.contrib_id == "peak"
            assert contribution.style.color.startswith("#")

            roi = contribution.roi
            assert roi.type_name == "ellipsoid"
            assert len(roi.vertices) == 4, len(roi.vertices)
            assert not roi.HasField("t_start"), "a current-timepoint ROI is timeless"

            # ViewState said t=1, c=1: the plugin must have read exactly that,
            # at level 0, and found that frame's blob.
            assert host.reads == [(1, CHANNEL, 0)], host.reads
            expected = peak_of(blob_volume(1, CHANNEL), EXTENTS)
            centre = roi.vertices[0].position
            assert (centre.z, centre.y, centre.x) == tuple(v + 0.5 for v in expected), (
                (centre.z, centre.y, centre.x),
                expected,
            )
            # §8.12: `SegmentRef` carries no region and `ReleaseDeposit`
            # requires one, so the plugin resolved it from its own table --
            # naming region 0 blindly is how a correct peer earns a
            # `release_region_mismatch` against itself.
            nbytes = blob_volume(1, CHANNEL).nbytes
            assert host.released == [(SEGMENT_A, 0, 32, nbytes)], host.released
            return f"ROI centered on {expected} (voxel centers), deposit released"

        def test_install_can_ride_the_reply():
            """§8.12: the ride-along arrives with the descriptor, and the `M`
            for it comes AFTER — so the plugin holds the descriptor unread
            rather than failing the message, and completes the read when the
            capability lands."""
            host.volume_segment = SEGMENT_B
            host.ride_along.add(SEGMENT_B)
            reads_before = len(host.reads)

            def late_capability():
                # Long enough that the reply is certainly deferred first.
                time.sleep(0.3)
                host.grant_capability(SEGMENT_B)

            threading.Thread(target=late_capability, daemon=True).start()
            start_run(host)
            batch = await_contribution(host)
            ack = host.await_ack(SEGMENT_B)
            assert ack.WhichOneof("outcome") == "installed", ack
            assert ack.installed.identity_proof == host.segments[SEGMENT_B].proof
            assert len(host.reads) == reads_before + 1, host.reads
            expected = peak_of(blob_volume(1, CHANNEL), EXTENTS)
            centre = batch.rois[0].roi.vertices[0].position
            assert (centre.z, centre.y, centre.x) == tuple(v + 0.5 for v in expected)
            assert host.released[-1][0] == SEGMENT_B, host.released[-1]
            return "descriptor held unread until `M` landed, then read and released"

        def test_install_standalone_with_the_capability_late():
            """§8.12's receiver rule: the unlock is the
            handoff, whichever half is missing.  The host's export shape
            is install standalone, then the descriptor at once, `M` dispatched
            at the grant and no acknowledgement waited for.  So:
            install standalone, a BARE reply into it (no ride-along), and the
            `M` delayed past the reply.  The plugin must hold the descriptor
            and read it when `M` lands, not fail the read for a segment it
            has not installed."""
            host.volume_segment = SEGMENT_C
            reads_before = len(host.reads)
            host.grant_install(SEGMENT_C)  # the meaning, standalone, first

            def late_capability():
                time.sleep(0.3)  # certainly after the bare reply
                host.grant_capability(SEGMENT_C)

            threading.Thread(target=late_capability, daemon=True).start()
            start_run(host)
            batch = await_contribution(host)
            ack = host.await_ack(SEGMENT_C)
            assert ack.WhichOneof("outcome") == "installed", ack
            assert len(host.reads) == reads_before + 1, host.reads
            expected = peak_of(blob_volume(1, CHANNEL), EXTENTS)
            centre = batch.rois[0].roi.vertices[0].position
            assert (centre.z, centre.y, centre.x) == tuple(v + 0.5 for v in expected)
            assert host.released[-1][0] == SEGMENT_C, host.released[-1]
            return "install first, bare reply, `M` last: held, then read and released"

        def test_all_timepoints():
            reads_before = len(host.reads)
            releases_before = len(host.released)
            assert host.invoke("peak.set_mode", index=1, value="All timepoints").ok
            start_run(host)
            batch = await_contribution(host)
            roi = batch.rois[0].roi

            frames = SHAPE_TCZYX[0]
            assert host.reads[reads_before:] == [(t, CHANNEL, 0) for t in range(frames)], (
                host.reads[reads_before:]
            )
            assert roi.t_start == 0 and roi.t_end == frames - 1
            assert roi.out_of_span == 0, "HOLD"
            assert len(roi.pose_translation) == 3 * frames, len(roi.pose_translation)

            first = peak_of(blob_volume(0, CHANNEL), EXTENTS)
            centre = roi.vertices[0].position
            assert (centre.z, centre.y, centre.x) == tuple(v + 0.5 for v in first)
            offsets = np.array(roi.pose_translation, dtype=float).reshape(frames, 3)
            for t in range(frames):
                want = np.array(peak_of(blob_volume(t, CHANNEL), EXTENTS)) - np.array(first)
                assert np.array_equal(offsets[t], want), (t, offsets[t], want)
            assert np.array_equal(offsets[0], (0, 0, 0)), "frame 0 is the origin"

            # Deposits after the first must have come back from a non-zero
            # offset, and every one of them was released, in FIFO order.
            fresh = host.released[releases_before:]
            assert len(fresh) == frames, fresh
            assert len({offset for _, _, offset, _ in fresh}) > 1, fresh
            assert [offset for _, _, offset, _ in fresh] == sorted(
                offset for _, _, offset, _ in fresh
            ), "reclamation is FIFO"
            return f"{frames} frames, dense pose, {len(fresh)} deposits released"

        def test_geometry_lands_on_the_blob():
            batch = None
            # Re-run in current-timepoint mode and inspect the compiled shape.
            assert host.invoke("peak.set_mode", index=0, value="Current timepoint").ok
            assert host.invoke("peak.set_y", value="3").ok
            assert host.invoke("peak.set_x", value="3").ok
            start_run(host)
            batch = await_contribution(host)
            roi = batch.rois[0].roi
            centre = np.array(
                [roi.vertices[0].position.z, roi.vertices[0].position.y, roi.vertices[0].position.x]
            )
            display = np.array(compile_display(roi))
            radii = np.array([4.0, 3.0, 3.0])
            tips = display[:6]
            norms = np.abs(tips - centre) / radii
            assert np.allclose(norms.sum(axis=1), 1.0), norms
            assert np.allclose(np.abs(tips - centre).max(axis=0), radii), tips
            return "6 tips exactly one semi-axis from the center"

        def test_busy_guard():
            # A second run while one is in flight must be refused, not queued.
            # The host PARKS the run's first read, so the run is provably in
            # flight when the second invoke lands, on any machine.
            assert host.invoke("peak.set_mode", index=1, value="All timepoints").ok
            host.hold_reads()
            try:
                start_run(host)  # the previous scenario's run may still be releasing its guard
                host.await_reads(1)
                done = host.invoke("peak.run")
                assert not done.ok, "a concurrent run should have been refused"
                assert done.message.strip(), "a refusal must say why"  # wording is the plugin's
            finally:
                host.release_reads()
            await_contribution(host)
            return "overlapping run refused while the first run's read was held"

        def test_doorbell_shutdown():
            """§8.13 and `doorbell_protocol.md` §10,
            from the host's chair: a doorbell that closes ENDS ITS SESSION,
            politely.  The notice stops the plugin's listener (on macOS
            nothing else can); the `DoorbellFinalReport` comes back inside the
            2 s window, which closes it early; the plugin's end of the socket
            is released; and then the host sends `Shutdown{session_fault}`
            naming the doorbell's failure and the plugin closes its stream.
            A plugin must not end the session itself on the
            notice; it ends on the `Shutdown`.  LAST before teardown, because
            it ends the session."""
            sent = time.monotonic()
            ended = host.end_session_for_doorbell(
                "hostsim pretends record 9 failed its CRC",
                reason=pb2.DOORBELL_SHUTDOWN_FRAMING_FAULT,
                failure_class=pb2.DOORBELL_FAILURE_BAD_CRC,
            )
            report = host.final_report
            assert report is not None, "no DoorbellFinalReport inside the 2 s window"
            # §8.13: `detail` is "the peer's diagnostic" -- free text, and the
            # IDL gives it no vocabulary, so only its presence is asserted.
            assert report.detail.strip(), "the DoorbellFinalReport carried no detail"
            released = host.doorbell_released_by_peer(timeout=2.0)
            if released is None:
                released_note = "release not observable on a datagram doorbell (macOS): the report is the signal"
            else:
                assert released, "the plugin's doorbell stayed open after the notice"
                released_note = "socket released"
            assert ended, f"the stream did not end within {SHUTDOWN_WINDOW_S:g} s of Shutdown{{session_fault}}"
            assert host.shutdown_report is not None, "no ShutdownReport ahead of the close"
            took = time.monotonic() - sent
            return f"final report ({report.detail.strip()[:60]!r}), {released_note}, Shutdown{{session_fault}}, stream closed {took:.1f} s after the notice"

        def test_wire_ceiling():
            """§9: the decoded-message ceiling is 32 MiB, sized
            against the ROI pose, and `grpc.max_receive_message_length` is
            what a plugin can ACCEPT -- the host's `RoiList` reply to
            `GetRois` is the message that grows.  One ROI with its three pose
            arrays at their bound is 24 MiB, under the
            ceiling.  A stray reply (no request outstanding) is exactly the
            right vehicle -- the control plane counts it and moves on -- so
            the only question is whether the TRANSPORT let it through, and
            the next command answering says it did; under a lower limit the
            stream errors RESOURCE_EXHAUSTED and nothing answers again."""
            pose = [0.0] * MAX_POSE_FRAMES
            roi = pb2.RoiRecord(region_id="stray", origin="user")
            roi.roi.pose_translation.extend(pose)
            roi.roi.pose_rotation.extend(pose)
            roi.roi.pose_stretch.extend(pose)
            reply = pb2.HostMsg(id="nobody-asked", rois=pb2.RoiList(rois=[roi]))
            size = reply.ByteSize()
            assert 16 << 20 < size < 32 << 20, size
            host.send(reply)
            done = host.invoke("peak.set_mode", index=0, value="Current timepoint")
            assert done.ok, "the session did not survive a 24 MiB reply"
            assert host.stream_error is None, host.stream_error
            return f"a {size / (1 << 20):.1f} MiB RoiList accepted; the session answers after it"

        def test_unknown_kind_answered():
            """`control_plane_protocol.md` §11: a
            request of a kind the plugin predates is answered
            `Error{UNSUPPORTED}` echoing its id.  Silence costs the sender its
            whole timeout, and neither end faults over one."""
            reply = host.request_unknown_kind()
            assert reply.WhichOneof("msg") == "error", f"answered with {reply.WhichOneof('msg')!r}"
            assert reply.error.kind == pb2.Error.UNSUPPORTED, pb2.Error.Kind.Name(reply.error.kind)
            assert host.invoke("peak.set_mode", index=0, value="Current timepoint").ok, "the session did not go on"
            return f"UNSUPPORTED ({reply.error.message.strip()[:60]!r}); the session goes on"

        def test_nothing_was_reported_against_us():
            """§11: the plugin reports a host bug it can see, and this emulator
            is trying not to be one.  A `ContractViolation` here is a real
            defect in the emulator, so it fails the run rather than being
            printed and forgotten."""
            assert not host.violations, [
                (pb2.ViolationSite.Name(v.where), v.detail) for v in host.violations
            ]
            return "no ContractViolation about this emulator's traffic"

        def test_teardown():
            # §3.3: both a plugin without a hook and one whose cleanup hook
            # completes successfully must close cleanly within the window.
            # Hook registration is an implementation choice. The window may
            # be too short to reliably send a new request inside it, so that
            # refusal rule is left to the SDK's own tests.
            closed = host.shutdown()
            assert closed, f"the stream did not end within {SHUTDOWN_WINDOW_S:g} s"
            assert host.stream_error is None, f"the stream ended in error: {host.stream_error}"
            report = host.shutdown_report
            assert report is not None, "no ShutdownReport arrived ahead of the close"
            outcome = pb2.ShutdownReport.Outcome.Name(report.outcome)
            assert report.outcome in (
                pb2.ShutdownReport.NO_HOOK,
                pb2.ShutdownReport.COMPLETED,
            ), outcome
            return f"ShutdownReport {outcome}, then a clean end of stream"

        check("handshake", test_handshake)
        if failures:
            # §3.1: a host fails the session at the handshake and sends nothing
            # further.  Every scenario below would otherwise wait out its own
            # timeout against a plugin that is not speaking 1.0.
            print("\nthe handshake failed; nothing else was run", file=sys.stderr)
            return 1
        check("grant: capability first", test_grant_capability_first)
        check("panel opened", test_panel_opened)
        check("widget arguments", test_widget_args)
        check("bad extent", test_bad_extent)
        check("run + release", test_run_and_release)
        check("grant: install rides the reply", test_install_can_ride_the_reply)
        check("grant: install standalone, M late", test_install_standalone_with_the_capability_late)
        check("all timepoints", test_all_timepoints)
        check("compiled geometry", test_geometry_lands_on_the_blob)
        check("busy guard", test_busy_guard)
        check("wire ceiling", test_wire_ceiling)
        check("an unknown kind answered UNSUPPORTED", test_unknown_kind_answered)
        check("no violations", test_nothing_was_reported_against_us)
        check("doorbell shutdown ends the session", test_doorbell_shutdown)
        check("teardown", test_teardown)

    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("\nhost emulation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
