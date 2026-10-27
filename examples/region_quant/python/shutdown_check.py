#!/usr/bin/env python3
"""region_quant at shutdown, for any implementation (SPEC §2's trace worker meets §3.3's
shutdown window).  Four scenarios, each deliberate:

- ``parked``: a trace read is held unanswered mid-trace, then `Shutdown`.
- ``startup``: `Shutdown` as soon as the panel opens, while the first
  `ViewChanged` / `RegionsChanged` handlers are still making requests -- a
  person closing vizi at once.  A request that crosses `Shutdown` is dropped
  unanswered, and must not stall the plugin.
- ``idle``: `Shutdown` only after both traces are complete and nothing runs.
- ``host-loss``: a trace read held, then the host's control stream vanishes.

Each time the plugin must exit on its own within EXIT_S, start no new volume
read once shutdown has begun, and put no traceback, exception or thrown-handler
line on stderr.  An orderly shutdown must report COMPLETED (a hook) or NO_HOOK
and exit 0.

    uv run python examples/region_quant/python/shutdown_check.py --plugin-dir examples/region_quant/<lang>
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # examples/, for vizi_hostsim

from vizi_hostsim import Dataset, Host, Segment, segment_id  # noqa: E402

EXIT_S = 5.0
NOISE = re.compile(r"Traceback|[Ee]xception|\bthrew\b|panicked|abandon", re.I)

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
            # vizi's order: P and PanelOpened in one step,
            # with no wait for the ack -- held images flush at the install.
            host.open_panel("quant", grant=panel)
            if host.park_trace:
                host.await_reads(timeout=10)
            elif scenario == "idle":
                # Two initial measurements + two complete three-frame traces.
                # `or None`: `_await` stops at anything that is not None.
                host._await(lambda: (len(host.reads) >= 8 and host.status_of("quant", widget="tracing") == "") or None, 10, "idle traces")
            # "startup": no wait at all.
            reads = len(host.reads)
            if scenario == "host-loss":
                host._responses.cancel()
            else:
                assert host.shutdown(), "shutdown overran its window"
                assert host.shutdown_report is not None
                assert host.shutdown_report.outcome in (pb2.ShutdownReport.COMPLETED, pb2.ShutdownReport.NO_HOOK), host.shutdown_report
            # The plugin must exit itself, before Host.__exit__ reaps it. The
            # parked read is deliberately NEVER answered by this test.
            try:
                code = host._plugin.wait(timeout=EXIT_S)
            except subprocess.TimeoutExpired:
                raise AssertionError(f"the plugin did not exit within {EXIT_S:g} s") from None
            if scenario != "host-loss":
                assert code == 0, f"exit status {code} after an orderly shutdown"
            stderr = host._plugin.stderr.read().decode(errors="replace")
            assert len(host.reads) == reads, "a volume read started after shutdown began"
            # A read sent after `Shutdown` never reaches `host.reads`: the
            # emulator drops it unanswered into `dropped_in_shutdown`, as vizi
            # does.  Only "startup" may have one, a request
            # that crossed `Shutdown` on the wire; parked and idle have nothing
            # in flight to cross it.
            if scenario in ("parked", "idle"):
                late = [kind for kind, _ in host.dropped_in_shutdown if kind == "read_data_volume"]
                assert not late, f"{len(late)} volume read(s) sent after shutdown began"
            noise = [line for line in stderr.splitlines() if NOISE.search(line)]
            assert not noise, "stderr at shutdown: " + " | ".join(noise[:4])
    print(f"ok    {scenario}: the trace worker stopped, the process exited, no new reads, no errors on stderr")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", type=Path, default=HERE)
    args = parser.parse_args()
    for scenario in ("parked", "startup", "idle", "host-loss"):
        exercise(args.plugin_dir.resolve(), scenario)


if __name__ == "__main__":
    main()
