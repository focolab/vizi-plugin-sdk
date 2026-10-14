"""Process-shared u64 atomics for the Python plugin SDK.

The host reads lane state with Rust atomics.  The standalone, otherwise
pure-Python SDK uses :mod:`atomicshm` to perform the matching operations over
its writable ``mmap`` without depending on Vizi's native extension.

``atomicshm.AtomicView`` pins the mapping's exported buffer.  Call
:meth:`SharedAtomicMapping.close` before closing the underlying ``mmap``;
``server._Segment.close`` owns and enforces that ordering.
"""

from __future__ import annotations


def _import_atomicshm():
    """Resolve the atomics backend at first use, never at module import.

    ``atomicshm`` belongs to the plugin environment, not to Vizi's runtime, so
    a Vizi wheel does not carry it.  Raising at module scope made this module
    unimportable in a clean install, and release CI sweeps every ``vizi.*``
    module with :func:`pkgutil.walk_packages` — so an SDK-only dependency
    announced at import time failed the wheel smoke on every platform.
    """
    try:
        import atomicshm
    except ImportError as exc:
        raise ImportError(
            "the plugin SDK's shared-memory state words -- live overlays, panel "
            "and stage slots -- need atomicshm in the plugin environment "
            "(pip install atomicshm)"
        ) from exc
    return atomicshm


class SharedAtomicMapping:
    """Atomic state-word access tied to one writable mapped segment."""

    def __init__(self, mapping) -> None:
        self._atomicshm = _import_atomicshm()
        self._view = self._atomicshm.AtomicView(mapping)

    @property
    def closed(self) -> bool:
        return bool(self._view.closed)

    def close(self) -> None:
        """Release the exported buffer before the owning mapping is closed."""
        self._view.close()

    def load(self, offset: int) -> int:
        return int(self._view.load_u64(offset, self._atomicshm.ACQUIRE))

    def store(self, offset: int, value: int) -> None:
        self._view.store_u64(offset, value, self._atomicshm.RELEASE)

    def compare_exchange(self, offset: int, expected: int, desired: int) -> tuple[bool, int]:
        observed = int(
            self._view.cas_u64(
                offset,
                expected,
                desired,
                self._atomicshm.ACQ_REL,
                self._atomicshm.ACQUIRE,
            )
        )
        return observed == expected, observed

    def __enter__(self) -> SharedAtomicMapping:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
