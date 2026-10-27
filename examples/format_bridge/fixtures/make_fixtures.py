#!/usr/bin/env python3
"""Regenerate the crate-format interop fixtures.

The pinned recipe behind every byte in this directory.  Pure standard
library on purpose: an implementor in any language must be able to read
this and reproduce the files, and a dependency they cannot install is a
fixture they cannot check.

Run from anywhere:

    python3 examples/format_bridge/fixtures/make_fixtures.py

It rewrites every file in its own directory and prints the size and
SHA-256 of each, which is what ``SPEC.md`` quotes.  If a regenerated
digest differs from the one in ``SPEC.md``, the format changed and the
specification is now wrong -- fix one or the other before committing.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent

MAGIC = b"VIZCRATE"
FOOTER_MAGIC = b"ETARCZIV"
CAL_MAGIC = b"VIZCAL01"

DTYPE_U8, DTYPE_U16, DTYPE_F32 = 1, 2, 3
FLAG_CALIBRATED = 1 << 0

# The sample dataset.  Small enough to hand-check, shaped so no two axis
# lengths coincide -- a transposed reader cannot accidentally produce a
# correctly shaped array.
T, C, Z, Y, X = 3, 2, 4, 6, 5

# Deliberately anisotropic, because real data is: a confocal stack with
# 0.325 um lateral sampling and 2 um between planes is ordinary, and a
# plugin that assumes cubic voxels reports a ~6x wrong Z extent without
# ever looking wrong on screen.  Canonical crate units: um and seconds.
SPACING_ZYX = (2.0, 0.325, 0.325)
TIME_INCREMENT = 1.5

MICRO = "µ"  # U+00B5 MICRO SIGN -- what OME actually writes


def ome(pixels_extra: str = "", name: str = "crate sample") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        f'<Image ID="Image:0" Name="{name}">'
        '<Pixels ID="Pixels:0" DimensionOrder="XYZCT" Type="uint16"'
        ' SizeX="5" SizeY="6" SizeZ="4" SizeC="2" SizeT="3"'
        f"{pixels_extra}/>"
        "</Image></OME>"
    )


# The document agrees with the header, so an implementor's OME scraper can
# be checked against the header field with no host in the loop.
METADATA_FORM = "ome-xml"
METADATA_TEXT = ome(
    f' PhysicalSizeX="0.325" PhysicalSizeXUnit="{MICRO}m"'
    f' PhysicalSizeY="0.325" PhysicalSizeYUnit="{MICRO}m"'
    f' PhysicalSizeZ="2.0" PhysicalSizeZUnit="{MICRO}m"'
    ' TimeIncrement="1.5" TimeIncrementUnit="s"'
)

# No PhysicalSize attributes at all -- the common case for a file nobody
# recorded the scope settings for.
METADATA_TEXT_NOSPACING = ome(name="crate sample, no spacing")

# The document disagrees with the header AND states Z in nanometers, so one
# fixture covers both the precedence rule and the unit conversion.
METADATA_TEXT_CONFLICT = ome(
    ' PhysicalSizeX="1.0" PhysicalSizeXUnit="{m}m"'
    ' PhysicalSizeY="1.0" PhysicalSizeYUnit="{m}m"'
    ' PhysicalSizeZ="1000" PhysicalSizeZUnit="nm"'
    ' TimeIncrement="500" TimeIncrementUnit="ms"'.format(m=MICRO),
    name="crate sample, conflicting spacing",
)

# Per-channel calibration offsets, published as the manifest asset.
CAL_OFFSETS = {0: 7, 1: 13}


def voxel(t: int, c: int, z: int, y: int, x: int) -> int:
    """Every axis independently legible in the decoded value.

    A reader that swaps two axes produces a number whose digits say which
    two.  Max value 21354, comfortably inside u16.
    """
    return 10000 * t + 1000 * c + 100 * z + 10 * y + x


def block_bytes(t: int, c: int, calibrated: bool) -> bytes:
    """One (Z, Y, X) volume, C order, little-endian u16."""
    bias = CAL_OFFSETS.get(c, 0) if calibrated else 0
    vals = [
        voxel(t, c, z, y, x) + bias
        for z in range(Z)
        for y in range(Y)
        for x in range(X)
    ]
    return struct.pack("<%dH" % len(vals), *vals)


def build_crate(
    *,
    calibrated: bool = False,
    finalized: bool = True,
    corrupt_block: int | None = None,
    metadata_text: str = METADATA_TEXT,
    spacing_zyx: tuple[float, float, float] = SPACING_ZYX,
    time_increment: float = TIME_INCREMENT,
) -> bytes:
    form = METADATA_FORM.encode("utf-8")
    text = metadata_text.encode("utf-8")

    header = struct.pack(
        "<8sHHIIIIIIHI6xddddII8x",
        MAGIC,
        1,  # format_version
        DTYPE_U16,
        FLAG_CALIBRATED if calibrated else 0,
        T,
        C,
        Z,
        Y,
        X,
        len(form),
        len(text),
        # The four doubles sit at offsets 48/56/64/72 -- naturally aligned,
        # so a header mapped as a struct needs no unaligned loads.
        spacing_zyx[0],
        spacing_zyx[1],
        spacing_zyx[2],
        time_increment,
        # provenance_len and names_len (offsets 80/84): no fixture
        # carries either section, so no fixture is DERIVED or names a channel.
        0,
        0,
    )
    assert len(header) == 96, len(header)

    out = bytearray(header + form + text)

    # Blocks are written in REVERSE canonical order.  The index is the only
    # thing that says where a volume lives; a reader that assumes sequential
    # layout reads the wrong volume every time and the diagnostic values say
    # so immediately.
    n = T * C
    index: dict[int, tuple[int, int, int]] = {}
    for i in reversed(range(n)):
        t, c = divmod(i, C)
        payload = bytearray(block_bytes(t, c, calibrated))
        offset = len(out)
        crc = zlib.crc32(bytes(payload)) & 0xFFFFFFFF
        if corrupt_block == i:
            # Flip one bit in the payload and LEAVE the CRC describing the
            # original bytes -- exactly what bit rot looks like.
            payload[0] ^= 0x01
        out += payload
        index[i] = (offset, len(payload), crc)

    if not finalized:
        # What an aborted export leaves behind: valid blocks, no index, no
        # footer.  Nothing in the file says how many blocks made it.
        return bytes(out)

    index_bytes = b"".join(struct.pack("<QQI4x", *index[i]) for i in range(n))
    index_offset = len(out)
    out += index_bytes
    out += struct.pack(
        "<QQI4x8s",
        index_offset,
        n,
        zlib.crc32(index_bytes) & 0xFFFFFFFF,
        FOOTER_MAGIC,
    )
    return bytes(out)


def build_calibration() -> bytes:
    entries = sorted(CAL_OFFSETS.items())
    out = struct.pack("<8sI4x", CAL_MAGIC, len(entries))
    for channel, offset in entries:
        out += struct.pack("<Ii", channel, offset)
    return out


def write(name: str, data: bytes) -> None:
    path = HERE / name
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    print(f"{name:26} {len(data):7} bytes  sha256 {digest}")


def main() -> None:
    write("sample.crate", build_crate())
    write("sample-calibrated.crate", build_crate(calibrated=True))
    write("unfinalized.crate", build_crate(finalized=False))
    write("badcrc.crate", build_crate(corrupt_block=2))
    write(
        "nospacing.crate",
        build_crate(
            metadata_text=METADATA_TEXT_NOSPACING,
            spacing_zyx=(0.0, 0.0, 0.0),
            time_increment=0.0,
        ),
    )
    write(
        "spacing-conflict.crate",
        build_crate(metadata_text=METADATA_TEXT_CONFLICT),
    )
    write("vizi-crate-cal.bin", build_calibration())


if __name__ == "__main__":
    main()
