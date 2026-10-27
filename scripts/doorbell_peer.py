#!/usr/bin/env python3
"""One end of a live doorbell, for scripts/head_to_head.py.

Adopts the descriptor in ``VIZI_DOORBELL_FD`` and either sends the `D` hints
named on the command line or prints the ones it receives, one canonical line
each.  The other end is another SDK's peer over the same socketpair, so this
exercises this SDK's encoder against that one's decoder — no recording, and no
implementation grading another.

    VIZI_DOORBELL_FD=3 scripts/doorbell_peer.py send 9/1/2/3,64
    VIZI_DOORBELL_FD=3 scripts/doorbell_peer.py receive

A hint is written ``segment/pane/epoch/regions``, with ``-`` for an absent pane.
"""

from __future__ import annotations

import os
import socket
import sys
import time
import traceback

from vizi_plugin_sdk import doorbell as db


# A sender outlives its own records; a receiver outlives the slowest peer's
# startup, which is the JVM's. Both are harness timing, not protocol.
SEND_WINDOW = 1.2
RECEIVE_WINDOW = 2.2


def parse(spec: str) -> tuple[int, list[int], int, int]:
    segment, pane, epoch, regions = spec.split("/")
    return (
        int(segment),
        [int(r) for r in regions.split(",")] if regions else [],
        0 if epoch == "-" else int(epoch),
        0 if pane == "-" else int(pane),
    )


def show(n: db.Notification) -> str:
    """The canonical line every peer prints for one received hint."""
    pane = "-" if n.epoch == 0 else str(n.view_id)
    epoch = "-" if n.epoch == 0 else str(n.epoch)
    return (f"hint segment={n.segment_id} pane={pane} epoch={epoch} "
            f"regions={','.join(str(r) for r in n.regions())}")


def main() -> int:
    mode = sys.argv[1]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET,
                         fileno=int(os.environ["VIZI_DOORBELL_FD"]))

    def on_outcome(outcome) -> None:
        # The transport turns a consumer exception into a quiet teardown, so a
        # peer that raised here would look like one that decoded nothing.
        try:
            if not isinstance(outcome, db.Frame):
                return
            if isinstance(outcome.record, db.Notification):
                print(show(outcome.record), flush=True)
            else:
                print(f"other {outcome.disposition.value}", flush=True)
        except Exception:
            traceback.print_exc()
            raise

    transport = db.DoorbellTransport(sock, "seqpacket", on_outcome)
    transport.start()
    transport.handshake_complete(db.PROTOCOL_MAJOR, db.PROTOCOL_MINOR)
    if mode == "send":
        for spec in sys.argv[2:]:
            result = transport.send_notification(*parse(spec))
            if result is not db.SendResult.SENT:
                print(f"refused {result.value}", flush=True)
        time.sleep(SEND_WINDOW)
    else:
        time.sleep(RECEIVE_WINDOW)
    transport.stop()
    transport.join(2.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
