#!/usr/bin/env python3
"""The region-quant example, in Python: every region's mean on one channel at
the frame on screen, kept current from the host's pushes (``../SPEC.md``).

- the ``view`` and ``regions`` subscriptions keep ``t``, the selection and the
  region list current, re-reading only the regions that changed;
- a region's voxels come from the host (``read_roi_inclusion``), never from
  rasterizing its geometry here;
- a ``list`` shows one row per region, with a colormap swatch per row;
- clicking a row selects the region in vizi, and "Brightest frame" moves
  ``t``, both through set-view (§8.15), answering every cause at once --
  brightest from a per-row trace a worker keeps ahead of time (§2);
- "Export CSV…" asks the host's save dialog for a path and writes the table;
- a Follow, offered as "Tour N regions", frames each region in turn once the
  person starts it, and picks up where it left off when resumed (§7).

The arithmetic is SPEC.md's, in integers, so every language prints the same.
The manifest is declared here, in code (SPEC §1): ``--install`` installs it,
and ``vizi-plugin.json`` beside this file is what ``--manifest`` writes.
"""

from __future__ import annotations

import struct
import sys
import threading
import time
import zlib

import numpy as np

from vizi_plugin_sdk import (
    CommandContext,
    HostError,
    SessionEnded,
    pb2,
    command,
    follow,
    panel,
    plugin,
    widgets as w,
    on_regions_changed,
    panel_opened,
    on_view_changed,
    serve,
)

plugin(
    "org.example.region_quant",
    name="Region Quant",
    version="0.1.0",
    permissions=["data_access"],
    subscriptions=["view", "regions"],
)

PANEL = "quant"
LIST = "regions"
COLORMAP = "viridis"
SWATCH = 8  # pixels per side
TOUR_DWELL_S = 0.5  # SPEC §7: how long the tour stays on each region
MAX_ROWS = 1024  # SPEC §1/§3: the list's max_rows (4 cells a row, 4096 cells)
WAIT_S = 0.9  # SPEC §2: how long a cause's command waits for the rows
FLOAT_STATUS = "Region Quant measures integer data; this dataset is f32"

_LOCK = threading.RLock()
_STATE = {
    "t": None,  # the frame on screen, from ViewChanged (or ViewState until one arrives)
    "channel": 0,
    "selected": "",  # vizi's selection, from ViewChanged
    "rows": {},  # region id -> {"name", "voxels", "sum"}; voxels None when not measured
    "table": None,  # the colormap's 256 RGBA entries, read once
    # SPEC §2: the open dataset, read from GetViewState when a generation opens.
    "generation": None,
    "T": None,
    "channels": None,
    "integer": True,
    "offered": None,  # the Follow offer's label as last sent ("" withdrawn)
    "tour_last": None,  # the order key (display name, id) of the last region a tour showed
    # SPEC §2's traces: region id -> {"c", "T", "frames": [(count, sum), ...]}, complete when
    # len(frames) == T on the current channel.  Kept by one worker (`_trace_worker`).
    "traces": {},
}

#: The trace worker's queue and wake-up: region ids waiting, in the order they were
#: invalidated, and each one's epoch, bumped whenever its trace is invalidated.
_TRACE_WAIT: list[str] = []
_TRACE_EPOCH: dict[str, int] = {}
_TRACE_CHANGED = threading.Condition(_LOCK)
_TRACER = {"host": None, "panel": None, "thread": None}
#: SPEC §2: a failed trace waits, doubling from 100 ms to 5 s.
_BACKOFF = {"delay": 0.1}


# -- SPEC §3: the arithmetic ---------------------------------------------------


def mean_text(total: int, count: int) -> str:
    """Thousandths rounded half up, in integers, then printed: ``"12.346"``.
    ``""`` when the region covers no voxel."""
    if count == 0:
        return ""
    thousandths = (2000 * total + count) // (2 * count)
    return f"{thousandths // 1000}.{thousandths % 1000:03d}"


def swatch_index(total: int, count: int) -> int:
    """The colormap entry: the floor of the mean, shifted right by 8 (u16 to
    0..255); entry 0 when the region covers no voxel."""
    return 0 if count == 0 else min(255, (total // count) >> 8)


def png_rgb(width: int, height: int, rgb) -> bytes:
    """A plain RGB PNG of one color."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


# -- measuring -------------------------------------------------------------------


def measure(host, region_id: str, t: int, channel: int) -> "tuple[int, int] | tuple[None, None]":
    """``(voxel count, exact sum)`` of ``channel`` at ``t`` over the voxels
    the host says the region covers; ``(None, None)`` on data that is not
    integer (SPEC §2: measure nothing)."""
    if not _STATE["integer"]:
        return None, None
    found = host.read_roi_inclusion(region_id, t)
    if found is None:
        return 0, 0
    mask, (z0, y0, x0) = found
    if mask.size == 0 or not mask.any():
        return 0, 0
    dz, dy, dx = mask.shape
    volume = host.read_volume(t, channel, roi=((z0, y0, x0), (z0 + dz, y0 + dy, x0 + dx)))
    values = np.asarray(volume)[np.asarray(mask, dtype=bool)]
    return int(values.size), int(values.astype(np.uint64).sum())


def current_t(host) -> int:
    if _STATE["t"] is None:
        _STATE["t"] = int(host.view_state().t)
    return _STATE["t"]


def remeasure(ctx, region_ids) -> None:
    """Measure these rows (``_LOCK`` held).  SPEC §2: a region that is gone
    (`NOT_FOUND`) loses its row and the rest commit; any other failure keeps
    that row's numbers and says so."""
    host = ctx.host
    t, channel = current_t(host), _STATE["channel"]
    for region_id in region_ids:
        row = _STATE["rows"].get(region_id)
        if row is None:
            continue
        try:
            row["voxels"], row["sum"] = measure(host, region_id, t, channel)
        except SessionEnded:
            raise  # the session ended: no "Measuring failed" for a closing panel, and the SDK reports nothing
        except HostError as exc:
            if exc.kind == pb2.Error.NOT_FOUND:
                _STATE["rows"].pop(region_id, None)
                forget_trace(region_id)
            else:
                status(ctx, f"Measuring failed: {exc}")


def dataset_opened(ctx, generation: int) -> None:
    """SPEC §2: a new generation is a new dataset -- read its frames, channels
    and dtype; a channel it lacks resets to 0; float data is not
    measured.  ``_LOCK`` held."""
    if generation == _STATE["generation"] or generation == 0:
        return
    _STATE["generation"] = generation
    view = ctx.host.view_state()
    _STATE["T"] = int(view.shape_tczyx[0])
    _STATE["channels"] = int(view.shape_tczyx[1])
    was_integer = _STATE["integer"]
    _STATE["integer"] = view.dtype is None or np.issubdtype(view.dtype, np.integer)
    if _STATE["integer"] and not was_integer:
        status(ctx, "Ready")
    if _STATE["channel"] >= _STATE["channels"]:
        _STATE["channel"] = 0
        ctx.panel(PANEL).set_value("channel", 0)
    if not _STATE["integer"]:
        for region_id in list(_STATE["rows"]):
            forget_trace(region_id)
        status(ctx, FLOAT_STATUS)


def order_key(region_id: str) -> tuple[str, str]:
    """SPEC §3: display name, then id, both by code point."""
    return (_STATE["rows"][region_id]["name"] or region_id, region_id)


def ordered() -> list[str]:
    """SPEC §3: rows by display name, then id."""
    return sorted(_STATE["rows"], key=order_key)


def publish(ctx) -> None:
    """The whole list: its keys in order, then each row's cells."""
    if _STATE["table"] is None:
        _STATE["table"] = ctx.host.get_colormap(COLORMAP)
    panel = ctx.panel(PANEL)
    every = ordered()
    keys = every[:MAX_ROWS]
    if len(every) > MAX_ROWS:
        status(ctx, f"Showing {MAX_ROWS} of {len(every)} regions")
    panel.set_rows(LIST, keys)
    for key in keys:
        row = _STATE["rows"][key]
        measured = row["voxels"] is not None
        panel.set_value("name", row["name"] or key, row=key)
        panel.set_value("voxels", str(row["voxels"]) if measured else "", row=key)
        panel.set_value("mean", mean_text(row["sum"], row["voxels"]) if measured else "", row=key)
        # SPEC §3: with the rest of the row, at any time -- the SDK holds an
        # image until the panel's segment is installed.
        entry = swatch_index(row["sum"], row["voxels"]) if measured else 0
        rgb = _STATE["table"][entry][:3]
        panel.push_image("swatch", png_rgb(SWATCH, SWATCH, [int(v) for v in rgb]), row=key)
    panel.set_value(LIST, _STATE["selected"] if _STATE["selected"] in _STATE["rows"] else "")
    # SPEC §7: the offer follows the number of rows; none withdraws it.
    n = len(keys)
    label = "" if n == 0 else f"Tour {n} region" + ("" if n == 1 else "s")
    if label != _STATE["offered"]:
        if label:
            panel.offer_follow(label)
        else:
            panel.withdraw_follow()
        _STATE["offered"] = label


def status(ctx, text: str) -> None:
    ctx.panel(PANEL).set_value("status", text)


# -- the traces (SPEC §2): brightest's answers, computed ahead of time -------------


def invalidate(ctx, region_ids) -> None:
    """Restart these rows' traces (called with ``_LOCK`` held).  None on float
    data (SPEC §2)."""
    if not _STATE["integer"]:
        return
    for region_id in region_ids:
        _TRACE_EPOCH[region_id] = _TRACE_EPOCH.get(region_id, 0) + 1
        _STATE["traces"].pop(region_id, None)
        if region_id not in _TRACE_WAIT:
            _TRACE_WAIT.append(region_id)
    if _TRACER["thread"] is None:
        _TRACER["host"], _TRACER["panel"] = ctx.host, ctx.panel(PANEL)
        _TRACER["thread"] = threading.Thread(target=_trace_worker, daemon=True, name="region-quant-trace")
        _TRACER["thread"].start()
    _TRACE_CHANGED.notify_all()


def forget_trace(region_id: str) -> None:
    """A deleted row's trace goes (called with ``_LOCK`` held)."""
    _TRACE_EPOCH[region_id] = _TRACE_EPOCH.get(region_id, 0) + 1
    _STATE["traces"].pop(region_id, None)
    if region_id in _TRACE_WAIT:
        _TRACE_WAIT.remove(region_id)


def _trace_worker() -> None:
    """Never dies while the session lives: a trace that fails is abandoned,
    and the next one runs.  The session's end stops it -- no read will be
    answered again, so there is nothing to retry and nothing to report."""
    while True:
        try:
            _trace_one()
        except SessionEnded:
            return  # Shutdown, or the host gone; `_TRACER["thread"]` stays set, so none restarts
        except Exception as exc:  # noqa: BLE001 -- an unexpected failure ends one trace, not the worker
            print(f"region_quant: a trace failed and was abandoned: {exc!r}", file=sys.stderr)


def _trace_one() -> None:
    """Take the next waiting row and trace it (SPEC §2)."""
    host, panel = _TRACER["host"], _TRACER["panel"]
    with _LOCK:
        while not _TRACE_WAIT:
            panel.set_value("tracing", "")
            _TRACE_CHANGED.wait()
        region_id = _TRACE_WAIT.pop(0)
        if region_id not in _STATE["rows"]:
            return
        epoch, channel, name = _TRACE_EPOCH.get(region_id, 0), _STATE["channel"], _name_of(region_id)
    frames_total = int(host.view_state().shape_tczyx[0])
    frames: list[tuple[int, int]] = []
    with _LOCK:
        if _TRACE_EPOCH.get(region_id, 0) != epoch:
            return
        _STATE["traces"][region_id] = {"c": channel, "T": frames_total, "frames": frames}
        panel.set_value("tracing", f"Measuring {name}: 0 of {frames_total} frames")
    for t in range(frames_total):
        try:
            count, total = measure(host, region_id, t, channel)
        except SessionEnded:
            raise  # ends the worker (`_trace_worker`): not retried, not reported
        except HostError as exc:
            if exc.kind == pb2.Error.NOT_FOUND:
                # SPEC §2: the region went (deleted, or a new dataset) while
                # its trace ran; the push that says so drops the row.
                return
            # SPEC §2: anything else is retried from frame 0, later.
            delay = _BACKOFF["delay"]
            _BACKOFF["delay"] = min(5.0, delay * 2)
            time.sleep(delay)
            with _LOCK:
                if _TRACE_EPOCH.get(region_id, 0) == epoch and region_id in _STATE["rows"]:
                    _STATE["traces"].pop(region_id, None)
                    if region_id not in _TRACE_WAIT:
                        _TRACE_WAIT.append(region_id)
            return
        with _LOCK:
            if _TRACE_EPOCH.get(region_id, 0) != epoch:
                break  # invalidated meanwhile: it is queued again, from frame 0
            frames.append((count, total))
            panel.set_value("tracing", f"Measuring {name}: {len(frames)} of {frames_total} frames")
    else:
        _BACKOFF["delay"] = 0.1


def trace_of(region_id: str):
    """The row's trace on the current channel, complete or not (``_LOCK`` held)."""
    trace = _STATE["traces"].get(region_id)
    return trace if trace is not None and trace["c"] == _STATE["channel"] else None


# -- the subscriptions (SPEC §2) -------------------------------------------------


@panel_opened(PANEL)
def opened(ctx) -> None:
    # Bounded, like a command's wait: a subscription handler holding the rows
    # publishes when it finishes, and one waiting on a request that crossed
    # `Shutdown` is never answered -- an unbounded wait here would keep the
    # process from exiting.
    if not _hold_briefly():
        return
    try:
        publish(ctx)
    finally:
        _LOCK.release()


@on_view_changed
def view_changed(ctx, change) -> None:
    with _LOCK:
        before = (_STATE["channel"], _STATE["integer"])
        dataset_opened(ctx, int(change.open_generation))
        moved = _STATE["t"] != change.t
        _STATE["t"] = int(change.t)
        _STATE["selected"] = change.selected_region_id
        if moved or (_STATE["channel"], _STATE["integer"]) != before:
            remeasure(ctx, list(_STATE["rows"]))
        if (_STATE["channel"], _STATE["integer"]) != before:
            invalidate(ctx, ordered())
        publish(ctx)


@on_regions_changed
def regions_changed(ctx, change) -> None:
    with _LOCK:
        dataset_opened(ctx, int(change.open_generation))
        if change.reset:
            for region_id in list(_STATE["rows"]):
                forget_trace(region_id)
            _STATE["rows"].clear()
        changed = []
        for c in change.changes:
            if c.deleted:
                _STATE["rows"].pop(c.region_id, None)
                forget_trace(c.region_id)
            else:
                changed.append(c.region_id)
        if changed:
            for record in ctx.host.get_rois(region_ids=changed):
                _STATE["rows"][record.region_id] = {"name": record.name, "voxels": 0, "sum": 0}
            remeasure(ctx, [r for r in changed if r in _STATE["rows"]])
            invalidate(ctx, [r for r in changed if r in _STATE["rows"]])
        publish(ctx)


# -- the panel's commands (SPEC §4-§6) --------------------------------------------


@command("quant.channel", title="Measure a channel", menu=None)
def choose_channel(ctx: CommandContext) -> str:
    with _LOCK:
        chosen = int(ctx.args["value"])
        if chosen == _STATE["channel"]:
            return f"already measuring channel {chosen}"  # SPEC §4
        _STATE["channel"] = chosen
        remeasure(ctx, list(_STATE["rows"]))
        invalidate(ctx, ordered())
        publish(ctx)
    return f"measuring channel {_STATE['channel']}"


def _hold_briefly() -> bool:
    """SPEC §2: a cause's command waits at most 900 ms for the rows."""
    return _LOCK.acquire(timeout=WAIT_S)


def _no_dataset(ctx) -> bool:
    """SPEC §5: cause 0 -- no dataset is open -- moves nothing."""
    if ctx.cause:
        return False
    status(ctx, "No dataset is open")
    return True


def _name_of(region_id: str) -> str:
    row = _STATE["rows"].get(region_id)
    return (row["name"] or region_id) if row is not None else region_id


@command("quant.pick", title="Select a region", menu=None)
def pick(ctx: CommandContext) -> str:
    """A click on a row: select that region in vizi (set-view)."""
    key = str(ctx.args.get("row", ""))
    if _no_dataset(ctx):
        return "no dataset"
    if not _hold_briefly():
        ctx.decline_view()
        return "busy"
    try:
        known = key in _STATE["rows"]
    finally:
        _LOCK.release()
    if not known:
        ctx.decline_view()
        return "no such row"
    result = ctx.move_view(select_region=key)
    status(ctx, f"Selected {_name_of(key)}" if result.applied else f"The view did not move: {result.reason}")
    return result.outcome


@command("quant.brightest", title="Go to the brightest frame", menu=None)
def brightest(ctx: CommandContext) -> str:
    """Move ``t`` to the frame where the selected region's mean is highest
    (the earliest on a tie), from its trace, reading nothing: a scan here would
    not fit the cause's life.  Decline while the trace is incomplete."""
    if _no_dataset(ctx):
        return "no dataset"
    if not _hold_briefly():
        ctx.decline_view()
        return "busy"
    try:
        selected = _STATE["selected"]
        known = selected in _STATE["rows"]
        integer = _STATE["integer"]
        trace = trace_of(selected) if known else None
        frames = list(trace["frames"]) if trace is not None else []
        frames_total = trace["T"] if trace is not None else _STATE["T"]
    finally:
        _LOCK.release()
    if not integer:
        ctx.decline_view()
        status(ctx, FLOAT_STATUS)
        return "declined"
    if not known:
        ctx.decline_view()
        status(ctx, "Select a region first")
        return "declined"
    if frames_total is None:
        frames_total = int(ctx.host.view_state().shape_tczyx[0])
    if trace is None or len(frames) < frames_total:
        ctx.decline_view()
        status(ctx, f"Measuring… {len(frames)} of {frames_total} frames")
        return "declined"
    best, best_key = None, None
    for t, (count, total) in enumerate(frames):
        if count == 0:
            continue
        key = (total, count)  # compare means exactly: total_a * count_b vs total_b * count_a
        if best_key is None or key[0] * best_key[1] > best_key[0] * key[1]:
            best, best_key = t, key
    if best is None:
        ctx.decline_view()
        status(ctx, f"{_name_of(selected)} covers no voxel")
        return "declined"
    result = ctx.move_view(t=best)
    status(ctx, f"Brightest frame for {_name_of(selected)}: t={best}" if result.applied else f"The view did not move: {result.reason}")
    return result.outcome


@follow(PANEL)
def tour(ctx, run) -> None:
    """SPEC §7: frame each row in list order, dwelling on each; a resumed
    tour starts after the last region shown.  Returning ends the Follow
    (`PLUGIN_ENDED`); the host ending it first is reported in ``status``."""
    with _LOCK:
        keys = ordered()
        last = _STATE["tour_last"] if run.resumed else None
        if last is not None:
            keys = [k for k in keys if order_key(k) > last]
    for key in keys:
        with _LOCK:
            if key not in _STATE["rows"]:
                continue  # deleted since the tour began
            now = ordered()
            name, where = _name_of(key), f"({now.index(key) + 1} of {len(now)})"
            shown = order_key(key)
        if not run.move(frame_region=key):
            break
        with _LOCK:
            _STATE["tour_last"] = shown
        status(ctx, f"Touring {name} {where}")
        if not run.wait(TOUR_DWELL_S):
            break
    if run.active:
        status(ctx, "Tour finished")
    else:
        status(ctx, f"Tour stopped: {run.reason}")


def csv_field(text: str) -> str:
    """RFC 4180: quoted only when it holds a comma, a quote or a line break."""
    if any(ch in text for ch in ',"\r\n'):
        return '"' + text.replace('"', '""') + '"'
    return text


def export_text() -> str:
    lines = ["region_id,name,t,channel,voxels,mean"]
    t, channel = _STATE["t"], _STATE["channel"]
    for key in ordered():
        row = _STATE["rows"][key]
        measured = row["voxels"] is not None
        voxels = row["voxels"] if measured else ""
        mean = mean_text(row["sum"], row["voxels"]) if measured else ""
        lines.append(f"{csv_field(key)},{csv_field(row['name'])},{t},{channel},{voxels},{mean}")
    return "\n".join(lines) + "\n"


@command("quant.export", title="Export region means", menu=None)
def export(ctx: CommandContext) -> str:
    """Acknowledge at once; ask where and write on a worker (the host's save
    dialog may take a person minutes)."""
    host = ctx.host
    panel = ctx.panel(PANEL)

    def work() -> None:
        try:
            path = host.request_save_path("regions.csv", extension="csv", title="Export region means", timeout=1800)
        except SessionEnded:
            return  # the session ended with the picker open: nothing to report to
        except HostError as exc:
            panel.set_value("status", f"Export failed: {exc}")
            return
        if path is None:
            panel.set_value("status", "Export canceled")
            return
        with _LOCK:
            text, count = export_text(), len(_STATE["rows"])
        try:
            path.write_text(text, encoding="utf-8", newline="")
        except OSError as exc:
            panel.set_value("status", f"Export failed: {exc}")  # SPEC §6: never swallowed
            return
        panel.set_value("status", f"Exported {count} regions to {path}")

    threading.Thread(target=work, daemon=True).start()
    return "exporting"


# -- SPEC §1: the manifest ---------------------------------------------------------

panel(
    PANEL,
    title="Region Quant",
    view_targets=["t", "select_region"],
    follow_targets=["frame_region"],
    root=w.column(
        "root",
        w.channel_picker("channel", command=choose_channel, value=0),
        w.list_rows(
            LIST,
            [w.label("name", ""), w.label("voxels", ""), w.label("mean", ""), w.image("swatch")],
            command=pick,
            selectable=True,
            moves_view=True,
            max_rows=MAX_ROWS,
            visible_rows=8,
        ),
        w.button("brightest", "Brightest frame", command=brightest, moves_view=True),
        w.button("export", "Export CSV…", command=export),
        w.label("tracing", ""),
        w.label("status", "Ready"),
    ),
)


if __name__ == "__main__":
    serve()
