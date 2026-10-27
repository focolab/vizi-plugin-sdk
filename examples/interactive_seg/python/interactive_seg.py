#!/usr/bin/env python3
"""Interactive segmentation — an implementation of ``../SPEC.md``
on the Python SDK.

A lasso tool on the doorbell (`press`/`drag`/`release`/`cancel`), a marker
trail on the overlay's Pulsed lane while the user drags, the segmentation on a
worker (the threshold, the anchor, the components -- ``seg/``), a label
volume pushed as a loan with ``uses=2`` so the same descriptor can be
compiled, the commit into native masks, and a mean-versus-t plot pushed
through the panel arena.  Nothing here touches gRPC, a descriptor or a
mapping.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import numpy as np

from vizi_plugin_sdk import (
    Column,
    CommandContext,
    HostError,
    InputEvent,
    SessionEnded,
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


# `seg.grow.Refusal` is the example's own -- a job refused with a sentence
# for the panel -- not the SDK's `Refusal(kind, message)` for a reader or
# exporter; this plugin has neither.
from seg.grow import LabelMap, Refusal, grow, lut, threshold
from seg.lasso import LIVE_MAX_POINTS, bbox_of, decimate, search_window, selected_columns
from seg.plot import png, render

#: The manifest is declared in code (SPEC's Manifest section): ``--install``
#: installs it, and ``vizi-plugin.json`` beside this file is what
#: ``--manifest`` writes.
PLUGIN_ID = "org.example.interactive_seg"
plugin(PLUGIN_ID, name="Interactive Segmentation", version="0.1.0", permissions=["data_access", "contribute_rois"])
PANEL = "seg"
TOOL = "seg.lasso"
TRAIL = "seg.trail"
LABELS = "seg.labels"
TRAIL_COLOR = (255, 255, 255, 220)
TRAIL_SIZE_PX = 5.0

MSG_MIP = "Turn off Z-MIP — the tool needs to know which plane you are pointing at."
MSG_LEVEL = "Zoom in to full resolution — the tool segments level-0 voxels."
MSG_CHANNELS = "Show a single channel — the tool cannot tell which one you mean."
MSG_LOOP = "Drag a closed loop around a region."
MSG_ACTIVE = "Lasso active — drag a loop in a 2D view"
MSG_READY = "Ready"
MSG_BUSY = "Busy — a job is already running."


# --- the model ------------------------------------------------------------------------


@dataclass
class Model:
    """The panel's state, seeded from the manifest's declared defaults."""

    min_size: int = 50
    threshold_scale: float = 1.0
    grow_3d: bool = True
    mode: str = "Replace"
    region_index: int = 0
    regions: list[tuple[str, str]] = field(default_factory=list)  # (name, region_id) in dropdown order
    status: str = MSG_READY
    can_commit: bool = False


@dataclass
class Segmentation:
    """§9's accumulation, scoped to one gesture frame ``(t, c, gen)``."""

    t: int
    c: int
    gen: int
    labels: LabelMap = field(default_factory=LabelMap)


_LOCK = threading.Lock()
_MODEL = Model()
_SEG: Segmentation | None = None
#: One job at a time (§"Budgets"): the gesture worker, commit and measure.
_JOB = threading.Lock()


# Every entry point has the panel: a command's context and a tool's
# (`ToolContext.panel(id)`), so `activated` can say *Lasso active* and a refusal can be keyed on the
# record it refused without a handle captured from whichever command happened
# to run first.  A patch is a queued push -- safe from the listener thread.


def _say(ctx, text: str) -> None:
    with _LOCK:
        _MODEL.status = text
    ctx.panel(PANEL).set_value("status", text)


def _push_model(ctx) -> None:
    """Re-push everything: the host rebuilds the panel from the manifest on
    every open and persists nothing."""
    panel = ctx.panel(PANEL)
    with _LOCK:
        m = Model(**_MODEL.__dict__)
    panel.set_value("size", int(m.min_size))
    panel.set_value("thresh", float(m.threshold_scale))
    panel.set_value("grow3d", bool(m.grow_3d))
    panel.set_value("mode", m.mode)
    panel.set_enabled("commit", m.can_commit)
    names = [name for name, _ in m.regions] or ["(none)"]
    panel.set_options("region", names)
    panel.set_value("region", names[min(m.region_index, len(names) - 1)])
    panel.set_visible("rmeasure", bool(m.regions))
    panel.set_value("status", m.status)


def _start_job(target, ctx, *args) -> bool:
    """Take the job lock and run ``target(ctx, *args)`` on a worker; ``False``
    if busy.  The context's handles outlive the handler that received it."""
    if not _JOB.acquire(blocking=False):
        return False

    def run() -> None:
        try:
            target(ctx, *args)
        except SessionEnded:
            pass  # the session ended mid-job (Shutdown, or the host gone): no news for a closing panel
        except HostError as exc:
            _say(ctx, f"Failed: {exc}")
        except Refusal as exc:
            _say(ctx, str(exc))
        except Exception as exc:  # noqa: BLE001 -- the panel is the only channel a worker has
            _say(ctx, f"Failed: {exc!r}")
        finally:
            _JOB.release()

    threading.Thread(target=run, daemon=True, name="seg-job").start()
    return True


# --- the tool -------------------------------------------------------------------------


@dataclass
class Gesture:
    """What the first usable record fixed for the whole gesture (§1) -- the
    press's in the ordinary case: the frame, and the drawn plane from the
    first usable Voxel, a miss included; later records may disagree and are
    ignored.  ``heard`` is whether any usable record reached it: a gesture
    none did ends in silence."""

    interaction: int
    shape_zyx: tuple[int, int, int] | None
    z_g: int | None = None
    t: int = 0
    level: int = 0
    gen: int = 0
    visible: frozenset[int] | None = None  # None: only the mask was available and bit 15 was set
    heard: bool = False
    refusal: str | None = None
    path: list[tuple[float, float]] = field(default_factory=list)  # (y, x)
    live_failed: bool = False


@tool(TOOL, name="Lasso segment", views=["2d"])
class Lasso:
    def __init__(self) -> None:
        self._gesture: Gesture | None = None

    # -- lifecycle (§"The UI state model"): the panel follows the engagement --

    def activated(self, ctx: ToolContext) -> None:
        _say(ctx, MSG_ACTIVE)

    def deactivated(self, ctx: ToolContext) -> None:
        _say(ctx, MSG_READY)

    # -- the gesture (§1) --

    @staticmethod
    def _visible(ctx: ToolContext, ev: InputEvent) -> frozenset[int] | None:
        pane = ctx.pane(ev)
        if pane is not None:
            return pane.visible_channels(ev.channels)
        if ev.channels & (1 << 15):
            return None  # an unknown number of channels above 14 (note 8)
        return frozenset(i for i in range(15) if ev.channels & (1 << i))

    def _take(self, ctx: ToolContext, ev: InputEvent, gesture: Gesture) -> None:
        """§1: what one record contributes.  An unusable record nothing at
        all; the first usable one the gesture's frame; a usable Voxel the
        drawn plane if none is fixed yet (a miss included: a 2D miss is the
        true off-image coordinate on the displayed plane) and, unless a miss,
        a point; a Column the refusal §3 reports at release."""
        if not ev.usable:
            return
        if not gesture.heard:
            gesture.heard = True
            gesture.t, gesture.level, gesture.gen = int(ev.t), int(ev.level), int(ev.gen)
            gesture.visible = self._visible(ctx, ev)
        if isinstance(ev.position, Column):
            gesture.refusal = MSG_MIP  # §3: the record is the authority on MIP; no point
        elif isinstance(ev.position, Voxel):
            if gesture.z_g is None:
                gesture.z_g = int(round(ev.position.z))
            if not ev.miss:
                gesture.path.append((ev.position.y, ev.position.x))

    def press(self, ctx: ToolContext, ev: InputEvent) -> None:
        pane = ctx.pane(ev)
        shape = None
        if pane is not None:
            (z0, zn), (y0, yn), (x0, xn) = pane.extent
            shape = (zn, yn, xn)
        gesture = Gesture(interaction=int(ev.interaction), shape_zyx=shape)
        self._gesture = gesture
        self._take(ctx, ev, gesture)
        self._publish(ctx, ev, gesture)

    def drag(self, ctx: ToolContext, ev: InputEvent) -> None:
        gesture = self._gesture
        if gesture is None or gesture.interaction != ev.interaction:
            return
        self._take(ctx, ev, gesture)
        self._publish(ctx, ev, gesture)

    def release(self, ctx: ToolContext, ev: InputEvent) -> None:
        gesture = self._gesture
        self._gesture = None
        if gesture is None or gesture.interaction != ev.interaction:
            return
        self._take(ctx, ev, gesture)
        ctx.overlay(TRAIL).clear()  # from the handler, before the handoff
        if gesture.refusal:  # only ever set by a usable record
            _say(ctx, gesture.refusal)
            return
        if not gesture.heard or gesture.z_g is None:
            return  # no usable record reached it: nothing taken, nothing said (Acceptance 16)
        with _LOCK:
            settings = (_MODEL.min_size, _MODEL.threshold_scale, _MODEL.grow_3d, _MODEL.mode)
        if not _start_job(_segment, ctx, gesture, settings):
            _say(ctx, MSG_BUSY)

    def cancel(self, ctx: ToolContext, ev) -> None:
        self._gesture = None
        ctx.overlay(TRAIL).clear()

    # -- the live lane (§2) --

    def _publish(self, ctx: ToolContext, ev: InputEvent, gesture: Gesture) -> None:
        # §2: no trail under a gesture §3 will refuse -- a Column has no points, and level-n
        # coordinates in the marker's level-0 frame would draw at the wrong scale
        if gesture.refusal or gesture.level != 0 or gesture.z_g is None or not gesture.path:
            return
        overlay = ctx.overlay(TRAIL)
        picks = decimate(len(gesture.path), LIVE_MAX_POINTS)
        points = np.array([(float(gesture.z_g), *gesture.path[i]) for i in picks], dtype=np.float64)
        try:
            overlay.set_items_live(
                [overlay.marker_item("trail", points, color_rgba=TRAIL_COLOR, size_px=TRAIL_SIZE_PX, inline=True)],
                pane=ev,
            )
        except SessionEnded:
            return  # the session ended: there is no preview to miss
        except Exception as exc:  # noqa: BLE001 -- once per gesture, on the status line
            if not gesture.live_failed:
                gesture.live_failed = True
                _say(ctx, f"Live preview unavailable ({exc}); the segmentation will still run on release.")


# --- the segmentation worker (§3-§9) --------------------------------------------------


def _segment(ctx: ToolContext, gesture: Gesture, settings: tuple) -> None:
    global _SEG
    min_size, scale, grow_3d, mode = settings
    host = ctx.host
    # §3: the refusals, keyed on the record
    if gesture.level != 0:
        raise Refusal(MSG_LEVEL)
    if gesture.visible is None or len(gesture.visible) != 1:
        raise Refusal(MSG_CHANNELS)
    c = next(iter(gesture.visible))
    t, z_g, gen = gesture.t, gesture.z_g, gesture.gen
    shape = gesture.shape_zyx
    if shape is None:
        view = host.view_state()
        shape = tuple(int(v) for v in view.shape_tczyx[2:])
    depth, height, width = shape
    if not 0 <= z_g < depth:
        raise Refusal(MSG_LOOP)

    # §4: the selection
    selected = selected_columns(gesture.path, (height, width))
    if not selected.any():
        raise Refusal(MSG_LOOP)

    # §5: the search window, one sub-box read
    lo, hi = search_window(selected, shape)
    _say(ctx, f"Reading {hi[1] - lo[1]}×{hi[2] - lo[2]} columns through {depth} planes…")
    window = host.read_volume(t, c, 0, roi=(lo, hi))
    if (int(getattr(window, "open_generation", gen)) & 0xFFFFFFFF) != (gen & 0xFFFFFFFF):
        raise Refusal("The dataset changed under the gesture — draw again.")
    window = np.asarray(window)
    sel_w = selected[lo[1] : hi[1], lo[2] : hi[2]]

    # §6-§8
    value = threshold(window, sel_w, scale)
    faces = (
        (lo[0] == 0, hi[0] == depth),
        (lo[1] == 0, hi[1] == height),
        (lo[2] == 0, hi[2] == width),
    )
    grown = grow(window, sel_w, z_g, value, grow_3d=grow_3d, min_size=int(min_size), window_is_volume_face=faces)

    # §9: accumulate, scoped to (t, c, gen)
    box = bbox_of(grown.component)
    assert box is not None
    (bz0, by0, bx0), (bz1, by1, bx1) = box
    component = grown.component[bz0:bz1, by0:by1, bx0:bx1]
    origin = (lo[0] + bz0, lo[1] + by0, lo[2] + bx0)
    with _LOCK:
        seg = _SEG
        fresh = seg is None or mode == "Replace" or (seg.t, seg.c, seg.gen) != (t, c, gen)
        note = ""
        if seg is not None and mode != "Replace" and (seg.t, seg.c, seg.gen) != (t, c, gen):
            note = " (new frame — starting a fresh accumulation)"
        if fresh:
            seg = Segmentation(t=t, c=c, gen=gen)
        label = seg.labels.add(origin, component)
        _SEG = seg
    # One use: the push alone.  §"Committing" offers `uses=2` so the commit can
    # name the same bytes, but a declaration cannot be lowered, so every push
    # the user supersedes before committing would pin its bytes until the
    # segment retires; the commit reserves afresh instead.
    overlay = ctx.overlay(LABELS)
    overlay.set_items([_label_item(overlay, seg)])
    with _LOCK:
        _MODEL.can_commit = True
    ctx.panel(PANEL).set_enabled("commit", True)
    size = int(component.sum())
    clipped = "; clipped by the search window" if grown.clipped else ""
    _say(ctx, f"Label {label}: {size} voxels at threshold {grown.threshold:.4g}{clipped}{note}")


# --- commands -------------------------------------------------------------------------


@command("seg.set_min_size", title="Set minimum size", menu=None)
def set_min_size(ctx: CommandContext) -> None:
    value = ctx.args.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or int(value) < 1:
        raise ValueError(f"minimum size must be a positive integer, not {value!r}")
    with _LOCK:
        _MODEL.min_size = int(value)


@command("seg.set_threshold", title="Set threshold scale", menu=None)
def set_threshold(ctx: CommandContext) -> None:
    value = ctx.args.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not np.isfinite(value) or value <= 0:
        raise ValueError(f"threshold scale must be a positive number, not {value!r}")
    with _LOCK:
        _MODEL.threshold_scale = float(value)


@command("seg.set_3d", title="Set 3D growth", menu=None)
def set_3d(ctx: CommandContext) -> None:
    with _LOCK:
        _MODEL.grow_3d = bool(ctx.args.get("value", True))


@command("seg.set_mode", title="Set accumulate mode", menu=None)
def set_mode(ctx: CommandContext) -> None:
    value = ctx.args.get("value")
    if value not in ("Replace", "Add"):
        raise ValueError(f"unknown mode {value!r}")
    with _LOCK:
        _MODEL.mode = value


@command("seg.set_region", title="Set measured region", menu=None)
def set_region(ctx: CommandContext) -> None:
    index = ctx.args.get("index")
    with _LOCK:
        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < max(1, len(_MODEL.regions)):
            _MODEL.region_index = index
        else:
            raise ValueError(f"no region at index {index!r}")


@command("seg.pick", title="Activate lasso", menu=None)
def pick(ctx: CommandContext) -> str:
    view = ctx.host.view_state()
    if view.mip:
        _say(ctx, MSG_MIP)
        return MSG_MIP
    try:
        active = ctx.host.activate_tool(TOOL)
    except SessionEnded:
        raise  # the session ended: nothing to tell the person
    except HostError as exc:
        _say(ctx, f"Could not activate the lasso: {exc}")
        raise
    return "Lasso active" if active else "The host declined to activate the lasso"


@command("seg.commit", title="Commit to annotations", menu=None)
def commit(ctx: CommandContext) -> str:
    with _LOCK:
        seg = _SEG
    if seg is None or not seg.labels.entries:
        raise ValueError("nothing to commit — segment something first")
    if not _start_job(_commit, ctx, seg):
        _say(ctx, MSG_BUSY)
        raise RuntimeError(MSG_BUSY)
    return "Commit started; the outcome is on the Interactive Segmentation panel."


def _label_item(overlay, seg: Segmentation):
    """§9's item: bbox-tight u8 labels, the LUT, an integer origin, the
    gesture's ``t``, names keyed by label id -- a fresh one-use reservation."""
    labels, position = seg.labels.render()
    return overlay.label_volume_item(
        "labels",
        labels,
        lut(),
        t=seg.t,
        position_zyx=position,
        label_names={k: f"Region {k}" for k, _, _ in seg.labels.entries},
    )


def _label_order(record) -> int:
    """A compiled label's region is `label:<value>` (SPEC.md, "Committing"):
    order by the value, anything else last."""
    value = record.contrib_id.removeprefix("label:")
    return int(value) if record.contrib_id.startswith("label:") and value.isdigit() else 1 << 31


def _commit(ctx: CommandContext, seg: Segmentation) -> None:
    host = ctx.host
    overlay = ctx.overlay(LABELS)
    # §"Committing", the reserve-twice route: the same labels, reserved again
    # for the compile alone, so nothing depends on the display push's bytes
    # still being lent.
    count = host.compile_label_volume(LABELS, _label_item(overlay, seg), replace_all=True)
    overlay.clear()  # the host draws the masks now; two copies read as two colors
    records = [r for r in host.get_rois() if r.origin == f"plugin:{PLUGIN_ID}"]
    records.sort(key=_label_order)
    regions = [(r.name, r.region_id) for r in records]
    with _LOCK:
        _MODEL.regions = regions
        _MODEL.region_index = 0
    panel = ctx.panel(PANEL)
    panel.set_options("region", [name for name, _ in regions] or ["(none)"])
    if regions:
        panel.set_value("region", regions[0][0])
    panel.set_visible("rmeasure", bool(regions))
    _say(ctx, f"Committed {count} region{'s' if count != 1 else ''} as native masks")


@command("seg.measure", title="Measure over time", menu=None)
def measure(ctx: CommandContext) -> str:
    with _LOCK:
        regions, index = list(_MODEL.regions), _MODEL.region_index
    if not regions:
        raise ValueError("nothing to measure — commit first")
    if not _start_job(_measure, ctx, regions, index):
        _say(ctx, MSG_BUSY)
        raise RuntimeError(MSG_BUSY)
    return "Measure started; the plot lands on the Interactive Segmentation panel."


def _measure(ctx: CommandContext, regions: list[tuple[str, str]], index: int) -> None:
    host = ctx.host
    if not 0 <= index < len(regions):
        raise Refusal("The selected region no longer exists — commit again.")
    name, region_id = regions[index]
    live = {r.region_id for r in host.get_rois()}
    if region_id not in live:
        records = [r for r in host.get_rois() if r.origin == f"plugin:{PLUGIN_ID}"]
        with _LOCK:
            _MODEL.regions = [(r.name, r.region_id) for r in records]
            _MODEL.region_index = 0
        ctx.panel(PANEL).set_options("region", [r.name for r in records] or ["(none)"])
        raise Refusal(f"Region {name!r} is gone — the list was refreshed; choose again.")
    _say(ctx, f"Reading mask {name}…")
    membership, origin = host.read_mask_voxels(region_id)
    mask = np.asarray(membership).astype(bool)
    lo = tuple(int(v) for v in origin)
    hi = tuple(int(o + s) for o, s in zip(lo, mask.shape))
    view = host.view_state()  # §"Measuring" step 3: the channel and T from a fresh snapshot
    # The first channel shown, or 0 when none is; never `ViewState.c` (deprecated, always 0).
    frames, c = int(view.shape_tczyx[0]), (int(view.visible_channels[0]) if view.visible_channels else 0)
    means: list[float] = []
    for t in range(frames):
        _say(ctx, f"Measuring {name}: timepoint {t + 1}/{frames}…")
        volume = np.asarray(host.read_volume(t, c, 0, roi=(lo, hi)), dtype=np.float64)
        picked = volume[mask]
        means.append(float(picked.mean()) if picked.size else float("nan"))
    ctx.panel(PANEL).push_image("plot", png(render(means)))
    _say(ctx, f"{name}: mean over {frames} timepoints, {int(mask.sum())} voxels, channel {c}")


@command("seg.reset", title="Clear segmentation", menu=None)
def reset(ctx: CommandContext) -> None:
    global _SEG
    with _LOCK:
        _SEG = None
        _MODEL.can_commit = False
    ctx.overlay(LABELS).clear()
    ctx.overlay(TRAIL).clear()
    ctx.panel(PANEL).set_enabled("commit", False)
    _say(ctx, "Cleared")


@panel_opened(PANEL)
def on_open(ctx: CommandContext) -> None:
    _push_model(ctx)


overlay(TRAIL, name="Lasso trail")
overlay(LABELS, name="Segmentation", segment_mib=64)
panel(
    PANEL,
    title="Interactive Segmentation",
    root=w.column(
        "root",
        w.button("pick", "Select region…", command=pick),
        w.row(
            "rsize",
            w.label("lsize", "Minimum size (voxels)"),
            w.slider("size", command=set_min_size, min=1, max=5000, step=10, value=50),
        ),
        w.row(
            "rthresh",
            w.label("lthresh", "Threshold ×"),
            w.slider("thresh", command=set_threshold, min=0.5, max=2.0, step=0.05, value=1.0, float=True),
        ),
        w.checkbox("grow3d", "Grow through Z", command=set_3d, value=True),
        w.dropdown("mode", ["Replace", "Add"], command=set_mode, value="Replace"),
        w.button("commit", "Commit to annotations", command=commit),
        w.row(
            "rmeasure",
            w.dropdown("region", ["(none)"], command=set_region, value="(none)"),
            w.button("measure", "Measure", command=measure),
        ),
        w.image("plot"),
        w.button("reset", "Clear", command=reset),
        w.label("status", "Ready"),
    ),
)


if __name__ == "__main__":
    serve(plugin_id=PLUGIN_ID)
