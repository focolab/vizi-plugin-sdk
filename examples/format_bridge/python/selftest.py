#!/usr/bin/env python3
"""Offline checks for the crate format bridge — everything that needs no host.

    .venv/bin/python selftest.py

The format against the committed fixtures, voxel size through both channels,
the writer against itself and against the interop crate, the comparator's
tiers and exit codes, the exporter's big-endian and rank paths, and the
manifest.  ``hostsim.py`` is the other half: the protocol, over a real
process.  Expectations here are written independently of the implementation
wherever the point is to catch the implementation being wrong about itself.
"""

from __future__ import annotations

import io
import json
import math
import struct
import subprocess
import sys
import tempfile
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"
INTEROP = HERE.parent / "interop"
sys.path.insert(0, str(HERE))

import crate_tool  # noqa: E402
import format_bridge  # noqa: E402


class _Panel:
    """A stand-in for the SDK's `PanelHandle`: records what the exporter
    patches, so an offline check sees the panel's side too."""

    def __init__(self) -> None:
        self.values: list[tuple[str, object]] = []

    def set_value(self, widget: str, value) -> None:
        self.values.append((widget, value))


class _Context:
    """The one-parameter constructor's `PluginContext`, offline:
    a panel that records, and nothing else the exporter touches."""

    def __init__(self) -> None:
        self.panels: dict[str, _Panel] = {}

    def panel(self, panel_id: str) -> _Panel:
        return self.panels.setdefault(panel_id, _Panel())


def exporter() -> "format_bridge.CrateExporter":
    return format_bridge.CrateExporter(_Context())
from crate.compare import BYTE_EQUAL, DIFFERENT, SEMANTIC, VOXEL, compare  # noqa: E402
from crate.format import (  # noqa: E402
    CRC_CHECK_VALUE,
    FLAG_DERIVED,
    Calibration,
    Crate,
    CrateError,
    CrateWriter,
    check_file,
    crc32,
    spacing_label,
)
from crate.ome import GREEK_MU, MICRO_SIGN, resolve_calibration, to_s, to_um  # noqa: E402
from vizi_plugin_sdk import ChannelName, ExportDescriptor, Refusal, pb2  # noqa: E402
from vizi_plugin_sdk.server import _COMMANDS, _EXPORTERS, _PANEL_OPENED, _REGISTERED  # noqa: E402

_failures: list[str] = []


def check(label: str, fn) -> None:
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001 — report every failure, don't stop
        _failures.append(label)
        print(f"FAIL  {label}: {type(exc).__name__}: {exc}")
    else:
        print(f"ok    {label}" + (f": {detail}" if detail else ""))


def expect_error(kind: str, fn, needle: str = "") -> CrateError:
    """A refusal by kind name: the container's `CrateError`, or -- from a
    reader/exporter METHOD, which translates at the SDK boundary
    (`_refusing`) -- the SDK's `Refusal`, whose kind is the enum value.  Either
    is returned as a `CrateError` so the checks read the same."""
    try:
        fn()
    except CrateError as exc:
        assert exc.kind == kind, f"expected {kind}, got {exc.kind}: {exc.message}"
        assert needle in exc.message, f"{needle!r} not in {exc.message!r}"
        return exc
    except Refusal as exc:
        name = pb2.Error.Kind.Name(exc.kind)
        assert name == kind, f"expected {kind}, got {name}: {exc}"
        assert needle in str(exc), f"{needle!r} not in {exc!s}"
        return CrateError(name, str(exc))
    raise AssertionError(f"no CrateError({kind}) was raised")


def voxel(t, c, z, y, x) -> int:
    """SPEC.md's formula, written here rather than imported from anywhere."""
    return 10000 * t + 1000 * c + 100 * z + 10 * y + x


def expected_volume(t, c) -> np.ndarray:
    z, y, x = np.meshgrid(np.arange(4), np.arange(6), np.arange(5), indexing="ij")
    return (10000 * t + 1000 * c + 100 * z + 10 * y + x).astype(np.uint16)


CAL = Calibration.load(FIXTURES / "vizi-crate-cal.bin")


# --- the format, against the fixtures -------------------------------------------


def test_crc_check_value():
    assert crc32(b"123456789") == CRC_CHECK_VALUE == 0xCBF43926
    return "0xCBF43926"


def test_sample_index_table():
    """SPEC.md's worked example, verbatim."""
    expected = [
        (1746, 240, 0x8D53B95B), (1506, 240, 0x1C3015AB), (1266, 240, 0xD56706B3),
        (1026, 240, 0x03EAAA2E), (786, 240, 0x5889BB44), (546, 240, 0x5AA02FDD),
    ]
    with Crate.open(FIXTURES / "sample.crate") as crate:
        assert crate.size == 2162 and crate.header.shape_tczyx == (3, 2, 4, 6, 5)
        assert crate.header.dtype_name == "u16" and crate.header.flags == 0
        assert crate.form == "ome-xml" and len(crate.text_bytes) == 443
        assert crate.header.data_start == 546
        got = [(e.offset, e.length, e.crc32) for e in crate.index]
        assert got == expected, got
    return "six entries, descending offsets, the six CRCs"


def test_sample_voxels():
    with Crate.open(FIXTURES / "sample.crate") as crate:
        for t in range(3):
            for c in range(2):
                assert np.array_equal(crate.read_block(t, c), expected_volume(t, c)), (t, c)
        first = crate.read_block(0, 0).ravel()[:10].tolist()
        assert first == [0, 1, 2, 3, 4, 10, 11, 12, 13, 14], first
        assert int(crate.read_block(2, 1)[0, 0, 0]) == 21000
        plane = crate.read_plane(1, 2, 1)
        assert plane.shape == (6, 5) and np.array_equal(plane, expected_volume(1, 1)[2])
    return "every voxel matches 10000t + 1000c + 100z + 10y + x; a plane read agrees"


def test_calibrated():
    with Crate.open(FIXTURES / "sample-calibrated.crate") as crate:
        assert crate.header.calibrated
        for t in range(3):
            for c in range(2):
                assert np.array_equal(crate.read_block(t, c, calibration=CAL), expected_volume(t, c))
        exc = expect_error("IO_ERROR", lambda: crate.read_block(0, 0, calibration=None), "calibration")
        assert "Plugin Manager" in exc.message
        raw = crate.read_block(0, 1, calibration=Calibration({}))
        assert int(raw[0, 0, 0]) == 1000 + 13, "channel 1 is biased by 13 in the fixture"
    # Saturation: a raw count below the offset decodes to 0, never wraps.
    biased = np.array([[[5, 13, 14]]], dtype=np.uint16)
    assert CAL.apply(biased, 1).tolist() == [[[0, 0, 1]]]
    assert CAL.apply(np.array([[[1.0]]], dtype=np.float32), 0).tolist() == [[[-6.0]]], "f32 is unclamped"
    return "decodes to sample.crate with the table; refuses naming Plugin Manager without; u16 saturates at 0"


def test_badcrc():
    with Crate.open(FIXTURES / "badcrc.crate") as crate:
        exc = expect_error("IO_ERROR", lambda: crate.read_block(1, 0), "(t=1, c=0)")
        assert "CRC" in exc.message
        clean = [(t, c) for t in range(3) for c in range(2) if (t, c) != (1, 0)]
        for t, c in clean:
            assert np.array_equal(crate.read_block(t, c), expected_volume(t, c)), (t, c)
        wrong = crate.read_block(1, 0, verify=False)
        assert wrong.shape == (4, 6, 5) and not np.array_equal(wrong, expected_volume(1, 0))
        plane = crate.read_plane(1, 0, 0)  # never verified, by design
        assert plane.shape == (6, 5)
    result = check_file(FIXTURES / "badcrc.crate")
    assert result.blocks == 6 and result.failed == ((1, 0),)
    assert result.summary("badcrc.crate") == "1 of 6 blocks failed CRC in badcrc.crate; first: (t=1, c=0)"
    return "(t=1, c=0) refused with CRC on, served wrong with it off; the other five read cleanly"


def test_unfinalized():
    exc = expect_error("IO_ERROR", lambda: Crate.open(FIXTURES / "unfinalized.crate"), "did not complete")
    assert "unfinished" in exc.message
    expect_error("IO_ERROR", lambda: check_file(FIXTURES / "unfinalized.crate"), "unfinished")
    return "rejected as unfinished, not as corrupt"


def test_check_file_fixtures():
    for name in ("sample", "sample-calibrated", "nospacing", "spacing-conflict"):
        result = check_file(FIXTURES / f"{name}.crate")
        assert result.ok and result.blocks == 6, (name, result)
    return "sample, sample-calibrated, nospacing, spacing-conflict: 6 blocks each, all CRCs good"


def test_not_a_crate():
    with tempfile.TemporaryDirectory() as tmp:
        junk = Path(tmp) / "junk.crate"
        junk.write_bytes(b"not a crate at all" * 20)
        expect_error("INVALID_ARGUMENT", lambda: Crate.open(junk), "magic")
        junk.write_bytes(b"VIZ")
        expect_error("INVALID_ARGUMENT", lambda: Crate.open(junk), "shorter")
        missing = Path(tmp) / "absent.crate"
        expect_error("NOT_FOUND", lambda: Crate.open(missing))
    return "bad magic and a truncated header are INVALID_ARGUMENT; a missing file NOT_FOUND"


# --- voxel size ------------------------------------------------------------------


def test_spacing_conflict_header_wins():
    with Crate.open(FIXTURES / "spacing-conflict.crate") as crate:
        assert crate.header.spacing_zyx == (2.0, 0.325, 0.325) and crate.header.time_increment == 1.5
        assert crate.spacing_label() == "2.000 × 0.325 × 0.325 µm (z × y × x), Δt 1.5 s"
        assert 'PhysicalSizeZ="1000"' in crate.metadata()[1], "the fixture's document disagrees, and is never read"
    return "header 2.000 × 0.325 × 0.325 µm / 1.5 s, whatever the document says"


def test_sections_and_derived():
    """The provenance and names sections (SPEC.md): the record carried, the
    names by index ("" unnamed, stopping short), malformed names read as
    none, and a DERIVED crate serving no document."""
    record = b'{"manifest_version": 1}'
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sections.crate"
        _write_sample_like(path, flags=FLAG_DERIVED, provenance=record, names=b"GFP")
        with Crate.open(path) as crate:
            h = crate.header
            assert h.derived and h.provenance_len == len(record) and h.names_len == 3
            assert h.data_start == 96 + len("ome-xml") + len("<OME/>") + len(record) + 3
            assert crate.provenance == record.decode() and crate.channel_names() == ("GFP",)
            assert crate.metadata() == ("ome-xml", "<OME/>") and crate.served_metadata() == ("", ""), "DERIVED: the source's document is not served"
            assert np.array_equal(crate.read_block(2, 1), expected_volume(2, 1)), "the blocks start after both sections"
        _write_sample_like(path, names=b"\0RFP")
        with Crate.open(path) as crate:
            assert crate.channel_names() == ("", "RFP") and crate.served_metadata() == ("ome-xml", "<OME/>")
        for bad in (b"a\0b\0c", b"\xff"):  # more names than C = 2; not UTF-8
            _write_sample_like(path, names=bad)
            with Crate.open(path) as crate:
                assert crate.channel_names() is None, bad
                assert np.array_equal(crate.read_block(0, 0), expected_volume(0, 0)), "malformed names are malformed metadata, not a malformed file"
        writer = CrateWriter(path, (1, 3, 4, 6, 5), np.uint16, "", b"", names=["", "a\0b", "DAPI"])
        assert writer.header.names_len == len(b"\0\0DAPI"), "a name holding 0x00 is written as ''"
        writer = CrateWriter(path, (1, 3, 4, 6, 5), np.uint16, "", b"", names=["GFP", "", ""])
        assert writer.header.names_len == 3, "trailing '' are dropped"
        expect_error("INVALID_ARGUMENT", lambda: CrateWriter(path, (1, 1, 4, 6, 5), np.uint16, "", b"", names=["a", "b"]))
    return "DERIVED serves no document; provenance and names carried; '' unnamed and trailing ones dropped; too many or non-UTF-8 names read as none"


def test_nospacing():
    with Crate.open(FIXTURES / "nospacing.crate") as crate:
        assert crate.header.spacing_zyx == (0.0, 0.0, 0.0) and crate.header.time_increment == 0.0
        assert crate.spacing_label() == "(unknown)"
        for t in range(3):
            for c in range(2):
                assert np.array_equal(crate.read_block(t, c), expected_volume(t, c))
    return "opens, reads, reports (unknown); nothing raises"


def test_micro_spellings_and_units():
    assert to_um("0.325", f"{MICRO_SIGN}m") == to_um("0.325", f"{GREEK_MU}m") == 0.325
    assert MICRO_SIGN != GREEK_MU and MICRO_SIGN == "µ" and GREEK_MU == "μ"
    assert to_um("1000", "nm") == 1.0 and to_um("2", "mm") == 2000.0 and to_um("1", "Å") == 1e-4
    assert to_um("3", None) == 3.0, "a missing unit is OME's default µm, not unknown"
    assert to_um("3", "") == 3.0
    assert to_um("3", "furlong") == 0.0, "a unit not in the table is unknown"
    assert to_um("three", "um") == 0.0 and to_um(None, "um") == 0.0 and to_um("-1", "um") == 0.0
    assert to_s("500", "ms") == 0.5 and to_s("2", "min") == 120.0 and to_s("1", "hr") == 3600.0
    assert to_s("1.5", None) == 1.5
    return "µ and μ agree; nm/mm/Å, ms/min/hr convert; missing unit = default; unknown unit / unparseable = unknown"


def test_exporter_calibration_rule():
    r = resolve_calibration((2.0, 0.325, 0.325), "um", 1.5, "s")
    assert r.spacing_zyx == (2.0, 0.325, 0.325) and r.time_increment == 1.5
    assert r.sources == ("fields", "fields", "fields", "fields") and r.all_from_fields
    r = resolve_calibration((2.0, None, None), "um", None, "")
    assert r.spacing_zyx == (2.0, 0.0, 0.0) and r.time_increment == 0.0
    assert r.sources == ("fields", "unknown", "unknown", "unknown") and r.any_known
    r = resolve_calibration(None, "", None, "")
    assert r.spacing_zyx == (0.0, 0.0, 0.0) and r.time_increment == 0.0 and not r.any_known
    assert r.sources == ("unknown",) * 4 and not r.all_from_fields
    r = resolve_calibration((2.0, 2.0, 2.0), "furlong", 1.0, "fortnight")
    assert r.spacing_zyx == (0.0, 0.0, 0.0) and r.time_increment == 0.0, "a unit not in the table is unknown, never a guess"
    r = resolve_calibration((2000.0, 325.0, 325.0), "nm", 1500.0, "ms")
    assert r.spacing_zyx == (2.0, 0.325, 0.325) and r.time_increment == 1.5, "field units convert"
    return "the fields per value, units converted, else unknown; an unknown unit is unknown"


def test_malformed_header_spacing():
    with tempfile.TemporaryDirectory() as tmp:
        for name, spacing, dt in (("neg", (-1.0, 0.325, 0.325), 1.5), ("inf", (math.inf, 0.325, 0.325), 1.5),
                                  ("nan", (2.0, math.nan, 0.325), math.nan)):
            path = Path(tmp) / f"{name}.crate"
            _write_sample_like(path, spacing_zyx=spacing, time_increment=dt)
            with Crate.open(path) as crate:
                assert all(math.isfinite(v) and v >= 0 for v in crate.header.spacing_zyx), crate.header
                assert crate.header.malformed, name
                assert np.array_equal(crate.read_block(2, 1), expected_volume(2, 1))
    return "negative, infinite and NaN spacing are unknown; the voxels still read"


# --- hand-built bytes ---------------------------------------------------------------


def _write_sample_like(path: Path, *, spacing_zyx=(2.0, 0.325, 0.325), time_increment=1.5,
                       form="ome-xml", text=b"<OME/>", order="ascending", reserved_junk=False,
                       shape=(3, 2, 4, 6, 5), flags=0, provenance=b"", names=b"") -> None:
    """A crate with sample.crate's voxels, built by hand here (not by
    CrateWriter) so the writer is not the only thing that knows the layout."""
    T, C, Z, Y, X = shape
    header = struct.pack("<8sHHIIIIIIHI", b"VIZCRATE", 1, 2, flags, T, C, Z, Y, X, len(form.encode()), len(text))
    header += (b"\xa5" * 6 if reserved_junk else b"\0" * 6)
    header += struct.pack("<ddddII", *spacing_zyx, time_increment, len(provenance), len(names))
    header += (b"\x5a" * 8 if reserved_junk else b"\0" * 8)
    assert len(header) == 96
    body = header + form.encode() + text + provenance + names
    slots = [(t, c) for t in range(T) for c in range(C)]
    if order == "reverse":
        slots.reverse()
    entries = {}
    for t, c in slots:
        vals = [voxel(t, c, z, y, x) for z in range(Z) for y in range(Y) for x in range(X)]
        block = struct.pack("<%dH" % len(vals), *vals)
        entries[(t, c)] = (len(body), len(block), zlib.crc32(block))
        body += block
    index = b"".join(struct.pack("<QQI", *entries[(t, c)]) + (b"\xff" * 4 if reserved_junk else b"\0" * 4)
                     for t in range(T) for c in range(C))
    footer = struct.pack("<QQI", len(body), T * C, zlib.crc32(index)) + (b"\xee" * 4 if reserved_junk else b"\0" * 4) + b"ETARCZIV"
    path.write_bytes(body + index + footer)


def test_reserved_bytes_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "junk.crate"
        _write_sample_like(path, reserved_junk=True)
        with Crate.open(path) as crate:
            assert np.array_equal(crate.read_block(1, 1), expected_volume(1, 1))
            assert check_file(path).ok
    return "non-zero reserved bytes in header, index and footer are ignored on read"


def test_empty_and_unknown_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "meta.crate"
        _write_sample_like(path, form="", text=b"")
        with Crate.open(path) as crate:
            assert crate.metadata() == ("", "") and crate.header.data_start == 96
        _write_sample_like(path, form="my-own-form", text=b"anything at all")
        with Crate.open(path) as crate:
            assert crate.metadata() == ("my-own-form", "anything at all")
        _write_sample_like(path, form="ome-xml", text=b"<OME>\xff\xfe</OME>")
        with Crate.open(path) as crate:
            assert not crate.text_utf8 and crate.metadata() == ("", ""), "not UTF-8: carried as no document"
            assert np.array_equal(crate.read_block(0, 0), expected_volume(0, 0)), "and the voxels still read"
    return "zero-length metadata; an unknown form carried verbatim; a non-UTF-8 document reported as none, voxels intact"


def test_zero_axis_refused():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "zero.crate"
        _write_sample_like(path, shape=(1, 1, 0, 6, 5))
        exc = expect_error("INVALID_ARGUMENT", lambda: Crate.open(path), "z is 0")
    expect_error("INVALID_ARGUMENT", lambda: CrateWriter(Path("/nonexistent"), (1, 0, 4, 6, 5), np.uint16, "", b""), "C = 0")
    return f"refused at the header: {exc.message!r}"


def test_index_inconsistent_with_header():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad.crate"
        _write_sample_like(path)
        data = bytearray(path.read_bytes())
        # Corrupt the index CRC in the footer.
        struct.pack_into("<I", data, len(data) - 16, 0xDEADBEEF)
        path.write_bytes(bytes(data))
        expect_error("IO_ERROR", lambda: Crate.open(path), "index CRC")
        # Now a footer whose entry count disagrees with the header.
        _write_sample_like(path)
        data = bytearray(path.read_bytes())
        struct.pack_into("<Q", data, len(data) - 24, 5)
        path.write_bytes(bytes(data))
        expect_error("IO_ERROR", lambda: Crate.open(path), "index entries")
    return "a corrupt index CRC and a footer/header disagreement are both IO_ERROR"


# --- the writer ----------------------------------------------------------------------


def test_writer_round_trip_and_stability():
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i in range(2):
            path = Path(tmp) / f"out{i}.crate"
            with Crate.open(FIXTURES / "sample.crate") as src:
                w = CrateWriter(path, src.header.shape_tczyx, np.uint16, src.form, src.text_bytes,
                                src.header.spacing_zyx, src.header.time_increment)
                w.begin()
                for t in range(3):
                    for c in range(2):
                        w.write_block(t, c, src.read_block(t, c))
                w.finish()
            paths.append(path)
        assert paths[0].read_bytes() == paths[1].read_bytes(), "the writer is not byte-stable"
        assert compare(paths[0], FIXTURES / "sample.crate").tier == SEMANTIC
        assert paths[0].read_bytes() == (INTEROP / "python.crate").read_bytes(), "interop/python.crate is not this writer's output on sample.crate"
        assert compare(paths[0], FIXTURES / "sample.crate").tier != BYTE_EQUAL, "ascending vs the fixture's reverse order"
    return "same input twice -> identical bytes; semantically equal to sample.crate; equals interop/python.crate"


def test_scrambled_order_reads():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "scrambled.crate"
        crate_tool.recreate(FIXTURES / "sample.crate", path, "scrambled", None)
        with Crate.open(path) as crate:
            offsets = [e.offset for e in crate.index]
            assert offsets != sorted(offsets) and offsets != sorted(offsets, reverse=True), offsets
            for t in range(3):
                for c in range(2):
                    assert np.array_equal(crate.read_block(t, c), expected_volume(t, c))
        assert compare(path, FIXTURES / "sample.crate").tier == SEMANTIC
        hand = Path(tmp) / "reverse.crate"
        _write_sample_like(hand, order="reverse", text=(FIXTURES / "sample.crate").read_bytes()[96 + 7:96 + 7 + 443])
        assert compare(hand, FIXTURES / "sample.crate").tier == BYTE_EQUAL, "the hand-built reverse crate should reproduce the fixture"
    return "blocks in a scrambled order read correctly through the index; the hand-built reverse crate is byte-equal to the fixture"


def test_finish_incomplete_and_abort_leave_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "partial.crate"
        w = CrateWriter(path, (2, 1, 4, 6, 5), np.uint16, "", b"")
        w.begin()
        w.write_block(0, 0, expected_volume(0, 0))
        exc = expect_error("IO_ERROR", w.finish, "1 of 2 blocks never arrived")
        assert "t=1, c=0" in exc.message and not path.exists()
        w = CrateWriter(path, (1, 1, 4, 6, 5), np.uint16, "", b"")
        w.begin()
        w.write_block(0, 0, expected_volume(0, 0))
        w.abort()
        assert not path.exists()
        w = CrateWriter(path, (1, 1, 4, 6, 5), np.uint16, "", b"")
        w.begin()
        expect_error("INVALID_ARGUMENT", lambda: w.write_block(0, 0, expected_volume(0, 0)[:2]), "shape")
        expect_error("INVALID_ARGUMENT", lambda: w.write_block(0, 0, expected_volume(0, 0).astype(np.uint8)), "dtype")
        expect_error("OUT_OF_BOUNDS", lambda: w.write_block(1, 0, expected_volume(0, 0)), "timepoint 1")
        w.abort()
        expect_error("UNSUPPORTED", lambda: CrateWriter(path, (1, 1, 4, 6, 5), np.float64, "", b""), "float64")
    return "an incomplete finish and an abort both delete the file; shape, dtype, bounds and f64 refused"


def test_repeat_block_last_wins():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "twice.crate"
        w = CrateWriter(path, (1, 1, 4, 6, 5), np.uint16, "", b"")
        w.begin()
        w.write_block(0, 0, expected_volume(0, 0))
        w.write_block(0, 0, expected_volume(2, 1))
        w.finish()
        with Crate.open(path) as crate:
            assert np.array_equal(crate.read_block(0, 0), expected_volume(2, 1))
            assert crate.index[0].offset == 96 + 240, "the second block was appended; the first is dead space"
            assert crate.size == 96 + 2 * 240 + 24 + 32
    return "a repeated (t, c) appends and overwrites the index slot"


# --- the exporter class (no host) ---------------------------------------------------------


def _descriptor(path: Path, **overrides) -> ExportDescriptor:
    fields = dict(locator=str(path), shape_tczyx=(1, 1, 4, 6, 5), dtype=np.dtype(np.uint16), little_endian=True,
                  source_metadata_form="", source_metadata_text="")
    fields.update(overrides)
    return ExportDescriptor(**fields)


def test_big_endian_source():
    """`little_endian = false`: the SDK hands `write` bytes reinterpreted in
    native order, so the values are swapped; the exporter swaps them back and
    the file is the format's fixed little-endian layout."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "be.crate"
        true = expected_volume(0, 0)
        be_bytes = true.astype(">u2").tobytes()  # what a big-endian host would deposit
        as_sdk_hands_it = np.frombuffer(be_bytes, dtype=np.uint16).reshape(true.shape)  # native reinterpretation
        assert not np.array_equal(as_sdk_hands_it, true)
        ex = exporter()
        ex.begin(_descriptor(path, little_endian=False))
        ex.write(0, 0, as_sdk_hands_it)
        ex.finish()
        with Crate.open(path) as crate:
            assert np.array_equal(crate.read_block(0, 0), true)
            assert crate.read_block(0, 0).tobytes() == true.astype("<u2").tobytes()
    return "a big-endian source lands as little-endian bytes with the true values"


def test_export_ranks_source_indices():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ranked.crate"
        ex = exporter()
        ex.begin(_descriptor(path, shape_tczyx=(3, 2, 4, 6, 5)))
        for t in (40, 41, 42):
            ex.write(t, 5, expected_volume(t - 40, 1))
            ex.write(t, 2, expected_volume(t - 40, 0))
        exc = expect_error("OUT_OF_BOUNDS", lambda: ex.write(43, 5, expected_volume(0, 0)), "4th distinct timepoint")
        expect_error("OUT_OF_BOUNDS", lambda: ex.write(40, 7, expected_volume(0, 0)), "3rd distinct channel")
        ex.finish()
        with Crate.open(path) as crate:
            for t in range(3):
                assert np.array_equal(crate.read_block(t, 0), expected_volume(t, 1)), "source channel 5 arrived first, so it is artifact channel 0"
                assert np.array_equal(crate.read_block(t, 1), expected_volume(t, 0))
    return f"t = 40..42 -> 0..2, channels 5, 2 -> 0, 1; one too many: {exc.message!r}"


def test_export_writes_what_it_is_and_keeps_what_it_came_from():
    """SPEC.md §7: spacing from the fields alone, the source's
    document kept under DERIVED and never read, the declared names written
    and a placeholder not, the record kept."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cal.crate"
        doc = '<OME><Pixels PhysicalSizeX="0.5" PhysicalSizeY="0.5" PhysicalSizeZ="1000" PhysicalSizeZUnit="nm" TimeIncrement="500" TimeIncrementUnit="ms"/></OME>'
        channels = (ChannelName(3, "Channel 4", "PLACEHOLDER"), ChannelName(1, "GCaMP", "FILE"))
        record = {"manifest_version": 1, "selection": {"channels": [3, 1]}}
        ex = exporter()
        ex.begin(_descriptor(path, shape_tczyx=(1, 2, 4, 6, 5), source_metadata_form="ome-xml", source_metadata_text=doc,
                             spacing_zyx=(2.0, None, None), spacing_unit="um", channels=channels, provenance=record))
        ex.write(0, 3, expected_volume(0, 0))
        ex.write(0, 1, expected_volume(0, 1))
        ex.finish()
        with Crate.open(path) as crate:
            assert crate.header.spacing_zyx == (2.0, 0.0, 0.0) and crate.header.time_increment == 0.0, "the source's document is never scraped"
            assert crate.header.derived and crate.metadata() == ("ome-xml", doc), "the source's document is kept verbatim, as provenance"
            assert crate.channel_names() == ("", "GCaMP"), crate.channel_names()
            assert json.loads(crate.provenance) == record
        ex = exporter()
        ex.begin(_descriptor(path))
        ex.write(0, 0, expected_volume(0, 0))
        ex.finish()
        with Crate.open(path) as crate:
            assert crate.spacing_label() == "(unknown)" and crate.metadata() == ("", "") and crate.provenance_bytes == b""
            assert crate.channel_names() == () and crate.header.derived
        assert "without voxel size" in format_bridge._MODEL["status"]
    return "field Z only, the document kept and not read; '' for the placeholder, GCaMP; the record kept; nothing known -> zeros and the panel says so"


# --- the comparator as a process ------------------------------------------------------------


def _run_tool(*args) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = crate_tool.main(list(args))
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, (out.getvalue() + err.getvalue()).strip()


def test_comparator_tiers_and_exit_codes():
    sample = str(FIXTURES / "sample.crate")
    code, text = _run_tool("compare", sample, sample)
    assert (code, text) == (0, BYTE_EQUAL), (code, text)
    code, text = _run_tool("compare", sample, str(INTEROP / "python.crate"))
    assert code == 0 and text == SEMANTIC, (code, text)
    code, text = _run_tool("compare", sample, str(FIXTURES / "sample-calibrated.crate"), "--calibration", str(FIXTURES / "vizi-crate-cal.bin"))
    assert code == 0 and text.startswith(SEMANTIC), (code, text)
    code, text = _run_tool("compare", sample, str(FIXTURES / "sample-calibrated.crate"))
    assert code == 2 and "calibration" in text, (code, text)
    code, text = _run_tool("compare", sample, str(FIXTURES / "nospacing.crate"))
    assert code == 0 and text.startswith(VOXEL) and "spacing" in text and "metadata" in text, (code, text)
    code, text = _run_tool("compare", sample, str(FIXTURES / "badcrc.crate"))
    assert code == 0 and text.startswith(DIFFERENT) and "(t=1, c=0)" in text, (code, text)
    code, text = _run_tool("compare", sample, str(FIXTURES / "unfinalized.crate"))
    assert code == 2 and "unfinished" in text, (code, text)
    code, text = _run_tool("compare", sample)
    assert code == 1, (code, text)
    code, text = _run_tool("check", str(FIXTURES / "badcrc.crate"))
    assert code == 3 and text == "1 of 6 blocks failed CRC in badcrc.crate; first: (t=1, c=0)", (code, text)
    return "byte-equal / semantic / voxel-equal / different named; exit 0 with a verdict, 2 unopenable, 1 usage"


def test_comparator_subprocess():
    """The same through a real process, since a harness will shell out."""
    proc = subprocess.run([sys.executable, str(HERE / "crate_tool.py"), "compare", str(FIXTURES / "sample.crate"),
                           str(FIXTURES / "nospacing.crate")], capture_output=True, text=True, check=False)
    assert proc.returncode == 0 and proc.stdout.startswith(VOXEL), proc
    proc = subprocess.run([sys.executable, str(HERE / "crate_tool.py"), "compare", "/nonexistent.crate",
                           str(FIXTURES / "sample.crate")], capture_output=True, text=True, check=False)
    assert proc.returncode == 2, proc
    return "exit codes hold across a process boundary"


# --- the plugin's own pieces ---------------------------------------------------------------


def test_spacing_label():
    assert spacing_label((2.0, 0.325, 0.325), 1.5) == "2.000 × 0.325 × 0.325 µm (z × y × x), Δt 1.5 s"
    assert spacing_label((0.0, 0.325, 0.325), 0.0) == "? × 0.325 × 0.325 µm (z × y × x), Δt ? s"
    assert spacing_label((0.0, 0.0, 0.0), 0.0) == "(unknown)"
    assert spacing_label((0.0, 0.0, 0.0), 2.0) == "? × ? × ? µm (z × y × x), Δt 2 s"
    return "SPEC.md's example string, ? per unknown component, (unknown) when nothing is known"


def test_manifest():
    manifest = json.loads((HERE / "vizi-plugin.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 1
    assert manifest["id"] == format_bridge.PLUGIN_ID
    assert manifest["engines"]["vizi"] == "1.0", "the major is the gate"
    assert manifest["entry"]["command"] == [".venv/bin/python", "format_bridge.py"]
    assert manifest["permissions"] == ["data_access"]
    (asset,) = manifest["assets"]
    table = (FIXTURES / "vizi-crate-cal.bin").read_bytes()
    import hashlib

    assert asset["id"] == format_bridge.CALIBRATION_ASSET and asset["filename"] == "vizi-crate-cal.bin"
    assert asset["size"] == len(table) == 32 and asset["sha256"] == hashlib.sha256(table).hexdigest()
    readers = manifest["contributions"]["readers"]
    exporters = manifest["contributions"]["exporters"]
    assert [r["id"] for r in readers] == ["crate"] == [e["id"] for e in exporters]
    assert set(_REGISTERED) == {"crate"} and set(_EXPORTERS) == {"crate"}
    commands = {c["id"]: c for c in manifest["contributions"]["commands"]}
    assert set(commands) == set(_COMMANDS), (set(commands), set(_COMMANDS))
    for cid, entry in commands.items():
        if cid == "crate.check_file":
            assert entry["menu"] == "tools" and entry["default_keybinding"] == "Ctrl+Shift+K"
        else:
            assert "menu" in entry and entry["menu"] is None, f"{cid} must set menu: null"
    (panel,) = manifest["contributions"]["panels"]
    assert panel["id"] == format_bridge.PANEL_ID and set(_PANEL_OPENED) == {panel["id"]}
    assert 0 < len(panel["menu_label"]) <= 128
    ids, bound = [], []

    def walk(node):
        ids.append(node["id"])
        if "command" in node:
            bound.append((node["id"], node["command"]))
        for child in node.get("children", []):
            walk(child)

    walk(panel["root"])
    assert len(ids) == len(set(ids)), "widget ids must be unique"
    for widget, cid in bound:
        assert cid in commands, f"{widget} binds undeclared {cid}"
    assert set(format_bridge._MODEL) <= set(ids), set(format_bridge._MODEL) - set(ids)
    verify = next(n for n in _nodes(panel["root"]) if n["id"] == "verify")
    assert verify["kind"] == "checkbox" and verify["value"] is True and format_bridge._MODEL["verify"] is True
    return "id, engines, entry, one permission, the asset's size and digest, menu: null on all but check_file, the model's widgets"


def _nodes(node):
    yield node
    for child in node.get("children", []):
        yield from _nodes(child)


# ---------------------------------------------------------------------------

check("CRC-32 check value", test_crc_check_value)
check("sample.crate index table", test_sample_index_table)
check("sample.crate voxels", test_sample_voxels)
check("calibration", test_calibrated)
check("badcrc.crate", test_badcrc)
check("unfinalized.crate", test_unfinalized)
check("check_file on the clean fixtures", test_check_file_fixtures)
check("not a crate", test_not_a_crate)
check("spacing-conflict: the header wins", test_spacing_conflict_header_wins)
check("provenance, names and DERIVED", test_sections_and_derived)
check("nospacing: nothing panics", test_nospacing)
check("units and both spellings of micro", test_micro_spellings_and_units)
check("exporter calibration rule", test_exporter_calibration_rule)
check("malformed header spacing", test_malformed_header_spacing)
check("reserved bytes ignored", test_reserved_bytes_ignored)
check("empty, unknown-form and non-UTF-8 metadata", test_empty_and_unknown_metadata)
check("zero-length axis refused", test_zero_axis_refused)
check("index inconsistent with header", test_index_inconsistent_with_header)
check("writer round trip, byte-stable, matches interop", test_writer_round_trip_and_stability)
check("scrambled block order", test_scrambled_order_reads)
check("incomplete finish and abort leave nothing", test_finish_incomplete_and_abort_leave_nothing)
check("repeated block: last wins", test_repeat_block_last_wins)
check("big-endian source", test_big_endian_source)
check("export chunks ranked by source index", test_export_ranks_source_indices)
check("export: what it is, and what it came from", test_export_writes_what_it_is_and_keeps_what_it_came_from)
check("comparator tiers and exit codes", test_comparator_tiers_and_exit_codes)
check("comparator as a subprocess", test_comparator_subprocess)
check("spacing label", test_spacing_label)
check("manifest", test_manifest)

if _failures:
    print(f"\n{len(_failures)} failed: {', '.join(_failures)}", file=sys.stderr)
    sys.exit(1)
print("\nself-test passed")
