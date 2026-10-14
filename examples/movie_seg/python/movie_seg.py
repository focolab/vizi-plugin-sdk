#!/usr/bin/env python3
"""Movie seg: a band segmentation of every frame of a movie, served to the
host as a CHUNKED overlay (`control_plane_protocol.md` §5.7).

The host names the ``(level, t)`` volumes it wants (`ChunksWanted`) and this
plugin answers each with one bbox-tight ``uint8`` label volume on that
level's lattice (`OverlayChunk`), reading the frame through the data plane
at the wanted level.  A second, `static` overlay serves one volume per level
-- the footprint: every voxel that is foreground at ANY timepoint.  Each
overlay's LUT, names and visibility travel once in an `OverlayLabelTable`;
a threshold change re-serves every volume the host currently wants; a
band's visibility change re-sends the table.  A panel (a threshold box, a
checkbox per band) is what a person opens to start it in vizi, and it shows
the plugin's state.  `SPEC.md` beside this directory is the behaviour;
`bands.py` is the arithmetic.
"""

from __future__ import annotations

import threading

import numpy as np

from vizi_plugin_sdk import Chunk, ChunkWant, CommandContext, chunk_source, command, panel, plugin, serve, widgets as w

from bands import (
    BAND_NAMES,
    DEFAULT_THRESHOLD,
    band_labels,
    band_lut,
    bbox_tight,
    footprint_labels,
    footprint_lut,
    format_threshold,
)

#: The manifest is declared in code (SPEC's manifest): ``--install``
#: installs it, and ``vizi-plugin.json`` beside this file is what
#: ``--manifest`` writes.
PLUGIN_ID = "org.example.movie_seg"
plugin(PLUGIN_ID, name="Movie seg", version="0.1.0", permissions=["data_access"])
BANDS_OVERLAY = "bands"
FOOTPRINT_OVERLAY = "footprint"
#: SPEC §5a: the panel a person opens in vizi, which starts the session.
PANEL_ID = "movie_seg"
THRESHOLD_WIDGET = "threshold"
#: The channel this plugin segments (`SPEC.md` §2): the first.
CHANNEL = 0

_lock = threading.RLock()  # re-entered: a table is snapshotted and sent under it
_threshold = DEFAULT_THRESHOLD
_hidden: set[int] = set()
#: The overlays whose table has gone out this session (`SPEC.md` §3: once,
#: no later than the overlay's first chunk; again on a visibility change).
_tables_sent: set[str] = set()


def _threshold_now() -> float:
    with _lock:
        return _threshold


def _send_table(ctx, overlay_id: str) -> None:
    if overlay_id == BANDS_OVERLAY:
        # Snapshot and send under one lock: otherwise the
        # first table, built before a toggle, can go out after the toggle's own.
        with _lock:
            lut, names, visible = band_lut(frozenset(_hidden))
            # draw_priority 1: the footprint encloses every band voxel, and in
            # 3D an enclosing overlay hides what it encloses unless it ranks
            # lower (§3).
            ctx.overlay(BANDS_OVERLAY).set_label_table(
                lut, label_names=names, label_visible=visible, opacity=0.6, draw_priority=1
            )
    else:
        lut, names = footprint_lut()
        ctx.overlay(FOOTPRINT_OVERLAY).set_label_table(lut, label_names=names, opacity=0.3)


def _table_once(ctx, overlay_id: str) -> None:
    with _lock:
        if overlay_id in _tables_sent:
            return
        _tables_sent.add(overlay_id)
        _send_table(ctx, overlay_id)


def _chunk(labels: np.ndarray) -> Chunk | None:
    tight = bbox_tight(labels)
    if tight is None:
        return None  # an empty chunk: a state, not a removal
    return Chunk(tight[0], position_zyx=tight[1])


@chunk_source(BANDS_OVERLAY, name="Intensity bands", time="per_t")
def serve_bands(ctx, want: ChunkWant) -> Chunk | None:
    """One frame at one level: read it from the host at that level, band
    it, send the tight box.  Called by the SDK once per want in the host's
    order, off the dispatch path; a full arena defers the publish until the
    host's next release (the SDK's doing, `SPEC.md` §4)."""
    _table_once(ctx, BANDS_OVERLAY)
    volume = ctx.host.read_volume(want.t, CHANNEL, level=want.level)
    return _chunk(band_labels(volume, _threshold_now()))


@chunk_source(FOOTPRINT_OVERLAY, name="Footprint", time="static")
def serve_footprint(ctx, want: ChunkWant) -> Chunk | None:
    """The static overlay's one volume per level: foreground at any
    timepoint, read frame by frame at the wanted level.  ``want.t`` is
    ``None`` here and the chunk goes out without a `t`."""
    _table_once(ctx, FOOTPRINT_OVERLAY)
    frames = ctx.host.view_state().shape_tczyx[0]
    threshold = _threshold_now()
    volumes = (ctx.host.read_volume(t, CHANNEL, level=want.level) for t in range(frames))
    return _chunk(footprint_labels(volumes, threshold))


def _parse_threshold(raw) -> float:
    """A finite number >= 0, or the text of one: the panel's box sends its
    text, a script sends a number.  A bool is neither."""
    if isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            value = None
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
    else:
        value = None
    if value is None or not np.isfinite(value) or value < 0:
        raise ValueError(f"value must be a finite number >= 0, got {raw!r}")
    return value


@command("movie_seg.threshold", title="Set threshold", menu=None)
def set_threshold(ctx: CommandContext) -> str:
    """``value``: the new threshold, a finite non-negative number (or its
    text) in the dataset's own intensity units.  Every volume the host
    currently wants, on both overlays, is served again, and the panel's box
    shows the threshold."""
    global _threshold
    value = _parse_threshold(ctx.args.get("value"))
    with _lock:
        _threshold = value
    ctx.panel(PANEL_ID).set_value(THRESHOLD_WIDGET, format_threshold(value))
    wanted = 0
    for overlay_id in (BANDS_OVERLAY, FOOTPRINT_OVERLAY):
        overlay = ctx.overlay(overlay_id)
        wanted += len(overlay.wants)
        overlay.invalidate_chunks()
    return f"threshold {format_threshold(value)}; {wanted} wanted volume(s) to serve again"


def _set_band(ctx: CommandContext, band: int, visible) -> str:
    """Show or hide one band: the bands table goes out again with the new
    visibility, if it has gone out at all, and the band's checkbox follows."""
    if not isinstance(visible, bool):
        raise ValueError(f"visible must be a bool, got {visible!r}")
    with _lock:
        if visible:
            _hidden.discard(band)
        else:
            _hidden.add(band)
        if BANDS_OVERLAY in _tables_sent:
            _send_table(ctx, BANDS_OVERLAY)
    ctx.panel(PANEL_ID).set_value(f"band_{band}", visible)
    return f"band {band} ({BAND_NAMES[band]}) {'visible' if visible else 'hidden'}"


@command("movie_seg.band_visible", title="Show or hide a band", menu=None)
def band_visible(ctx: CommandContext) -> str:
    """``band``: 1..3; ``visible``: a bool.  The scripted form of the
    panel's three checkboxes."""
    band = ctx.args.get("band")
    if isinstance(band, bool) or not isinstance(band, int) or band not in BAND_NAMES:
        raise ValueError(f"band must be one of {sorted(BAND_NAMES)}, got {band!r}")
    return _set_band(ctx, band, ctx.args.get("visible"))


# A checkbox fires its command with ``{"value": bool}`` and nothing naming the
# widget, so each band's checkbox has a command of its own.
@command("movie_seg.band_1", title="Show band 1 (dim)", menu=None)
def band_1(ctx: CommandContext) -> str:
    return _set_band(ctx, 1, ctx.args.get("value"))


@command("movie_seg.band_2", title="Show band 2 (mid)", menu=None)
def band_2(ctx: CommandContext) -> str:
    return _set_band(ctx, 2, ctx.args.get("value"))


@command("movie_seg.band_3", title="Show band 3 (bright)", menu=None)
def band_3(ctx: CommandContext) -> str:
    return _set_band(ctx, 3, ctx.args.get("value"))


panel(
    PANEL_ID,
    title="Movie seg",
    root=w.column(
        "root",
        w.row("rt", w.label("lt", "Threshold"), w.line_edit(THRESHOLD_WIDGET, command=set_threshold, value=format_threshold(DEFAULT_THRESHOLD))),
        w.checkbox("band_1", "Band 1 (dim)", command=band_1, value=True),
        w.checkbox("band_2", "Band 2 (mid)", command=band_2, value=True),
        w.checkbox("band_3", "Band 3 (bright)", command=band_3, value=True),
    ),
)


if __name__ == "__main__":
    serve(plugin_id=PLUGIN_ID)
