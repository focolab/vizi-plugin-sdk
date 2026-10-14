#!/usr/bin/env python3
"""A prompt row-click decline while a view read is parked, and partial commits
when a region disappears mid-measure, over real sockets; run after a release build."""
from pathlib import Path
import sys
import time

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from vizi_hostsim import Dataset, Host, Segment, segment_id  # noqa: E402
from vizi_plugin_sdk import pb2, roi_pb2  # noqa: E402


class RacingHost(Host):
    missing = ""

    def _on_read_roi_inclusion(self, request_id, request):
        if request.region_id == self.missing:
            # The fetched RoiRecord existed, but its measurement is gone.
            # Keep the host's region snapshot unchanged so no later deletion
            # delta can conceal a plugin that discarded this whole update.
            return self._fail(request_id, pb2.Error.NOT_FOUND, "deleted during measurement")
        return super()._on_read_roi_inclusion(request_id, request)


def exercise(scenario):
    dataset = Dataset(
        shape_tczyx=(2, 2, 2, 2, 2), np_dtype=np.dtype(np.uint16),
        wire_dtype=pb2.DTYPE_U16, open_generation=11,
        volume=lambda t, c: np.full((2, 2, 2), 100 + t + c, dtype=np.uint16),
        t=0, visible_channels=(0,),
    )
    host = RacingHost(HERE, dataset)
    ids = [host.add_region(roi_pb2.Roi(type_name="box", vertices=[
        roi_pb2.Vertex(position=roi_pb2.Vec3(z=0, y=0, x=0)),
        roi_pb2.Vertex(position=roi_pb2.Vec3(z=1, y=1, x=1)),
    ]), name=name) for name in ("alpha", "beta", "gamma")]
    panel = segment_id(1)
    host.add(Segment.plugin_arena(panel, "P", 1 << 20, host.directory, subject="quant"))
    if scenario == "regions":
        host.missing = ids[1]
    with host:
        host._await(lambda: host.ready or None, 10, "Ready")
        host.open_panel("quant", grant=panel)
        expected = [ids[0], ids[2]] if scenario == "regions" else ids
        host._await(lambda: (host.list_rows.get(("quant", "regions")) == expected) or None, 10, "initial rows")
        if scenario in ("view", "channel"):
            host.missing = ids[1]
            if scenario == "view":
                dataset.t = 1
            else:
                assert host.invoke_widget("quant", "channel", value=1).ok
            host._await(lambda: (host.list_rows.get(("quant", "regions")) == [ids[0], ids[2]]) or None,
                        10, "surviving rows committed")
        elif scenario == "pick":
            # Let initial traces finish before parking the view re-measure.
            host._await(lambda: (len(host.reads) >= 9 and host.status_of("quant", widget="tracing") == "") or None,
                        10, "completed traces")
            host.set_view_toggle("quant", True)
            host.hold_reads()
            dataset.t = 1
            host.await_reads(timeout=10)
            start = time.monotonic()
            done = host.invoke_widget("quant", "regions", row=ids[0], timeout=1.5)
            elapsed = time.monotonic() - start
            assert done.ok and elapsed < 1.0, (done, elapsed)
            request, result = host.view_requests[-1]
            assert request.decline and result.outcome == pb2.VIEW_OUTCOME_DECLINED, (request, result)
            host.release_reads()
            print(f"ok    pick: answered in {elapsed:.3f}s while the view re-measure was parked")
        assert host.shutdown()
    if scenario != "pick":
        print(f"ok    {scenario}: NOT_FOUND removed beta and committed alpha and gamma")


if __name__ == "__main__":
    for scenario in ("regions", "view", "channel", "pick"):
        exercise(scenario)
