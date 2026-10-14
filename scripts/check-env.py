#!/usr/bin/env python3
"""Verify a plugin environment can actually speak the protocol.

Run with the interpreter you want to check — this repo's baseline
(``uv run scripts/check-env.py``) or a plugin's own venv
(``/path/to/plugin/.venv/bin/python scripts/check-env.py``), which is the
interpreter ``entry.command`` will launch.

Read-only, exits nonzero on the first failure.  This is a transport-level
check, deliberately narrower than the per-plugin ``scripts/doctor.py``
contract in docs/environments.md: that one also covers lockfile digests,
declared assets, and backend/GPU probes, which are plugin-specific.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Bumped in lockstep with the SDK version; the host rejects a mismatch at
# launch, so a baseline that handshakes as anything else is not usable.
#
# It is a LITERAL on purpose -- the point is to catch an environment holding a
# different SDK than the deployment expects, and deriving it from the installed
# SDK would make that check vacuous.  `_stubs` cross-checks it against what the
# SDK actually handshakes with, so the literal cannot go stale unnoticed.
EXPECTED_PROTOCOL = (1, 0)

failures: list[str] = []


def check(label: str, fn) -> None:
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001 - report, don't propagate
        failures.append(label)
        print(f"FAIL  {label}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok    {label}" + (f": {detail}" if detail else ""))


def _interpreter() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro} at {sys.executable}"


def _sdk() -> str:
    import vizi_plugin_sdk as sdk

    missing = [n for n in sdk.__all__ if not hasattr(sdk, n)]
    if missing:
        raise AssertionError(f"exports fail to resolve: {', '.join(missing)}")
    return f"{len(sdk.__all__)} exports resolve"


def _runtime() -> str:
    import grpc
    import numpy as np
    from google.protobuf import __version__ as protobuf_version

    return f"protobuf {protobuf_version}, grpcio {grpc.__version__}, numpy {np.__version__}"


def _stubs() -> str:
    """Both stub modules import, and a Hello round-trips through the wire."""
    import vizi_plugin_sdk as sdk

    from vizi_plugin_sdk import server

    assert sdk.pb2_grpc.PluginStub is not None
    assert sdk.roi_pb2.Roi is not None
    major, minor = EXPECTED_PROTOCOL
    speaks = (server.PROTOCOL_MAJOR, server.PROTOCOL_MINOR)
    if speaks != EXPECTED_PROTOCOL:
        raise AssertionError(
            f"this SDK handshakes as {speaks[0]}.{speaks[1]}, and this baseline "
            f"expects {major}.{minor}"
        )
    hello = sdk.pb2.Hello(
        protocol_major=major, protocol_minor=minor, session_token="check-env"
    )
    if sdk.pb2.Hello.FromString(hello.SerializeToString()) != hello:
        raise AssertionError("Hello did not survive a serialize/parse round-trip")
    return f"protocol {major}.{minor} round-trips"


def _decorators() -> str:
    """A reader registers and returns a well-formed ReaderInfo."""
    import numpy as np
    from vizi_plugin_sdk import ReaderInfo, reader

    @reader("check-env")
    class _Probe:
        def open(self, uri: str) -> ReaderInfo:
            return ReaderInfo(shape_tczyx=(1, 1, 2, 2, 2), dtype="u16")

        def read_volume(self, t: int, c: int) -> np.ndarray:
            return np.zeros((2, 2, 2), dtype=np.uint16)

    info = _Probe().open("check-env://probe")
    assert info.shape_tczyx == (1, 1, 2, 2, 2), info.shape_tczyx
    return "@reader registers"


def _assets() -> str:
    """asset_path() resolves a host-published declared asset."""
    from vizi_plugin_sdk import asset_path

    previous = os.environ.get("VIZI_PLUGIN_ASSETS")
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "model.bin"
        target.write_bytes(b"model")
        os.environ["VIZI_PLUGIN_ASSETS"] = json.dumps({"model": str(target)})
        try:
            if asset_path("model") != target:
                raise AssertionError("asset_path did not resolve the declared asset")
        finally:
            if previous is None:
                del os.environ["VIZI_PLUGIN_ASSETS"]
            else:
                os.environ["VIZI_PLUGIN_ASSETS"] = previous
    return "asset_path resolves"


check("interpreter", _interpreter)
check("vizi_plugin_sdk import", _sdk)
check("runtime deps", _runtime)
check("protocol stubs", _stubs)
check("decorators", _decorators)
check("declared assets", _assets)

if failures:
    print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}", file=sys.stderr)
    sys.exit(1)
print("\nenvironment ready")
