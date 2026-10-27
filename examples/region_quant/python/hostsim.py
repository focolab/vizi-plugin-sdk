#!/usr/bin/env python3
"""Drive the region-quant example over the real transport, with no vizi.

    .venv/bin/python hostsim.py                       # this implementation
    .venv/bin/python hostsim.py --plugin-dir ../rust  # any other implementation

The scenarios are ``../SPEC.md``'s driver checks: the view and region
subscriptions, a list with a row per region and a colormap swatch per row,
set-view from a row click and from "Brightest frame", the channel picker, a
Follow tour taken back and resumed, and the export through the host's save
dialog.  Every expected value is computed
here from the dataset and the emulator's box rule, never by the plugin's code.
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # examples/, for vizi_hostsim

from vizi_hostsim import Dataset, Host, Segment, check, failures, segment_id  # noqa: E402
from vizi_plugin_sdk import pb2, roi_pb2  # noqa: E402

PANEL, LIST = "quant", "regions"
FLOAT_STATUS = "Region Quant measures integer data; this dataset is f32"
PANEL_SEG = segment_id(1)
T, C, Z, Y, X = 5, 2, 4, 12, 12
#: SPEC §5: a cause is answered within 1 s; the emulator's reader thread may
#: hold a request behind one slow read, so the bound allows for that.
ANSWER_S = 1.5
SLOW_READ_S = 0.5


def source(t: int, c: int) -> np.ndarray:
    z, y, x = np.meshgrid(np.arange(Z), np.arange(Y), np.arange(X), indexing="ij")
    return (500 * c + 10 * z + 5 * y + x + 60 * ((t + x + y) % 5)).astype(np.uint16)


DATASET = Dataset(
    shape_tczyx=(T, C, Z, Y, X),
    np_dtype=np.dtype(np.uint16),
    wire_dtype=pb2.DTYPE_U16,
    open_generation=11,
    volume=source,
    t=1,
    visible_channels=(0,),
)

# -- regions: boxes the "person" drew, with two corner vertices (the emulator's
#    box rule: every voxel whose center is within the corners, inclusive) -------


def box(lo, hi) -> roi_pb2.Roi:
    return roi_pb2.Roi(type_name="box", vertices=[
        roi_pb2.Vertex(position=roi_pb2.Vec3(z=lo[0], y=lo[1], x=lo[2])),
        roi_pb2.Vertex(position=roi_pb2.Vec3(z=hi[0], y=hi[1], x=hi[2])),
    ])


BOXES = {
    "alpha": ((0, 2, 2), (1, 4, 5)),
    "beta": ((1, 6, 1), (3, 9, 3)),
    "gamma": ((2, 0, 7), (3, 3, 11)),
    "beta2": ((0, 5, 5), (2, 7, 8)),  # beta, redrawn
}


def measured(lo, hi, t: int, c: int) -> tuple[int, int]:
    block = source(t, c)[lo[0]: hi[0] + 1, lo[1]: hi[1] + 1, lo[2]: hi[2] + 1].astype(np.uint64)
    return int(block.size), int(block.sum())


def mean_text(total: int, count: int) -> str:
    if count == 0:
        return ""
    th = (2000 * total + count) // (2 * count)
    return f"{th // 1000}.{th % 1000:03d}"


def swatch_index(total: int, count: int) -> int:
    return 0 if count == 0 else min(255, (total // count) >> 8)


def decode_png(data: bytes) -> np.ndarray:
    """An 8-bit RGB or RGBA PNG, every filter type, as (h, w, 3)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, idat, width = 8, b"", None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos: pos + 4])
        kind, body = data[pos + 4: pos + 8], data[pos + 8: pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
            assert depth == 8 and ctype in (2, 6) and interlace == 0, (depth, ctype, interlace)
            bpp = 3 if ctype == 2 else 4
        elif kind == b"IDAT":
            idat += body
    raw = zlib.decompress(idat)
    stride = width * bpp
    out = np.zeros((height, stride), dtype=np.uint8)
    prev = np.zeros(stride, dtype=np.int32)
    for r in range(height):
        f, line = raw[r * (stride + 1)], np.frombuffer(raw[r * (stride + 1) + 1: (r + 1) * (stride + 1)], dtype=np.uint8).astype(np.int32)
        cur = np.zeros(stride, dtype=np.int32)
        for i in range(stride):
            a = cur[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 0:
                p = 0
            elif f == 1:
                p = a
            elif f == 2:
                p = b
            elif f == 3:
                p = (a + b) // 2
            else:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                p = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
            cur[i] = (line[i] + p) & 0xFF
        out[r] = cur
        prev = cur
    return out.reshape(height, width, bpp)[:, :, :3]


def wait_for(pred, what: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(0.03)
    raise AssertionError(f"{what} within {timeout:g}s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-dir", type=Path, default=HERE)
    args = parser.parse_args()
    host = Host(args.plugin_dir.resolve(), DATASET)
    ids = {name: host.add_region(box(*BOXES[name]), name=name) for name in ("beta", "alpha")}
    host.add(Segment.plugin_arena(PANEL_SEG, "P", 1 << 20, host.directory, subject=PANEL))
    out = Path(tempfile.mkdtemp(prefix="region-quant-"))
    shapes = {ids["alpha"]: BOXES["alpha"], ids["beta"]: BOXES["beta"]}
    names = {ids["alpha"]: "alpha", ids["beta"]: "beta"}
    deleted: list[str] = []
    #: The plugin's channel as the checks leave it: 1 from check 3 on (check 8
    #: puts it back), 0 once check 16's one-channel dataset resets it.
    current = {"c": 1}

    def cell(widget: str, row: str):
        found = [v for p, w, r, v in host.row_patches if p == PANEL and w == widget and r == row]
        return found[-1] if found else None

    def rows_are(t: int, c: int) -> bool:
        keys = host.list_rows.get((PANEL, LIST))
        want = sorted(shapes, key=lambda rid: (names[rid], rid))
        if keys != want:
            return False
        for rid in want:
            count, total = measured(*shapes[rid], t, c)
            if (cell("name", rid), cell("voxels", rid), cell("mean", rid)) != (names[rid], str(count), mean_text(total, count)):
                return False
        return True

    def swatches_right(t: int, c: int) -> bool:
        table = np.frombuffer(host.colormaps["viridis"], dtype=np.uint8).reshape(256, 4)
        for rid in shapes:
            loan = host.row_images.get((PANEL, "swatch", rid))
            if loan is None:
                return False
            count, total = measured(*shapes[rid], t, c)
            pixels = decode_png(loan.payload)
            if not (pixels == table[swatch_index(total, count)][:3]).all():
                return False
        return True

    with host:
        wait_for(lambda: host.ready, "Ready")
        # vizi's order: P granted and PanelOpened sent in
        # one step, with no wait for the GrantAck.
        host.open_panel(PANEL, grant=PANEL_SEG)

        def test_start():
            """The subscriptions arranged; the first pushes build a row per
            region, in name order, measured at the frame on screen on channel
            0, each with its swatch."""
            assert {"view", "regions"} <= host.subscriptions, host.subscriptions
            wait_for(lambda: rows_are(DATASET.t, 0), "the first rows")
            wait_for(lambda: swatches_right(DATASET.t, 0), "the swatches")
            return f"rows {host.list_rows[(PANEL, LIST)]} at t={DATASET.t}, channel 0, swatches from GetColormap"

        def test_frame():
            """The frame moves: every row is measured again."""
            DATASET.t = 3
            wait_for(lambda: rows_are(3, 0) and swatches_right(3, 0), "the rows at t=3")
            return "re-measured at t=3"

        def test_channel():
            """The channel picker: every row on channel 1."""
            done = host.invoke_widget(PANEL, "channel", value=1)
            assert done.ok, done.message
            wait_for(lambda: rows_are(3, 1) and swatches_right(3, 1), "the rows on channel 1")
            return "re-measured on channel 1"

        def test_regions_come_and_go():
            """A region added gets a row in order; one redrawn is measured
            again; one deleted loses its row."""
            gamma = host.add_region(box(*BOXES["gamma"]), name="gamma")
            shapes[gamma], names[gamma] = BOXES["gamma"], "gamma"
            wait_for(lambda: rows_are(3, 1), "the gamma row")
            redrawn = pb2.RoiRecord()
            redrawn.CopyFrom(host.rois[ids["beta"]])
            redrawn.roi.CopyFrom(box(*BOXES["beta2"]))
            with host._lock:
                host.rois[ids["beta"]] = redrawn  # a new record: a new revision
            shapes[ids["beta"]] = BOXES["beta2"]
            wait_for(lambda: rows_are(3, 1), "beta re-measured")
            host.delete_region(gamma)
            deleted.append(gamma)
            del shapes[gamma], names[gamma]
            wait_for(lambda: rows_are(3, 1), "gamma's row gone")
            return "added, redrawn, deleted: the rows follow"

        def test_selection_shown():
            """vizi's selection is the list's selection."""
            host.select_region(ids["alpha"])
            wait_for(lambda: host.list_selection.get((PANEL, LIST)) == ids["alpha"], "the list selecting alpha")
            return "vizi selects alpha; the list does too"

        def test_row_click_selects():
            """A row click is a set-view cause.  With the person's switch off
            -- as it is at every open in vizi -- the host refuses it
            `NOT_ENABLED` and the status says so; with it on, the plugin's
            request to select that region is applied."""
            assert not host.view_toggle, "the switch is off at open"
            done = host.invoke_widget(PANEL, LIST, row=ids["beta"])
            assert done.ok, done.message
            assert host.view_requests[-1][1].reason == pb2.VIEW_REFUSAL_NOT_ENABLED, host.view_requests[-1][1]
            wait_for(lambda: host.status_of(PANEL) == "The view did not move: NOT_ENABLED", "the NOT_ENABLED status")
            host.set_view_toggle(PANEL, True)
            done = host.invoke_widget(PANEL, LIST, row=ids["beta"])
            assert done.ok, done.message
            req, res = host.view_requests[-1]
            assert req.cause == host.last_cause and req.select_region == ids["beta"], req
            assert res.outcome == pb2.VIEW_OUTCOME_APPLIED, res
            assert DATASET.selected_region_id == ids["beta"]
            wait_for(lambda: host.status_of(PANEL) == "Selected beta", "the status")
            wait_for(lambda: host.list_selection.get((PANEL, LIST)) == ids["beta"], "the list selecting beta")
            return "clicked beta: NOT_ENABLED with the switch off, then select_region applied"

        def best_frame(c: int) -> int:
            lo, hi = BOXES["beta2"]
            means = [measured(lo, hi, t, c) for t in range(T)]
            return max(range(T), key=lambda t: (means[t][1] / means[t][0], -t))

        def press_brightest(deadline_s: float = 15.0):
            """Press until brightest answers from a complete trace: a press
            that finds the trace still running is declined with "Measuring…",
            which is correct, so it is pressed again.  Every press must be
            answered within ANSWER_S."""
            deadline = time.monotonic() + deadline_s
            while True:
                pressed = time.monotonic()
                done = host.invoke_widget(PANEL, "brightest")
                assert done.ok, done.message
                req, res = host.view_requests[-1]
                took = time.monotonic() - pressed
                assert took < ANSWER_S, f"brightest took {took:.2f} s to answer (a synchronous scan?)"
                if not (req.decline and host.await_status(PANEL, "Measuring…").startswith("Measuring…")):
                    return req, res, took
                assert time.monotonic() < deadline, "the trace never completed"
                time.sleep(0.2)

        def test_brightest():
            """'Brightest frame': t moves to the frame where the selected
            region's mean is highest, the earliest on a tie -- answered from
            the trace, at once."""
            best = best_frame(1)
            req, res, _ = press_brightest()
            assert req.t == best and res.outcome == pb2.VIEW_OUTCOME_APPLIED, (req, res)
            wait_for(lambda: host.status_of(PANEL) == f"Brightest frame for beta: t={best}", "the status")
            assert DATASET.t == best
            wait_for(lambda: rows_are(best, 1), f"the rows at t={best}")
            return f"t moved to {best}"

        def test_brightest_while_measuring():
            """A slow host (each volume read 0.5 s: a synchronous scan of the
            5 frames would take 2.5 s and go STALE).  The channel changes;
            brightest pressed at once declines "Measuring… k of 5 frames",
            within ANSWER_S; once `tracing` is empty it answers APPLIED."""
            host.read_delay = SLOW_READ_S
            try:
                assert host.invoke_widget(PANEL, "channel", value=0).ok
                pressed = time.monotonic()
                done = host.invoke_widget(PANEL, "brightest")
                assert done.ok, done.message
                took = time.monotonic() - pressed
                req, res = host.view_requests[-1]
                assert took < ANSWER_S, f"brightest took {took:.2f} s to answer (a synchronous scan?)"
                assert req.decline and res.outcome == pb2.VIEW_OUTCOME_DECLINED, (req, res)
                said = host.await_status(PANEL, "Measuring…")
                m = re.fullmatch(r"Measuring… (\d+) of 5 frames", said)
                assert m and int(m.group(1)) < 5, said
                wait_for(lambda: host.status_of(PANEL, widget="tracing").startswith("Measuring"), "tracing shown", 10.0)
                wait_for(lambda: host.status_of(PANEL, widget="tracing") == "", "the traces done", 60.0)
                best = best_frame(0)
                req, res, took2 = press_brightest()
                assert req.t == best and res.outcome == pb2.VIEW_OUTCOME_APPLIED, (req, res)
                wait_for(lambda: host.status_of(PANEL) == f"Brightest frame for beta: t={best}", "the status")
            finally:
                # Back as the later checks expect, whatever happened above.
                host.read_delay = 0.0
                assert host.invoke_widget(PANEL, "channel", value=1).ok
                wait_for(lambda: rows_are(DATASET.t, 1), "the rows back on channel 1")
            return f"declined {said!r} in {took:.2f} s; then t={best} from the trace in {took2:.2f} s"

        def test_brightest_without_selection():
            """Nothing selected: the cause is declined, never left silent."""
            host.select_region("")
            wait_for(lambda: host.list_selection.get((PANEL, LIST)) == "", "the list selecting nothing")
            done = host.invoke_widget(PANEL, "brightest")
            assert done.ok, done.message
            req, res = host.view_requests[-1]
            assert req.decline and res.outcome == pb2.VIEW_OUTCOME_DECLINED, (req, res)
            wait_for(lambda: host.status_of(PANEL) == "Select a region first", "the status")
            return "declined, and said why"

        def in_order() -> list[str]:
            return sorted(shapes, key=lambda r: (names[r], r))

        def tour_of(fid: int) -> list[str]:
            return [targets["frame_region"] for f, _, targets in host.follow_applied if f == fid]

        def ended(fid: int) -> pb2.FollowEnded:
            return wait_for(lambda: next((e for e in host.follows_ended if e.follow_id == fid), None), f"Follow {fid} ended")

        def test_offer():
            """SPEC §7: the offer follows the number of rows."""
            wait_for(lambda: host.follow_offers.get(PANEL) == f"Tour {len(shapes)} regions", "the offer")
            return f"offered {host.follow_offers[PANEL]!r}"

        def test_tour():
            """The person presses Follow: every row framed in list order, then
            the plugin ends its own Follow."""
            fid = host.press_follow(PANEL)
            assert fid is not None, "the Follow button could not be pressed"
            end = ended(fid)
            assert end.reason == pb2.FOLLOW_END_PLUGIN_ENDED, (pb2.FollowEndReason.Name(end.reason), end.detail)
            assert tour_of(fid) == in_order(), (tour_of(fid), in_order())
            wait_for(lambda: host.status_of(PANEL) == "Tour finished", "the status")
            return f"framed {[names[r] for r in tour_of(fid)]}, then PLUGIN_ENDED"

        def test_taken_back_and_resumed():
            """While a tour runs a row click is refused FOLLOW_ACTIVE; the
            person's selection ends it; Resume shows only what was left."""
            order = in_order()
            fid = host.press_follow(PANEL)
            assert fid is not None
            wait_for(lambda: tour_of(fid), "the tour's first state")
            done = host.invoke_widget(PANEL, LIST, row=order[-1])
            assert done.ok, done.message
            req, res = host.view_requests[-1]
            assert res.reason == pb2.VIEW_REFUSAL_FOLLOW_ACTIVE, (req, res)
            host.person_touches("selection")
            end = ended(fid)
            assert end.reason == pb2.FOLLOW_END_USER_TOOK_CONTROL, pb2.FollowEndReason.Name(end.reason)
            wait_for(lambda: host.status_of(PANEL) == "Tour stopped: USER_TOOK_CONTROL", "the stopped status")
            shown = tour_of(fid)
            rest = order[order.index(shown[-1]) + 1:]
            again = host.resume_follow()
            assert again is not None and host.follows_started[-1].resumed, host.follows_started[-1]
            end = ended(again)
            assert end.reason == pb2.FOLLOW_END_PLUGIN_ENDED, (pb2.FollowEndReason.Name(end.reason), end.detail)
            assert tour_of(again) == rest, (tour_of(again), rest)
            wait_for(lambda: host.status_of(PANEL) == "Tour finished", "the finished status")
            return f"FOLLOW_ACTIVE; taken back after {[names[r] for r in shown]}; resumed with {[names[r] for r in rest]}"

        def test_every_cause_answered():
            """§8.15: the plugin answers every cause from a moves_view surface."""
            silent = [c for c, v in host._causes.items() if v["answer"] is None]
            assert not silent, silent
            return f"{len(host._causes)} causes, every one answered"

        def test_export():
            """'Export CSV…': the host's save dialog, the extension added, the
            table written exactly; then a cancel."""
            host.save_path_answer = str(out / "table")
            done = host.invoke_widget(PANEL, "export")
            assert done.ok, done.message
            path = out / "table.csv"
            wait_for(lambda: host.status_of(PANEL) == f"Exported 2 regions to {path}", "the export's status")
            t = DATASET.t
            lines = ["region_id,name,t,channel,voxels,mean"]
            for rid in sorted(shapes, key=lambda r: (names[r], r)):
                count, total = measured(*shapes[rid], t, 1)
                lines.append(f"{rid},{names[rid]},{t},1,{count},{mean_text(total, count)}")
            want = "\n".join(lines) + "\n"
            got = path.read_bytes().decode("utf-8")
            assert got == want, (got, want)
            host.save_path_answer = None
            assert host.invoke_widget(PANEL, "export").ok
            wait_for(lambda: host.status_of(PANEL) == "Export canceled", "the cancel's status")
            return f"{path.name} written exactly; a cancel said so"

        def test_many_regions():
            """SPEC check 15: 70 regions: every row listed and measured.  Then
            the extra 68 are deleted."""
            extra = []
            for n in range(68):
                z, y, x = n % Z, (n // Z) % Y, (n // (Z * Y)) * 4
                lo = hi = (z, y, x)
                rid = host.add_region(box(lo, hi), name=f"r{n:02d}")
                shapes[rid], names[rid] = (lo, hi), f"r{n:02d}"
                extra.append(rid)
            wait_for(lambda: rows_are(DATASET.t, current["c"]), "70 rows", timeout=60)
            for rid in extra:
                host.delete_region(rid)
                deleted.append(rid)
                del shapes[rid], names[rid]
            wait_for(lambda: rows_are(DATASET.t, current["c"]), "the 68 rows gone", timeout=30)
            return "70 rows listed and measured; 68 deleted again"

        def test_second_dataset():
            """SPEC check 16: a dataset with one channel opens while
            c is 1: c resets to 0, the picker says so, every row on channel 0."""
            assert current["c"] == 1, current
            one = Dataset(shape_tczyx=(T, 1, Z, Y, X), np_dtype=np.dtype(np.uint16), wire_dtype=pb2.DTYPE_U16,
                          open_generation=DATASET.open_generation + 1, volume=source, t=DATASET.t, visible_channels=(0,))
            host.dataset = one
            current["c"] = 0
            wait_for(lambda: host.status_of(PANEL, "channel") == "0", "the channel picker set to 0")
            wait_for(lambda: rows_are(one.t, 0), "the rows on channel 0 of the new dataset")
            return "c reset to 0 on a one-channel dataset; the picker set; re-measured"

        def test_float_dataset():
            """SPEC check 17: an f32 dataset is not measured, and the
            status says why; Brightest declines the same; the integer dataset
            back, the rows are measured again."""
            gen = host.dataset.open_generation
            floats = Dataset(shape_tczyx=(T, 1, Z, Y, X), np_dtype=np.dtype(np.float32), wire_dtype=pb2.DTYPE_F32,
                             open_generation=gen + 1, volume=lambda t, c: source(t, c).astype(np.float32), t=DATASET.t, visible_channels=(0,))
            host.dataset = floats
            wait_for(lambda: host.status_of(PANEL) == FLOAT_STATUS, "the float status")
            wait_for(lambda: all(cell("voxels", rid) == "" and cell("mean", rid) == "" for rid in shapes), "empty numbers")
            done = host.invoke_widget(PANEL, "brightest")
            assert done.ok, done.message
            assert host.view_requests[-1][0].decline, host.view_requests[-1][0]
            wait_for(lambda: host.status_of(PANEL) == FLOAT_STATUS, "Brightest's status")
            again = Dataset(shape_tczyx=(T, C, Z, Y, X), np_dtype=np.dtype(np.uint16), wire_dtype=pb2.DTYPE_U16,
                            open_generation=gen + 2, volume=source, t=DATASET.t, visible_channels=(0,))
            host.dataset = again
            wait_for(lambda: rows_are(again.t, 0) and host.status_of(PANEL) == "Ready", "measured again, Ready")
            return "f32: nothing measured, the status says so, Brightest declines; integer data measured again"

        def test_no_dataset():
            """SPEC check 18: with no dataset open, vizi sends a
            `moves_view` interaction with cause 0.  A row click and Brightest
            each send no view request and say `"No dataset is open"`; the
            dataset back, the rows are measured again."""
            requests = len(host.view_requests)
            host.close_dataset()
            key = next(iter(shapes))
            for widget, args in ((LIST, {"row": key}), ("brightest", {})):
                host.patches.append((PANEL, "status", ""))  # a marker: the next status must be new
                done = host.invoke_widget(PANEL, widget, **args)
                assert done.ok, done.message
                assert host.last_cause == 0
                wait_for(lambda: host.status_of(PANEL) == "No dataset is open", f"{widget}'s status")
            time.sleep(0.3)
            assert len(host.view_requests) == requests, host.view_requests[requests:]
            host.open_dataset()
            wait_for(lambda: rows_are(host.dataset.t, current["c"]), "the rows measured again")
            return "a row click and Brightest with cause 0: no request, 'No dataset is open'; measured again on reopening"

        def test_clean():
            """Nothing refused.  A cell op for a row the driver deleted, queued
            before the plugin's newer `set_rows` and drained after it, is
            refused like anything else: an SDK drops such ops."""
            assert not host.unhandled, host.unhandled
            assert not host.violations, [(pb2.ViolationSite.Name(v.where), v.detail) for v in host.violations]
            assert not host.refusals, host.refusals
            assert not host.diagnostics, host.diagnostics
            return "nothing refused, nothing unhandled"

        for label, fn in (
            ("start: a row per region, measured, with swatches", test_start),
            ("the frame moves: re-measured", test_frame),
            ("the channel picker: re-measured", test_channel),
            ("regions added, redrawn, deleted", test_regions_come_and_go),
            ("vizi's selection shown in the list", test_selection_shown),
            ("a row click selects the region (set-view)", test_row_click_selects),
            ("brightest frame moves t (set-view)", test_brightest),
            ("brightest frame while measuring declines at once", test_brightest_while_measuring),
            ("brightest frame with nothing selected declines", test_brightest_without_selection),
            ("the Follow offer follows the rows", test_offer),
            ("a tour frames every row, then ends itself", test_tour),
            ("a tour taken back, then resumed", test_taken_back_and_resumed),
            ("every cause answered", test_every_cause_answered),
            ("export through the save dialog", test_export),
            ("70 regions: every row listed and measured", test_many_regions),
            ("a second dataset with one channel: c resets to 0", test_second_dataset),
            ("a float dataset: not measured, and said so", test_float_dataset),
            ("no dataset open: cause 0, nothing sent, and said so", test_no_dataset),
            ("nothing refused", test_clean),
        ):
            check(label, fn)

    def test_relaunch(visible: bool):
        """vizi's crash relaunch: the panel stays on
        screen, and the relaunched session is granted `P`.  vizi then posts
        `PanelOpened` at once, without waiting for the `GrantAck`, when the
        dock is visible, and nothing when it is hidden.
        Either way the rows, measured, reach the panel: a plugin must not
        wait for a `PanelOpened` that may never come, and must not lose its
        state to one that arrives before its `P` is installed."""
        again = Host(args.plugin_dir.resolve(), DATASET)
        fresh = {name: again.add_region(box(*BOXES[name]), name=name) for name in ("beta", "alpha")}
        with again:
            wait_for(lambda: again.ready, "Ready")
            again.reprovision_panel(PANEL, Segment.plugin_arena(segment_id(2), "P", 1 << 20, again.directory, subject=PANEL), visible=visible)
            want = [fresh[n] for n in ("alpha", "beta")]

            def shown() -> bool:
                if again.list_rows.get((PANEL, LIST)) != want:
                    return False
                for name, rid in fresh.items():
                    count, total = measured(*BOXES[name], DATASET.t, 0)
                    got = {w: v for p, w, r, v in again.row_patches if p == PANEL and r == rid}
                    if (got.get("name"), got.get("voxels"), got.get("mean")) != (name, str(count), mean_text(total, count)):
                        return False
                    loan = again.row_images.get((PANEL, "swatch", rid))
                    if loan is None:
                        return False
                    table = np.frombuffer(again.colormaps["viridis"], dtype=np.uint8).reshape(256, 4)
                    if not (decode_png(loan.payload) == table[swatch_index(total, count)][:3]).all():
                        return False
                return True

            wait_for(shown, "the rows on the relaunched session's panel")
            assert not again.refusals, again.refusals
        return f"rows and swatches for {len(want)} regions, measured at t={DATASET.t}, " + ("PanelOpened right after the grant" if visible else "with no PanelOpened")

    check("a relaunch, hidden dock: P re-granted, no PanelOpened, the rows and swatches still arrive", lambda: test_relaunch(False))
    check("a relaunch, visible dock: P re-granted, PanelOpened before its ack, the rows and swatches still arrive", lambda: test_relaunch(True))
    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("\nhost emulation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
