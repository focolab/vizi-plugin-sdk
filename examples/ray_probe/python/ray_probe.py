#!/usr/bin/env python3
"""Ray probe -- the Python implementation of ``examples/ray_probe/SPEC.md``.

A tool that turns a click in the 3D pane into the brightest voxel along the
segment the host reports, refined at level 0; a numbered list of such picks
with markers and labels; a profile panel; undo and clear as actions fired
into an open gesture; the picks handed over as ellipsoid ROIs.  The
arithmetic -- level maps, the traversal, the pick rules, the plot -- is in
``probe/`` and knows nothing about vizi; what is here is the plugin.
"""

from __future__ import annotations

import math
import sys
import threading
from dataclasses import dataclass, field

import numpy as np

from vizi_plugin_sdk import (
    Column,
    CommandContext,
    HostError,
    InputEvent,
    Ray,
    RayAndPicked,
    SessionEnded,
    ToolContext,
    Voxel,
    action as tool_action,
    command,
    overlay,
    pane as pane_button,
    panel,
    panel_opened,
    pb2,
    plugin,
    serve,
    tool,
    trigger,
    widgets as w,
)
# The column kinds are not on the package's top level; the
# `Column` an event carries names its collapsed axis by these.
from vizi_plugin_sdk.doorbell import KIND_COLUMN_YX, KIND_COLUMN_ZX, KIND_COLUMN_ZY

from probe.ellipsoid import ellipsoid
from probe.geometry import aabb, block_of, clip, column_cells, crop_at_level, level0_of, traverse
from probe.pick import block_max, distances, first_max, sample_indices
from probe.plot import png, render

#: The manifest is declared in code (SPEC's Manifest section): ``--install``
#: installs it, icons included, and ``vizi-plugin.json`` beside this file is
#: what ``--manifest`` writes.
PLUGIN_ID = "org.example.ray_probe"
plugin(PLUGIN_ID, name="Ray probe", version="0.1.0", permissions=["data_access", "contribute_rois"])
PANEL = "probe"
PROFILE = "profile"
TOOL = "probe.point"
PICKS = "probe.picks"
AIM = "probe.aim"

#: SPEC.md §"The tool" -- the words the driver reads are `active`, the numerals
#: and the `count` forms; the rest is this plugin's.
MSG_READY = "Ready"
MSG_ACTIVE = "Probe active — click in a view"
MSG_NOTHING = "Nothing under the pointer"
MSG_NO_CHANNEL = "No channel is visible"
MSG_UNUSABLE = "The record's context could not be reconciled with the pane; nothing picked"
MSG_BUSY = "Busy — a pick is still in flight"
MSG_TOO_LARGE = "Too large — the segment's box exceeds 64 Mi voxels; zoom out"
MSG_CHANGED = "The dataset changed under the click; nothing picked"

READ_CAP = 1 << 26
MARKER_COLOR = (255, 204, 0, 255)
MARKER_SIZE_PX = 8.0
ROI_COLOR = "#ffcc00"
AIM_COLOR = (0, 200, 255, 255)


# --- the model -----------------------------------------------------------------


@dataclass(frozen=True)
class Pick:
    number: int
    position: tuple[float, float, float]  # level-0 lattice coordinate
    value: float
    t: int
    time_s: float | None
    c: int
    level: int
    refined: bool

    def label(self, spacing: tuple[float, float, float] | None, unit: str) -> str:
        z, y, x = self.position
        where = f"({z:g}, {y:g}, {x:g})"
        if spacing is not None:
            where += f" = ({z * spacing[0]:.4g}, {y * spacing[1]:.4g}, {x * spacing[2]:.4g}) {unit}"
        when = f"t={self.t}"
        if self.time_s is not None:
            when += f" ({self.time_s:g} s)"
        return f"Pick {self.number}: {where}, value {self.value:g}, c={self.c}, {when}"


@dataclass
class Model:
    picks: list[Pick] = field(default_factory=list)
    radius: float = 3.0
    refine: bool = True
    status: str = MSG_READY
    pick_text: str = "No pick yet"
    plot: bytes | None = None

    def count_text(self) -> str:
        n = len(self.picks)
        return "No picks" if n == 0 else f"{n} pick{'s' if n != 1 else ''}"


_LOCK = threading.Lock()
_MODEL = Model()
#: One pick at a time (§"Budgets"); actions and commands that touch the list
#: wait for it rather than refuse.
_JOB = threading.Lock()


def _say(ctx, text: str) -> None:
    with _LOCK:
        _MODEL.status = text
    ctx.panel(PANEL).set_value("status", text)


def _push_count(ctx) -> None:
    with _LOCK:
        text = _MODEL.count_text()
    ctx.panel(PANEL).set_value("count", text)


def _push_picks(ctx) -> None:
    """§5 step 2: one marker item with every pick, one text item per pick;
    an empty list clears the overlay."""
    with _LOCK:
        picks = list(_MODEL.picks)
    overlay = ctx.overlay(PICKS)
    if not picks:
        overlay.clear()
        return
    positions = np.array([p.position for p in picks], dtype=np.float64)
    items = [overlay.marker_item("picks", positions, color_rgba=MARKER_COLOR, size_px=MARKER_SIZE_PX, shape="disc", inline=True)]
    for p in picks:
        items.append(overlay.text_item(f"label-{p.number}", str(p.number), p.position, color_rgba=MARKER_COLOR))
    overlay.set_items(items)


def _push_probe_panel(ctx) -> None:
    with _LOCK:
        m = Model(**{k: v for k, v in _MODEL.__dict__.items()})
    panel = ctx.panel(PANEL)
    panel.set_value("radius", f"{m.radius:g}")
    panel.set_value("refine", bool(m.refine))
    panel.set_value("count", m.count_text())
    panel.set_value("status", m.status)


def _push_profile_panel(ctx) -> None:
    with _LOCK:
        text, plot = _MODEL.pick_text, _MODEL.plot
    panel = ctx.panel(PROFILE)
    panel.set_value("pick", text)
    if plot is not None:
        panel.push_image("plot", plot)


# --- the tool ------------------------------------------------------------------


def _segment_level0(ev: InputEvent, scale) -> np.ndarray | None:
    """The aim: the record's segment as two level-0 lattice points, or
    ``None`` when there is nothing to draw (a voxel, a miss, no position)."""
    pos = ev.position
    if pos is None or ev.miss:
        return None
    if isinstance(pos, RayAndPicked):
        pos = pos.ray
    if isinstance(pos, Ray):
        entry = pos.entry
        a = (entry.z, entry.y, entry.x)
        b = (pos.far.z, pos.far.y, pos.far.x)
    elif isinstance(pos, Column):
        free = {KIND_COLUMN_YX: 0, KIND_COLUMN_ZX: 1, KIND_COLUMN_ZY: 2}.get(pos.kind)
        if free is None:
            return None
        fixed = _column_fixed(pos, free)
        a = [fixed.get(k, 0.0) for k in range(3)]
        b = list(a)
        a[free], b[free] = pos.lo, pos.hi - 1.0
    else:
        return None
    return np.array([[level0_of(v, scale[k]) for k, v in enumerate(a)], [level0_of(v, scale[k]) for k, v in enumerate(b)]], dtype=np.float64)


def _column_fixed(pos: Column, free: int) -> dict[int, float]:
    """A `Column`'s two fixed coordinates by axis: `COLUMN_YX` carries
    ``(y, x)``, `COLUMN_ZX` ``(z, x)``, `COLUMN_ZY` ``(z, y)``."""
    fixed_axes = [k for k in range(3) if k != free]
    return {fixed_axes[0]: pos.a, fixed_axes[1]: pos.b}


def _keys_of(binding) -> str:
    """What engages the tool and what fires what, from the ToolBinding the
    host installed -- the honored subset, `""` for a key the host declined."""
    if binding is None:
        return ""
    engages = []
    for trig in binding.triggers:
        if trig.key:
            engages.append(f"{trig.key} {'holds' if trig.hold == pb2.TRIGGER_HOLD_MOMENTARY else 'toggles'}")
        elif trig.button:
            name = {pb2.MOUSE_BUTTON_LEFT: "left", pb2.MOUSE_BUTTON_RIGHT: "right", pb2.MOUSE_BUTTON_MIDDLE: "middle"}.get(trig.button, "?")
            mods = "+".join(m for bit, m in ((1, "Shift"), (2, "Ctrl"), (4, "Alt"), (8, "Meta")) if trig.mods & bit)
            engages.append(f"{mods + '+' if mods else ''}{name} button holds")
    verbs = {"undo": "undoes", "clear": "clears"}
    fires = [f"{a.key} {verbs.get(a.action_id, a.action_id + 's')}" for a in binding.actions if a.key]
    parts = []
    if engages:
        parts.append(", ".join(engages))
    if fires:
        parts.append(", ".join(fires))
    return "; ".join(parts)


@tool(
    TOOL,
    name="Probe",
    views=["2d", "3d"],
    mode="gesture",
    cancel_on_camera_move=False,
    triggers=[trigger(key="Shift+P", hold="toggle"), trigger(button="middle")],
    actions=[
        tool_action("undo", name="Undo pick", key="U", pane=pane_button("icons/undo.svg", tooltip="Undo the last pick")),
        tool_action("clear", name="Clear picks", key="X", pane=pane_button("icons/clear.svg", tooltip="Clear every pick")),
    ],
    pane=pane_button("icons/probe.svg", tooltip="Probe: the brightest voxel under the pointer", hold="toggle"),
)
class Probe:
    def __init__(self) -> None:
        self._aiming: int | None = None  # the open gesture's interaction, while an aim is up
        self._live_failed = False

    # -- engagement (§1) --

    def activated(self, ctx: ToolContext, binding) -> None:
        # The hook is handed the ToolBinding just
        # installed, so the honored triggers and keys are said here, at the
        # moment the tool is engaged.
        keys = _keys_of(binding)
        _say(ctx, f"{MSG_ACTIVE} — {keys}" if keys else MSG_ACTIVE)

    def deactivated(self, ctx: ToolContext, binding) -> None:
        _say(ctx, MSG_READY)

    # -- the aim (§2) --

    def press(self, ctx: ToolContext, ev: InputEvent) -> None:
        self._live_failed = False
        self._aim(ctx, ev)

    def drag(self, ctx: ToolContext, ev: InputEvent) -> None:
        self._aim(ctx, ev)

    def _aim(self, ctx: ToolContext, ev: InputEvent) -> None:
        if not ev.usable:
            return
        pane = ctx.pane(ev)
        if pane is None:
            return
        try:
            scale = pane.level_scale(ev.level)
        except IndexError:
            return
        points = _segment_level0(ev, scale)
        if points is None:
            return
        overlay = ctx.overlay(AIM)
        try:
            overlay.set_items_live(
                [overlay.line_item("aim", points, color_rgba=AIM_COLOR, topology="segments", inline=True)],
                pane=ev,
            )
            self._aiming = ev.interaction
        except SessionEnded:
            return  # the session ended: there is no aim to miss
        except Exception as exc:  # noqa: BLE001 -- once per gesture, on the status line; the pick is the job
            if not self._live_failed:
                self._live_failed = True
                _say(ctx, f"Aim unavailable ({exc}); the pick still runs on release")

    def _clear_aim(self, ctx: ToolContext) -> None:
        if self._aiming is not None:
            self._aiming = None
            ctx.overlay(AIM).clear()

    def cancel(self, ctx: ToolContext, ev) -> None:
        self._clear_aim(ctx)

    # -- the pick (§3) --

    def release(self, ctx: ToolContext, ev: InputEvent) -> None:
        self._clear_aim(ctx)
        if not ev.usable:
            _say(ctx, MSG_UNUSABLE)
            return
        if ev.position is None or ev.miss:
            _say(ctx, MSG_NOTHING)
            return
        pane = ctx.pane(ev)
        visible = pane.visible_channels(ev.channels) if pane is not None else frozenset()
        if not visible:
            _say(ctx, MSG_NO_CHANNEL)
            return
        if not _JOB.acquire(blocking=False):
            _say(ctx, MSG_BUSY)
            return
        with _LOCK:
            refine = _MODEL.refine
        threading.Thread(target=_pick_job, args=(ctx, ev, pane, min(visible), refine), daemon=True, name="probe-pick").start()

    # -- actions (§6) --

    def action(self, ctx: ToolContext, ev: InputEvent) -> None:
        name = ctx.action(ev)
        if name == "undo":
            _undo(ctx)
        elif name == "clear":
            _clear(ctx)
        # None: a FIRE naming an action this manifest does not declare -- a
        # newer host; ignored, not a fault.


def _pick_job(ctx: ToolContext, ev: InputEvent, pane, channel: int, refine: bool) -> None:
    """§3 and §4 on the worker: the set, the read, the guard, the coarse pick,
    the refinement, then §5's publication.  Holds the job lock throughout."""
    try:
        try:
            pick, cells, values, spacing = _pick(ctx, ev, pane, channel, refine)
        except _Refused as exc:
            _say(ctx, str(exc))
            return
        except SessionEnded:
            raise  # to the quiet ending below, not "Read failed"
        except HostError as exc:
            _say(ctx, f"Read failed: {exc}")
            return
        _publish_pick(ctx, pane, pick, cells, values, spacing)
    except SessionEnded:
        pass  # the session ended mid-pick (Shutdown, or the host gone): no news for a closing panel
    except Exception as exc:  # noqa: BLE001 -- the panel is the only channel a worker has
        _say(ctx, f"Failed: {exc!r}")
    finally:
        _JOB.release()


class _Refused(Exception):
    pass


def _cells_of(ev: InputEvent, pane, level: int, scale, shape) -> list:
    """§3: the ordered set ``S`` for the record's position kind, clipped to
    the crop at the record's level and to the level's shape."""
    pos = ev.position
    if isinstance(pos, RayAndPicked):
        pos = pos.ray  # the policy is this plugin's; the host's pick is not compared
    if isinstance(pos, Voxel):
        cells = [(math.floor(pos.z + 0.5), math.floor(pos.y + 0.5), math.floor(pos.x + 0.5))]
    elif isinstance(pos, Column):
        free = {KIND_COLUMN_YX: 0, KIND_COLUMN_ZX: 1, KIND_COLUMN_ZY: 2}.get(pos.kind)
        if free is None:
            raise _Refused(MSG_NOTHING)
        cells = column_cells(free, _column_fixed(pos, free), pos.lo, pos.hi)
    elif isinstance(pos, Ray):
        entry = pos.entry
        cells = traverse((entry.z, entry.y, entry.x), (pos.far.z, pos.far.y, pos.far.x))
    else:
        raise _Refused(MSG_NOTHING)
    c = pane.crop
    ranges = crop_at_level((c.z0, c.z1, c.y0, c.y1, c.x0, c.x1), scale, shape)
    if ranges is None:
        raise _Refused(MSG_NOTHING)
    cells = clip(cells, ranges)
    if not cells:
        raise _Refused(MSG_NOTHING)
    return cells


def _pick(ctx: ToolContext, ev: InputEvent, pane, channel: int, refine: bool):
    level = ev.level
    scale = pane.level_scale(level)
    extent = tuple(hi - lo for lo, hi in pane.extent)
    shape = tuple(max(1, int(extent[a] // scale[a])) for a in range(3))
    cells = _cells_of(ev, pane, level, scale, shape)
    lo, hi = aabb(cells)
    if math.prod(h - l for l, h in zip(lo, hi)) > READ_CAP:
        raise _Refused(MSG_TOO_LARGE)
    host = ctx.host
    box = host.read_volume(ev.t, channel, level, roi=(lo, hi))
    if (int(getattr(box, "open_generation", ev.gen)) & 0xFFFFFFFF) != (ev.gen & 0xFFFFFFFF):
        raise _Refused(MSG_CHANGED)
    values = [float(box[c[0] - lo[0], c[1] - lo[1], c[2] - lo[2]]) for c in cells]
    coarse = first_max(cells, values)
    if coarse is None:
        raise _Refused(MSG_NOTHING)
    index, value = coarse
    cell = cells[index]
    refined = False
    if level > 0 and refine:
        blo, bhi = block_of(cell, scale, extent)
        block = host.read_volume(ev.t, channel, 0, roi=(blo, bhi))
        if (int(getattr(block, "open_generation", ev.gen)) & 0xFFFFFFFF) != (ev.gen & 0xFFFFFFFF):
            raise _Refused(MSG_CHANGED)
        best = block_max(np.asarray(block), blo)
        if best is None:
            raise _Refused(MSG_NOTHING)
        position = tuple(float(v) for v in best[0])
        value = best[1]
        refined = True
    else:
        position = tuple(level0_of(cell[a], scale[a]) for a in range(3))
    spacing = pane.spacing_zyx if pane.any_axis_known else None
    table = pane.time_table
    time_s = float(table[ev.t]) if table is not None and 0 <= ev.t < len(table) else None
    with _LOCK:
        number = len(_MODEL.picks) + 1
    pick = Pick(number, position, value, ev.t, time_s, channel, level, refined)
    return pick, cells, values, spacing


def _publish_pick(ctx: ToolContext, pane, pick: Pick, cells, values, spacing) -> None:
    """§5, in its order: the list and the count, the picks overlay, the
    profile panel, the status."""
    with _LOCK:
        _MODEL.picks.append(pick)
    _push_count(ctx)
    _push_picks(ctx)
    coarse_index = first_max(cells, values)[0]
    xs = distances(cells, spacing)
    shown = sample_indices(len(values))
    if coarse_index not in shown:
        shown = sorted(shown + [coarse_index])
    raster = render(
        [values[i] for i in shown],
        [xs[i] for i in shown],
        pick=shown.index(coarse_index),
        x_name="d" if spacing is None else "d um",
    )
    image = png(raster)
    text = pick.label(spacing, pane.spacing_unit if spacing is not None else "")
    with _LOCK:
        _MODEL.plot = image
        _MODEL.pick_text = text
    profile = ctx.panel(PROFILE)
    try:
        # SPEC §5 step 3: the profile panel may be closed (the SDK holds the
        # image until it opens); a failure to update it is not the pick failing.
        profile.push_image("plot", image)
        profile.set_value("pick", text)
    except Exception as exc:  # noqa: BLE001 -- reported on its own, never as the pick's outcome
        print(f"ray_probe: the profile panel was not updated: {exc!r}", file=sys.stderr)
    _say(ctx, f"Pick {pick.number}: value {pick.value:g} at ({pick.position[0]:g}, {pick.position[1]:g}, {pick.position[2]:g})")


# --- undo, clear, contribute (§6, §"Contributing") -----------------------------


def _undo(ctx) -> None:
    with _JOB:
        with _LOCK:
            if not _MODEL.picks:
                gone = None
            else:
                gone = _MODEL.picks.pop()
        _push_count(ctx)
        _push_picks(ctx)
        _say(ctx, f"Undid pick {gone.number}" if gone else "Nothing to undo")


def _clear(ctx) -> None:
    with _JOB:
        with _LOCK:
            _MODEL.picks.clear()
        _push_count(ctx)
        _push_picks(ctx)
        _say(ctx, "Cleared")


@command("probe.undo", title="Undo pick", menu=None)
def undo(ctx: CommandContext) -> str:
    _undo(ctx)
    return "Undid the last pick"


@command("probe.clear", title="Clear picks", menu=None)
def clear(ctx: CommandContext) -> str:
    _clear(ctx)
    return "Cleared the picks"


@command("probe.set_radius", title="Set ROI radius", menu=None)
def set_radius(ctx: CommandContext) -> str:
    raw = ctx.args.get("value")
    try:
        radius = float(str(raw).strip())
    except (TypeError, ValueError):
        radius = math.nan
    if not (math.isfinite(radius) and radius > 0):
        raise ValueError(f"ROI radius {raw!r} is not a positive number")
    with _LOCK:
        _MODEL.radius = radius
    return f"ROI radius {radius:g} voxels"


@command("probe.set_refine", title="Refine at level 0", menu=None)
def set_refine(ctx: CommandContext) -> str:
    value = ctx.args.get("value")
    if not isinstance(value, bool):
        raise ValueError(f"expected the checkbox's bool, got {ctx.args!r}")
    with _LOCK:
        _MODEL.refine = value
    return f"Refinement at level 0 {'on' if value else 'off'}"


@command("probe.contribute", title="Contribute picks as ROIs", menu="tools")
def contribute(ctx: CommandContext) -> str:
    with _JOB:
        with _LOCK:
            picks, radius = list(_MODEL.picks), _MODEL.radius
        batch = [
            pb2.RoiContribution(
                contrib_id=f"pick-{p.number}",
                roi=ellipsoid(p.position, (radius, radius, radius)),
                name=f"Pick {p.number}",
                style=pb2.RoiStyle(color=ROI_COLOR),
            )
            for p in picks
        ]
        try:
            ctx.host.contribute_rois(batch, replace_all=True)
        except SessionEnded:
            raise  # the session ended: nothing to tell the person
        except HostError as exc:
            _say(ctx, f"Contribute failed: {exc}")
            raise RuntimeError(f"contribute failed: {exc}") from exc
        n = len(batch)
        _say(ctx, f"Contributed {n} ROI{'s' if n != 1 else ''}" if n else "Cleared the contributed ROIs")
        return f"{n} pick{'s' if n != 1 else ''} contributed as ROIs"


@panel_opened(PANEL)
def on_probe_opened(ctx: CommandContext) -> None:
    _push_probe_panel(ctx)


@panel_opened(PROFILE)
def on_profile_opened(ctx: CommandContext) -> None:
    _push_profile_panel(ctx)


overlay(PICKS, name="Picks", segment_mib=4)
overlay(AIM, name="Aim", segment_mib=4)
panel(
    PANEL,
    title="Ray probe",
    root=w.column(
        "root",
        w.label("status", "Ready"),
        w.row("rradius", w.label("lradius", "ROI radius (voxels)"), w.line_edit("radius", command=set_radius, value="3")),
        w.checkbox("refine", "Refine at level 0", command=set_refine, value=True),
        w.row(
            "rbuttons",
            w.button("undo", "Undo", command=undo),
            w.button("clear", "Clear", command=clear),
            w.button("contribute", "Contribute", command=contribute),
        ),
        w.label("count", "No picks"),
    ),
)
panel(PROFILE, title="Ray profile", root=w.column("root", w.image("plot"), w.label("pick", "No pick yet")))


if __name__ == "__main__":
    serve()
