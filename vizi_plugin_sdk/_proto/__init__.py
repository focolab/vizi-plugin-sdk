"""Pre-generated Python stubs for ``vizi_plugin.proto`` (the v0 plugin contract).

Committed (not generated at install): plugin authors import these to speak the
protocol without a protoc toolchain. Regenerate after a proto change with the
stubs script, ``scripts/regen-stubs.sh`` (the grpc stub's intra-package import
is rewritten to be relative so this imports cleanly as a package). The
``.proto`` is the single source of truth, shared with the host.
"""

from __future__ import annotations

from . import roi_pb2
from . import vizi_plugin_pb2 as pb2

__all__ = ["pb2", "pb2_grpc", "roi_pb2"]


def __getattr__(name: str):
    # pb2 is eager (protobuf is a HOST runtime dep — the host builds/parses
    # protocol messages as data), but the grpc service stub stays LAZY: it
    # imports grpcio, which only the plugin environment carries.  An eager
    # import here would drag grpc into the host graph the moment anything
    # touched pb2, and a host without grpcio would fail at startup.
    if name in ("pb2_grpc", "vizi_plugin_pb2_grpc"):
        # import_module, not `from . import`: the fromlist form re-enters this
        # __getattr__ while the submodule is mid-import (PEP-562 recursion).
        import importlib

        return importlib.import_module(".vizi_plugin_pb2_grpc", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
