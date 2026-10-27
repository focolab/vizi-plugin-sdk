#!/usr/bin/env python3
"""Peak-finder ROI plugin — the Python implementation of ``examples/peak_roi/SPEC.md``.

Blur the active volume, find the brightest voxel, drop an ellipsoidal ROI on
it.  Everything the protocol needs is the SDK's job; what is left here is the
plugin: five command handlers, a mirrored widget model, and a worker thread.
"""

from __future__ import annotations

import threading

from vizi_plugin_sdk import (
    CommandContext,
    HostError,
    SessionEnded,
    command,
    panel,
    panel_opened,
    pb2,
    plugin,
    serve,
    widgets as w,
)

from peak.analysis import peak_of
from peak.ellipsoid import ellipsoid

#: The manifest is declared in code (SPEC's Manifest section): ``--install``
#: installs it, and ``vizi-plugin.json`` beside this file is what
#: ``--manifest`` writes.
PLUGIN_ID = "org.example.peak_roi"
plugin(PLUGIN_ID, name="Peak ROI", version="0.1.0", permissions=["data_access", "contribute_rois"])
PANEL_ID = "peak"
STATUS_WIDGET = "status"

#: Stable within this plugin's namespace: re-running replaces this row rather
#: than accumulating duplicates.
CONTRIB_ID = "peak"
ROI_NAME = "Peak"
ROI_COLOR = "#ff4081"

CURRENT_TIMEPOINT = "Current timepoint"
ALL_TIMEPOINTS = "All timepoints"
MODES = (CURRENT_TIMEPOINT, ALL_TIMEPOINTS)

#: Each widget's invocation carries only that widget's own value, a button
#: sends nothing at all, and there is no way to ask the host what another
#: widget currently holds — so the plugin owns the model.  Seeded from the
#: same defaults the manifest declares, because a widget the user never
#: touches never invokes anything.
_EXTENT_WIDGETS = {"z": "ez", "y": "ey", "x": "ex"}
_STATE_LOCK = threading.Lock()
_EXTENTS = {"z": 4.0, "y": 8.0, "x": 8.0}
_MODE = CURRENT_TIMEPOINT
_STATUS = "Ready"

#: Held for the whole background run, so a repeat click reports "busy" instead
#: of starting a second overlapping job.
_RUN_LOCK = threading.Lock()


# --- panel feedback ---------------------------------------------------------
#
# The panel is the channel for the RUN's outcome.  The host shows
# a successful `CommandDone.message` too, so `run` can say it accepted the job
# -- but the analysis finishes after that reply has gone, so what it found can
# only ever reach the user through the panel.


def _say(panel, text: str) -> None:
    global _STATUS
    with _STATE_LOCK:
        _STATUS = text
    panel.set_value(STATUS_WIDGET, text)


def _format_extent(value: float) -> str:
    return f"{value:g}"


# --- widget state -----------------------------------------------------------


def _parse_extent(text: str) -> float:
    value = float(str(text).strip())
    if not (value > 0.0) or value == float("inf"):
        raise ValueError("extent must be a positive number of voxels")
    return value


def _set_extent(ctx: CommandContext, axis: str) -> None:
    label = axis.upper()
    raw = ctx.args.get("value", "")
    try:
        value = _parse_extent(raw)
    except (TypeError, ValueError):
        _say(ctx.panel(PANEL_ID), f"{label} extent {raw!r} is not a positive number.")
        raise ValueError(f"{label} extent {raw!r} is not a positive number") from None
    with _STATE_LOCK:
        _EXTENTS[axis] = value
    _say(ctx.panel(PANEL_ID), f"{label} extent set to {_format_extent(value)}.")


@command("peak.set_z", title="Set Z extent", menu=None)
def set_z(ctx: CommandContext) -> None:
    _set_extent(ctx, "z")


@command("peak.set_y", title="Set Y extent", menu=None)
def set_y(ctx: CommandContext) -> None:
    _set_extent(ctx, "y")


@command("peak.set_x", title="Set X extent", menu=None)
def set_x(ctx: CommandContext) -> None:
    _set_extent(ctx, "x")


@command("peak.set_mode", title="Set timepoint mode", menu=None)
def set_mode(ctx: CommandContext) -> None:
    global _MODE
    # A dropdown sends both `index` and `value`; prefer the text and fall back
    # to the index so a manifest edit that reorders the options still works.
    chosen = ctx.args.get("value")
    if chosen not in MODES:
        index = ctx.args.get("index")
        if not isinstance(index, int) or not 0 <= index < len(MODES):
            raise ValueError(f"unknown timepoint mode {ctx.args!r}")
        chosen = MODES[index]
    with _STATE_LOCK:
        _MODE = chosen
    _say(ctx.panel(PANEL_ID), f"Mode: {chosen.lower()}.")


@panel_opened(PANEL_ID)
def on_panel_opened(ctx: CommandContext) -> None:
    """Re-push what the plugin knows.

    The host rebuilds the panel from the manifest's declared initial values on
    every open and does not persist earlier patches, so a reopened panel would
    otherwise show `4 / 8 / 8` regardless of what the user last entered.
    """
    panel = ctx.panel(PANEL_ID)
    with _STATE_LOCK:
        extents = dict(_EXTENTS)
        mode = _MODE
        status = _STATUS
    for axis, widget in _EXTENT_WIDGETS.items():
        panel.set_value(widget, _format_extent(extents[axis]))
    panel.set_value("mode", mode)
    panel.set_value(STATUS_WIDGET, status)


# --- the run ----------------------------------------------------------------


@command("peak.run", title="Find peak", menu=None)
def run(ctx: CommandContext) -> str:
    """Accept the job, then get out of the way.

    The host allows 10 seconds for the whole handler, which an all-timepoints
    run over a real volume will exceed, so `CommandDone` cannot be the failure
    channel for the analysis.  `ok=true` means *accepted*, and the returned
    string is what the host shows for it; everything after that is reported on
    the panel.
    """
    panel = ctx.panel(PANEL_ID)
    if not _RUN_LOCK.acquire(blocking=False):
        _say(panel, "Busy — a run is already in progress.")
        raise RuntimeError("a peak run is already in progress")
    try:
        job = _accept(ctx, panel)
        threading.Thread(
            target=_work, args=(job,), daemon=True, name="peak-run"
        ).start()
    except BaseException:
        _RUN_LOCK.release()
        raise
    return "Peak run started; progress is on the Peak ROI panel."


def _accept(ctx: CommandContext, panel) -> tuple:
    """Validate everything decidable right now; raise to fail the invocation."""
    with _STATE_LOCK:
        extents = (_EXTENTS["z"], _EXTENTS["y"], _EXTENTS["x"])
        mode = _MODE
    host = ctx.host

    try:
        view = host.view_state()
    except SessionEnded:
        raise  # the session ended: no status for a closing panel
    except HostError as exc:
        _say(panel, f"Failed: {exc}")
        raise RuntimeError(f"could not read the view state: {exc}") from exc

    shape = tuple(view.shape_tczyx)
    # Which of the two "no dataset" signals arrives is not specified, so treat
    # both a host_error (above) and a degenerate shape as the same answer.
    if len(shape) != 5 or any(extent == 0 for extent in shape):
        _say(panel, "No dataset is open.")
        raise RuntimeError("no dataset is open")

    frames = int(shape[0])
    # The channel is the first one shown, `visible_channels[0]` (0 when none
    # is), not the deprecated, always-0 `c` (SPEC.md §1).  It is fixed
    # at run start: it does not follow the user mid-run.
    channel = int(view.visible_channels[0]) if view.visible_channels else 0
    return panel, host, extents, mode, int(view.t), channel, frames


def _work(job: tuple) -> None:
    panel, host, extents, mode, timepoint, channel, frames = job
    try:
        if mode == ALL_TIMEPOINTS:
            peaks = []
            for frame in range(frames):
                _say(panel, f"Reading frame {frame + 1}/{frames}…")
                peaks.append(_peak_at(host, frame, channel, extents))
            first = peaks[0]
            # Dense by contract: one row per frame in the domain, frame 0 at
            # the origin because the shape is built at that frame's peak.
            offsets = [
                tuple(float(later - start) for later, start in zip(peak, first))
                for peak in peaks
            ]
            roi = ellipsoid(_voxel_centre(first), extents, pose_translation=offsets)
            summary = f"peak {_show(first)} at t=0, tracked over {frames} frames"
        else:
            _say(panel, f"Reading t={timepoint}, c={channel}…")
            peak = _peak_at(host, timepoint, channel, extents)
            roi = ellipsoid(_voxel_centre(peak), extents)
            summary = f"peak {_show(peak)} at t={timepoint}"

        _say(panel, "Contributing ROI…")
        host.contribute_rois(
            [
                pb2.RoiContribution(
                    contrib_id=CONTRIB_ID,
                    roi=roi,
                    name=ROI_NAME,
                    style=pb2.RoiStyle(color=ROI_COLOR),
                )
            ]
        )
        _say(panel, f"Done — {summary}.")
    except SessionEnded:
        pass  # the session ended mid-run (Shutdown, or the host gone): no news for a closing panel
    except HostError as exc:
        _say(panel, f"Failed: {exc}")
    except Exception as exc:  # noqa: BLE001 — the panel is the only report channel
        _say(panel, f"Failed: {type(exc).__name__}: {exc}")
    finally:
        _RUN_LOCK.release()


def _peak_at(host, timepoint: int, channel: int, extents) -> tuple:
    """Read one volume and return its blurred peak index.

    Read at level 0, never at `ViewState.level`: ROI positions are voxel
    indices with no pyramid level attached, and converting a level-*n* index
    back to level 0 is not specified for every downsampling policy.
    """
    volume = host.read_volume(timepoint, channel, level=0)
    return peak_of(volume, extents)


def _voxel_centre(index) -> tuple:
    """Voxel index to position: integers are corners, half-integers are centers."""
    return tuple(float(value) + 0.5 for value in index)


def _show(index) -> str:
    return "(" + ", ".join(str(value) for value in index) + ")"


panel(
    PANEL_ID,
    title="Peak ROI",
    root=w.column(
        "root",
        w.row("rz", w.label("lz", "Z extent (voxels)"), w.line_edit("ez", command=set_z, value="4")),
        w.row("ry", w.label("ly", "Y extent (voxels)"), w.line_edit("ey", command=set_y, value="8")),
        w.row("rx", w.label("lx", "X extent (voxels)"), w.line_edit("ex", command=set_x, value="8")),
        w.dropdown("mode", ["Current timepoint", "All timepoints"], command=set_mode, value="Current timepoint"),
        w.button("go", "Find peak", command=run),
        w.label("status", "Ready"),
    ),
)


if __name__ == "__main__":
    serve()
