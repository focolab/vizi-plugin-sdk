"""Deferred access to the generated protocol stubs.

The HOST imports this package -- ``doorbell``'s phase and pane codes,
``memory``'s shared constants, ``pb2`` on its control-plane paths -- from an
install that carries neither grpcio nor protobuf, those being plugin-environment
deps, so no module a host might import may pull the stubs in at import time.  Every module
that needs them goes through this shim, which resolves on first *use*.
"""

from __future__ import annotations

MISSING_DEPS = (
    "the vizi plugin SDK needs its plugin-side dependencies; "
    "install grpcio, protobuf and atomicshm in the plugin environment "
    "(pip install grpcio protobuf atomicshm)"
)


class LazyProto:
    """Resolve one generated module on first attribute access."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._mod = None

    def __getattr__(self, attr: str):
        if self._mod is None:
            try:
                from . import _proto
            except ImportError as exc:
                raise ImportError(MISSING_DEPS) from exc
            self._mod = getattr(_proto, self._name)
        return getattr(self._mod, attr)
