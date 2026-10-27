"""Voxel size, as the exporter is told it.

``resolve_calibration`` is the exporter's rule (SPEC.md §7): ``OpenExport``'s
fields, per value, converted to the crate's canonical micrometers and seconds
by the table in SPEC.md §"Units"; else unknown.  The ``source_metadata``
document is the SOURCE's, and an exporter writes no measurement it was not
given, so nothing here reads one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MICRO_SIGN = "µ"  # what OME writes
GREEK_MU = "μ"  # what real files in the wild contain; visually identical

#: Length units to micrometers.  Both spellings of micro, deliberately.
LENGTH_TO_UM: dict[str, float] = {
    "m": 1e6,
    "cm": 1e4,
    "mm": 1e3,
    f"{MICRO_SIGN}m": 1.0,
    f"{GREEK_MU}m": 1.0,
    "um": 1.0,
    "micron": 1.0,
    "nm": 1e-3,
    "Å": 1e-4,  # Å
    "angstrom": 1e-4,
}

#: Time units to seconds.
TIME_TO_S: dict[str, float] = {
    "s": 1.0,
    "sec": 1.0,
    "ms": 1e-3,
    "min": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
}

#: OME's own defaults, which is why the crate's canonical units are µm and s.
DEFAULT_LENGTH_UNIT = f"{MICRO_SIGN}m"
DEFAULT_TIME_UNIT = "s"

def to_um(value: str | float | None, unit: str | None) -> float:
    """``value`` in ``unit`` as micrometers, or ``0.0`` (unknown) for an
    absent, unparseable or non-positive value or a unit not in the table.
    An absent unit is OME's default, not unknown."""
    return _convert(value, unit, LENGTH_TO_UM, DEFAULT_LENGTH_UNIT)


def to_s(value: str | float | None, unit: str | None) -> float:
    return _convert(value, unit, TIME_TO_S, DEFAULT_TIME_UNIT)


def _convert(value, unit, table: dict[str, float], default_unit: str) -> float:
    if value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number <= 0.0:
        return 0.0
    factor = table.get(unit.strip() if isinstance(unit, str) and unit.strip() else default_unit)
    if factor is None:
        return 0.0
    return number * factor


@dataclass(frozen=True)
class Resolved:
    spacing_zyx: tuple[float, float, float]
    time_increment: float
    #: Per value — ``"fields"`` or ``"unknown"`` — in the order z, y, x, t;
    #: the exporter reports this.
    sources: tuple[str, str, str, str]

    @property
    def any_known(self) -> bool:
        return any(self.spacing_zyx) or bool(self.time_increment)

    @property
    def all_from_fields(self) -> bool:
        """Every value came from `OpenExport`'s fields: nothing to report
        (SPEC.md §7 logs only when some value is unknown)."""
        return all(source == "fields" for source in self.sources)


def resolve_calibration(spacing_zyx, spacing_unit: str, time_increment, time_unit: str) -> Resolved:
    """SPEC.md §7, per value: a known ``OpenExport`` field, its unit
    converted (a unit not in the table is unknown), else unknown.  Never a
    measurement nobody gave."""
    fields = list(spacing_zyx) if spacing_zyx is not None else [None, None, None]
    out = [to_um(v, spacing_unit) if v is not None else 0.0 for v in fields]
    increment = to_s(time_increment, time_unit) if time_increment is not None else 0.0
    sources = tuple("fields" if v else "unknown" for v in (*out, increment))
    return Resolved((out[0], out[1], out[2]), increment, sources)  # type: ignore[arg-type]
