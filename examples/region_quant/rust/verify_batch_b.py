#!/usr/bin/env python3
"""Cases beyond the shared driver (same-channel no-ops, partial commits, cause 0,
export failures, the row cap, held swatches, f32); run after a release build."""
from pathlib import Path
import sys
import tempfile
import time

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from vizi_hostsim import Dataset, Host, Segment, segment_id  # noqa: E402
from vizi_plugin_sdk import pb2, roi_pb2  # noqa: E402

FLOAT_STATUS = "Region Quant measures integer data; this dataset is f32"


class FaultHost(Host):
    failure = ""
    inclusion_reads = 0

    def _on_read_roi_inclusion(self, request_id, request):
        self.inclusion_reads += 1
        if request.region_id == self.failure:
            return self._fail(request_id, pb2.Error.INTERNAL, "temporary measurement failure")
        return super()._on_read_roi_inclusion(request_id, request)


def dataset(generation=11, floating=False):
    dtype = np.float32 if floating else np.uint16
    return Dataset(
        shape_tczyx=(2, 2, 1, 1, 1), np_dtype=np.dtype(dtype),
        wire_dtype=pb2.DTYPE_F32 if floating else pb2.DTYPE_U16,
        open_generation=generation, visible_channels=(0,),
        volume=lambda t, c: np.full((1, 1, 1), 100 + t + c, dtype=dtype),
    )


def setup(count=3, floating=False):
    host = FaultHost(HERE, dataset(floating=floating))
    ids = [host.add_region(roi_pb2.Roi(type_name="box", vertices=[
        roi_pb2.Vertex(position=roi_pb2.Vec3(z=0, y=0, x=0)),
        roi_pb2.Vertex(position=roi_pb2.Vec3(z=0, y=0, x=0)),
    ]), name=f" region {n:04}") for n in range(count)]
    panel = segment_id(1)
    host.add(Segment.plugin_arena(panel, "P", 1 << 20, host.directory, subject="quant"))
    return host, ids, panel


def wait(host, predicate, what, timeout=30):
    return host._await(lambda: predicate() or None, timeout, what)


def cells(host):
    return {(w, r): v for p, w, r, v in host.row_patches if p == "quant"}


def clean(host):
    assert not host.unhandled, host.unhandled
    assert not host.violations, host.violations
    assert not host.refusals, host.refusals


def failures_and_commands():
    host, ids, panel = setup()
    with host, tempfile.TemporaryDirectory() as output:
        wait(host, lambda: host.ready, "Ready")
        host.open_panel("quant", grant=panel)
        wait(host, lambda: len(host.reads) >= 9 and host.status_of("quant", "tracing") == "", "traces complete")
        before = host.inclusion_reads
        assert host.invoke_widget("quant", "channel", value=0).ok
        time.sleep(0.15)
        assert host.inclusion_reads == before, "same-channel selection read data"
        host.failure = ids[1]
        host.dataset.t = 1
        wait(host, lambda: cells(host).get(("mean", ids[2])) == "101.000", "partial commit")
        assert cells(host)["mean", ids[1]] == "100.000"
        assert host.status_of("quant") == "Measuring failed: temporary measurement failure"
        host.failure = ""
        # A cause-zero command must never send a ViewRequest.
        before = len(host.view_requests)
        for command in ("quant.pick", "quant.brightest"):
            assert host.invoke(command, row=ids[0]).ok
            wait(host, lambda: host.status_of("quant") == "No dataset is open", "cause-zero status")
            assert len(host.view_requests) == before
        host.save_path_answer = str(Path(output) / "missing" / "table.csv")
        assert host.invoke_widget("quant", "export").ok
        wait(host, lambda: host.status_of("quant").startswith("Export failed: "), "failed write status")
        # Both cause-bearing commands must decline a parked model within 1 s.
        host.set_view_toggle("quant", True)
        host.hold_reads()
        host.dataset.t = 0
        host.await_reads(timeout=10)
        previous = host.status_of("quant")
        for widget in ("regions", "brightest"):
            start = time.monotonic()
            assert host.invoke_widget("quant", widget, row=ids[0], timeout=1.5).ok
            assert time.monotonic() - start < 1.0
            assert host.view_requests[-1][0].decline
            assert host.status_of("quant") == previous
        host.release_reads()
        wait(host, lambda: cells(host).get(("mean", ids[2])) == "100.000", "resumed table")
        clean(host)
        assert host.shutdown()
    print("ok    same-channel no-op, partial errors, cause 0, export failure, bounded pick and brightest")


def large_list_and_float_export():
    host, ids, panel = setup(1025, floating=True)
    with host, tempfile.TemporaryDirectory() as output:
        wait(host, lambda: host.ready, "Ready")
        # No PanelOpened: the SDK must flush the entire held image set on grant.
        wait(host, lambda: ("mean", "set_value", ids[1023]) in host._held_patches.get("quant", {}), "held row cells")
        host.reprovision_panel("quant", panel)
        wait(host, lambda: len(host.row_images) == 1024, "1024 held swatches")
        assert host.status_of("quant") == FLOAT_STATUS
        assert host.inclusion_reads == 0 and not host.reads
        values = cells(host)
        assert all(values["voxels", rid] == values["mean", rid] == "" for rid in ids[:1024])
        host.save_path_answer = str(Path(output) / "float.csv")
        assert host.invoke_widget("quant", "export").ok
        wait(host, lambda: host.status_of("quant").startswith("Exported "), "float export")
        lines = (Path(output) / "float.csv").read_text().splitlines()
        assert len(lines) == 1026
        assert lines[1] == f"{ids[0]}, region 0000,0,0,,", lines[1]
        host.dataset = dataset(generation=12)
        wait(host, lambda: host.status_of("quant") == "Showing 1024 of 1025 regions", "cap status", timeout=90)
        wait(host, lambda: cells(host).get(("mean", ids[1023])) == "100.000", "last visible integer row", timeout=90)
        assert host.list_rows["quant", "regions"] == ids[:1024]
        clean(host)
        assert host.shutdown()
    print("ok    1025 regions capped at 1024; all held swatches; f32 reads nothing and exports empty numbers")


if __name__ == "__main__":
    failures_and_commands()
    large_list_and_float_export()
