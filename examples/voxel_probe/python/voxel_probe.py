#!/usr/bin/env python3
"""Voxel probe — the developer guide's tool sample, made real.

`docs/contributions/tools.md`'s ``VoxelProbe`` verbatim (a marker that follows
the cursor on the live lane and stays where you release), plus what the guide's
sample leaves out and the emulator wants to drive: a second overlay carrying
the gesture's trail as a segment-backed line on release, a panel with an image
widget a command pushes a PNG into, and a ``@panel_opened`` re-push.  Every
push path the SDK has -- live lane, control-plane overlay, panel image -- runs
over a real process here, which nothing under ``tests/`` does.
"""

from __future__ import annotations

import struct
import threading
import zlib

import numpy as np

from vizi_plugin_sdk import (
    CommandContext,
    InputEvent,
    ToolContext,
    Voxel,
    command,
    overlay,
    panel,
    panel_opened,
    plugin,
    serve,
    tool,
    widgets as w,
)

#: The manifest is declared in code: ``--install`` installs it, and
#: ``vizi-plugin.json`` beside this file is what ``--manifest`` writes.
PLUGIN_ID = "org.example.voxel_probe"
plugin(PLUGIN_ID, name="Voxel Probe", version="0.1.0")
PANEL = "probe"
MARKER = "probe_marker"
TRAIL = "probe_trail"

_LOCK = threading.Lock()
#: The voxels of the last completed gesture, in order, and the count of
#: gestures seen -- what the panel reports and the snapshot draws.
_TRAIL: list[tuple[float, float, float]] = []
_GESTURES = 0
_STATUS = "No gesture yet"


def _say(panel, text: str) -> None:
    global _STATUS
    with _LOCK:
        _STATUS = text
    panel.set_value("status", text)


@tool("probe", name="Voxel probe", views=["2d", "3d"])
class VoxelProbe:
    def __init__(self) -> None:
        self._path: list[tuple[float, float, float]] = []

    def _marker(self, overlay, at: Voxel, inline: bool):
        pos_zyx = np.array([[at.z, at.y, at.x]], dtype=np.float64)
        return [
            overlay.marker_item(
                "probe", pos_zyx, color_rgba=(0, 255, 128, 255), size_px=10.0, inline=inline
            )
        ]

    def _at(self, ev: InputEvent) -> "Voxel | None":
        # A 3D pane resolves a ray rather than a voxel, and a MISS is a real
        # coordinate in a place this tool does not want; both mean "no marker".
        return ev.position if isinstance(ev.position, Voxel) and not ev.miss else None

    def press(self, ctx: ToolContext, ev: InputEvent) -> None:
        at = self._at(ev)
        if at is None:
            return
        self._path = [(at.z, at.y, at.x)]
        overlay = ctx.overlay(MARKER)
        overlay.set_items_live(self._marker(overlay, at, inline=True), pane=ev)

    def drag(self, ctx: ToolContext, ev: InputEvent) -> None:
        at = self._at(ev)
        if at is None:
            return
        self._path.append((at.z, at.y, at.x))
        overlay = ctx.overlay(MARKER)
        overlay.set_items_live(self._marker(overlay, at, inline=True), pane=ev)

    def release(self, ctx: ToolContext, ev: InputEvent) -> None:
        global _GESTURES
        at = self._at(ev)
        if at is None:
            return
        self._path.append((at.z, at.y, at.x))
        overlay = ctx.overlay(MARKER)
        overlay.set_items(self._marker(overlay, at, inline=False))
        # Beyond the guide's sample: the whole gesture as a segment-backed
        # line on a second overlay, which is a loan the host releases.
        trail = ctx.overlay(TRAIL)
        points = np.array(self._path, dtype=np.float64)
        trail.set_items(
            [trail.line_item("path", points, color_rgba=(255, 200, 0, 255), thickness=2.0)]
        )
        with _LOCK:
            _TRAIL[:] = self._path
            _GESTURES += 1
        self._path = []

    def cancel(self, ctx: ToolContext, ev) -> None:
        self._path = []
        ctx.overlay(MARKER).clear()


# --- the panel: a snapshot of the trail as a PNG through the P arena --------


def _png(gray: np.ndarray) -> bytes:
    """A minimal 8-bit grayscale PNG encoder: signature, IHDR, IDAT, IEND."""
    height, width = gray.shape
    raw = b"".join(b"\0" + bytes(row) for row in gray)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


@command("probe.snapshot", title="Snapshot the trail", menu=None)
def snapshot(ctx: CommandContext) -> str:
    with _LOCK:
        trail = list(_TRAIL)
    image = np.zeros((64, 64), dtype=np.uint8)
    for _, y, x in trail:
        image[int(y) % 64, int(x) % 64] = 255
    ctx.panel(PANEL).push_image("snapshot", _png(image))
    _say(ctx.panel(PANEL), f"Snapshot of {len(trail)} points")
    return f"snapshot pushed ({len(trail)} points)"


@command("probe.clear", title="Clear the probe", menu=None)
def clear(ctx: CommandContext) -> None:
    with _LOCK:
        _TRAIL.clear()
    ctx.overlay(MARKER).clear()
    ctx.overlay(TRAIL).clear()
    _say(ctx.panel(PANEL), "Cleared")


@panel_opened(PANEL)
def on_open(ctx: CommandContext) -> None:
    with _LOCK:
        status, gestures = _STATUS, _GESTURES
    ctx.panel(PANEL).set_value("status", f"{status} ({gestures} gestures so far)")


overlay(MARKER, name="Probe marker")
overlay(TRAIL, name="Probe trail", segment_mib=4)
panel(
    PANEL,
    title="Voxel Probe",
    root=w.column(
        "root",
        w.label("status", "No gesture yet"),
        w.image("snapshot"),
        w.button("snap", "Snapshot", command=snapshot),
        w.button("clear", "Clear", command=clear),
    ),
)


if __name__ == "__main__":
    serve(plugin_id=PLUGIN_ID)
