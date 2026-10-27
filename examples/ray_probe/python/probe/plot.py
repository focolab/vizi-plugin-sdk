"""The intensity profile along a probe as a PNG, with no dependency: an
8-bit grayscale raster, axes, a 3x5 bitmap font for the labels, the pick
marked, and a minimal encoder (signature, IHDR, IDAT, IEND).  `SPEC.md`
§"The tool" 5 asks for the values of the traversal against distance with the
coarse pick marked, and nothing more.  The raster and the encoder are the
interactive-seg example's (`examples/interactive_seg/python/seg/plot.py`).
"""

from __future__ import annotations

import struct
import zlib

import numpy as np

# 3x5 glyphs, rows top to bottom, 1 = ink
_FONT = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "001", "001"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    ".": ("000", "000", "000", "000", "010"),
    "-": ("000", "000", "111", "000", "000"),
    "t": ("010", "111", "010", "010", "011"),
    "m": ("000", "110", "111", "101", "101"),
    "e": ("000", "111", "111", "100", "111"),
    "a": ("000", "111", "001", "111", "111"),
    "n": ("000", "110", "101", "101", "101"),
    "d": ("001", "001", "111", "101", "111"),
    "v": ("000", "101", "101", "101", "010"),
    "l": ("010", "010", "010", "010", "011"),
    "u": ("000", "101", "101", "101", "111"),
    "x": ("000", "101", "010", "101", "101"),
    " ": ("000", "000", "000", "000", "000"),
}


def _text(canvas: np.ndarray, y: int, x: int, text: str, ink: int = 255) -> None:
    for ch in text:
        glyph = _FONT.get(ch, _FONT[" "])
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1" and 0 <= y + row < canvas.shape[0] and 0 <= x + col < canvas.shape[1]:
                    canvas[y + row, x + col] = ink
        x += 4


def _line(canvas: np.ndarray, y0: int, x0: int, y1: int, x1: int, ink: int = 255) -> None:
    steps = max(abs(y1 - y0), abs(x1 - x0), 1)
    for i in range(steps + 1):
        y = int(round(y0 + (y1 - y0) * i / steps))
        x = int(round(x0 + (x1 - x0) * i / steps))
        if 0 <= y < canvas.shape[0] and 0 <= x < canvas.shape[1]:
            canvas[y, x] = ink


def render(
    values: list[float],
    xs: list[float] | None = None,
    pick: int | None = None,
    x_name: str = "d",
    width: int = 240,
    height: int = 140,
) -> np.ndarray:
    """A grayscale ``(height, width)`` raster: axes, the profile joined with
    each sample at its distance ``xs[i]`` along the probe (evenly spaced when
    ``xs`` is ``None``), the y-range printed, the axis names, and the pick as
    a filled square."""
    canvas = np.zeros((height, width), dtype=np.uint8)
    left, top, right, bottom = 28, 8, width - 8, height - 18
    _line(canvas, top, left, bottom, left, 128)
    _line(canvas, bottom, left, bottom, right, 128)
    _text(canvas, bottom + 8, (left + right) // 2 - 1, x_name)
    _text(canvas, top, 2, "value")
    finite = [v for v in values if np.isfinite(v)]
    if not finite:
        return canvas
    lo, hi = min(finite), max(finite)
    if hi == lo:
        lo, hi = lo - 1.0, hi + 1.0
    _text(canvas, top + 8, 2, f"{hi:.4g}"[:6])
    _text(canvas, bottom - 6, 2, f"{lo:.4g}"[:6])
    n = len(values)
    if xs is None or len(xs) != n:
        xs = [float(i) for i in range(n)]
    x0, x1 = min(xs), max(xs)
    span = (x1 - x0) or 1.0
    points = []
    for i, v in enumerate(values):
        x = left + int(round((right - left) * (xs[i] - x0) / span))
        if not np.isfinite(v):
            points.append(None)
            continue
        y = bottom - int(round((bottom - top) * (v - lo) / (hi - lo)))
        points.append((y, x))
    previous = None
    for point in points:
        if point is not None and previous is not None:
            _line(canvas, previous[0], previous[1], point[0], point[1], 200)
        previous = point
    if pick is not None and 0 <= pick < n and points[pick] is not None:
        y, x = points[pick]
        canvas[max(0, y - 2) : y + 3, max(0, x - 2) : x + 3] = 255
    return canvas


def png(gray: np.ndarray) -> bytes:
    """An 8-bit grayscale PNG."""
    height, width = gray.shape
    raw = b"".join(b"\0" + bytes(row) for row in np.asarray(gray, dtype=np.uint8))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
