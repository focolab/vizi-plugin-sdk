"""The ``.crate`` container, byte for byte from ``../SPEC.md`` §"The .crate format".

Pure standard library plus numpy.  ``Crate`` opens and validates a file and
reads blocks and planes out of it; ``CrateWriter`` writes one; ``check_file``
validates every block of one without opening it as a dataset; ``Calibration``
is the manifest asset.  Refusals are ``CrateError``s carrying the ``Error.kind``
name SPEC.md assigns, so the plugin can put the kind in the message even where
the SDK cannot put it on the wire.
"""

from __future__ import annotations

import math
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MAGIC = b"VIZCRATE"
FOOTER_MAGIC = b"ETARCZIV"
CAL_MAGIC = b"VIZCAL01"
FORMAT_VERSION = 1
HEADER_BYTES = 96
INDEX_ENTRY_BYTES = 24
FOOTER_BYTES = 32
FLAG_CALIBRATED = 1 << 0
#: Written by an exporter: the metadata section is the SOURCE's document, kept
#: as provenance, and the reader serves no document (SPEC.md §4).
FLAG_DERIVED = 1 << 1

#: The format's dtype codes, deliberately `DType`'s first three enumerators.
#: ``f64`` has no crate encoding: it is overlay geometry, not voxels.
DTYPE_OF_CODE = {1: np.dtype("<u1"), 2: np.dtype("<u2"), 3: np.dtype("<f4")}
CODE_OF_DTYPE = {np.dtype(np.uint8): 1, np.dtype(np.uint16): 2, np.dtype(np.float32): 3}
DTYPE_NAME_OF_CODE = {1: "u8", 2: "u16", 3: "f32"}

#: ``crc32(b"123456789")`` for the IEEE 802.3 / zlib CRC-32 the format uses.
CRC_CHECK_VALUE = 0xCBF43926

_HEADER = struct.Struct("<8sHHIIIIIIHI6xddddII8x")
_INDEX_ENTRY = struct.Struct("<QQI4x")
_FOOTER = struct.Struct("<QQI4x8s")
_CAL_HEADER = struct.Struct("<8sI4x")
_CAL_ENTRY = struct.Struct("<Ii")
assert _HEADER.size == HEADER_BYTES and _INDEX_ENTRY.size == INDEX_ENTRY_BYTES
assert _FOOTER.size == FOOTER_BYTES and _CAL_HEADER.size == 16


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


class CrateError(Exception):
    """A refusal, with the ``Error.kind`` name SPEC.md assigns to it.

    This package knows nothing about vizi; the plugin translates it at the
    boundary into the SDK's ``Refusal(kind, message)`` (``format_bridge.py``
    ``_refusing``), which the SDK forwards with the kind set and ``message``
    alone as the text; ``str(exc)`` keeps the name in front for the command-line
    tool and a log."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


def sanitize_spacing(value: float) -> float:
    """A negative, infinite or NaN spacing is malformed metadata, not a
    malformed file: treat it as unknown (``0.0``) and keep reading voxels."""
    return float(value) if math.isfinite(value) and value > 0.0 else 0.0


def spacing_label(spacing_zyx, time_increment: float) -> str:
    """The panel's ``vox`` line, formatted so anisotropy is visible at a
    glance: ``2.000 × 0.325 × 0.325 µm (z × y × x), Δt 1.5 s``; ``?`` per
    unknown component, ``(unknown)`` when nothing is known."""
    z, y, x = (float(v) for v in spacing_zyx)
    if not any((z, y, x, time_increment)):
        return "(unknown)"

    def fmt(v: float) -> str:
        return f"{v:.3f}" if v else "?"

    dt = f"{time_increment:g}" if time_increment else "?"
    return f"{fmt(z)} × {fmt(y)} × {fmt(x)} µm (z × y × x), Δt {dt} s"


# --- the header ---------------------------------------------------------------


@dataclass(frozen=True)
class Header:
    dtype_code: int
    flags: int
    t_count: int
    c_count: int
    z: int
    y: int
    x: int
    metadata_form_len: int
    metadata_text_len: int
    spacing_zyx: tuple[float, float, float]  # sanitized: 0.0 = unknown
    time_increment: float  # sanitized: 0.0 = unknown
    malformed: tuple[str, ...] = ()  # header fields that were not finite/positive
    provenance_len: int = 0
    names_len: int = 0

    @property
    def shape_tczyx(self) -> tuple[int, int, int, int, int]:
        return (self.t_count, self.c_count, self.z, self.y, self.x)

    @property
    def shape_zyx(self) -> tuple[int, int, int]:
        return (self.z, self.y, self.x)

    @property
    def np_dtype(self) -> np.dtype:
        return DTYPE_OF_CODE[self.dtype_code]

    @property
    def dtype_name(self) -> str:
        return DTYPE_NAME_OF_CODE[self.dtype_code]

    @property
    def itemsize(self) -> int:
        return self.np_dtype.itemsize

    @property
    def block_bytes(self) -> int:
        return self.z * self.y * self.x * self.itemsize

    @property
    def calibrated(self) -> bool:
        return bool(self.flags & FLAG_CALIBRATED)

    @property
    def derived(self) -> bool:
        return bool(self.flags & FLAG_DERIVED)

    @property
    def data_start(self) -> int:
        return HEADER_BYTES + self.metadata_form_len + self.metadata_text_len + self.provenance_len + self.names_len

    @property
    def blocks(self) -> int:
        return self.t_count * self.c_count

    @classmethod
    def parse(cls, data: bytes) -> "Header":
        if len(data) < HEADER_BYTES:
            raise CrateError("INVALID_ARGUMENT", "not a crate: shorter than a header")
        (magic, version, dtype_code, flags, t, c, z, y, x, form_len, text_len,
         sz, sy, sx, dt, provenance_len, names_len) = _HEADER.unpack_from(data, 0)
        if magic != MAGIC:
            raise CrateError("INVALID_ARGUMENT", "not a crate: bad magic")
        if version != FORMAT_VERSION:
            raise CrateError("INVALID_ARGUMENT", f"not a crate this reader knows: format_version {version}")
        if dtype_code not in DTYPE_OF_CODE:
            raise CrateError("UNSUPPORTED", f"dtype code {dtype_code} is not defined by the crate format")
        for name, extent in (("t_count", t), ("c_count", c), ("z", z), ("y", y), ("x", x)):
            if extent == 0:
                raise CrateError("INVALID_ARGUMENT", f"not a crate: {name} is 0, and a volume with no voxels is not a smaller volume")
        malformed = tuple(
            name for name, v in (("spacing_z", sz), ("spacing_y", sy), ("spacing_x", sx), ("time_increment", dt))
            if not (math.isfinite(v) and v >= 0.0)
        )
        return cls(
            dtype_code, flags, t, c, z, y, x, form_len, text_len,
            (sanitize_spacing(sz), sanitize_spacing(sy), sanitize_spacing(sx)),
            sanitize_spacing(dt),
            malformed,
            provenance_len,
            names_len,
        )

    def pack(self) -> bytes:
        sz, sy, sx = self.spacing_zyx
        return _HEADER.pack(
            MAGIC, FORMAT_VERSION, self.dtype_code, self.flags,
            self.t_count, self.c_count, self.z, self.y, self.x,
            self.metadata_form_len, self.metadata_text_len,
            sz, sy, sx, self.time_increment,
            self.provenance_len, self.names_len,
        )


@dataclass(frozen=True)
class IndexEntry:
    offset: int
    length: int
    crc32: int

    def pack(self) -> bytes:
        return _INDEX_ENTRY.pack(self.offset, self.length, self.crc32)


@dataclass(frozen=True)
class Footer:
    index_offset: int
    index_entries: int
    index_crc32: int

    @classmethod
    def parse(cls, data: bytes) -> "Footer":
        if len(data) < FOOTER_BYTES:
            raise CrateError("IO_ERROR", "unfinished crate — an export of this file did not complete")
        index_offset, entries, index_crc, magic = _FOOTER.unpack_from(data, len(data) - FOOTER_BYTES)
        if magic != FOOTER_MAGIC:
            raise CrateError("IO_ERROR", "unfinished crate — an export of this file did not complete")
        return cls(index_offset, entries, index_crc)

    def pack(self) -> bytes:
        return _FOOTER.pack(self.index_offset, self.index_entries, self.index_crc32, FOOTER_MAGIC)


# --- the calibration table ------------------------------------------------------


@dataclass(frozen=True)
class Calibration:
    """``vizi-crate-cal.bin``: a per-channel offset a calibrated crate's raw
    counts are decoded by.  A channel absent from the table has offset 0."""

    offsets: dict[int, int]

    @classmethod
    def parse(cls, data: bytes) -> "Calibration":
        if len(data) < _CAL_HEADER.size:
            raise CrateError("IO_ERROR", "calibration table is shorter than its header")
        magic, count = _CAL_HEADER.unpack_from(data, 0)
        if magic != CAL_MAGIC:
            raise CrateError("IO_ERROR", "calibration table has the wrong magic")
        if len(data) != _CAL_HEADER.size + count * _CAL_ENTRY.size:
            raise CrateError("IO_ERROR", f"calibration table declares {count} entries but is {len(data)} bytes")
        offsets = {}
        for i in range(count):
            channel, offset = _CAL_ENTRY.unpack_from(data, _CAL_HEADER.size + i * _CAL_ENTRY.size)
            offsets[channel] = offset
        return cls(offsets)

    @classmethod
    def load(cls, path: Path) -> "Calibration":
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            raise CrateError("IO_ERROR", f"calibration table {path} cannot be read: {exc}") from exc
        return cls.parse(data)

    def apply(self, raw: np.ndarray, channel: int) -> np.ndarray:
        """``reported = raw - offset[c]`` in the dtype's own arithmetic —
        saturating at zero for the unsigned dtypes, unclamped for ``f32``."""
        offset = self.offsets.get(channel, 0)
        if offset == 0:
            return raw
        if raw.dtype.kind == "u":
            wide = raw.astype(np.int64) - offset
            return np.clip(wide, 0, np.iinfo(raw.dtype).max).astype(raw.dtype)
        return (raw - np.array(offset, dtype=raw.dtype)).astype(raw.dtype)


# --- an open crate ------------------------------------------------------------------


class Crate:
    """An opened, validated crate: header, metadata, the index, and positional
    reads off one descriptor (``os.pread``, so the reader worker and a
    ``describe`` on another thread never race on a file position)."""

    def __init__(self, path: Path, fd: int, size: int, header: Header, form: bytes, text: bytes,
                 index: list[IndexEntry], provenance: bytes = b"", names: bytes = b"") -> None:
        self.path = Path(path)
        self._fd: int | None = fd
        self.size = size
        self.header = header
        self.form_bytes = form
        self.text_bytes = text
        self.index = index
        self.provenance_bytes = provenance
        self.names_bytes = names

    @classmethod
    def open(cls, path: Path) -> "Crate":
        path = Path(path)
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError as exc:
            raise CrateError("NOT_FOUND", f"{path}: {exc.strerror}") from exc
        try:
            size = os.fstat(fd).st_size
            header = Header.parse(os.pread(fd, HEADER_BYTES, 0))
            form = os.pread(fd, header.metadata_form_len, HEADER_BYTES)
            text = os.pread(fd, header.metadata_text_len, HEADER_BYTES + header.metadata_form_len)
            if len(form) != header.metadata_form_len or len(text) != header.metadata_text_len:
                raise CrateError("IO_ERROR", f"{path.name}: the metadata section is shorter than the header declares")
            after = HEADER_BYTES + header.metadata_form_len + header.metadata_text_len
            provenance = os.pread(fd, header.provenance_len, after)
            names = os.pread(fd, header.names_len, after + header.provenance_len)
            if len(provenance) != header.provenance_len or len(names) != header.names_len:
                raise CrateError("IO_ERROR", f"{path.name}: the provenance or names section is shorter than the header declares")
            footer = Footer.parse(os.pread(fd, FOOTER_BYTES, max(0, size - FOOTER_BYTES)))
            if footer.index_entries != header.blocks:
                raise CrateError("IO_ERROR", f"{path.name}: the footer declares {footer.index_entries} index entries, the header {header.blocks} blocks")
            index_bytes = footer.index_entries * INDEX_ENTRY_BYTES
            if footer.index_offset < header.data_start or footer.index_offset + index_bytes != size - FOOTER_BYTES:
                raise CrateError("IO_ERROR", f"{path.name}: the index does not sit between the blocks and the footer")
            raw_index = os.pread(fd, index_bytes, footer.index_offset)
            if crc32(raw_index) != footer.index_crc32:
                raise CrateError("IO_ERROR", f"{path.name}: index CRC-32 mismatch — the index is corrupt")
            index = []
            for i in range(footer.index_entries):
                offset, length, crc = _INDEX_ENTRY.unpack_from(raw_index, i * INDEX_ENTRY_BYTES)
                if length != header.block_bytes:
                    raise CrateError("IO_ERROR", f"{path.name}: index entry {i} has length {length}, the header says {header.block_bytes}")
                if offset < header.data_start or offset + length > footer.index_offset:
                    raise CrateError("IO_ERROR", f"{path.name}: index entry {i} points outside the block region")
                index.append(IndexEntry(offset, length, crc))
        except BaseException:
            os.close(fd)
            raise
        return cls(path, fd, size, header, form, text, index, provenance, names)

    # -- lifetime --

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    @property
    def closed(self) -> bool:
        return self._fd is None

    def __enter__(self) -> "Crate":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- what the file says about itself --

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def form(self) -> str:
        """The metadata form, or ``""`` when the crate carries no document or
        one that is not valid UTF-8 (malformed metadata, not a malformed file)."""
        try:
            return self.form_bytes.decode("utf-8") if self.text_utf8 else ""
        except UnicodeDecodeError:
            return ""

    @property
    def text_utf8(self) -> bool:
        try:
            self.text_bytes.decode("utf-8")
            self.form_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True

    def metadata(self) -> tuple[str, str]:
        """``(form, text)`` verbatim, or ``("", "")`` — *no document* — when the
        bytes on disk are not UTF-8 and so cannot cross a proto3 ``string``.
        This is the section as it is: for a ``DERIVED`` crate it is the
        SOURCE's document, and the reader serves none (``served_metadata``)."""
        if not self.text_utf8:
            return ("", "")
        return (self.form_bytes.decode("utf-8"), self.text_bytes.decode("utf-8"))

    def served_metadata(self) -> tuple[str, str]:
        """What the reader answers `Metadata` with (SPEC.md §4): the document,
        unless the crate is ``DERIVED``, whose document is its source's."""
        return ("", "") if self.header.derived else self.metadata()

    @property
    def provenance(self) -> str:
        """The provenance record as it was written, or ``""`` for none or for
        bytes that are not UTF-8.  Never parsed."""
        try:
            return self.provenance_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return ""

    def channel_names(self) -> tuple[str, ...] | None:
        """The names section by index (``""`` = unnamed; it may stop short), or
        ``None`` when it is malformed -- not UTF-8, or more names than
        ``c_count`` -- which is malformed metadata, not a malformed file."""
        if not self.names_bytes:
            return ()
        try:
            names = tuple(self.names_bytes.decode("utf-8").split("\0"))
        except UnicodeDecodeError:
            return None
        return names if len(names) <= self.header.c_count else None

    def spacing_label(self) -> str:
        return spacing_label(self.header.spacing_zyx, self.header.time_increment)

    # -- reads --

    def entry(self, t: int, c: int) -> IndexEntry:
        h = self.header
        if not 0 <= t < h.t_count:
            raise CrateError("OUT_OF_BOUNDS", f"t={t} is outside [0, {h.t_count}) in {self.name}")
        if not 0 <= c < h.c_count:
            raise CrateError("OUT_OF_BOUNDS", f"c={c} is outside [0, {h.c_count}) in {self.name}")
        return self.index[t * h.c_count + c]

    def _pread(self, length: int, offset: int, what: str) -> bytes:
        if self._fd is None:
            raise CrateError("NOT_FOUND", f"{self.name} is closed")
        raw = os.pread(self._fd, length, offset)
        if len(raw) != length:
            raise CrateError("IO_ERROR", f"short read of {what} in {self.name}")
        return raw

    def read_block(self, t: int, c: int, *, verify: bool = True,
                   calibration: Calibration | None = None) -> np.ndarray:
        """One ``(Z, Y, X)`` volume in the native dtype, CRC-checked when
        ``verify``, calibration applied when the header's flag is set."""
        entry = self.entry(t, c)
        raw = self._pread(entry.length, entry.offset, f"block (t={t}, c={c})")
        if verify and crc32(raw) != entry.crc32:
            raise CrateError("IO_ERROR", f"CRC-32 mismatch at (t={t}, c={c}) in {self.name}: the block's bytes do not match its index entry")
        arr = np.frombuffer(raw, dtype=self.header.np_dtype).reshape(self.header.shape_zyx)
        return self._decode(arr, c, calibration)

    def read_plane(self, t: int, z: int, c: int, *, calibration: Calibration | None = None) -> np.ndarray:
        """One ``(Y, X)`` plane: one seek and a short read, and **no CRC** —
        the checksum covers the whole block, and a plane read exists to be
        cheap (SPEC.md §3)."""
        entry = self.entry(t, c)
        h = self.header
        if not 0 <= z < h.z:
            raise CrateError("OUT_OF_BOUNDS", f"z={z} is outside [0, {h.z}) in {self.name}")
        plane_bytes = h.y * h.x * h.itemsize
        raw = self._pread(plane_bytes, entry.offset + z * plane_bytes, f"plane (t={t}, z={z}, c={c})")
        arr = np.frombuffer(raw, dtype=h.np_dtype).reshape((h.y, h.x))
        return self._decode(arr, c, calibration)

    def _decode(self, arr: np.ndarray, c: int, calibration: Calibration | None) -> np.ndarray:
        arr = arr.astype(arr.dtype.newbyteorder("="), copy=False)  # native order, whatever the host
        if self.header.calibrated:
            if calibration is None:
                raise CrateError("IO_ERROR", f"{self.name} is calibrated and no calibration table is available — provision vizi-crate-cal.bin in Plugin Manager")
            arr = calibration.apply(arr, c)
        return np.ascontiguousarray(arr)


# --- whole-file validation (`crate.check_file`) ---------------------------------------


@dataclass(frozen=True)
class CheckResult:
    blocks: int
    failed: tuple[tuple[int, int], ...]  # (t, c) of every block whose CRC failed

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self, name: str) -> str:
        if self.ok:
            return f"OK: {name} — {self.blocks} blocks, index and footer verified"
        t, c = self.failed[0]
        return f"{len(self.failed)} of {self.blocks} blocks failed CRC in {name}; first: (t={t}, c={c})"


def check_file(path: Path) -> CheckResult:
    """Validate header, footer, index CRC and every block CRC, counting every
    failure and not stopping at the first.  Raises ``CrateError`` for a file
    that is not a finalized, consistent crate."""
    with Crate.open(path) as crate:
        failed = []
        h = crate.header
        for i, entry in enumerate(crate.index):
            raw = crate._pread(entry.length, entry.offset, f"block {i}")
            if crc32(raw) != entry.crc32:
                failed.append((i // h.c_count, i % h.c_count))
        return CheckResult(h.blocks, tuple(failed))


# --- writing one --------------------------------------------------------------------


def encode_names(names, c_count: int) -> bytes:
    """The names section (SPEC.md): ``0x00``-separated UTF-8, trailing ``""``s
    dropped, so naming nothing is zero bytes.  A name holding ``0x00`` cannot
    be written and is written as ``""``."""
    names = ["" if "\0" in str(n) else str(n) for n in names]
    if len(names) > c_count:
        raise CrateError("INVALID_ARGUMENT", f"{len(names)} channel names for {c_count} channels")
    while names and not names[-1]:
        names.pop()
    return "\0".join(names).encode("utf-8")


class CrateWriter:
    """Write a crate block by block, in arrival order, and finalize — or
    abandon, leaving nothing behind."""

    def __init__(self, path: Path, shape_tczyx, np_dtype: np.dtype, form: str, text: bytes,
                 spacing_zyx=(0.0, 0.0, 0.0), time_increment: float = 0.0, *, flags: int = 0,
                 provenance: bytes = b"", names=()) -> None:
        dtype = np.dtype(np_dtype)
        if dtype not in CODE_OF_DTYPE:
            raise CrateError("UNSUPPORTED", f"dtype {dtype} has no crate encoding (u8, u16 and f32 do)")
        form_bytes = form.encode("utf-8")
        t, c, z, y, x = (int(v) for v in shape_tczyx)
        for name, extent in (("T", t), ("C", c), ("Z", z), ("Y", y), ("X", x)):
            if extent <= 0:
                raise CrateError("INVALID_ARGUMENT", f"a crate cannot have {name} = {extent}")
        names_bytes = encode_names(names, c)
        provenance = bytes(provenance)
        if len(form_bytes) > 0xFFFF or len(text) > 0xFFFFFFFF or len(provenance) > 0xFFFFFFFF or len(names_bytes) > 0xFFFFFFFF:
            raise CrateError("INVALID_ARGUMENT", "metadata too long for the crate header's length fields")
        self.path = Path(path)
        self.header = Header(
            CODE_OF_DTYPE[dtype], flags, t, c, z, y, x, len(form_bytes), len(text),
            tuple(sanitize_spacing(v) for v in spacing_zyx), sanitize_spacing(time_increment),
            (), len(provenance), len(names_bytes),
        )
        self._form = form_bytes
        self._text = bytes(text)
        self._provenance = provenance
        self._names = names_bytes
        self._file = None
        self._position = 0
        self._entries: list[IndexEntry | None] = [None] * self.header.blocks

    @property
    def le_dtype(self) -> np.dtype:
        return self.header.np_dtype  # explicitly little-endian

    def begin(self) -> None:
        self._file = open(self.path, "wb")
        head = self.header.pack() + self._form + self._text + self._provenance + self._names
        self._file.write(head)
        self._position = len(head)

    def write_block(self, t: int, c: int, volume: np.ndarray) -> IndexEntry:
        """Append one ``(Z, Y, X)`` volume for artifact indices ``(t, c)``.
        A repeat ``(t, c)`` wins: the new block is appended and the index slot
        overwritten, the old bytes left stranded (SPEC.md §8)."""
        if self._file is None:
            raise CrateError("IO_ERROR", "the writer has not begun")
        h = self.header
        if not 0 <= t < h.t_count:
            raise CrateError("OUT_OF_BOUNDS", f"artifact timepoint {t} is outside [0, {h.t_count})")
        if not 0 <= c < h.c_count:
            raise CrateError("OUT_OF_BOUNDS", f"artifact channel {c} is outside [0, {h.c_count})")
        arr = np.asarray(volume)
        if tuple(arr.shape) != h.shape_zyx:
            raise CrateError("INVALID_ARGUMENT", f"volume shape {tuple(arr.shape)} is not the declared {h.shape_zyx}")
        if arr.dtype != h.np_dtype.newbyteorder("="):
            raise CrateError("INVALID_ARGUMENT", f"volume dtype {arr.dtype} is not the declared {h.dtype_name}")
        data = np.ascontiguousarray(arr).astype(self.le_dtype, copy=False).tobytes()
        entry = IndexEntry(self._position, len(data), crc32(data))
        self._file.write(data)
        self._position += len(data)
        self._entries[t * h.c_count + c] = entry
        return entry

    def missing(self) -> list[tuple[int, int]]:
        c_count = self.header.c_count
        return [(i // c_count, i % c_count) for i, e in enumerate(self._entries) if e is None]

    def finish(self) -> None:
        """Index, footer, flush, close.  An incomplete index fails the finish
        **and deletes the file**: half an artifact is not a smaller one."""
        if self._file is None:
            raise CrateError("IO_ERROR", "the writer has not begun")
        missing = self.missing()
        if missing:
            self.abort()
            t, c = missing[0]
            raise CrateError("IO_ERROR", f"{len(missing)} of {self.header.blocks} blocks never arrived (first missing: t={t}, c={c}); {self.path.name} was not written")
        index = b"".join(e.pack() for e in self._entries)  # type: ignore[union-attr]
        footer = Footer(self._position, len(self._entries), crc32(index))
        self._file.write(index)
        self._file.write(footer.pack())
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        self._file = None

    def abort(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
