#!/usr/bin/env python3
"""Drive the interactive-segmentation plugin end to end over the real transport, with no vizi.

    .venv/bin/python hostsim.py                     # this implementation
    .venv/bin/python hostsim.py --plugin-dir ../go  # any other implementation of SPEC.md

The emulator (``examples/vizi_hostsim``) launches the plugin as the Broker
would, grants an `O` segment per overlay, a `P` for the panel and a `V` ring,
runs the arrangements, composes `E` records, reads the trail off the Pulsed
lane through its own §6.5 reader, reads every loan a push names, compiles
label volumes into masks and answers `GetRois`/`ReadMaskVoxels`/
`ReadDataVolume`.  What is here is this example's dataset -- one bright
ellipsoid, one bright square a few planes away -- and its scenarios, in
``SPEC.md``'s order: the lasso and its trail, the label volume it publishes,
each refusal the gesture can earn, the anchor rule, Add and Replace, the
commit into masks (twice), the measurement and its plot, the panel's re-push,
the busy guard, teardown.

**It drives any implementation of ``SPEC.md``, not only this one.**  The
assertions are the specification's: the messages ``SPEC.md`` quotes, the
label volume's shape, the LUT rows, the mask names and colours, the calls
made and not made.  Where ``SPEC.md`` leaves the words to the plugin the
driver asserts that ``status`` moved, not what it says.  Unix only; needs an
interpreter carrying the Python SDK, ``grpcio`` and ``atomicshm``.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # examples/, for vizi_hostsim

from vizi_hostsim import (  # noqa: E402
    KIND_COLUMN_YX,
    SHUTDOWN_WINDOW_S,
    VIEW_2D,
    VIEW_3D,
    Column,
    Dataset,
    Host,
    Segment,
    Voxel,
    check,
    check_encoder,
    failures,
    segment_id,
)
from vizi_plugin_sdk import pb2  # noqa: E402

PANEL = "seg"
TOOL = "seg.lasso"
TRAIL_SEG = segment_id(1)
LABELS_SEG = segment_id(2)
PANEL_SEG = segment_id(3)
#: This driver grants no fixed `V` (the emulator grants it the host's way);
#: the id is for the per-language check scripts that pre-grant one.
V_SEG = segment_id(4)
LANE_SLOT = 16 << 10

# --- the words SPEC.md pins ------------------------------------------------------------

MSG_MIP = "Turn off Z-MIP — the tool needs to know which plane you are pointing at."
MSG_LEVEL = "Zoom in to full resolution — the tool segments level-0 voxels."
MSG_CHANNELS = "Show a single channel — the tool cannot tell which one you mean."
MSG_LOOP = "Drag a closed loop around a region."
MSG_ACTIVE = "Lasso active — drag a loop in a 2D view"
MSG_READY = "Ready"

# --- the dataset --------------------------------------------------------------------------

SHAPE_TCZYX = (3, 2, 12, 48, 64)
OPEN_GENERATION = (7 << 32) | 3  # more than 32 bits: `E.gen` carries the low ones
BACKGROUND = 100
BLOB_CENTRE = (6, 24, 30)  # z, y, x
BLOB_RADII = (3.0, 5.0, 6.0)
SQUARE_Z, SQUARE_Y, SQUARE_X = 3, (6, 16), (46, 56)  # a bright 10x10 square on plane 3 only
SQUARE_VALUE = 1500


def blob_mask() -> np.ndarray:
    _, _, depth, height, width = SHAPE_TCZYX
    z, y, x = np.mgrid[0:depth, 0:height, 0:width]
    cz, cy, cx = BLOB_CENTRE
    rz, ry, rx = BLOB_RADII
    return ((z - cz) / rz) ** 2 + ((y - cy) / ry) ** 2 + ((x - cx) / rx) ** 2 <= 1.0


def square_mask() -> np.ndarray:
    _, _, depth, height, width = SHAPE_TCZYX
    m = np.zeros((depth, height, width), dtype=bool)
    m[SQUARE_Z, SQUARE_Y[0] : SQUARE_Y[1], SQUARE_X[0] : SQUARE_X[1]] = True
    return m


BLOB = blob_mask()
SQUARE = square_mask()


def blob_value(t: int, c: int) -> int:
    return 1000 + 40 * t - 300 * c


def volume(t: int, c: int) -> np.ndarray:
    _, _, depth, height, width = SHAPE_TCZYX
    v = np.full((depth, height, width), BACKGROUND, dtype=np.uint16)
    v[BLOB] = blob_value(t, c)
    v[SQUARE] = SQUARE_VALUE
    return v


@dataclass
class TwoLevelDataset(Dataset):
    """The emulator's dataset with a second pyramid level declared, so that a
    record at `level = 1` reconciles (the SDK marks a record at an undeclared
    level unusable, which is a different scenario) and reaches the tool."""

    def geometry(self) -> pb2.Geometry:
        g = super().geometry()
        g.levels.append(pb2.LevelScale(z=2.0, y=2.0, x=2.0))
        return g

    def view_state(self) -> pb2.ViewState:
        vs = super().view_state()
        _, _, depth, height, width = self.shape_tczyx
        vs.levels.append(pb2.LevelInfo(level=1, shape_zyx=[depth // 2, height // 2, width // 2]))
        return vs


DATASET = TwoLevelDataset(
    shape_tczyx=SHAPE_TCZYX,
    np_dtype=np.dtype(np.uint16),
    wire_dtype=pb2.DTYPE_U16,
    open_generation=OPEN_GENERATION,
    volume=volume,
    t=1,
    z=BLOB_CENTRE[0],
    c=0,
    dataset_uri="hostsim://blob",
    dataset_name="blob",
)


# --- helpers -------------------------------------------------------------------------------


def loop_around(z: float, cy: float, cx: float, radius: float, points: int = 24) -> list[Voxel]:
    """A closed lasso: ``points`` voxels on a circle of ``radius`` around ``(cy, cx)`` on plane ``z``."""
    angles = np.linspace(0.0, 2.0 * np.pi, points, endpoint=False)
    return [Voxel(float(z), float(cy + radius * np.sin(a)), float(cx + radius * np.cos(a))) for a in angles]


def drive(host: Host, tool: int, path: list[Voxel], **fields):
    """press, drag..., release along ``path``; returns the gesture handle."""
    gesture = host.press(tool, VIEW_2D, path[0], **fields)
    for p in path[1:-1]:
        gesture.drag(p, **fields)
        time.sleep(0.005)
    gesture.release(path[-1], **fields)
    return gesture


def positions_of(buf: pb2.GeomBuffer) -> np.ndarray:
    assert buf.WhichOneof("source") == "inline", buf.WhichOneof("source")
    assert buf.dtype == pb2.DTYPE_F64, buf.dtype
    return np.frombuffer(buf.inline, dtype="<f8").reshape(tuple(buf.shape))


def await_status(host: Host, needle: str, timeout: float = 10.0) -> str:
    """For a status `SPEC.md` quotes verbatim.  Wording the specification
    does not quote is never waited on by its words: use `await_until` on
    the observable state and `await_status_set` on the line."""
    return host.await_status(PANEL, needle, timeout)


def await_until(ok, timeout: float = 10.0) -> bool:
    """Poll ``ok()`` until it holds or the deadline passes."""
    deadline = time.monotonic() + timeout
    while not ok() and time.monotonic() < deadline:
        time.sleep(0.02)
    return bool(ok())


def statuses_since(host: Host, index: int) -> list[str]:
    """Every value the status line was set to since ``host.patches[index]``."""
    return [str(v) for p, w, v in host.patches[index:] if p == PANEL and w == "status" and not isinstance(v, pb2.PatchOp)]


def await_status_set(host: Host, patches_before: int, timeout: float = 10.0) -> str | None:
    """The status line once it has been set again since
    ``host.patches[patches_before]`` -- the same words as before count, a
    plugin may well say "Done." twice -- or None at the deadline.  The bar
    for wording the specification leaves free."""
    if await_until(lambda: statuses_since(host, patches_before), timeout):
        return host.status_of(PANEL)
    return None


def await_change(host: Host, previous: str, widget: str = "status", timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while host.status_of(PANEL, widget) == previous and time.monotonic() < deadline:
        time.sleep(0.02)
    return host.status_of(PANEL, widget)


def updates_on(host: Host, overlay_id: str):
    return [p for p in host.pushes if p.kind == "update" and p.overlay_id == overlay_id]


def removes_on(host: Host, overlay_id: str):
    return [p for p in host.pushes if p.kind == "remove" and p.overlay_id == overlay_id]


def await_label_push(host: Host, count: int, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while len(updates_on(host, "seg.labels")) < count and time.monotonic() < deadline:
        time.sleep(0.02)
    pushes = updates_on(host, "seg.labels")
    assert len(pushes) >= count, f"{len(pushes)} label pushes, wanted {count}"
    return pushes[count - 1]


def decode_labels(push) -> tuple[np.ndarray, np.ndarray, tuple[int, int, int], pb2.OverlayItem]:
    """The pushed label volume: (labels u8 ZYX, LUT (256,4) u8, origin zyx, item)."""
    assert push.update is not None and len(push.update.items) == 1, push
    item = push.update.items[0]
    assert item.WhichOneof("geometry") == "label_volume", item.WhichOneof("geometry")
    lv = item.label_volume
    by_ref = {(ln.segment, ln.offset, ln.length): ln for ln in push.loans}
    assert lv.labels.WhichOneof("source") == "segment" and lv.colormap.WhichOneof("source") == "segment", "a compiled item's geometry is segment-backed (note 3)"

    def payload(buf: pb2.GeomBuffer) -> bytes:
        ref = buf.segment
        loan = by_ref[(ref.segment_id, ref.offset, ref.len)]
        return loan.payload

    assert lv.labels.dtype == pb2.DTYPE_U8, pb2.DType.Name(lv.labels.dtype)
    labels = np.frombuffer(payload(lv.labels), dtype=np.uint8).reshape(tuple(lv.labels.shape))
    lut = np.frombuffer(payload(lv.colormap), dtype=np.uint8).reshape(tuple(lv.colormap.shape))
    pos = tuple(item.transform.position_zyx)
    assert len(pos) == 3 and all(float(v).is_integer() for v in pos), f"position_zyx {pos} is not integral"
    return labels, lut, tuple(int(v) for v in pos), item


def placed(labels: np.ndarray, origin: tuple[int, int, int]) -> np.ndarray:
    """The pushed labels laid into a full-volume u8 array."""
    _, _, depth, height, width = SHAPE_TCZYX
    full = np.zeros((depth, height, width), dtype=np.uint8)
    z, y, x = origin
    full[z : z + labels.shape[0], y : y + labels.shape[1], x : x + labels.shape[2]] = labels
    return full


def bbox_tight(labels: np.ndarray) -> bool:
    return bool(labels.any()) and all(
        np.take(labels, 0, axis=a).any() and np.take(labels, -1, axis=a).any() for a in range(3)
    )


def unexpected_violations(host: Host) -> str | None:
    """Why the run's `ContractViolation`s are not all accounted for, or None:
    any at a site other than the one `test_unusable_record` provokes, or more
    at that site than its two records can explain."""
    site, allowed = getattr(host, "expected_unusable", (None, 0))
    named = [(pb2.ViolationSite.Name(v.where), v.detail) for v in host.violations]
    elsewhere = [n for n in named if n[0] != (pb2.ViolationSite.Name(site) if site is not None else None)]
    if elsewhere:
        return f"reports at a site the run does not provoke: {elsewhere}"
    if len(named) > allowed:
        return f"{len(named)} unusable_context reports where the two level-3 records explain at most {allowed}: {named}"
    return None


def ops_on(host: Host, widget: str, op_name: str):
    """Non-`set_value` patch ops the emulator recorded for one widget."""
    return [
        op
        for panel, w, op in host.patches
        if panel == PANEL and w == widget and isinstance(op, pb2.PatchOp) and op.WhichOneof("op") == op_name
    ]


PALETTE = [
    (230, 25, 75), (60, 180, 75), (67, 99, 216), (245, 130, 49),
    (145, 30, 180), (66, 212, 244), (240, 50, 230), (191, 239, 69),
]


# --- the run ------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-dir", type=Path, default=HERE)
    plugin_dir = parser.parse_args().plugin_dir

    check("record encoder", check_encoder)
    if failures:
        print("\nthe encoder disagrees with the corpus; nothing else was run", file=sys.stderr)
        return 1

    host = Host(plugin_dir, DATASET)
    host.add(Segment.overlay(TRAIL_SEG, "seg.trail", 1 << 20, host.directory, slot_bytes=LANE_SLOT))
    host.add(Segment.overlay(LABELS_SEG, "seg.labels", 8 << 20, host.directory, slot_bytes=LANE_SLOT))
    host.add(Segment.plugin_arena(PANEL_SEG, "P", 1 << 20, host.directory, subject=PANEL))
    # No `V` here: the emulator grants it the host's way -- at the first
    # read, sized to it, grown by supersede (`Host._v_for`).

    with host:
        state: dict = {}

        def test_handshake():
            return host.check_handshake()

        def test_grants():
            """Four segments, the two `O`s in the two orders a grant can
            arrive: `M` first for the trail, `InstallSegment` first for the
            labels with the `M` half late."""
            ack = host.grant(TRAIL_SEG)
            assert ack.WhichOneof("outcome") == "installed", ack
            host.grant_install(LABELS_SEG)
            time.sleep(0.1)
            assert LABELS_SEG not in host.acks, "the install half alone must not be acknowledged installed"
            host.grant_capability(LABELS_SEG)
            assert host.await_ack(LABELS_SEG).WhichOneof("outcome") == "installed"
            assert host.grant(PANEL_SEG).WhichOneof("outcome") == "installed"
            return "O(seg.trail) M-first, O(seg.labels) install-first with M late, P(seg): all installed"

        def test_panel_opened():
            """Before any tool is engaged the panel shows the manifest's
            defaults with `commit` disabled and the measure row hidden."""
            host.open_panel(PANEL)
            status = await_status(host, MSG_READY)
            assert status == MSG_READY, status
            deadline = time.monotonic() + 5.0
            while not (ops_on(host, "commit", "set_enabled") and ops_on(host, "rmeasure", "set_visible")) and time.monotonic() < deadline:
                time.sleep(0.02)
            enabled = ops_on(host, "commit", "set_enabled")
            visible = ops_on(host, "rmeasure", "set_visible")
            assert enabled and enabled[-1].set_enabled is False, "commit is disabled until a segmentation exists"
            assert visible and visible[-1].set_visible is False, "the measure row is hidden until a commit"
            return f"status {status!r}; commit disabled; measure row hidden"

        def test_arrangements():
            """`ToolBinding` (no triggers, no actions -- the modal rule),
            `TimeMapping`, one `EpochChange` per pane.  Then Acceptance 17 as
            SPEC.md's *UI state model* qualifies it: on an SDK that surfaces
            the arrangement's arrival to the tool, the status line moves to
            `MSG_ACTIVE` now; on one that does not, it stays at `MSG_READY`
            -- not a failure -- and must
            not claim an engagement the plugin could not see."""
            state["tool"] = host.arrange(TOOL)
            state["epoch"] = host.epochs[VIEW_2D]
            assert host.tool_acks[state["tool"]] == 1 and host.epoch_acks == {VIEW_2D: 1, VIEW_3D: 1}
            status = await_status(host, MSG_ACTIVE, timeout=3.0)
            state["activated_hook"] = status == MSG_ACTIVE
            if state["activated_hook"]:
                return f"tool handle {state['tool']}, panes at epoch 1; status {status!r} -- the activated hook"
            assert status == MSG_READY, f"without an activated hook the status line stays {MSG_READY!r}, not {status!r}"
            return (
                f"tool handle {state['tool']}, panes at epoch 1; status still {status!r} -- "
                "no activated hook (SPEC.md, the UI state model), not a failure"
            )

        def test_pick_activates_and_refuses_mip():
            """`seg.pick` -> `ActivateTool`; with `mip` set it refuses up
            front and sends no activation (Acceptance 7)."""
            before = len(host.activations)
            done = host.invoke("seg.pick")
            assert done.ok, done.message
            deadline = time.monotonic() + 5.0
            while len(host.activations) == before and time.monotonic() < deadline:
                time.sleep(0.02)
            assert len(host.activations) == before + 1 and host.activations[-1].tool_id == TOOL and not host.activations[-1].deactivate, host.activations[before:]
            host.dataset.mip = True
            try:
                done = host.invoke("seg.pick")
                status = await_status(host, MSG_MIP)
            finally:
                host.dataset.mip = False
            assert status == MSG_MIP, status
            assert len(host.activations) == before + 1, "a MIP refusal must not activate the tool"
            return f"ActivateTool({TOOL}) sent once; under MIP: {status!r}, no activation"

        def test_lasso_segments_the_blob():
            """A loop around the ellipsoid on its middle plane: markers on the
            trail lane at `z_g` while dragging, the trail retracted on
            release, then one label volume pushed as two loans with `uses=2`
            -- bbox-tight u8, the LUT, an integer position, `t` set -- whose
            label 1 is exactly the ellipsoid (Acceptance 2, 3)."""
            live_before = len(host.live)
            patches_before = len(host.patches)
            path = loop_around(BLOB_CENTRE[0], BLOB_CENTRE[1], BLOB_CENTRE[2], 5.0)
            gesture = host.press(state["tool"], VIEW_2D, path[0])
            live = host.await_live(live_before + 1)
            assert live and live[-1].segment == TRAIL_SEG and live[-1].region == 0 and live[-1].applied, live[-1:]
            assert live[-1].pane == (VIEW_2D, state["epoch"]), live[-1].pane
            for p in path[1:-1]:
                gesture.drag(p)
                time.sleep(0.005)
            # the newest value is what must arrive; intermediate ones may coalesce
            deadline = time.monotonic() + 5.0
            newest = None
            while time.monotonic() < deadline:
                host.scan(TRAIL_SEG)
                trail = [v for v in host.live[live_before:] if v.segment == TRAIL_SEG]
                if trail:
                    item = trail[-1].update.items[0]
                    pos = positions_of(item.marker.positions_zyx)
                    if pos.shape[0] >= len(path) - 1:
                        newest = trail[-1]
                        break
                time.sleep(0.02)
            assert newest is not None, "the full trail never reached the lane"
            item = newest.update.items[0]
            assert item.WhichOneof("geometry") == "marker", item
            # The positions are level-0 voxels, and "" is WORLD on the wire: a
            # trail sent without a frame draws nowhere in a 2D pane.
            assert item.transform.frame == "voxel", f"the trail's frame is {item.transform.frame!r}, not 'voxel'"
            pos = positions_of(item.marker.positions_zyx)
            assert pos.shape == (len(path) - 1, 3) and np.all(pos[:, 0] == float(BLOB_CENTRE[0])), "the trail is drawn at z_g"
            assert np.allclose(pos[-1, 1:], (path[-2].y, path[-2].x)), pos[-1]
            assert newest.update.overlay_id == "seg.trail"
            removes_before = len(removes_on(host, "seg.trail"))
            gesture.release(path[-1])
            host.await_pushes(removes_before + 1, kind="remove")
            assert len(removes_on(host, "seg.trail")) == removes_before + 1, "the trail was not retracted on release"
            push = await_label_push(host, 1)
            labels, lut, origin, _ = decode_labels(push)
            assert len(push.loans) == 2, "labels and LUT are two loans"
            assert all(loan.uses in (0, 1, 2) for loan in push.loans), [loan.uses for loan in push.loans]
            state["push_uses"] = push.loans[0].uses
            assert bbox_tight(labels), "the label volume is not bbox-tight"
            full = placed(labels, origin)
            assert np.array_equal(full == 1, BLOB), f"label 1 is not the ellipsoid ({int((full == 1).sum())} vs {int(BLOB.sum())} voxels)"
            assert labels.max() == 1
            assert tuple(lut[0]) == (0, 0, 0, 0) and tuple(lut[1]) == (*PALETTE[0], 180), (lut[0], lut[1])
            item = push.update.items[0]
            assert item.label_volume.HasField("t") and item.label_volume.t == DATASET.t, item.label_volume.t
            assert item.transform.frame in ("", "voxel"), item.transform.frame
            assert dict(item.label_volume.label_names).get(1) == "Region 1", dict(item.label_volume.label_names)
            # §9: "report the result: label id, voxel count, threshold, clipped" -- the count as a numeral
            assert await_until(lambda: any(str(int(BLOB.sum())) in v for v in statuses_since(host, patches_before))), (
                f"§9: the status line must report the voxel count ({int(BLOB.sum())}): {statuses_since(host, patches_before)}"
            )
            status = host.status_of(PANEL)
            state["label_push"] = push
            return f"{pos.shape[0]}-point trail at z={BLOB_CENTRE[0]}, retracted; labels {labels.shape} at {origin}, {int(BLOB.sum())} voxels = the ellipsoid; {status!r}"

        def test_loose_loop_floods_and_says_so():
            """§6 as specified: a loop with more background than object under
            it has a background median, so the threshold admits the whole
            search window -- and §5 requires the clip to be said out loud.
            Replace mode, so the next scenario starts clean."""
            count = len(updates_on(host, "seg.labels"))
            drive(host, state["tool"], loop_around(BLOB_CENTRE[0], BLOB_CENTRE[1], BLOB_CENTRE[2], 8.0))
            push = await_label_push(host, count + 1)
            labels, _, origin, _ = decode_labels(push)
            assert int((labels == 1).sum()) > int(BLOB.sum()) * 10, "a loose loop should have flooded the window"
            status = await_status(host, "clipped by the search window")
            assert "clipped by the search window" in status, status
            # back to the ellipsoid alone before anything accumulates on it
            drive(host, state["tool"], loop_around(BLOB_CENTRE[0], BLOB_CENTRE[1], BLOB_CENTRE[2], 5.0))
            push = await_label_push(host, count + 2)
            labels, _, origin, _ = decode_labels(push)
            assert np.array_equal(placed(labels, origin) == 1, BLOB)
            return f"radius-8 loop: {int((labels == 1).sum())} voxels flooded, {status!r}; radius-5 loop restores the ellipsoid"

        def test_bare_click():
            pushes_before = len(host.pushes)
            g = host.press(state["tool"], VIEW_2D, Voxel(6.0, 24.0, 30.0))
            g.release(Voxel(6.0, 24.0, 30.0))
            status = await_status(host, MSG_LOOP)
            assert status == MSG_LOOP, status
            assert len([p for p in host.pushes[pushes_before:] if p.kind == "update"]) == 0, "a bare click pushed"
            return status

        def test_cancel_clears():
            updates_before = len(updates_on(host, "seg.labels"))
            removes_before = len(removes_on(host, "seg.trail"))
            path = loop_around(6.0, 24.0, 30.0, 5.0)
            g = host.press(state["tool"], VIEW_2D, path[0])
            g.drag(path[1])
            host.await_live(len(host.live) + 1)
            g.cancel()
            host.await_pushes(removes_before + 1, kind="remove")
            assert len(removes_on(host, "seg.trail")) == removes_before + 1, "the trail survived a CANCEL"
            time.sleep(0.3)
            assert len(updates_on(host, "seg.labels")) == updates_before, "a cancelled gesture segmented"
            return "OverlayRemove on seg.trail; nothing pushed"

        def test_level_refused():
            """A record at level 1 -- declared by this dataset's geometry, so
            it reconciles and reaches the tool -- is refused by name."""
            updates_before = len(updates_on(host, "seg.labels"))
            live_before = len(host.live)
            path = loop_around(3.0, 12.0, 15.0, 4.0, points=8)
            drive(host, state["tool"], path, level=1)
            status = await_status(host, MSG_LEVEL)
            assert status == MSG_LEVEL, status
            assert len(updates_on(host, "seg.labels")) == updates_before
            host.scan()
            # §2: level-1 coordinates in the marker's level-0 frame would draw at the wrong scale
            assert len(host.live) == live_before, "a level-1 gesture published a trail"
            return f"{status}; no trail"

        def test_column_refused():
            """§3's first bullet: the record is the authority on a projection.
            A gesture whose records carry a `Column` takes no point and is
            refused with the MIP message; nothing is published or pushed."""
            updates_before = len(updates_on(host, "seg.labels"))
            live_before = len(host.live)
            column = Column(KIND_COLUMN_YX, 24.0, 30.0, 0.0, float(SHAPE_TCZYX[2]))
            g = host.press(state["tool"], VIEW_2D, column)
            g.drag(Column(KIND_COLUMN_YX, 25.0, 31.0, 0.0, float(SHAPE_TCZYX[2])))
            g.release(column)
            status = await_status(host, MSG_MIP)
            assert status == MSG_MIP, status
            time.sleep(0.2)
            host.scan()
            assert len(host.live) == live_before, "a Column gesture published a trail"
            assert len(updates_on(host, "seg.labels")) == updates_before
            return status

        def test_two_channels_refused():
            updates_before = len(updates_on(host, "seg.labels"))
            path = loop_around(6.0, 24.0, 30.0, 5.0, points=8)
            drive(host, state["tool"], path, channels=0b11)
            status = await_status(host, MSG_CHANNELS)
            assert status == MSG_CHANNELS, status
            assert len(updates_on(host, "seg.labels")) == updates_before
            return status

        def test_unusable_record():
            """A record at an undeclared level: the SDK reports
            `unusable_context` and still delivers it; the tool takes no point
            and says nothing; the next gesture is live (Acceptance 16)."""
            before = len(host.violations)
            status_before = host.status_of(PANEL)
            g = host.press(state["tool"], VIEW_2D, Voxel(2.0, 3.0, 4.0), level=3)
            g.release(Voxel(2.0, 3.0, 4.0), level=3)
            reported = host.await_violations("unusable_context")
            assert reported, "no unusable_context report"
            unexpected = [v for v in host.violations[before:] if v.where != reported[-1].where]
            assert not unexpected, [(pb2.ViolationSite.Name(v.where), v.detail) for v in unexpected]
            time.sleep(0.3)
            assert host.status_of(PANEL) == status_before, f"an unusable record reported something: {host.status_of(PANEL)!r}"
            live_before = len(host.live)
            g2 = host.press(state["tool"], VIEW_2D, Voxel(6.0, 24.0, 30.0))
            live = host.await_live(live_before + 1)
            assert live[-1].applied and live[-1].pane == (VIEW_2D, state["epoch"]), live[-1]
            removes_before = len(removes_on(host, "seg.trail"))
            g2.cancel()
            host.await_pushes(removes_before + 1, kind="remove")
            # the two level-3 records are the only `unusable_context` this run may draw; a repeat at a
            # site is deferred by the reporter's rate (§7: a rate, not a loss), so the second may land
            # any time up to shutdown -- hence a count bound, checked again after the stream ends, and
            # not a whitelist of the site for the rest of the run (which hid a spurious report once)
            host.expected_unusable = (reported[-1].where, 2)
            return f"level 3: {pb2.ViolationSite.Name(reported[-1].where)} reported, nothing said; next gesture live"

        def test_anchor_rule():
            """The square is bright on plane 3 only.  A loop around its
            footprint drawn on plane 9 has nothing above the threshold on the
            drawn plane; the refusal names both planes (Acceptance 6)."""
            updates_before = len(updates_on(host, "seg.labels"))
            cy = (SQUARE_Y[0] + SQUARE_Y[1]) / 2.0
            cx = (SQUARE_X[0] + SQUARE_X[1]) / 2.0
            drive(host, state["tool"], loop_around(9.0, cy, cx, 3.5, points=12))
            status = await_status(host, "Nothing above the threshold on plane 9")
            assert status.startswith("Nothing above the threshold on plane 9"), status
            assert f"z={SQUARE_Z}" in status, status
            assert len(updates_on(host, "seg.labels")) == updates_before, "the anchor rule pushed a mask off the drawn plane"
            return status

        def test_add_mode():
            """Add: a loop around the square on its own plane becomes label 2
            beside the ellipsoid's label 1; the newer LUT row, both names."""
            assert host.invoke("seg.set_mode", value="Add", index=1).ok
            cy = (SQUARE_Y[0] + SQUARE_Y[1]) / 2.0
            cx = (SQUARE_X[0] + SQUARE_X[1]) / 2.0
            count = len(updates_on(host, "seg.labels"))
            patches_before = len(host.patches)
            drive(host, state["tool"], loop_around(float(SQUARE_Z), cy, cx, 3.5, points=12))
            push = await_label_push(host, count + 1)
            labels, lut, origin, item = decode_labels(push)
            full = placed(labels, origin)
            assert np.array_equal(full == 1, BLOB) and np.array_equal(full == 2, SQUARE), (
                f"labels 1 and 2 are not the ellipsoid and the square: ids {np.unique(labels).tolist()}, "
                f"1 -> {int((full == 1).sum())}/{int(BLOB.sum())}, 2 -> {int((full == 2).sum())}/{int(SQUARE.sum())}, "
                f"status {host.status_of(PANEL)!r}"
            )
            assert tuple(lut[2]) == (*PALETTE[1], 180)
            names = dict(item.label_volume.label_names)
            assert names.get(1) == "Region 1" and names.get(2) == "Region 2", names
            assert bbox_tight(labels)
            assert await_until(lambda: any(str(int(SQUARE.sum())) in v for v in statuses_since(host, patches_before))), (
                f"§9: the status line must report the voxel count ({int(SQUARE.sum())}): {statuses_since(host, patches_before)}"
            )
            status = host.status_of(PANEL)
            return f"labels {labels.shape} at {origin}: 1 = ellipsoid, 2 = square ({int(SQUARE.sum())} voxels); {status!r}"

        def test_commit():
            """`CompileLabelVolume` naming the labels' bytes: one mask per label, named and coloured from the LUT, the overlay
            retracted, `GetRois` answered, the dropdown patched; then a second
            commit updates the same rows (Acceptance 9)."""
            patches_before = len(host.patches)
            done = host.invoke("seg.commit")
            assert done.ok, done.message
            assert await_until(lambda: len(host.compiles) >= 1), "no CompileLabelVolume arrived"
            assert await_until(lambda: ops_on(host, "region", "set_options") and ops_on(host, "rmeasure", "set_visible")), "the dropdown was not patched or the measure row not shown after the commit"
            # §"Committing": "a status line naming the count" -- the numeral, not the reference's sentence
            assert await_until(lambda: any("2" in v for v in statuses_since(host, patches_before))), (
                f"the status line must name the count (2) after a commit: {statuses_since(host, patches_before)}"
            )
            status = host.status_of(PANEL)
            assert len(host.compiles) == 1, host.compiles
            by_label = {m.label: m for m in host.masks.values()}
            assert sorted(by_label) == [1, 2], sorted(by_label)
            assert by_label[1].name == "Region 1" and by_label[2].name == "Region 2"
            assert tuple(by_label[1].color[:3]) == PALETTE[0] and tuple(by_label[2].color[:3]) == PALETTE[1], (by_label[1].color, by_label[2].color)
            assert by_label[1].membership.sum() == BLOB.sum() and by_label[2].membership.sum() == SQUARE.sum()
            assert removes_on(host, "seg.labels"), "the overlay was not retracted after the commit"
            # §"Committing": either route conforms.  With `uses=2` the compile is the
            # push's second use; reserving twice leaves nothing pending either way --
            # except that a `uses=2` push superseded before its compile stays pinned.
            pending = [k for k in host.loan_uses if k[0] == LABELS_SEG]
            assert not pending or state.get("push_uses") == 2, f"reservations still pending a use: {pending}"
            assert not host.diagnostics, host.diagnostics
            options = ops_on(host, "region", "set_options")
            assert options and list(options[-1].set_options.options) == ["Region 1", "Region 2"], options[-1:]
            visible = ops_on(host, "rmeasure", "set_visible")
            assert visible and visible[-1].set_visible is True
            ids_before = {m.region_id for m in host.masks.values()}
            options_before = len(ops_on(host, "region", "set_options"))
            done = host.invoke("seg.commit")
            assert done.ok, done.message
            assert await_until(lambda: len(host.compiles) >= 2), "no second CompileLabelVolume arrived"
            assert len(host.compiles) == 2
            assert await_until(lambda: len(ops_on(host, "region", "set_options")) > options_before), "the dropdown was not re-patched after the re-commit"
            assert {m.region_id for m in host.masks.values()} == ids_before and len(host.masks) == 2, "a re-commit changed the rows"
            assert not host.diagnostics, host.diagnostics
            return f"2 masks: {by_label[1].name} {by_label[1].color}, {by_label[2].name} {by_label[2].color}; overlay retracted; re-commit kept the rows"

        def test_measure():
            """`ReadMaskVoxels` once, `ReadDataVolume` per timepoint with the
            mask's box, a PNG through the `P` arena (Acceptance 10).  Only
            channel 1 is shown, so the channel is `visible_channels[0]` = 1,
            never the deprecated `ViewState.c` (0)."""
            DATASET.visible_channels = (1,)
            reads_before = len(host.reads)
            mask_reads_before = len([r for r in host.released if r[0] in host.v_rings])
            patches_before = len(host.patches)
            done = host.invoke("seg.measure")
            assert done.ok, done.message
            deadline = time.monotonic() + 10.0
            while not host.images and time.monotonic() < deadline:
                time.sleep(0.02)
            assert host.images, "no set_image arrived"
            panel, widget, loan = host.images[-1]
            assert (panel, widget) == (PANEL, "plot") and loan.segment == PANEL_SEG
            assert loan.payload[:8] == b"\x89PNG\r\n\x1a\n"
            width, height = struct.unpack(">II", loan.payload[16:24])
            reads = host.reads[reads_before:]
            frames = SHAPE_TCZYX[0]
            assert len(reads) == frames and [r[0] for r in reads] == list(range(frames)), reads
            DATASET.visible_channels = None
            assert all(r[1] == 1 and r[2] == 0 for r in reads), f"measured channel {sorted({r[1] for r in reads})}, not visible_channels[0] = 1 (ViewState.c is deprecated): {reads}"
            # A plugin may release its last deposit AFTER the plot it drew from
            # it -- the image and the `ReleaseDeposit` are independent -- so the
            # count is waited for, not taken the moment the image lands.
            def released_since() -> int:
                return len([r for r in host.released if r[0] in host.v_rings]) - mask_reads_before

            deadline = time.monotonic() + 5.0
            while released_since() < frames + 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            mask_reads = released_since()
            assert mask_reads == frames + 1, f"{mask_reads} V deposits released: the mask once plus one volume per timepoint"
            status = await_status_set(host, patches_before, timeout=2.0) or host.status_of(PANEL)
            return f"{width}x{height} PNG out of the P arena; {frames} volume reads after one mask read; {status!r}"

        def test_single_plane():
            """Grow through Z off: the result is one plane deep, on the plane
            the drag started (Acceptance 5); Replace starts over."""
            assert host.invoke("seg.set_3d", value=False).ok
            assert host.invoke("seg.set_mode", value="Replace", index=0).ok
            count = len(updates_on(host, "seg.labels"))
            drive(host, state["tool"], loop_around(BLOB_CENTRE[0], BLOB_CENTRE[1], BLOB_CENTRE[2], 5.0))
            push = await_label_push(host, count + 1)
            labels, _, origin, _ = decode_labels(push)
            assert labels.shape[0] == 1 and origin[0] == BLOB_CENTRE[0], (labels.shape, origin)
            full = placed(labels, origin)
            expected = np.zeros_like(BLOB)
            expected[BLOB_CENTRE[0]] = BLOB[BLOB_CENTRE[0]]
            assert np.array_equal(full == 1, expected) and labels.max() == 1, "Replace + single plane is not the ellipsoid's middle plane alone"
            return f"labels {labels.shape} at {origin}: the middle plane only, label 1 only"

        def test_miss_press():
            """§1: a press that contributes nothing -- a miss, off the image --
            still opens the gesture; `z_g` is that Voxel's plane and the loop
            drawn from the next record on segments as usual."""
            count = len(updates_on(host, "seg.labels"))
            path = loop_around(BLOB_CENTRE[0], BLOB_CENTRE[1], BLOB_CENTRE[2], 5.0)
            off_image = Voxel(float(BLOB_CENTRE[0]), -3.0, -3.0)
            gesture = host.press(state["tool"], VIEW_2D, off_image, miss=True)
            for p in path:
                gesture.drag(p)
                time.sleep(0.005)
            gesture.release(path[0])
            push = await_label_push(host, count + 1)
            labels, _, origin, _ = decode_labels(push)
            assert labels.shape[0] == 1 and origin[0] == BLOB_CENTRE[0], (labels.shape, origin)
            full = placed(labels, origin)
            expected = np.zeros_like(BLOB)
            expected[BLOB_CENTRE[0]] = BLOB[BLOB_CENTRE[0]]
            assert np.array_equal(full == 1, expected), "a miss press, then a loop: not the ellipsoid's middle plane"
            return f"miss press at z={BLOB_CENTRE[0]}, then a loop: labels {labels.shape} at {origin}, the middle plane"

        def test_min_size():
            assert host.invoke("seg.set_min_size", value=5000).ok
            count = len(updates_on(host, "seg.labels"))
            drive(host, state["tool"], loop_around(BLOB_CENTRE[0], BLOB_CENTRE[1], BLOB_CENTRE[2], 5.0))
            status = await_status(host, "No component of at least 5000 voxels.")
            assert status == "No component of at least 5000 voxels.", status
            time.sleep(0.2)
            assert len(updates_on(host, "seg.labels")) == count, "a too-small component was pushed"
            return status

        def test_panel_reopen():
            """The panel shows the plugin's state, not the manifest defaults
            (Acceptance 13)."""
            before = len(host.patches)
            host.open_panel(PANEL)
            deadline = time.monotonic() + 5.0
            while len(host.patches) < before + 8 and time.monotonic() < deadline:
                time.sleep(0.02)
            values = {w: v for p, w, v in host.patches[before:] if p == PANEL and not isinstance(v, pb2.PatchOp)}
            assert values.get("size") == 5000 and values.get("grow3d") is False and values.get("mode") == "Replace", values
            assert isinstance(values.get("thresh"), float), values
            enabled = [op for p, w, op in host.patches[before:] if p == PANEL and w == "commit" and isinstance(op, pb2.PatchOp)]
            assert enabled and enabled[-1].set_enabled is True, "commit is enabled while a segmentation exists"
            options = [op for p, w, op in host.patches[before:] if p == PANEL and w == "region" and isinstance(op, pb2.PatchOp)]
            assert options and list(options[-1].set_options.options) == ["Region 1", "Region 2"]
            return f"re-pushed {sorted(values)} + commit enabled + region options"

        def test_busy_guard():
            """One job at a time: a second `seg.measure` while the first is
            reading answers `ok = false` and says so on the status line."""
            images_before = len(host.images)
            patches_before = len(host.patches)
            # The host PARKS the first measure's volume read, so the job is
            # provably running when the second invoke lands.
            host.hold_reads()
            try:
                first = host.invoke("seg.measure")
                host.await_reads(1)
                second = host.invoke("seg.measure")
            finally:
                host.release_reads()
            assert first.ok, first.message
            assert not second.ok, "an overlapping measure was accepted"
            assert second.message, "the refusal carries no message"
            # §"Budgets": "plus the same message in the status line".  Looked
            # for among every value the line took, and NOT required: the panel
            # lane COALESCES by widget, so a refusal pushed while the running
            # job is reporting "timepoint 2/3..." can be superseded before it
            # leaves the plugin.  The refusal itself is asserted above, where
            # it is observable; requiring it here makes the check depend on
            # which push wins a race the protocol says either may.
            if not await_until(lambda: second.message in statuses_since(host, patches_before), timeout=2.0):
                note = " (the refusal's line was coalesced away by the running job's reports)"
            else:
                note = ""
            assert await_until(lambda: len(host.images) > images_before, timeout=15.0), "the first measure never finished (no plot)"
            status = host.status_of(PANEL)
            return f"second measure refused: {second.message!r}{note}; first finished with its plot; status {status!r}"

        def test_reset():
            """The overlay retracted and `commit` re-disabled (§"The UI state
            model"); what the line says is the plugin's."""
            removes_before = len(removes_on(host, "seg.labels"))
            disables_before = len([op for op in ops_on(host, "commit", "set_enabled") if op.set_enabled is False])
            patches_before = len(host.patches)
            assert host.invoke("seg.reset").ok
            assert await_until(lambda: len(removes_on(host, "seg.labels")) > removes_before, timeout=5.0), "the label overlay was not retracted on reset"
            assert await_until(lambda: len([op for op in ops_on(host, "commit", "set_enabled") if op.set_enabled is False]) > disables_before, timeout=5.0), "commit was not re-disabled on reset"
            status = await_status_set(host, patches_before, timeout=2.0) or host.status_of(PANEL)
            return f"overlay retracted; commit disabled; {status!r}"

        def test_no_violations():
            assert unexpected_violations(host) is None, unexpected_violations(host)
            assert not host.unhandled, host.unhandled
            quarantined = [s.lane.quarantined for s in host.segments.values() if s.purpose == "O" and s.lane.quarantined]
            assert not quarantined, quarantined
            return "no ContractViolation about this emulator's traffic, no quarantined lane"

        def test_teardown():
            closed = host.shutdown()
            assert closed, f"the stream did not end within {SHUTDOWN_WINDOW_S:g} s"
            assert host.stream_error is None, host.stream_error
            report = host.shutdown_report
            assert report is not None and report.outcome in (pb2.ShutdownReport.NO_HOOK, pb2.ShutdownReport.COMPLETED), report
            # deferred repeats flush at shutdown (§7): the violation bound holds over the whole run
            assert unexpected_violations(host) is None, unexpected_violations(host)
            return f"ShutdownReport {pb2.ShutdownReport.Outcome.Name(report.outcome)}, then a clean end of stream; {len(host.violations)} violation report(s) in all, none unexpected"

        check("handshake", test_handshake)
        if failures:
            print("\nthe handshake failed; nothing else was run", file=sys.stderr)
            return 1
        check("grants: two O in both orders, P, V", test_grants)
        check("panel opened: defaults", test_panel_opened)
        check("arrangements -> activated, or the no-hook fallback", test_arrangements)
        check("seg.pick: ActivateTool; MIP refused", test_pick_activates_and_refuses_mip)
        check("lasso -> trail on the lane -> label volume", test_lasso_segments_the_blob)
        check("loose loop floods, and says so", test_loose_loop_floods_and_says_so)
        check("bare click", test_bare_click)
        check("cancel mid-gesture", test_cancel_clears)
        check("level 1 refused", test_level_refused)
        check("Column (projection) refused", test_column_refused)
        check("two channels refused", test_two_channels_refused)
        check("unusable record: nothing taken, next gesture live", test_unusable_record)
        check("anchor rule", test_anchor_rule)
        check("Add: a second label", test_add_mode)
        check("commit -> masks, twice", test_commit)
        check("measure -> plot", test_measure)
        check("Grow through Z off, Replace", test_single_plane)
        check("miss press, then a loop", test_miss_press)
        check("minimum size", test_min_size)
        check("panel reopen", test_panel_reopen)
        check("busy guard", test_busy_guard)
        check("reset", test_reset)
        check("no violations", test_no_violations)
        check("teardown", test_teardown)

    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("\nhost emulation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
