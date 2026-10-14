#!/usr/bin/env python3
"""Real-atomic Python peer for Go's memory conformance tests; fd 3 is inherited.

Run through VIZI_MEMORY_PYTHON=/path/to/python go test ./memory -run Python.
No stand-in atomics or test_memory imports are permitted here.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import struct
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import atomicshm
from vizi_plugin_sdk import memory


def payload(sequence: int) -> bytes:
    size = 16 + sequence % 241
    return struct.pack("<Q", sequence) + bytes((sequence + i * 17) % 251 for i in range(8, size))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("publish", "read"))
    parser.add_argument("segment", type=int)
    args = parser.parse_args()
    store = memory.SegmentStore(frozenset({"test"}))
    store.capability(args.segment, 4096, memory.PERM_PLUGIN_READ | memory.PERM_PLUGIN_WRITE,
                     fd=os.dup(3), record=b"M" + bytes(111))
    producer = memory.Producer.PLUGIN if args.role == "publish" else memory.Producer.HOST
    answer = store.install(args.segment, "test", [memory.Region(
        0, 32, 3 * (16 + 256), memory.Discipline.PULSED, producer, 3, 256,
    )])
    if not isinstance(answer, memory.Installed):
        raise RuntimeError(f"install failed: {answer}")
    segment = store.segment(args.segment)
    # Initialize the actual native view now, so missing/broken atomics fail the
    # handshake instead of silently skipping or using a single-process shim.
    atomic = segment._atomics()
    if not isinstance(atomic._view, atomicshm.AtomicView):
        raise RuntimeError("expected native atomicshm.AtomicView")
    lane = segment.pulsed_writer(0) if args.role == "publish" else segment.pulsed_reader(0)

    def reply(value):
        print(json.dumps(value), flush=True)

    reply({"backend": atomicshm.BACKEND, "version": atomicshm.__version__, "proof": answer.identity_proof.hex()})
    try:
        for line in sys.stdin:
            command = json.loads(line)
            op = command["op"]
            if op == "quit":
                break
            try:
                if op == "publish":
                    reply({"published": lane.publish(bytes.fromhex(command["data"]))})
                elif op == "read":
                    data = lane.read()
                    reply({"data": None if data is None else data.hex(), "revision": lane.last_seen})
                elif op == "publish_many":
                    count = command["count"]
                    for sequence in range(1, count + 1):
                        ok = lane.publish(payload(sequence))
                        if sequence == count and not ok:
                            raise RuntimeError("final publication coalesced")
                        if sequence % 64 == 0:
                            time.sleep(0)
                    reply({"published": True, "dropped": lane.dropped})
                elif op == "read_until":
                    last, samples = 0, 0
                    deadline = time.monotonic() + 15
                    while last < command["count"]:
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"last value {last}")
                        data = lane.read()
                        if data is None:
                            time.sleep(0)
                            continue
                        sequence = struct.unpack_from("<Q", data)[0]
                        if sequence <= last or data != payload(sequence):
                            raise RuntimeError(f"torn/regressed payload {sequence} after {last}")
                        last = sequence
                        samples += 1
                    reply({"samples": samples, "last": last})
                else:
                    raise ValueError(op)
            except memory.Quarantined as exc:
                reply({"where": exc.where, "isolated": exc.isolated})
    finally:
        store.close()


if __name__ == "__main__":
    main()
