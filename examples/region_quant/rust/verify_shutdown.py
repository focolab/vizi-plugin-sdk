#!/usr/bin/env python3
"""Exercise Rust trace shutdown during parked reads, startup, idle work and host loss.

From the repository root, after a release build:
    uv run python examples/region_quant/rust/verify_shutdown.py
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from vizi_hostsim import Dataset, Host, Segment, segment_id  # noqa: E402
from vizi_plugin_sdk import pb2, roi_pb2  # noqa: E402


class TraceHost(Host):
    park_trace = False

    def _answer_read(self, request_id, request):
        # Initial table reads use t=0. A request for t=1 proves the trace
        # worker is running; park it without blocking the host's message pump.
        if self.park_trace and request.t == 1:
            self.hold_reads()
        super()._answer_read(request_id, request)


def exercise(plugin_dir: Path, scenario: str) -> None:
    dataset = Dataset(
        shape_tczyx=(3, 1, 2, 2, 2),
        np_dtype=np.dtype(np.uint16),
        wire_dtype=pb2.DTYPE_U16,
        open_generation=11,
        volume=lambda t, c: np.full((2, 2, 2), 100 + t, dtype=np.uint16),
        t=0,
        visible_channels=(0,),
    )
    host = TraceHost(plugin_dir, dataset)
    host.park_trace = scenario in ("parked", "host-loss")
    for name in ("alpha", "beta"):
        host.add_region(roi_pb2.Roi(type_name="box", vertices=[
            roi_pb2.Vertex(position=roi_pb2.Vec3(z=0, y=0, x=0)),
            roi_pb2.Vertex(position=roi_pb2.Vec3(z=1, y=1, x=1)),
        ]), name=name)
    panel = segment_id(1)
    host.add(Segment.plugin_arena(panel, "P", 1 << 20, host.directory, subject="quant"))
    popen = subprocess.Popen
    with patch("vizi_hostsim.subprocess.Popen", side_effect=lambda *a, **kw: popen(*a, **kw, stderr=subprocess.PIPE)):
        with host:
            host._await(lambda: host.ready, 10, "Ready")
            host.open_panel("quant", grant=panel)
            if host.park_trace:
                host.await_reads(timeout=10)
            elif scenario == "idle":
                # Two initial measurements + two complete three-frame traces.
                # _await stops at anything other than None, including False.
                host._await(lambda: (len(host.reads) >= 8 and host.status_of("quant", widget="tracing") == "") or None, 10, "idle traces")
            # Startup deliberately sends Shutdown immediately after opening.
            reads = len(host.reads)
            if scenario == "host-loss":
                host._responses.cancel()
            else:
                assert host.shutdown(), "shutdown overran its window"
                assert host.shutdown_report is not None
                assert host.shutdown_report.outcome == pb2.ShutdownReport.COMPLETED, host.shutdown_report
            # The plugin must exit itself, before Host.__exit__ reaps it. The
            # parked read is deliberately NEVER answered by this test.
            assert host._plugin.wait(timeout=5) == 0
            stderr = host._plugin.stderr.read().decode(errors="replace")
            assert len(host.reads) == reads, "another read started during shutdown"
            assert "trace " not in stderr, stderr
            assert "trace progress:" not in stderr, stderr
            assert "new requests are refused during shutdown" not in stderr, stderr
            assert "session is shutting down" not in stderr, stderr
    print(f"ok    {scenario}: worker stopped, process exited, no extra reads or trace cancellation diagnostics")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", type=Path, default=HERE)
    args = parser.parse_args()
    for scenario in ("parked", "startup", "idle", "host-loss"):
        exercise(args.plugin_dir.resolve(), scenario)


if __name__ == "__main__":
    main()
