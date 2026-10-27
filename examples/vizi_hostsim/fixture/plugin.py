#!/usr/bin/env python3
"""The emulator's fixture: one of everything the emulator answers.

A reader (the guide's ramp, with `read_plane` and `metadata`), a reader of
files holding several images (the stack), an exporter
(per-volume means to CSV, with abort cleanup), and three commands that make
the plugin-initiated requests the emulator serves: a label-volume push and
compile naming one reservation twice (``uses=2``, the route
`examples/interactive_seg/SPEC.md` §"Committing" describes), then `get_rois`
and `read_mask_voxels`; a native ROI
contribution; and a runtime asset request.  Every result is reported in the
command's returned message so ``selfcheck.py`` can assert on it.
"""

from __future__ import annotations

import json
import os
import threading
import time

import numpy as np

from vizi_plugin_sdk import (
    Appearance,
    Cancelled,
    Chunk,
    ChunkWant,
    CommandContext,
    ExportDescriptor,
    HostError,
    ReaderInfo,
    Refusal,
    SeriesChoice,
    SeriesEntry,
    cancelled,
    appearance,
    report_progress as report_work_progress,
    chunk_source,
    command,
    on_regions_changed,
    on_view_changed,
    exporter,
    follow,
    pb2,
    reader,
    region_shaper,
    roi_pb2,
    serve,
    SettingsDirUnavailableError,
    settings_dir,
    WidgetSize,
    widget_geometry,
)

SHAPE = (3, 2, 4, 8, 8)


@chunk_source("chunks")
def chunks(ctx, want: ChunkWant):
    """§5.7: one (level, t) volume per want -- a 1x2x2 block labeled t+1
    at a t-dependent origin, so the emulator can tell the chunks apart."""
    t = 0 if want.t is None else want.t
    return Chunk(np.full((1, 2, 2), 1 + t, dtype=np.uint8), position_zyx=(0, t, 0))


#: The stack's series: id -> (label, shape).  ``""`` is a real id --
#: presence is the signal, not emptiness.
STACK_SERIES = {"s0": ("first", (1, 1, 2, 4, 4)), "s1": ("second", (2, 1, 2, 4, 4)), "": ("unnamed", (1, 1, 1, 4, 4))}
_stack_instances = 0


@reader("stack")
class StackReader:
    """A reader whose files hold several images, named by the file's stem:
    ``one.stack`` lists a single image (the SDK opens it directly),
    ``many.stack`` lists 70 000 (past the 65 536 repeated-field bound, which
    `SeriesList.entries` is exempt from), ``huge.stack`` more than one message
    carries (the SDK trims and sets `truncated`), ``defect.stack`` answers a
    list even when a series is named, ``dup.stack`` lists one id twice (the
    SDK refuses it), ``named.stack`` is one image of three channels
    the reader names ``"GCaMP"``, ``""`` and, stopping short, nothing;
    anything else holds `STACK_SERIES`.
    `metadata` says which series is open and whether the instance that listed
    the file opened it."""

    def __init__(self, ctx) -> None:
        global _stack_instances
        _stack_instances += 1
        self.instance = _stack_instances
        self.listed = None
        self.by_listing = False

    def open(self, uri: str, series=None):
        stem = os.path.basename(uri).split(".")[0]
        if stem == "one":
            if series is None:
                return SeriesChoice([SeriesEntry("only", "the only one")])
            series, shape = "only", (1, 1, 1, 2, 2)
        elif stem == "many":
            return SeriesChoice([SeriesEntry(f"t{n}") for n in range(70_000)])
        elif stem == "huge":
            return SeriesChoice([SeriesEntry(f"h{n}", description="x" * 100_000) for n in range(400)])
        elif stem == "defect":
            return SeriesChoice([SeriesEntry("a"), SeriesEntry("b")])
        elif stem == "dup":
            return SeriesChoice([SeriesEntry("a"), SeriesEntry("b"), SeriesEntry("a")])
        elif stem == "named":
            self.uri, self.series, self.shape = uri, series, (1, 3, 1, 2, 2)
            return ReaderInfo(shape_tczyx=self.shape, dtype="u8", channel_names=("GCaMP", ""))
        elif series is None:
            self.listed = uri
            return SeriesChoice(
                [SeriesEntry(sid, label, shape_tczyx=shape if sid else None, dtype="u8" if sid else None) for sid, (label, shape) in STACK_SERIES.items()]
            )
        elif series not in STACK_SERIES:
            raise Refusal(pb2.Error.NOT_FOUND, f"{uri} holds no series {series!r}")
        else:
            shape = STACK_SERIES[series][1]
        self.uri, self.series, self.shape = uri, series, shape
        self.by_listing = self.listed == uri
        return ReaderInfo(shape_tczyx=shape, dtype="u8")

    def read_volume(self, t: int, c: int) -> np.ndarray:
        return np.full(self.shape[2:], 1 + t, dtype=np.uint8)

    def metadata(self):
        return ("json", json.dumps({"series": self.series, "by_listing": self.by_listing, "instance": self.instance}))

    def close(self) -> None:
        pass


#: §8.16: how long the long-running shapes run when nobody cancels
#: them -- far past the 10 s budget of an unsupervised command.
LONG_WORK_S = 30.0
_BACKGROUND: dict = {}


@reader("slow")
class SlowReader:
    """A reader whose volume read takes `LONG_WORK_S`, reporting
    progress as it goes and stopping when the host cancels it."""

    def open(self, uri: str) -> ReaderInfo:
        return ReaderInfo(shape_tczyx=(1, 1, 1, 2, 2), dtype="u8")

    def read_volume(self, t: int, c: int) -> np.ndarray:
        deadline = time.monotonic() + LONG_WORK_S
        step = 0
        while time.monotonic() < deadline:
            if cancelled():
                raise Cancelled("the read was canceled")
            step += 1
            report_work_progress(None, f"reading, step {step}")
            time.sleep(0.02)
        return np.zeros((1, 2, 2), dtype=np.uint8)

    def close(self) -> None:
        pass


@command("fixture.long_honours")
def long_honours(ctx: CommandContext) -> str:
    """Long work that reports a fraction and stops when canceled."""
    started = time.monotonic()
    while time.monotonic() - started < LONG_WORK_S:
        if ctx.cancelled:
            raise Cancelled("stopped at the person's request")
        ctx.progress(min(1.0, (time.monotonic() - started) / LONG_WORK_S), "working")
        time.sleep(0.02)
    return "finished"


@command("fixture.long_ignores")
def long_ignores(ctx: CommandContext) -> str:
    """Long work that never looks at the cancel flag -- what vizi
    offers to stop with `Shutdown{USER_STOPPED}`."""
    seconds = float(ctx.args.get("seconds", LONG_WORK_S))
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        ctx.progress(None, "not listening")
        time.sleep(0.02)
    return "finished anyway"


@command("fixture.background")
def background(ctx: CommandContext) -> str:
    """Returns at once, and goes on as a background task that
    reports progress and ends -- "canceled" when the host cancels it, or
    "done" when told to finish (`finish` true) -- without holding the slot."""
    if ctx.args.get("finish"):
        event = _BACKGROUND.get("finish")
        if event is not None:
            event.set()
        return "told to finish"
    task = ctx.start_task("fixture background work")
    finish = threading.Event()
    _BACKGROUND["finish"] = finish

    def run() -> None:
        step = 0
        while not finish.wait(0.02):
            if task.cancelled:
                task.done("canceled")
                return
            step += 1
            task.progress(None, f"step {step}")
        task.done("done")

    threading.Thread(target=run, daemon=True, name="fixture-task").start()
    return f"started {task.id}"


@reader("ramp")
class RampReader:
    def __init__(self, ctx) -> None:
        # The folder for the file this reader is constructed to open.
        self.state_dir = ctx.file_state_dir

    def open(self, uri: str) -> ReaderInfo:
        self.uri = uri
        return ReaderInfo(shape_tczyx=SHAPE, dtype="u16")

    def read_volume(self, t: int, c: int) -> np.ndarray:
        _, _, z, y, x = SHAPE
        return (np.arange(z * y * x, dtype=np.uint16).reshape(z, y, x) + 1000 * t + 100 * c).astype(np.uint16)

    def read_plane(self, t: int, z: int, c: int) -> np.ndarray:
        return np.ascontiguousarray(self.read_volume(t, c)[z : z + 1])

    def metadata(self):
        return ("ome-xml", f"<OME uri={self.uri!r} state={str(self.state_dir) if self.state_dir else ''!r}/>")

    def close(self) -> None:
        pass


@exporter("stats")
class StatsExporter:
    def begin(self, desc: ExportDescriptor) -> None:
        self.path = desc.locator
        # An export to a file named `cancel-me.csv` holds each chunk
        # until the host cancels it, then answers CANCELLED.
        self.wait_for_cancel = os.path.basename(self.path) == "cancel-me.csv"
        self.f = open(self.path, "w", encoding="utf-8")
        self.f.write(f"# {desc.source_metadata_form or 'no-metadata'} {tuple(desc.shape_tczyx)} {desc.dtype} state={desc.file_state_dir or ''}\n")
        # What the artifact's channels are called and whose
        # names those are, and the host's provenance record as it arrived.
        names = ",".join(f"{ch.index}:{ch.name}:{ch.source}" for ch in desc.channels)
        self.f.write(f"# channels={names} provenance={json.dumps(desc.provenance, sort_keys=True)}\n")

    def write(self, t: int, c: int, volume_zyx) -> None:
        if self.wait_for_cancel:
            deadline = time.monotonic() + 10.0
            while not cancelled() and time.monotonic() < deadline:
                time.sleep(0.01)
            raise Cancelled("the chunk was canceled")
        self.f.write(f"{t},{c},{float(np.asarray(volume_zyx).mean()):.3f}\n")

    def finish(self) -> None:
        self.f.close()

    def abort(self) -> None:
        self.f.close()
        os.unlink(self.path)


@command("fixture.compile")
def compile_labels(ctx: CommandContext) -> str:
    overlay = ctx.overlay("labels")
    labels = np.zeros((2, 3, 3), dtype=np.uint16)
    labels[0, :2, :2] = 1  # a 2x2 square on plane 0
    labels[1, 1:, 1:] = 2  # a 2x2 square on plane 1
    labels[1, 0, 0] = 256  # one voxel whose id wraps onto LUT row 1 (§8.8's LUT rule)
    lut = np.zeros((256, 4), dtype=np.uint8)
    lut[1] = (230, 25, 75, 180)
    lut[2] = (60, 180, 75, 180)
    # Keyed by LABEL ID (1.0): 256 shares row 1 with 1 and keeps its own name;
    # a name for 0 is ignored.
    names = {0: "background", 1: "nucleus", 2: "membrane", 256: "wrapped"}

    # One reservation, two messages: the display push and the compile both
    # name these bytes, and only the sender knows that (§5.6, §8.8).
    item = overlay.label_volume_item("seg", labels, lut, position_zyx=(1, 2, 3), label_names=names, uses=2)
    overlay.set_items([item])
    count = ctx.host.compile_label_volume("labels", item)
    records = [r for r in ctx.host.get_rois() if r.origin.startswith("plugin:")]
    first = min(records, key=lambda r: int(r.contrib_id.removeprefix("label:")))
    membership, origin = ctx.host.read_mask_voxels(first.region_id)
    ctx.panel("fixture").set_value("status", f"compiled {count}")
    return (
        f"compiled {count}; rois {[(r.contrib_id, r.name) for r in sorted(records, key=lambda r: r.contrib_id)]}; "
        f"mask {first.contrib_id}: {int(membership.sum())} voxels of {membership.shape} at {tuple(origin)}"
    )


@command("fixture.chunk_misuse")
def chunk_misuse(ctx: CommandContext) -> str:
    """What a conforming plugin never sends to a CHUNKED overlay, so the
    emulator's refusal and drop paths run somewhere: an `OverlayUpdate`
    (refused, site 30), a chunk for a key the host did not name (dropped
    and counted), a compile (`INVALID_ARGUMENT`) -- and, so the table path
    runs too, a well-formed `OverlayLabelTable`."""
    overlay = ctx.overlay("chunks")
    overlay.set_items([overlay.marker_item("stray", np.zeros((1, 3)), inline=True)])
    overlay.publish_chunk(ChunkWant(0, 99), np.ones((1, 1, 1), dtype=np.uint8))
    lut = np.zeros((256, 4), dtype=np.uint8)
    lut[1] = (255, 0, 0, 255)
    overlay.set_label_table(lut, label_names={1: "one", 2: "two"}, label_visible={2: False}, opacity=0.5)
    labels = np.ones((1, 1, 1), dtype=np.uint8)
    item = ctx.overlay("labels").label_volume_item("seg", labels, lut)
    try:
        ctx.host.compile_label_volume("chunks", item)
    except HostError as exc:
        return f"compile refused: {exc}"
    return "compile of a chunked overlay was answered"


@command("fixture.reads")
def growing_reads(ctx: CommandContext) -> str:
    """`ReadDataVolume`s that outgrow the host's `V`: a one-voxel ROI first,
    so the host sizes the ring to it, then two whole volumes -- the second
    read forces a larger ring superseding the first.  Values are reported so
    the driver checks the bytes arrived through each ring."""
    small = ctx.host.read_volume(1, 0, roi=((0, 0, 0), (1, 1, 1)))
    whole = ctx.host.read_volume(1, 0)
    again = ctx.host.read_volume(2, 1)
    return f"{int(small.sum())} {int(whole.sum())} {int(again.sum())}"


@command("fixture.reads_together")
def concurrent_reads(ctx: CommandContext) -> str:
    """Four whole-volume reads in flight at once, as a prefetching plugin
    makes them: replies into a ring whose installation has already ridden an
    earlier reply arrive BARE, and a ring can grow while deposits in the old
    one are still lent."""
    import threading

    sums: dict[tuple[int, int], int] = {}
    errors: list[str] = []

    def one(t: int, c: int) -> None:
        try:
            sums[(t, c)] = int(ctx.host.read_volume(t, c).sum())
        except Exception as exc:  # noqa: BLE001 -- reported, not raised
            errors.append(f"({t},{c}): {exc}")

    threads = [threading.Thread(target=one, args=(t, c)) for t in range(2) for c in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise RuntimeError("; ".join(errors))
    return " ".join(f"{t}{c}:{sums[(t, c)]}" for t, c in sorted(sums))


@command("fixture.folders")
def folders(ctx: CommandContext) -> str:
    """The settings folder (module-level, as a reader or exporter
    would reach it) and the open dataset's per-file folder -- neither of
    which the SDK may create."""
    try:
        settings = str(settings_dir())
    except SettingsDirUnavailableError:
        settings = "unavailable"
    state = ctx.host.view_state().file_state_dir
    return f"settings={settings} state={state if state is not None else 'None'}"


@command("fixture.needs_value")
def needs_value(ctx: CommandContext) -> str:
    """An argument check: ``value`` must be a number.  A refusal raises, and
    the `CommandDone` that reports it still names this command (§8.3)."""
    value = ctx.args.get("value")
    if value is None:
        raise ValueError("value: missing")
    if not isinstance(value, float) or isinstance(value, bool):
        raise ValueError(f"value: expected a number, got {type(value).__name__}")
    return f"value={value:g}"


# `fixture.unhandled` is declared in vizi-plugin.json and deliberately has no
# @command here: a declared command with no handler is answered
# CommandDone{ok=false} naming it, in every SDK.


@command("fixture.series")
def which_series(ctx: CommandContext) -> str:
    """Which image of the open file the host says is open."""
    view = ctx.host.view_state()
    names = ",".join(f"{ch.name}:{ch.source}" for ch in view.channels)
    return f"series={view.dataset_series!r} channels={names}"


@command("fixture.rois")
def contribute_box(ctx: CommandContext) -> str:
    box = roi_pb2.Roi(type_name="box")
    ctx.host.contribute_rois(
        [pb2.RoiContribution(contrib_id="box", roi=box, name="A box", style=pb2.RoiStyle(color="#123456"))]
    )
    rows = ctx.host.get_rois()
    return f"{len(rows)} rois after the box"


#: A STAR region whose root (its center) is a handle -- four
#: enclosing corners about it, ZYX.
BLOB_CORNERS = np.array([[10, 10, 10], [10, 30, 12], [28, 18, 14], [16, 20, 32]], dtype=np.float64)
_IDENTITY = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


@command("fixture.blob")
def contribute_blob(ctx: CommandContext) -> str:
    def vertex(p, **kw):
        return roi_pb2.Vertex(position=roi_pb2.Vec3(z=float(p[0]), y=float(p[1]), x=float(p[2])), **kw)

    centre = vertex(BLOB_CORNERS.mean(axis=0), handle=roi_pb2.HandleSpec(frame=_IDENTITY, mode=roi_pb2.ALL, dof_mask=7))
    blob = roi_pb2.Roi(type_name="fixture.blob", root=4, auto_surface=roi_pb2.AUTO_SURFACE_STAR,
                       vertices=[vertex(p, enclosing=True) for p in BLOB_CORNERS] + [centre])
    ctx.host.contribute_rois([pb2.RoiContribution(contrib_id="blob", roi=blob, name="A blob")])
    return "blob contributed"


@region_shaper("fixture.blob", name="Blob")
def shape_blob(ctx, edit):
    """The handle goes where it is dragged, nothing else moves --
    so a center dragged out of its corners leaves the region no inside,
    which the host refuses."""
    out = np.asarray(edit.positions, dtype=np.float64).copy()
    out[int(edit.handle)] = np.asarray(edit.p1, dtype=np.float64)
    return out


@command("fixture.raw_read")
def raw_read(ctx: CommandContext) -> str:
    """The same volume read as shared and as `raw`, and the
    pitch per axis as `ViewState` carries it, for the self-check."""
    shared = ctx.host.read_volume(0, 0)
    raw = ctx.host.read_volume(0, 0, raw=True)
    view = ctx.host.view_state()
    return json.dumps({
        "shared": [float(np.asarray(shared).sum()), None if shared.filter is None else shared.filter.stage_id],
        "raw": [float(np.asarray(raw).sum()), None if raw.filter is None else raw.filter.stage_id],
        "view_filter": None if view.filter is None else view.filter.stage_id,
        "known": list(view.axis_known_zyx),
        "measured": list(view.measured_spacing_zyx),
        "unit": view.spacing_unit,
    })


@command("fixture.progress")
def report_progress(ctx: CommandContext) -> str:
    """The progress widget.  Determinate steps, then the
    indeterminate state, which outranks any value; the host never echoes it,
    so nothing comes back and there is no binding to answer."""
    panel = ctx.panel("fixture")
    for step in (0.0, 0.5, 1.0):
        panel.set_progress("bar", step)
    panel.set_progress("bar", 0.25, indeterminate=True)
    return "progress: 0, .5, 1, then indeterminate"


@command("fixture.track")
def extend_a_track(ctx: CommandContext) -> str:
    """Contribute a posed region, then GROW it with the
    delta form instead of re-sending the table, read the window back, and
    show that a gap is refused."""
    # The dataset has T=3 frames, and every frame of a pose
    # lies inside [0, T): contribute frame 0, show a gap refused while there
    # is still room for one, then grow to frames 1..2.
    posed = roi_pb2.Roi(type_name="point", t_start=0, t_end=0)
    posed.pose_translation.extend([0.0, 0.0, 0.0])
    ctx.host.contribute_rois([pb2.RoiContribution(contrib_id="track", roi=posed, name="A track")])
    try:
        ctx.host.extend_roi_poses(
            [pb2.RoiPoseExtension(contrib_id="track", t_start=2, span=1, pose_translation=[9.0] * 3)]
        )
        gap = "a gap was ACCEPTED"
    except HostError as exc:
        gap = f"gap refused ({exc})"
    grown, frames = ctx.host.extend_roi_poses(
        [pb2.RoiPoseExtension(contrib_id="track", t_start=1, span=2, pose_translation=[1.0] * 3 + [2.0] * 3)]
    )
    window = [r for r in ctx.host.get_rois(pose_t_start=1, pose_t_end=2) if r.contrib_id == "track"][0]
    rows = list(window.roi.pose_translation)
    domain = (window.pose_domain_t_start, window.pose_domain_t_end)
    # Without a window the rows are not in the reply at all: the host returns
    # each region posed at the frame on screen with its pose CLEARED,
    # which makes the window the only way to read a pose back.
    bare = [r for r in ctx.host.get_rois() if r.contrib_id == "track"][0]
    # The domain pair is on EVERY
    # posed record, so a slice is told by comparing it with the roi's own
    # span, never by asking whether the pair is there.  The UNWINDOWED record
    # is posed at the frame on screen, which bakes the time in: it carries no
    # rows and NO t_start/t_end at all, while
    # its domain pair still says where the pose is.
    cleared = len(bare.roi.pose_translation) == 0
    timeless = not bare.roi.HasField("t_start") and not bare.roi.HasField("t_end")
    carried = (bare.pose_domain_t_start, bare.pose_domain_t_end) == domain
    sliced = (window.pose_domain_t_start, window.pose_domain_t_end) != (window.roi.t_start, window.roi.t_end)
    # A window that misses the domain returns the shape timeless; an inverted one is refused.
    missed = [r for r in ctx.host.get_rois(pose_t_start=10, pose_t_end=12) if r.contrib_id == "track"][0]
    miss_timeless = not missed.roi.HasField("t_start") and len(missed.roi.pose_translation) == 0
    try:
        ctx.host.get_rois(pose_t_start=2, pose_t_end=1)
        inverted = "an inverted window was ANSWERED"
    except HostError as exc:
        inverted = f"inverted window refused ({pb2.Error.Kind.Name(exc.kind)})"
    # The host's batch-shape refusals: one region twice in a batch, an empty
    # batch, an extension carrying no component.
    shape = []
    for label, batch in (
        ("twice", [pb2.RoiPoseExtension(contrib_id="track", t_start=2, span=1, pose_translation=[4.0] * 3)] * 2),
        ("empty", []),
        ("no component", [pb2.RoiPoseExtension(contrib_id="track", t_start=2, span=1)]),
    ):
        try:
            ctx.host.extend_roi_poses(batch)
            shape.append(f"{label} ACCEPTED")
        except HostError as exc:
            shape.append(f"{label} {pb2.Error.Kind.Name(exc.kind)}")
    return (f"extended {grown} region over {frames} frames; window {rows} of domain {domain}; "
            f"bare cleared={cleared} timeless={timeless} domain carried={carried}; window reads as a slice={sliced}; "
            f"missed window timeless={miss_timeless}; {inverted}; {gap}; batch refusals: {', '.join(shape)}")


@command("fixture.inclusion")
def roi_inclusion(ctx: CommandContext) -> str:
    """Ask the host which voxels a region covers at a frame,
    instead of re-implementing its rasterizer from the shape, the inclusion
    mode, the pose row and the parent chain."""
    track = [r for r in ctx.host.get_rois() if r.contrib_id == "track"]
    if not track:
        return "no track to measure"
    region = track[0].region_id
    at2 = ctx.host.read_roi_inclusion(region, 2)
    membership, origin = at2
    # A t past the DATASET (T=3) is refused OUT_OF_BOUNDS -- not "absent".
    try:
        ctx.host.read_roi_inclusion(region, 99)
        past = "t=99 ANSWERED"
    except HostError as exc:
        past = f"t=99 refused {pb2.Error.Kind.Name(exc.kind)}"
    # Past the POSE domain but inside the dataset the region is HELD at its
    # end row -- HOLD is the wire default -- and only a HIDE region is absent.
    # A region clipped away entirely is present and covers nothing.
    held = roi_pb2.Roi(type_name="point", t_start=0, t_end=0)
    held.pose_translation.extend([1.0, 1.0, 1.0])
    hidden = roi_pb2.Roi(type_name="point", t_start=0, t_end=0, out_of_span=roi_pb2.OutOfSpan.Value("HIDE"))
    hidden.pose_translation.extend([1.0, 1.0, 1.0])
    away = roi_pb2.Roi(type_name="point", t_start=0, t_end=2)
    away.pose_translation.extend([50.0] * 9)
    ctx.host.contribute_rois([
        pb2.RoiContribution(contrib_id="held", roi=held, name="Held"),
        pb2.RoiContribution(contrib_id="hidden", roi=hidden, name="Hidden"),
        pb2.RoiContribution(contrib_id="away", roi=away, name="Away"),
    ])
    # A windowed read lists every region in its rest form.  Unwindowed, the
    # HIDE region is absent at the frame on screen (t=1) and is not listed at
    # all -- the host poses each record at that frame.
    ids = {r.contrib_id: r.region_id for r in ctx.host.get_rois(pose_t_start=0)}
    listed = "hidden" in {r.contrib_id for r in ctx.host.get_rois()}
    held_at2 = ctx.host.read_roi_inclusion(ids["held"], 2)
    hidden_at2 = ctx.host.read_roi_inclusion(ids["hidden"], 2)
    away_at1 = ctx.host.read_roi_inclusion(ids["away"], 1)
    held_text = "absent" if held_at2 is None else f"present voxels={int(held_at2[0].sum())} origin={tuple(held_at2[1])}"
    away_text = "absent" if away_at1 is None else f"present shape={tuple(away_at1[0].shape)}"
    return (f"t=2 present=True voxels={int(membership.sum())} origin={tuple(origin)}; {past}; "
            f"held past its domain: {held_text}; hidden past its domain: {'absent' if hidden_at2 is None else 'PRESENT'}; "
            f"clipped away: {away_text}; hidden listed unwindowed at t=1: {listed}")


@command("fixture.inclusion_box")
def inclusion_box(ctx: CommandContext) -> str:
    """The clip box: `read_roi_inclusion(region, t, roi=...)`
    clipped to a level-0 box with `read_volume`'s semantics, and the box's
    refusals.  Measured on mask label 1 (four voxels of (1, 2, 2) at
    (1, 2, 3), from `fixture.compile`)."""
    region = [r for r in ctx.host.get_rois() if r.origin.startswith("plugin:") and r.contrib_id == "label:1"][0].region_id
    out = []
    clipped = ctx.host.read_roi_inclusion(region, 0, roi=((1, 2, 3), (2, 3, 5)))
    out.append(f"clipped: {int(clipped[0].sum())} voxels of {tuple(clipped[0].shape)} at {tuple(clipped[1])}")
    missed = ctx.host.read_roi_inclusion(region, 0, roi=((0, 0, 0), (1, 1, 1)))
    out.append(f"missed: present shape={tuple(missed[0].shape)}")
    for label, roi in (
        ("empty", ((1, 2, 3), (1, 3, 5))),
        ("past the dataset", ((0, 0, 0), (5, 8, 8))),
        ("malformed", ((1, 2), (2, 3))),
    ):
        try:
            ctx.host.read_roi_inclusion(region, 0, roi=roi)
            out.append(f"{label} ANSWERED")
        except HostError as exc:
            out.append(f"{label} {pb2.Error.Kind.Name(exc.kind)}")
    return "; ".join(out)


@command("fixture.inclusion_budget")
def inclusion_budget(ctx: CommandContext) -> str:
    """A region whose clipped box is past the host's voxel budget
    is refused `OUT_OF_BOUNDS` (the driver lowers the budget first)."""
    region = [r for r in ctx.host.get_rois() if r.origin.startswith("plugin:") and r.contrib_id == "label:1"][0].region_id
    try:
        ctx.host.read_roi_inclusion(region, 0)
        return "over budget ANSWERED"
    except HostError as exc:
        return f"over budget {pb2.Error.Kind.Name(exc.kind)}"


@command("fixture.roi_refusals")
def roi_refusals(ctx: CommandContext) -> str:
    """§8.8: non-finite values refused at the wire, in
    `ContributeRois` and `ExtendRoiPoses` alike, and a pose domain outside
    [0, T) (T=3 here) -- each `INVALID_ARGUMENT`, and nothing stored."""
    nan, inf = float("nan"), float("inf")

    def point(**fields) -> roi_pb2.Roi:
        roi = roi_pb2.Roi(type_name="point")
        roi.vertices.add().position.CopyFrom(roi_pb2.Vec3(z=0.0, y=0.0, x=0.0))
        for key, value in fields.items():
            if key == "position":
                roi.vertices[0].position.CopyFrom(roi_pb2.Vec3(z=value, y=0.0, x=0.0))
            elif key == "direction":
                roi.vertices[0].direction.CopyFrom(roi_pb2.Vec3(z=value, y=0.0, x=1.0))
            elif key in ("t_start", "t_end"):
                setattr(roi, key, value)
            else:
                getattr(roi, key).extend(value)
        return roi

    out = []
    for label, roi in (
        ("nan position", point(position=nan)),
        ("inf direction", point(direction=inf)),
        ("nan pose row", point(t_start=0, t_end=0, pose_translation=[0.0, nan, 0.0])),
        ("inf frame basis", point(frame_basis=[1.0, 0, 0, 0, 1.0, 0, 0, 0, inf])),
        ("domain past T", point(t_start=1, t_end=3, pose_translation=[0.0] * 9)),
    ):
        try:
            ctx.host.contribute_rois([pb2.RoiContribution(contrib_id="bad", roi=roi, name="Bad")])
            out.append(f"{label} ACCEPTED")
        except HostError as exc:
            out.append(f"{label} {pb2.Error.Kind.Name(exc.kind)}")
    try:  # a compiled label's id: the prefix is CompileLabelVolume's
        ctx.host.contribute_rois([pb2.RoiContribution(contrib_id="label:7", roi=point(), name="Bad")])
        out.append("label prefix ACCEPTED")
    except HostError as exc:
        out.append(f"label prefix {pb2.Error.Kind.Name(exc.kind)}")
    for label, ext in (
        ("extension nan", pb2.RoiPoseExtension(contrib_id="track", t_start=2, span=1, pose_translation=[nan, 0.0, 0.0])),
        ("extension past T", pb2.RoiPoseExtension(contrib_id="track", t_start=2, span=2, pose_translation=[0.0] * 6)),
    ):
        try:
            ctx.host.extend_roi_poses([ext])
            out.append(f"{label} ACCEPTED")
        except HostError as exc:
            out.append(f"{label} {pb2.Error.Kind.Name(exc.kind)}")
    stored = "bad" in {r.contrib_id for r in ctx.host.get_rois()}
    return "; ".join(out) + f"; stored={stored}"


@command("fixture.hang_up")
def hang_up(ctx: CommandContext) -> str:
    """Doorbell §10's `peer_closed` row: hang up the doorbell
    with no fault -- shut down this end of the inherited socket -- so the
    host sees a plain EOF.  The host opens the grace window as
    `DoorbellShutdown{peer_closed}` and ends the session after it; the SDK
    waits for that `Shutdown` rather than ending the session itself."""
    import socket

    dup = socket.socket(fileno=os.dup(int(os.environ["VIZI_PLUGIN_DOORBELL_FD"])))
    try:
        dup.shutdown(socket.SHUT_RDWR)
    finally:
        dup.close()
    return "hung up"


@command("fixture.activate")
def activate_undeclared(ctx: CommandContext) -> str:
    """`ActivateTool` for a tool this plugin does not declare is refused
    `INVALID_ARGUMENT` by the host -- the fixture declares no tool at all."""
    try:
        ctx.host.activate_tool("fixture.no_such_tool")
    except HostError as exc:
        return f"refused {pb2.Error.Kind.Name(exc.kind)}: {exc}"
    return "an undeclared tool was ACTIVATED"


@command("fixture.bad_patches")
def bad_patches(ctx: CommandContext) -> str:
    """One `PanelPatch` of three ops the host refuses op by op on
    `panel_patch_refused` -- an undeclared widget, progress on
    a label, a value on a progress bar -- beside one it applies.  One
    message, because a panel patch coalesces and separate ones may not all
    arrive."""
    ctx.panel("fixture")._patch(
        pb2.PatchOp(widget_id="nope", set_value=pb2.Value(s="x")),
        pb2.PatchOp(widget_id="status", set_progress=pb2.Progress(value=0.5)),
        pb2.PatchOp(widget_id="bar", set_value=pb2.Value(i=3)),
        pb2.PatchOp(widget_id="picked", set_value=pb2.Value(s="applied")),
    )
    return "three bad ops and one good"


@command("fixture.read_status")
def read_status(ctx: CommandContext) -> str:
    """The host's ledger holds EVERY request it has
    taken in and not answered, so a read the host is sitting on is PENDING
    (not awaiting a user).  The read runs in the background; a watcher asks
    about it while it is outstanding and reports on the panel."""
    import threading
    import time as _time

    host = ctx.host
    panel = ctx.panel("fixture")

    def watch() -> None:
        for _ in range(500):
            outstanding = list(getattr(host, "_waiters", {}))
            if outstanding:
                status = host.request_status(outstanding[0])
                panel.set_value("status", f"read pending={status.pending} awaiting_user={status.awaiting_user}")
                return
            _time.sleep(0.01)
        panel.set_value("status", "no read to ask about")

    def read() -> None:
        try:
            host.read_volume(0, 0)
        except HostError:
            pass

    threading.Thread(target=read, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    return "reading in the background"


@command("fixture.big_window")
def big_window(ctx: CommandContext) -> str:
    """An oversized windowed `GetRois` is REFUSED, never
    clipped -- a clipped window is indistinguishable from a small one."""
    try:
        ctx.host.get_rois(pose_t_start=0, pose_t_end=4_000_000)
    except HostError as exc:
        return f"refused: {exc}"
    return "an oversized window was answered"


@command("fixture.picker_status")
def picker_status(ctx: CommandContext) -> str:
    """A plugin waiting on a pick can ask whether the delay
    is a person.

    The picker runs on a BACKGROUND THREAD, not in this handler -- which is
    what `docs/contributions/commands.md` tells authors to do, and what at
    least one SDK enforces outright (a picker on a serialized command worker
    queues every later command behind whoever is browsing).  So this command
    returns at once and the pick continues behind it.

    The held command deadline is NOT exercised here,
    because with the picker off the handler nothing is waiting on it.  A
    driver that wants to see the hold has to put the picker where this
    deliberately does not.
    """
    import threading
    import time as _time

    host = ctx.host
    panel = ctx.panel("fixture")

    def work() -> None:
        # The request id is the SDK's and it does not hand it out;
        # `_waiters` is the only place it exists.
        def watch() -> None:
            for _ in range(500):
                outstanding = [i for i in getattr(host, "_waiters", {})]
                if outstanding:
                    status = host.request_status(outstanding[0])
                    panel.set_value("status", f"pending={status.pending} awaiting_user={status.awaiting_user}")
                    return
                _time.sleep(0.01)
            panel.set_value("status", "no request to ask about")

        threading.Thread(target=watch, daemon=True).start()
        try:
            path = host.request_asset("weights", timeout=600)
            panel.set_value("picked", "yes" if path else "no")
        except HostError as exc:
            panel.set_value("picked", f"failed: {exc}")

    threading.Thread(target=work, daemon=True).start()
    return "picking in the background"


@command("fixture.asset")
def request_asset(ctx: CommandContext) -> str:
    try:
        path = ctx.host.request_asset("weights")
    except HostError as exc:
        return f"refused: {pb2.Error.Kind.Name(exc.kind)} {exc}"
    return "declined" if path is None else f"asset at {path}"


# -- the subscribed pushes, the save dialog, colormaps, pickers

_SEEN: dict[str, list] = {"views": [], "regions": []}


@on_view_changed
def _viewed(ctx, change) -> None:
    _SEEN["views"].append(change)


@on_regions_changed
def _regioned(ctx, change) -> None:
    _SEEN["regions"].append(change)


@command("fixture.subs")
def subs(ctx: CommandContext) -> str:
    """What the two handlers saw, as JSON: every view change, and every
    regions call with its changes."""
    views = [[v.open_generation, v.t, v.z, list(v.visible_channels), v.selected_region_id] for v in _SEEN["views"]]
    regions = [[r.reset, [[c.region_id, c.revision, c.origin, c.deleted] for c in r.changes]] for r in _SEEN["regions"]]
    return json.dumps({"views": views, "regions": regions})


@command("fixture.pick")
def pick(ctx: CommandContext) -> str:
    return json.dumps(ctx.args)


@command("fixture.save_path")
def save_path(ctx: CommandContext) -> str:
    try:
        path = ctx.host.request_save_path(ctx.args.get("name", "results"), extension="csv", title="Save results")
    except HostError as exc:
        return f"refused: {pb2.Error.Kind.Name(exc.kind)} {exc}"
    return "declined" if path is None else f"path {path}"


@command("fixture.colormap")
def colormap(ctx: CommandContext) -> str:
    try:
        table = ctx.host.get_colormap(ctx.args.get("name", ""))
    except HostError as exc:
        return f"refused: {pb2.Error.Kind.Name(exc.kind)} {exc}"
    return json.dumps({"shape": list(table.shape), "dtype": str(table.dtype), "mid": table[128].tolist()})


@command("fixture.rois_by_id")
def rois_by_id(ctx: CommandContext) -> str:
    ids = [i for i in ctx.args.get("ids", "").split(",") if i]
    records = ctx.host.get_rois(region_ids=ids)
    return json.dumps([[r.region_id, r.revision, r.content_hash] for r in records])


@command("fixture.pickers_set")
def pickers_set(ctx: CommandContext) -> str:
    panel = ctx.panel("fixture")
    panel.set_value("ch", [0, 1])
    panel.set_value("col", "#00FF00")
    panel.set_value("cm", "viridis")
    panel.set_value("roi", ctx.args.get("roi", ""))
    return "set"


@command("fixture.pickers_bad")
def pickers_bad(ctx: CommandContext) -> str:
    """Values the host's pickers do not hold -- a single channel on a
    multiple picker, a channel the dataset lacks, an unknown colormap, an
    unknown region -- each refused op by op (raw, so no SDK check intervenes)."""
    if ctx.args.get("scalar"):
        # Its own command: ops coalesce by widget, so this and the list below
        # in one would leave one.
        ctx.panel("fixture")._patch(pb2.PatchOp(widget_id="ch", set_value=pb2.Value(i=0)))
        return "a single channel on a multiple picker"
    ctx.panel("fixture")._patch(
        pb2.PatchOp(widget_id="ch", set_value=pb2.Value(ints=pb2.IntList(values=[0, 9]))),
        pb2.PatchOp(widget_id="cm", set_value=pb2.Value(s="nonesuch")),
        pb2.PatchOp(widget_id="roi", set_value=pb2.Value(s="nonesuch")),
    )
    return "three bad picker values"


# -- the list widget, and a panel's state kept until it opens


@command("fixture.list_ops")
def list_ops(ctx: CommandContext) -> str:
    """Rows, row-addressed cells and a selection."""
    panel = ctx.panel("fixture")
    panel.set_rows("results", ["a", "b", "c"])
    panel.set_value("rname", "Alpha", row="a")
    panel.set_value("rshow", True, row="b")
    panel.set_value("results", "b")
    return "rows a b c; cells set; b selected"


@command("fixture.list_bad")
def list_bad(ctx: CommandContext) -> str:
    """In one raw patch (a separate command, since ops coalesce per (widget,
    property, row) and a bad set_rows would replace the good one), the four
    refusals the host owes: a cell op with no row, a row_key on a widget that
    is not a cell, a row the list does not hold, and more rows than max_rows."""
    ctx.panel("fixture")._patch(
        # `rshow`, not `rname`: the reference SDK coalesces a patch's ops by
        # (panel, widget) without the row, so a second `rname`
        # op below would swallow this one before it left.
        pb2.PatchOp(widget_id="rshow", set_value=pb2.Value(b=False)),
        pb2.PatchOp(widget_id="status", set_value=pb2.Value(s="x"), row_key="a"),
        pb2.PatchOp(widget_id="rname", set_value=pb2.Value(s="ghost"), row_key="zzz"),
        pb2.PatchOp(widget_id="results", set_rows=pb2.RowKeys(keys=["1", "2", "3", "4", "5"])),
    )
    return "four bad ops"


@command("fixture.late_patch")
def late_patch(ctx: CommandContext) -> str:
    """Patches to the 'late' panel, which the host has not opened: a value
    (kept and shown at open), twice so the newer wins, and a widget the panel
    does not declare (refused)."""
    panel = ctx.panel("late")
    panel.set_value("lbl", "first")
    panel.set_value("lbl", "kept")
    panel._patch(pb2.PatchOp(widget_id="nope", set_value=pb2.Value(s="x")))
    return "patched late before it opened"


# -- set-view (§8.15)


@command("fixture.jump")
def jump(ctx: CommandContext) -> str:
    """Answer this interaction's cause as ``how`` says, reporting each
    result as [outcome, reason]."""
    import time as _time

    def r(res):
        return [res.outcome, res.reason]

    how = ctx.args.get("how", "move")
    if how == "move":
        got = r(ctx.move_view(t=int(ctx.args.get("t", 0))))
    elif how == "decline":
        got = r(ctx.decline_view())
    elif how == "undeclared":
        got = r(ctx.move_view(look_at=(1.0, 2.0, 3.0)))
    elif how == "both":
        # Framing a region selects it, so a different select_region
        # beside it is refused INVALID.
        got = r(ctx.move_view(select_region=str(ctx.args.get("a", "")), frame_region=str(ctx.args.get("b", ""))))
    elif how == "region":
        got = r(ctx.move_view(select_region=str(ctx.args.get("id", ""))))
    elif how == "twice":
        got = [r(ctx.move_view(t=1)), r(ctx.move_view(t=1)), r(ctx.move_view(t=2))]
    elif how == "wait":
        _time.sleep(float(ctx.args.get("wait", 0.6)))
        got = r(ctx.move_view(t=int(ctx.args.get("t", 1))))
    else:
        return f"unknown how {how!r}"
    return json.dumps({"cause": ctx.cause, "got": got})


# -- Follow (§8.15) -----------------------------------------------------------

_FOLLOW = {"mode": "hold"}
_FOLLOW_LOG: list[dict] = []


@command("fixture.follow_mode")
def follow_mode(ctx: CommandContext) -> str:
    """How the next Follow from ``tour`` drives; ``offer`` (if given) is
    offered, or withdrawn when ``""``."""
    _FOLLOW["mode"] = str(ctx.args.get("mode", _FOLLOW["mode"]))
    if "offer" in ctx.args:
        label = str(ctx.args["offer"])
        if label:
            ctx.panel("tour").offer_follow(label)
        else:
            ctx.panel("tour").withdraw_follow()
    return _FOLLOW["mode"]


@command("fixture.follow_log")
def follow_log(ctx: CommandContext) -> str:
    return json.dumps(_FOLLOW_LOG)


@follow("tour")
def tour(ctx, f) -> None:
    """``hold``: t=1, then wait for the host to end it.  ``steps``: t 0..2,
    then return (the SDK ends it).  ``bad``: t=99, which the host cannot
    apply.  ``undeclared``: a target the Follow does not drive, refused here."""
    mode = _FOLLOW["mode"]
    entry = {"id": f.id, "targets": sorted(f.targets), "resumed": f.resumed, "mode": mode, "moves": []}
    _FOLLOW_LOG.append(entry)
    if mode == "steps":
        for t in range(3):
            entry["moves"].append(f.move(t=t))
            if not f.wait(0.06):
                break
        return
    if mode == "undeclared":
        try:
            f.move(select_region="r1")
        except ValueError as exc:
            entry["refused"] = str(exc)
        f.end()
    elif mode == "bad":
        entry["moves"].append(f.move(t=99))
        f.wait(5.0)
    else:
        entry["moves"].append(f.move(t=1))
        f.wait(30.0)
    entry["reason"], entry["detail"], entry["active"] = f.reason, f.detail, f.active


# -- a reservation held across its segment's retirement (§9.1) -------------------

_PARKED: dict = {}


@command("fixture.reserve_parked")
def reserve_parked(ctx: CommandContext) -> str:
    """Build a loaned item (reserving in the `labels` arena) and park it: its
    push goes out only on ``fixture.release_parked``.  Until then the
    reservation is in flight, and a retirement of that arena must wait."""
    overlay = ctx.overlay("labels")
    item = overlay.marker_item("parked", np.zeros((1, 3)), inline=False)
    _PARKED.clear()
    _PARKED.update(overlay=overlay, item=item)
    return "reserved"


@command("fixture.release_parked")
def release_parked(ctx: CommandContext) -> str:
    """Send the parked item, then push one more into whatever arena is
    current (the successor, after a supersede)."""
    overlay, item = _PARKED["overlay"], _PARKED["item"]
    results = []
    for label, build in (("parked", lambda: item), ("fresh", lambda: overlay.marker_item("fresh", np.zeros((1, 3)), inline=False))):
        try:
            overlay.set_items([build()])
            results.append([label, "sent"])
        except Exception as exc:  # noqa: BLE001 -- reported to the driver, which judges it
            results.append([label, repr(exc)])
    return json.dumps(results)


@command("fixture.forge_cause")
def forge_cause(ctx: CommandContext) -> str:
    res = ctx.host.request_view(cause=424242, t=1)
    return json.dumps([res.outcome, res.reason])


# -- the table widget, and an image's geometry (§8.4) ------------------------------


@command("fixture.table_fill")
def table_fill(ctx: CommandContext) -> str:
    """The `grid` panel's `stats` table through the SDK: rows whole --
    positional cells and cells by column id, a blank (``None``) and a missing
    trailing cell -- then a keyed patch that deletes one row, replaces one in
    place and appends one, then a selection of two rows."""
    panel = ctx.panel("grid")
    panel.set_table(
        "stats",
        [
            ("a", ["Alpha", 12.5, True]),
            ("b", {"name": "beta", "area": None, "kept": False}),
            ("c", ["Gamma", 3]),
            ("d", ["delta", 100.0, True]),
        ],
    )
    panel.patch_table("stats", delete=["d"], upsert=[("b", ["Beta", 7.25, False]), ("e", ["Epsilon", None, None])])
    panel.set_value("stats", ["a", "e"])
    return "4 rows; d deleted, b replaced, e appended; a and e selected"


@command("fixture.table_bad")
def table_bad(ctx: CommandContext) -> str:
    """Table ops the host refuses whole -- ``which`` picks one, raw, since
    ops for one widget coalesce and a later table op would merge an earlier
    one away: more cells than columns, an empty key, keys not distinct, a
    `PanelPatch` carrying a table op past 1 MiB, a selection of a key the table lacks, a `set_value` on a
    table that is not selectable, `set_rows` on a table, and a patch that
    would take a full table past 100,000 rows.  ``which`` past
    those sends one the host does NOT refuse: a cell of a kind it does not
    show (a list), which is blank."""
    panel = ctx.panel("grid")
    row = pb2.TableRow
    s = pb2.Value
    bad = [
        pb2.PatchOp(widget_id="stats", set_table=pb2.TableRows(rows=[row(key="x", cells=[s(s="1"), s(f=2), s(b=True), s(s="4")])])),
        pb2.PatchOp(widget_id="stats", patch_table=pb2.TablePatch(upsert=[row(key="", cells=[s(s="no key")])])),
        pb2.PatchOp(widget_id="stats", set_table=pb2.TableRows(rows=[row(key="k"), row(key="k")])),
        pb2.PatchOp(widget_id="stats", patch_table=pb2.TablePatch(upsert=[row(key="big", cells=[s(s="x" * (1 << 20))])])),
        pb2.PatchOp(widget_id="stats", set_value=pb2.Value(strings=pb2.StringList(values=["a", "nonesuch"]))),
        pb2.PatchOp(widget_id="plain", set_value=pb2.Value(s="a")),
        pb2.PatchOp(widget_id="plain", set_rows=pb2.RowKeys(keys=["a"])),
        # Only after `fixture.table_big` has filled the table to the bound.
        pb2.PatchOp(widget_id="stats", patch_table=pb2.TablePatch(upsert=[row(key="overflow", cells=[s(s="one too many")])])),
    ]
    which = int(ctx.args.get("which", 0))
    if which < len(bad):
        panel._patch(bad[which])
        return f"bad table op {which} of {len(bad)}"
    panel._patch(pb2.PatchOp(widget_id="plain", set_table=pb2.TableRows(rows=[row(key="odd", cells=[s(ints=pb2.IntList(values=[1, 2]))])])))
    return "a cell the host shows blank"


@command("fixture.table_big")
def table_big(ctx: CommandContext) -> str:
    """``rows`` rows through `set_table`, which the SDK sends as a whole
    statement followed by appending patches when it passes one message."""
    n = int(ctx.args.get("rows", 100_000))
    ctx.panel("grid").set_table("stats", ((f"r{i:06d}", [f"row {i}", float(i % 977), i % 2 == 0]) for i in range(n)))
    return f"{n} rows"


@command("fixture.table_burst")
def table_burst(ctx: CommandContext) -> str:
    """``n`` small patches to the `stats` table, one call each and as fast as
    the SDK takes them: one row appended by each.  None is refused, by the
    SDK or the host."""
    n = int(ctx.args.get("n", 1000))
    panel = ctx.panel("grid")
    for i in range(n):
        panel.patch_table("stats", upsert=[(f"burst{i:04d}", [f"burst {i}", float(i), i % 2 == 0])])
    return f"{n} patches"


@command("fixture.table_held")
def table_held(ctx: CommandContext) -> str:
    """The `shelf` panel's table while the panel is closed: 60,000 rows
    (past one message, so a statement and appends), then three patches and a
    selection, each given time to leave on its own -- all of which the host
    validates as each arrives, holds as one table, and shows when the panel
    opens."""
    panel = ctx.panel("shelf")
    panel.set_table("held", ((f"r{i:05d}", [f"row {i}", i]) for i in range(60_000)))
    for step in (
        lambda: panel.patch_table("held", delete=["r00000"], upsert=[("r00001", ["one", -1])]),
        lambda: panel.patch_table("held", upsert=[("extra1", ["first extra", 1])]),
        lambda: panel.patch_table("held", delete=["extra1"], upsert=[("extra2", ["second extra", 2]), ("r00002", ["two", 2.5])]),
        lambda: panel.set_value("held", ["r00001", "extra2"]),
    ):
        time.sleep(0.2)
        step()
    return "held"


@command("fixture.table_held_bad")
def table_held_bad(ctx: CommandContext) -> str:
    """While `shelf` is still closed, after `fixture.table_held`: a row with
    more cells than columns, a selection naming a row the table will not
    hold, and between them a good patch -- each sent on its own.  The two bad
    ops are refused at once, alone; the good one is held."""
    panel = ctx.panel("shelf")
    for step in (
        lambda: panel.patch_table("held", upsert=[("wide", ["too", 1, "many"])]),
        lambda: panel.patch_table("held", upsert=[("extra3", ["third extra", 3])]),
        lambda: panel.set_value("held", ["nonesuch"]),
    ):
        time.sleep(0.2)
        step()
    return "sent"


_GEOMETRY: list = []


@widget_geometry("grid")
def _grid_geometry(ctx, size: WidgetSize) -> None:
    _GEOMETRY.append([size.widget_id, size.width_px, size.height_px, size.dpr, list(size.pixels)])


@command("fixture.geometry")
def geometry_heard(ctx: CommandContext) -> str:
    return json.dumps(_GEOMETRY)


_APPEARANCES: list = []


@appearance
def _appearance(ctx, look: Appearance) -> None:
    _APPEARANCES.append([look.theme, look.hex_colors, list(look.text)])


@command("fixture.appearance")
def appearance_heard(ctx: CommandContext) -> str:
    """Every appearance the handler heard, and the newest the context holds."""
    newest = ctx.appearance
    return json.dumps({"heard": _APPEARANCES, "newest": newest.theme if newest is not None else None})


if __name__ == "__main__":
    # A slow start, for the launch-budget scenario.
    time.sleep(float(os.environ.get("VIZI_FIXTURE_START_DELAY_S", "0")))
    serve(plugin_id="org.example.hostsim_fixture")
