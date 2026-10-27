#!/usr/bin/env python3
"""Write the protocol's FileDescriptorSet from the COMPILED Python modules.

    python compat/make_descriptor_set.py OUT.binpb

The set is built from `vizi_plugin_sdk/_proto/*_pb2.py`, the modules every
Python plugin actually runs, rather than from a fresh protoc run over
`proto/`: the baseline is what shipped, and `regen-stubs.sh --check` (CI) is
what keeps the two in agreement.  Source info (comments, line numbers) is not
part of a compiled module and is not part of the contract.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from google.protobuf import descriptor_pb2  # noqa: E402

from vizi_plugin_sdk._proto import roi_pb2, vizi_plugin_pb2  # noqa: E402


def descriptor_set() -> bytes:
    fds = descriptor_pb2.FileDescriptorSet()
    # Dependency order: roi.proto first, since vizi_plugin.proto imports it.
    for module in (roi_pb2, vizi_plugin_pb2):
        fds.file.add().ParseFromString(module.DESCRIPTOR.serialized_pb)
    return fds.SerializeToString(deterministic=True)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    Path(argv[1]).write_bytes(descriptor_set())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
