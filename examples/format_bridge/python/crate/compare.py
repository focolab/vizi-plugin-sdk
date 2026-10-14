"""The semantic comparator: which of SPEC.md §"The round trip"'s three tiers
two crates match at — never a boolean.

    byte-equal            the files are identical
    semantically equal    voxel-equal, plus form, text, spacing, time increment
                          and channel names (DERIVED and provenance only noted)
    voxel-equal           same dtype and shape, identical decoded voxels
    different             anything less

As a process (``crate_tool.py compare``): exit 0 once both crates opened and a
verdict was printed, whatever the tier; 2 when either could not be opened or
decoded; 1 for a usage error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .format import Calibration, Crate, crc32

BYTE_EQUAL = "byte-equal"
SEMANTIC = "semantically equal"
VOXEL = "voxel-equal"
DIFFERENT = "different"


@dataclass
class Verdict:
    tier: str
    notes: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return self.tier + ("" if not self.notes else ": " + "; ".join(self.notes))


def _decode_all(crate: Crate, calibration: Calibration | None, notes: list[str], label: str) -> list[np.ndarray]:
    h = crate.header
    out = []
    for i, entry in enumerate(crate.index):
        t, c = i // h.c_count, i % h.c_count
        raw = crate._pread(entry.length, entry.offset, f"block {i}")
        if crc32(raw) != entry.crc32:
            notes.append(f"{label}: block (t={t}, c={c}) fails its CRC-32; compared on its bytes anyway")
        out.append(crate.read_block(t, c, verify=False, calibration=calibration))
    return out


def compare(a: Path, b: Path, calibration: Calibration | None = None) -> Verdict:
    """Raises ``CrateError`` when either file cannot be opened or decoded (a
    calibrated crate with no table cannot be)."""
    a, b = Path(a), Path(b)
    notes: list[str] = []
    with Crate.open(a) as ca, Crate.open(b) as cb:
        ha, hb = ca.header, cb.header
        # Both files opened as crates -- only now may bytes settle it.  Two
        # identical unfinished files are not "byte-equal crates": SPEC.md's
        # exit 2 is for either file not opening, whatever the other holds.
        if a.read_bytes() == b.read_bytes():
            _decode_all(ca, calibration, notes, a.name)  # a calibrated crate with no table cannot be decoded
            return Verdict(BYTE_EQUAL, notes)
        if ha.dtype_code != hb.dtype_code:
            return Verdict(DIFFERENT, [f"dtype {ha.dtype_name} vs {hb.dtype_name}"])
        if ha.shape_tczyx != hb.shape_tczyx:
            return Verdict(DIFFERENT, [f"shape {ha.shape_tczyx} vs {hb.shape_tczyx}"])
        va = _decode_all(ca, calibration, notes, a.name)
        vb = _decode_all(cb, calibration, notes, b.name)
        differing = [
            (i // ha.c_count, i % ha.c_count)
            for i, (x, y) in enumerate(zip(va, vb))
            if not np.array_equal(x, y)
        ]
        if differing:
            t, c = differing[0]
            notes.append(f"{len(differing)} of {ha.blocks} volumes differ; first: (t={t}, c={c})")
            return Verdict(DIFFERENT, notes)
        semantic = True
        if ca.form_bytes != cb.form_bytes or ca.text_bytes != cb.text_bytes:
            semantic = False
            notes.append(f"metadata form or text differs byte-for-byte ({len(ca.text_bytes)} vs {len(cb.text_bytes)} bytes)")
        if ha.spacing_zyx != hb.spacing_zyx or ha.time_increment != hb.time_increment:
            semantic = False
            notes.append(f"header spacing or time increment differs ({ha.spacing_zyx}, {ha.time_increment} vs {hb.spacing_zyx}, {hb.time_increment})")
        na, nb = ca.channel_names(), cb.channel_names()
        if na != nb:
            semantic = False
            notes.append(f"channel names differ ({_names(na)} vs {_names(nb)})")
        if ha.calibrated != hb.calibrated:
            notes.append("one crate stores calibrated counts and the other decoded values (equal once decoded)")
        if ha.derived != hb.derived:
            notes.append(f"{(a if ha.derived else b).name} is DERIVED: its document is its source's, and its reader serves none")
        if ca.provenance_bytes != cb.provenance_bytes:
            notes.append(f"provenance records differ ({len(ca.provenance_bytes)} vs {len(cb.provenance_bytes)} bytes; not compared)")
        return Verdict(SEMANTIC if semantic else VOXEL, notes)


def _names(names) -> str:
    return "malformed" if names is None else ("none" if not names else ", ".join(repr(n) for n in names))
