#!/usr/bin/env python3
"""Additional checks of the Rust implementation, using the shared driver/emulator.
Run from the repo root with PYTHONDONTWRITEBYTECODE=1 VIZI_HOSTSIM_AUTHORITY=localhost.
"""
import struct
import sys
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "python"))
import hostsim as fixture
from hostsim import (
    Host, Segment, DATASET, TRAIL_SEG, LABELS_SEG, PANEL_SEG, V_SEG, PANEL,
    TOOL, VIEW_2D, LANE_SLOT, pb2, np, await_until, await_status,
    await_label_push, decode_labels, drive, loop_around, updates_on, ops_on,
)


def finished(host):
    assert await_until(lambda: host.status_of(PANEL).startswith(("Label ", "Committed", "Measured")))
    # The final status precedes the worker releasing its guard by a few instructions.
    time.sleep(0.03)


def published_revision(host):
    """Observe READY words without consuming a value (slow-consumer checks)."""
    lane = host.segments[TRAIL_SEG].lane
    view = lane._open()
    words = [int(view.load_u64(lane.offset + 16 * i, lane._atomics.ACQUIRE))
             for i in range(lane.buffers)]
    return max([lane.last_seen] + [word for word in words if word >> 62 == 0])


def await_publication(host, revision):
    assert await_until(lambda: published_revision(host) >= revision), (
        f"preview publication {revision} did not arrive; latest READY/consumed "
        f"revision {published_revision(host)}, status={host.status_of(PANEL)!r}, "
        f"diagnostics={host.diagnostics}"
    )


def await_preview(host, revision, points, endpoint):
    # A D is disposable, and one may describe several publications. Act as a
    # display-rate consumer even if every hint was dropped. Only this thread
    # scans the lane, including later when consumption is deliberately paused.
    def consumed():
        host.scan(TRAIL_SEG)
        return host.live and host.live[-1].revision >= revision

    assert await_until(consumed), (
        f"preview revision {revision} was not consumed; latest publication "
        f"{published_revision(host)}, status={host.status_of(PANEL)!r}, "
        f"diagnostics={host.diagnostics}"
    )
    latest = host.live[-1]
    assert latest.applied, latest.reason
    positions = fixture.positions_of(latest.update.items[0].marker.positions_zyx)
    assert positions.shape == (min(points, 512), 3), positions.shape
    assert np.allclose(positions[-1], [endpoint.z, endpoint.y, endpoint.x]), positions[-1]
    return latest


def check_png(png):
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    cursor, compressed = 8, bytearray()
    while cursor < len(png):
        size = struct.unpack_from(">I", png, cursor)[0]
        kind = png[cursor + 4:cursor + 8]
        data = png[cursor + 8:cursor + 8 + size]
        assert zlib.crc32(kind + data) == struct.unpack_from(">I", png, cursor + 8 + size)[0]
        if kind == b"IHDR":
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            assert (depth, color, compression, filtering, interlace) == (8, 2, 0, 0, 0)
        if kind == b"IDAT":
            compressed.extend(data)
        cursor += size + 12
    raw = zlib.decompress(compressed)
    assert len(raw) == height * (1 + width * 3)
    assert all(raw[y * (1 + width * 3)] == 0 for y in range(height))
    assert len(set(raw)) > 3
    (HERE / "target" / "measurement.png").write_bytes(png)
    return width, height


def main():
    host = Host(HERE, DATASET)
    host.scan_on_hint = False
    host.add(Segment.overlay(TRAIL_SEG, "seg.trail", 1 << 20, host.directory, slot_bytes=LANE_SLOT))
    host.add(Segment.overlay(LABELS_SEG, "seg.labels", 8 << 20, host.directory, slot_bytes=LANE_SLOT))
    host.add(Segment.plugin_arena(PANEL_SEG, "P", 1 << 20, host.directory, subject=PANEL))
    host.add(Segment.host_ring(V_SEG, "V", 4 << 20, host.directory))
    host.volume_segment = V_SEG
    with host:
        host.check_handshake()
        for segment in (TRAIL_SEG, LABELS_SEG, PANEL_SEG, V_SEG):
            assert host.grant(segment).WhichOneof("outcome") == "installed"
        host.open_panel(PANEL)
        tool = host.arrange(TOOL)
        assert await_status(host, fixture.MSG_ACTIVE) == fixture.MSG_ACTIVE
        for command in ("seg.commit", "seg.measure"):
            refusal = host.invoke(command)
            assert not refusal.ok and refusal.message
            assert await_status(host, refusal.message) == refusal.message
        host.dataset.mip = True
        refusal = host.invoke("seg.pick")
        host.dataset.mip = False
        assert not refusal.ok and refusal.message == fixture.MSG_MIP

        path = loop_around(6, 24, 30, 5, points=2048)
        before = len(host.live)
        started = time.monotonic()
        revision = published_revision(host)
        gesture = host.press(tool, VIEW_2D, path[0])
        for points, point in enumerate(path[1:], start=2):
            gesture.drag(point)
            # Bound outstanding input well below the SDK's 256-event queue.
            # A fixed sleep assumes the plugin gets CPU at least that often.
            if points % 64 == 0 or points == len(path):
                latest = await_preview(host, revision + points, points, point)
        size = latest.update.ByteSize()
        assert size <= host.segments[TRAIL_SEG].lane.capacity
        assert all(v.applied for v in host.live[before:])
        elapsed = time.monotonic() - started
        print(f"long preview: 2048 records in {elapsed:.3f}s; {len(host.live)-before} consumed values; 512 markers, {size}/{host.segments[TRAIL_SEG].lane.capacity} bytes", flush=True)
        gesture.release(path[0])
        push = await_label_push(host, 1)
        labels, lut, origin, item = decode_labels(push)
        assert np.array_equal(fixture.placed(labels, origin) == 1, fixture.BLOB)
        assert tuple(lut[0]) == (0, 0, 0, 0)
        for i in range(1, 256):
            assert tuple(lut[i]) == (*fixture.PALETTE[(i - 1) % 8], 180)
        assert item.transform.frame == "voxel"
        assert not item.transform.rotation_xyzw and not item.transform.scale_zyx
        assert item.label_volume.t == DATASET.t
        assert all(loan.uses == 1 for loan in push.loans)
        finished(host)

        # Reset during an Add segmentation must make the arriving result label 1.
        assert host.invoke("seg.set_mode", value="Add").ok
        host.hold_reads()
        try:
            drive(host, tool, loop_around(3, 11, 51, 3))
            host.await_reads()
            # Keep the job in flight even if this driver is descheduled before
            # Reset. A fixed read_delay can expire before Reset is dispatched.
            assert host.invoke("seg.reset").ok
            assert len(updates_on(host, "seg.labels")) == 1, "Add finished while its read was held"
        finally:
            host.release_reads()
        labels, _, origin, _ = decode_labels(await_label_push(host, 2))
        assert labels.max() == 1, "Add result after Reset must start at label 1"
        assert np.array_equal(fixture.placed(labels, origin) == 1, fixture.SQUARE)
        finished(host)
        print("reset during Add job: accepted; pending square lands alone as label 1", flush=True)
        assert host.invoke("seg.commit").ok
        assert await_until(lambda: host.rois and ops_on(host, "region", "set_options"))
        finished(host)
        options = list(ops_on(host, "region", "set_options")[-1].set_options.options)
        images = len(host.images)
        host.hold_reads()
        try:
            assert host.invoke("seg.measure").ok
            host.await_reads()
            commit_updates = len(ops_on(host, "commit", "set_enabled"))
            assert host.invoke("seg.reset").ok
            assert len(host.images) == images, "Measurement finished while its read was held"
            assert list(ops_on(host, "region", "set_options")[-1].set_options.options) == options
            assert ops_on(host, "rmeasure", "set_visible")[-1].set_visible
            # The command reply can overtake its panel patch. Wait for this
            # Reset's update, not an older disabled state already in the log.
            assert await_until(lambda: (
                len(ops := ops_on(host, "commit", "set_enabled")) > commit_updates
                and not ops[-1].set_enabled
            )), "Reset did not disable Commit"
        finally:
            host.release_reads()
        assert await_until(lambda: len(host.images) > images)
        finished(host)
        print(f"reset during measure: regions retained, measurement finishes; independent PNG decode {check_png(host.images[-1][2].payload)}", flush=True)

        # With consumption paused, intermediate publications can be replaced.
        # The newest READY value still arrives; after a pane change it is stale.
        revision = published_revision(host)
        gesture = host.press(tool, VIEW_2D, path[0])
        for point in path[1:101]:
            gesture.drag(point)
        # All 101 publications must complete before consuming, independent of
        # how many hints arrive. Merely seeing ANY hint is not a completion fence.
        await_publication(host, revision + 101)
        found = host.scan(TRAIL_SEG)
        assert len(found) == 1 and found[0].applied, found
        assert fixture.positions_of(found[0].update.items[0].marker.positions_zyx).shape == (101, 3)
        revision = published_revision(host)
        gesture.drag(path[101])
        await_publication(host, revision + 1)
        host.arrange_pane(VIEW_2D)
        found = host.scan(TRAIL_SEG)
        assert found and not found[-1].applied and host.segments[TRAIL_SEG].lane.stale_pairs == 1
        host.retire_tool(TOOL)
        assert await_status(host, "Lasso inactive") == "Lasso inactive"
        assert not host.violations and not host.diagnostics and not host.loan_uses
        assert not host.unhandled and not host.segments[TRAIL_SEG].lane.quarantined
        print("slow consumer: newest of 101 updates preserved; old-pane value rejected; retirement hook runs; loan ledger empty", flush=True)
        assert host.shutdown()
        assert host.shutdown_report.outcome == pb2.ShutdownReport.COMPLETED
        print("extra checks passed", flush=True)


if __name__ == "__main__":
    main()
