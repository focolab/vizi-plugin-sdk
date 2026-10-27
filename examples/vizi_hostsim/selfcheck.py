#!/usr/bin/env python3
"""The emulator's own smoke test, against ``fixture/`` over the real transport.

    fixture/.venv/bin/python selfcheck.py

Every path the emulator answers that neither ``peak_roi`` nor ``voxel_probe``
reaches: a reader's `G` deposits read as loans and released, `ReadPlane`,
`Metadata`, `CloseReader`; an exporter's `X` chunks deposited into a
host-produced ring (with the install riding the first chunk, and inline), the
plugin's `ReleaseDeposit`, `FinishExport`, `AbortExport`; a label volume pushed
and compiled naming one reservation twice (``uses=2``), released once, the
masks the host made, `GetRois` and `ReadMaskVoxels`; a native ROI
contribution; `RequestAsset` picked, declined, and refused for want of the
permission.  Also covered: `ReadRoiInclusion`
clipped to a box and its refusals, non-finite ROI values and a pose domain
past `T` refused, a second picker refused while one is up, the settings
folder created only on first use, the §2.3 folder table with its case rule,
and -- in a second session, since it ends one -- a plain doorbell hang-up
answered `DoorbellShutdown{peer_closed}`.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # examples/, for vizi_hostsim

from vizi_hostsim import (  # noqa: E402
    APPEARANCE_PALETTES,
    DOORBELL_WINDOW_MS,
    INCLUSION_VOXEL_BUDGET,
    SHUTDOWN_WINDOW_S,
    TABLE_HELD_BYTES,
    VIEW_2D,
    Dataset,
    Host,
    PluginError,
    SeriesChoiceRequired,
    reader_opened_problem,
    series_list_problem,
    Segment,
    check,
    check_encoder,
    content_hash,
    failures,
    plugin_folder_name,
    segment_id,
    slider_readout,
    slider_value,
)
from vizi_plugin_sdk import pb2  # noqa: E402
from vizi_plugin_sdk.control import MAX_MESSAGE_BYTES  # noqa: E402
from vizi_plugin_sdk.manifest import ManifestError  # noqa: E402

FIXTURE = HERE / "fixture"
SHAPE = (3, 2, 4, 8, 8)
G_SEG, V_SEG, X_SEG, O_SEG, P_SEG, G2_SEG, C_SEG = (segment_id(i) for i in range(1, 8))


def ramp(t: int, c: int) -> np.ndarray:
    _, _, z, y, x = SHAPE
    return (np.arange(z * y * x, dtype=np.uint16).reshape(z, y, x) + 1000 * t + 100 * c).astype(np.uint16)


DATASET = Dataset(
    shape_tczyx=SHAPE,
    np_dtype=np.dtype(np.uint16),
    wire_dtype=pb2.DTYPE_U16,
    open_generation=3,
    volume=ramp,
    t=1,
    c=0,
)


#: Outcomes the Python SDK does not give yet: case -> (the answer it gives
#: instead, why).  A listed case passes while the SDK gives exactly that
#: answer and fails on any other wrong one; once the SDK gives the stated
#: answer it is reported "now passes", so it can leave the list (as
#: `scripts/check_manifest_fixtures.py` does).  The list is empty.
PENDING_VIZI: dict[str, tuple[str, str]] = {}


#: `control_plane_protocol.md` §2.3's published (id, folder) table, copied row
#: for row, in the spec's order.
FOLDER_TABLE = [
    ("org.example.plug", "org.example.plug"),
    ("Org.Example", "Org.Example-353f0972"),
    ("con", "_con-1143da2b"),
    ("NUL", "_NUL-ae6de182"),
    ("aux.plugin", "_aux.plugin-6e31b3b9"),
    ("Lpt9.cfg", "_Lpt9.cfg-50f37a5c"),
    ("abc.", "abc-5ac9481b"),
    ("a/b", "a_b-c14cddc0"),
    ("../../etc", "etc-74ccf3c5"),
    ("..", "plugin-5ec1f7e7"),
    (" spaced id ", "spaced_id-6d01a897"),
    ("\u00e9", "plugin-4a99557e"),
]


def check_slider_rendering() -> str:
    """§3.3.1: an integer slider, and a float slider with a declared step,
    stop at ``min + k * step`` with the last position clamped to ``max``, so
    every stop below max is reached even when the range is not a multiple of
    the step.  An integer slider's step is whole: one that is not was refused
    at parse (`check_manifest_refused`), and the rule refuses it too.  A float
    slider reads out at the step's precision (0.05 -> 2, 0.5 -> 1, 10 -> 0).  With no step it has 1000 positions and three decimals.  The
    event carries the value at the readout's precision, never a position."""
    whole = {"min": 0, "max": 10, "step": 2}
    fifteen = {"min": 0, "max": 100, "step": 15}
    seven = {"min": 0, "max": 10, "step": 7}
    cut = {"id": "level", "min": 0, "max": 10, "step": 2.5}
    fine = {"min": 0.1, "max": 1, "step": 0.05, "float": True}
    half = {"min": 0, "max": 10, "step": 0.5, "float": True}
    tens = {"min": 0, "max": 95, "step": 10, "float": True}
    four = {"min": 0, "max": 1, "step": 0.4, "float": True}
    one = {"min": 0, "max": 2, "step": 1.0, "float": True}
    free = {"min": 0, "max": 1, "float": True}
    assert slider_value(whole, 5.2) == 6 and slider_readout(whole, 6) == "6"
    assert slider_value(whole, 99) == 10 and slider_value(whole, -3) == 0
    # 100 / 15 is not whole: 90 is a stop, and the last move clamps at 100.
    assert [slider_value(fifteen, v) for v in (94, 97, 100)] == [90, 100, 100]
    # Two positions and max: 0, 7, then 10, clamped.
    assert [slider_value(seven, v) for v in (3, 4, 8.4, 8.6)] == [0, 7, 7, 10]
    # A non-whole step on an integer slider is no slider at all.
    try:
        slider_value(cut, 2.4)
    except ValueError as exc:
        assert "slider 'level' step 2.5 is not a whole number" in str(exc), exc
    else:
        raise AssertionError("an integer slider with step 2.5 was given positions")
    # 0.1 + 2 * 0.05 is 0.20000000000000004 in floats; the event carries 0.2.
    assert slider_value(fine, 0.21) == 0.2 and slider_readout(fine, 0.2) == "0.20"
    assert slider_value(fine, 1) == 1 and slider_readout(fine, 1) == "1.00"
    assert slider_value(half, 2.3) == 2.5 and slider_readout(half, 2.5) == "2.5"
    # 95 / 10 is not whole: 90 is a stop, the last move clamps at 95.
    assert slider_value(tens, 88) == 90 and slider_readout(tens, 90) == "90"
    assert slider_value(tens, 92) == 90 and slider_value(tens, 94) == 95
    assert slider_value(tens, 95) == 95 and slider_readout(tens, 95) == "95"
    # Step 0.4 to 1: 0.8 is reached before max, never skipped.
    assert [slider_value(four, v) for v in (0.4, 0.8, 0.85, 0.95, 1)] == [0.4, 0.8, 0.8, 1, 1]
    # A declared 1.0 is a step, not the default: two positions, no decimals.
    assert slider_value(one, 0.6) == 1 and slider_readout(one, 1) == "1"
    v = slider_value(free, 0.5004)
    assert v == 0.5 and slider_readout(free, v) == "0.500", (v, slider_readout(free, v))
    assert slider_value(free, 0.1236) == 0.124 and slider_readout(free, 1.0) == "1.000"
    return ("integer step 2: 5.2 -> 6; step 15 to 100: 94 -> 90, 97 -> 100; step 7 to 10: 8.6 -> 10; integer step 2.5 refused; "
            "float step 0.05: 0.21 -> 0.2 '0.20'; step 0.5: 2.3 -> 2.5 '2.5'; step 10 to 95: 88 -> 90, 94 -> 95; "
            "step 0.4 to 1: 0.8 reached, then 1; step 1.0 declared: '1'; no step: 0.1236 -> 0.124 '0.124'")


def check_table_cell_kinds() -> str:
    """§8.4's `table`: a cell whose kind is not its column's type -- text or
    a bool in a number column, a number in a bool or string column -- is
    shown as its text and sorts as a blank, after every value in both
    directions."""
    from vizi_hostsim import _table_shown, _table_sort_key

    number, flag, text = {"type": "number", "precision": 1}, {"type": "bool"}, {"type": "string"}
    blank = _table_sort_key(number, None)
    assert _table_shown(number, "n/a") == "n/a" and _table_sort_key(number, "n/a") == blank
    assert _table_shown(number, True) == "True" and _table_sort_key(number, True) == blank
    assert _table_shown(flag, 1) == "1" and _table_sort_key(flag, 1) == blank
    assert _table_shown(text, 2.5) == "2.5" and _table_sort_key(text, 2.5) == blank
    assert _table_sort_key(number, 0.0) < blank and _table_sort_key(number, 3) < blank
    assert _table_shown(number, 3) == "3.0" and _table_shown(flag, True) == "✓"
    return "'n/a' and True in a number column, 1 in a bool column, 2.5 in a string column: shown as text, sorted as blanks"


def check_table_replay() -> str:
    """§8.4: a table's rows and selection are the state applying its ops in
    order reaches, open or closed.  Random runs of `set_table`, `patch_table`
    and `set_value` -- a key deleted and upserted in one patch, a selected
    key dropped and re-added across two, duplicate keys, a row too wide, a
    selection naming a missing key or too many keys -- go one `PanelPatch`
    each to an open panel and to a closed one that then opens, and both end
    on the keys, rows and selection `tables.apply_op` and `tables.select`
    reach applied in order, with the same ops refused for the same reasons.
    A selected key deleted and upserted again in ONE patch is a fresh row
    and loses its selection, open or closed, as two patches would.  Then the
    1 MiB bound, per `PanelPatch` as sent: two table ops each under it but
    together past it are refused `INVALID_ARGUMENT`, and the patch's other
    op applies."""
    import random

    from vizi_plugin_sdk import tables

    def bare(widgets):
        h = Host.__new__(Host)
        h._lock, h.sent, h.refusals, h.patches = threading.Lock(), [], [], []
        h.send = h.sent.append
        h._panel_widgets, h.opened_panels, h._held_patches = {"p": widgets}, set(), {}
        h.tables, h._table_cells, h.table_blank_kinds = {}, {}, set()
        h.retiring_segments, h.list_rows, h.dataset = set(), {}, DATASET
        h.hold_table_drain, h.table_transport, h._table_transport_bytes = False, [], {}
        return h

    row, value = pb2.TableRow, pb2.Value
    keys = "abcdef"

    def random_op(rng, multiple):
        def a_row(key=None):
            cells = [value(s=rng.choice("xyz")), value(f=rng.random())][: rng.randint(0, 2)]
            if rng.random() < 0.04:
                cells.append(value(b=True))  # a third cell for two columns
            return row(key=key or rng.choice(keys), cells=cells)

        roll = rng.random()
        if roll < 0.2:
            rows = [a_row(k) for k in rng.sample(keys, rng.randint(0, 5))]
            if rows and rng.random() < 0.1:
                rows.append(row(key=rows[0].key))
            return pb2.PatchOp(widget_id="t", set_table=pb2.TableRows(rows=rows))
        if roll < 0.65:
            delete = rng.sample(keys, rng.randint(0, 3))
            upsert = [a_row() for _ in range(rng.randint(0, 3))]
            if delete and rng.random() < 0.4:
                upsert.append(a_row(delete[0]))  # deleted and upserted in one patch
            return pb2.PatchOp(widget_id="t", patch_table=pb2.TablePatch(delete=delete, upsert=upsert))
        chosen = rng.sample(keys, rng.randint(0, 3 if multiple else 1))
        if chosen and rng.random() < 0.2:
            chosen.append(chosen[0])  # a duplicate collapses
        if len(chosen) == 1 and rng.random() < 0.5:
            return pb2.PatchOp(widget_id="t", set_value=value(s=chosen[0]))
        if not chosen and rng.random() < 0.5:
            return pb2.PatchOp(widget_id="t", set_value=value(s=""))
        return pb2.PatchOp(widget_id="t", set_value=value(strings=pb2.StringList(values=chosen)))

    runs = ops_total = refused_total = 0
    for seed in range(300):
        rng = random.Random(seed)
        multiple = seed % 3 != 0
        spec = {"kind": "table", "id": "t", "selectable": True, "multiple": multiple,
                "columns": [{"id": "name"}, {"id": "n", "type": "number"}]}
        ops = [random_op(rng, multiple) for _ in range(rng.randint(1, 30))]
        oracle, whys = tables.TableState(), []
        for op in ops:
            if op.WhichOneof("op") == "set_value":
                why = tables.select(oracle, op.set_value, multiple, "t")
            else:
                why = tables.apply_op(oracle, op, 2, "t")
            if why is not None:
                whys.append(why)
        for opened in (True, False):
            h = bare({"t": ("table", spec)})
            if opened:
                h._mark_panel_open("p")
            for op in ops:
                h._on_panel_patch(pb2.PanelPatch(panel_id="p", ops=[op]), 64)
            if not opened:
                h._mark_panel_open("p")
            got = h.tables.get(("p", "t"), tables.TableState())
            where = f"seed {seed}, {'open' if opened else 'closed until open'}"
            assert got.keys == oracle.keys, (where, got.keys, oracle.keys)
            assert all(got.rows[k] == oracle.rows[k] for k in oracle.keys), where
            assert got.selection == oracle.selection, (where, got.selection, oracle.selection)
            details = [d for _, d in h.refusals]
            assert len(details) == len(whys) and all(d.endswith(w) for d, w in zip(details, whys)), (where, details, whys)
        runs += 1
        ops_total += len(ops)
        refused_total += len(whys)

    # A fresh row: deleted and upserted in one patch, it is not selected,
    # exactly as the delete and the upsert sent as two patches leave it.
    spec = {"kind": "table", "id": "t", "selectable": True, "multiple": True, "columns": [{"id": "name"}]}
    start = [pb2.PatchOp(widget_id="t", set_table=pb2.TableRows(rows=[row(key=k) for k in "abc"])),
             pb2.PatchOp(widget_id="t", set_value=value(strings=pb2.StringList(values=["a", "b"])))]
    one = [pb2.PatchOp(widget_id="t", patch_table=pb2.TablePatch(delete=["a"], upsert=[row(key="a", cells=[value(s="again")])]))]
    two = [pb2.PatchOp(widget_id="t", patch_table=pb2.TablePatch(delete=["a"])),
           pb2.PatchOp(widget_id="t", patch_table=pb2.TablePatch(upsert=[row(key="a", cells=[value(s="again")])]))]
    for opened in (True, False):
        for ops in (one, two):
            h = bare({"t": ("table", spec)})
            if opened:
                h._mark_panel_open("p")
            for op in start + ops:
                h._on_panel_patch(pb2.PanelPatch(panel_id="p", ops=[op]), 64)
            if not opened:
                h._mark_panel_open("p")
            got = h.tables[("p", "t")]
            assert got.keys == ["b", "c", "a"] and got.selection == ["b"] and not h.refusals, (opened, len(ops), got.keys, got.selection, h.refusals)

    # The bound is the PanelPatch's, read before the split.
    columns = [{"id": "name"}]
    h = bare({"t": ("table", {"kind": "table", "id": "t", "columns": columns}),
              "u": ("table", {"kind": "table", "id": "u", "columns": columns}),
              "l": ("label", {"kind": "label", "id": "l"})})
    h._mark_panel_open("p")
    half = [row(key=f"k{i:06d}", cells=[value(s="x" * 100)]) for i in range(5000)]
    big = pb2.PluginMsg(panel_patch=pb2.PanelPatch(panel_id="p", ops=[
        pb2.PatchOp(widget_id="t", set_table=pb2.TableRows(rows=half)),
        pb2.PatchOp(widget_id="u", patch_table=pb2.TablePatch(upsert=half)),
        pb2.PatchOp(widget_id="l", set_value=value(s="applied")),
    ]))
    size = big.panel_patch.ByteSize()
    assert all(op.ByteSize() < (1 << 20) < size for op in big.panel_patch.ops[:2]), size
    h._on_panel_patch(big.panel_patch, size)
    details = [d for _, d in h.refusals]
    assert len(details) == 2 and all(f"INVALID_ARGUMENT: the PanelPatch carrying this table op is {size} bytes" in d for d in details), details
    assert "'t'" in details[0] and "'u'" in details[1], details
    assert not h.tables and h.patches == [("p", "l", "applied")], (h.tables, h.patches)
    return (f"{runs} random runs, {ops_total} ops ({refused_total} refused), open and closed: the rows and selection "
            f"of the ops applied in order; a selected key deleted and re-added in one patch unselected, as in two; "
            f"a {size}-byte PanelPatch: both table ops refused, its label applied")


def check_table_transport() -> str:
    """§8.4 and §9: the host's transport holds every op of a table in order,
    its selection included, none superseding or displacing another; exempt
    from the 256 held-whole ops and bounded per widget by 64 MiB of encoded
    ops, past which the session faults naming the widget and the op.  With
    the drain held: 1,000 small patches and the selections between them,
    then a delete-and-re-add of a selected row, all held and then applied in
    order without a refusal, open and closed; and 64 MiB of ops on one table
    faults the session on the op that passes it, while another table's op
    still holds."""
    from vizi_plugin_sdk import tables

    row, value = pb2.TableRow, pb2.Value

    def bare(widgets):
        h = Host.__new__(Host)
        h._lock, h.sent, h.refusals, h.patches = threading.Lock(), [], [], []
        h.send = h.sent.append
        h._panel_widgets, h.opened_panels, h._held_patches = {"p": widgets}, set(), {}
        h.tables, h._table_cells, h.table_blank_kinds = {}, {}, set()
        h.retiring_segments, h.list_rows, h.dataset = set(), {}, DATASET
        h.hold_table_drain, h.table_transport, h._table_transport_bytes = True, [], {}
        h.session_faults, h.unhandled = [], []
        h.shutdown = lambda **_: None
        return h

    spec = {"kind": "table", "id": "t", "selectable": True, "multiple": True, "columns": [{"id": "name"}, {"id": "n", "type": "number"}]}
    ops = [pb2.PatchOp(widget_id="t", set_table=pb2.TableRows(rows=[row(key="seed")]))]
    for i in range(1000):
        ops.append(pb2.PatchOp(widget_id="t", patch_table=pb2.TablePatch(upsert=[row(key=f"k{i:04d}", cells=[value(s=f"row {i}"), value(f=i)])])))
        if i % 100 == 99:
            ops.append(pb2.PatchOp(widget_id="t", set_value=value(strings=pb2.StringList(values=[f"k{i:04d}", "seed"]))))
    # The selection names k0999 and seed; seed deleted and re-added fresh.
    ops.append(pb2.PatchOp(widget_id="t", patch_table=pb2.TablePatch(delete=["seed"], upsert=[row(key="seed", cells=[value(s="again")])])))
    oracle = tables.TableState()
    for op in ops:
        why = (tables.select(oracle, op.set_value, True, "t") if op.WhichOneof("op") == "set_value"
               else tables.apply_op(oracle, op, 2, "t"))
        assert why is None, why
    assert oracle.selection == ["k0999"], oracle.selection
    for opened in (True, False):
        h = bare({"t": ("table", spec), "l": ("label", {"kind": "label", "id": "l"})})
        if opened:
            h._mark_panel_open("p")
        for op in ops:
            h._on_panel_patch(pb2.PanelPatch(panel_id="p", ops=[op]), 64)
        assert len(h.table_transport) == len(ops) and not h.tables and not h.refusals, (opened, len(h.table_transport))
        assert [o for _, o in h.table_transport] == ops, "the transport reordered or merged a table's ops"
        h.hold_table_drain = False
        assert h.drain_tables() == len(ops)
        if not opened:
            h._mark_panel_open("p")
        got = h.tables[("p", "t")]
        where = "open" if opened else "closed until open"
        assert got.keys == oracle.keys and got.selection == oracle.selection, (where, got.keys[:3], got.selection)
        assert not h.refusals and not h.session_faults, (where, h.refusals, h.session_faults)

    # The byte bound: one table's held ops, a little under 1 MiB each.
    columns = [{"id": "name"}]
    h = bare({"t": ("table", {"kind": "table", "id": "t", "columns": columns}),
              "u": ("table", {"kind": "table", "id": "u", "columns": columns})})
    h._mark_panel_open("p")
    chunk = pb2.PatchOp(widget_id="t", patch_table=pb2.TablePatch(upsert=[row(key="k", cells=[value(s="x" * ((1 << 20) - 4096))])]))
    fits = TABLE_HELD_BYTES // chunk.ByteSize()
    for _ in range(fits):
        h._on_panel_patch(pb2.PanelPatch(panel_id="p", ops=[chunk]), 1 << 20)
    other = pb2.PatchOp(widget_id="u", set_table=pb2.TableRows(rows=[row(key="u")]))
    h._on_panel_patch(pb2.PanelPatch(panel_id="p", ops=[other]), 64)
    assert not h.session_faults and len(h.table_transport) == fits + 1, (h.session_faults, len(h.table_transport))
    h._on_panel_patch(pb2.PanelPatch(panel_id="p", ops=[chunk]), 1 << 20)
    assert len(h.session_faults) == 1, h.session_faults
    fault = h.session_faults[0]
    assert "table widget 't' of panel 'p' holds 64 MiB of ops" in fault and "the next a patch_table" in fault, fault
    return (f"{len(ops)} table ops held (past 256) and drained in order, open and closed: the oracle's rows and selection, "
            f"nothing refused; {fits} ops of {chunk.ByteSize()} bytes held, the next one faults the session naming 't' and patch_table")


def check_replace_all_namespaces() -> str:
    """§8.8: `replace_all` replaces the request's own namespace only.  A
    `ContributeRois` with it removes the plugin's contributed regions absent
    from the batch and never its compiled labels (`label:<n>`); checked on a
    bare host, no plugin."""
    import queue

    from vizi_plugin_sdk import roi_pb2

    h = Host.__new__(Host)
    h.plugin_id, h.contributions, h.region_types, h.pose_domains = "x", queue.Queue(), {}, {}
    h._region_seq, h.sent = 0, []
    h.send = h.sent.append
    h._roi_refusal = lambda roi: None
    h._cancel_drags_of = lambda region_id, why: None
    origin = "plugin:x"
    h.rois = {
        "r100": pb2.RoiRecord(region_id="r100", origin=origin, contrib_id="label:3", name="label 3"),
        "r101": pb2.RoiRecord(region_id="r101", origin=origin, contrib_id="old", name="old"),
        "r102": pb2.RoiRecord(region_id="r102", origin="user", name="the person's"),
    }
    h.masks = {"r100": object()}
    h._on_contribute("q1", pb2.ContributeRois(replace_all=True, rois=[pb2.RoiContribution(contrib_id="nucleus", roi=roi_pb2.Roi(type_name="box"), name="nucleus")]))
    kept = sorted((r.contrib_id or r.origin) for r in h.rois.values())
    assert kept == ["label:3", "nucleus", "user"], kept
    assert "r100" in h.masks and [m.WhichOneof("msg") for m in h.sent] == ["rois_contributed"], h.sent
    return "replace_all removed the omitted contribution 'old', kept the compiled label:3 and the person's region"


def check_folder_names() -> str:
    """§2.3's folder names, exactly as the host derives them: the published
    table, plus ids that must NOT change (all lower case -- an
    upper-case letter is derived)."""
    wrong = [(i, plugin_folder_name(i), want) for i, want in FOLDER_TABLE if plugin_folder_name(i) != want]
    kept = [i for i in ("console", "nullable", "com10", "org.aux.plugin") if plugin_folder_name(i) != i]
    assert not wrong and not kept, (wrong, kept)
    return f"all {len(FOLDER_TABLE)} rows of §2.3's table, and 4 safe ids kept"


def check_manifest_refused() -> str:
    """`manifest_schema.md` §3.5: a manifest whose overlays nothing
    can start is refused before anything launches, as vizi refuses it at scan.
    The same manifest with a panel is accepted.  §3.3.1: an integer slider
    whose step is not whole is refused at load, through the SDK's parser;
    so, §3.5, is `segment_mib: true`."""
    manifest = {
        "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1",
        "engines": {"vizi": "1.0"}, "entry": {"command": ["python3"]},
        "contributions": {
            "overlays": [{"id": "labels", "chunked": True, "time": "static"}],
            "commands": [{"id": "c", "title": "C", "menu": None}],
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "vizi-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        try:
            Host(Path(tmp), DATASET)
        except ManifestError as exc:
            assert "nothing declared here starts it" in str(exc), exc
        else:
            raise AssertionError("an overlay nothing can start was launched")
        manifest["contributions"]["panels"] = [{"id": "p", "title": "X", "root": {"kind": "label", "id": "l", "text": "X"}}]
        (Path(tmp) / "vizi-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        Host(Path(tmp), DATASET)._tempdir.cleanup()
        # §3.3.1: an integer slider's step is whole, refused at load by the
        # SDK's parser in its exact words; the same step on a float slider is
        # accepted.
        slider = {"kind": "slider", "id": "level", "command": "c", "min": 0, "max": 100, "step": 2.5}
        manifest["contributions"]["panels"] = [{"id": "p", "title": "X", "root": slider}]
        (Path(tmp) / "vizi-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        try:
            Host(Path(tmp), DATASET)
        except ManifestError as exc:
            assert "slider 'level' step 2.5 is not a whole number" in str(exc), exc
        else:
            raise AssertionError("an integer slider with step 2.5 was launched")
        slider["float"] = True
        (Path(tmp) / "vizi-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        Host(Path(tmp), DATASET)._tempdir.cleanup()
        # §3.5: `segment_mib` is a number, and a boolean is not one.
        manifest["contributions"]["overlays"] = [{"id": "labels", "name": "Labels", "segment_mib": True}]
        (Path(tmp) / "vizi-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        try:
            Host(Path(tmp), DATASET)
        except ManifestError as exc:
            assert "overlays[0]: segment_mib must be an int in [1, 512]" in str(exc), exc
        else:
            raise AssertionError("an overlay with segment_mib true was launched")
    return ("overlays with nothing to start them: refused before launch; with a panel: accepted; "
            "an integer slider with step 2.5: refused at load; the same slider float: accepted; "
            "segment_mib true: refused at load")


def check_host_rules() -> str:
    """The emulator's own copies of two host rules, checked without a
    plugin: the header fault classes in the
    host's order, and §9's per-field bound on the pose arrays -- 1 048 576
    doubles for ONE pose extension as for a contribution, read from
    the synced SDK's table rather than copied."""
    from vizi_hostsim import RECORD_SIZE, _header_fault, _over_bound, encode_record
    from vizi_plugin_sdk import roi_pb2

    good = bytearray(encode_record(0x44, 1, bytes(100)))
    assert _header_fault(bytes(good)) is None
    for index, value, klass in ((1, 3, pb2.DOORBELL_FAILURE_BAD_VER), (2, 1, pb2.DOORBELL_FAILURE_BAD_FLAGS), (3, 1, pb2.DOORBELL_FAILURE_BAD_RESERVED)):
        bad = bytearray(good)
        bad[index] = value
        assert _header_fault(bytes(bad))[1] == klass, (index, _header_fault(bytes(bad)))
    assert len(good) == RECORD_SIZE
    def extension(frames: int):
        return pb2.PluginMsg(id="x", extend_roi_poses=pb2.ExtendRoiPoses(extensions=[
            pb2.RoiPoseExtension(contrib_id="a", t_start=0, span=frames, pose_stretch=[0.0] * (6 * frames))]))

    old_limit = 10_923  # one past 65 536 / 6, the general repeated-field bound: allowed
    assert _over_bound(extension(old_limit)) is None, _over_bound(extension(old_limit))
    frames = 174_763  # one past 1 048 576 / 6
    over = _over_bound(extension(frames))
    assert over is not None and over[0] == "repeated" and "RoiPoseExtension.pose_stretch" in over[1], over
    roi = roi_pb2.Roi(t_start=0, t_end=frames - 1)
    roi.pose_stretch.extend([0.0] * (6 * frames))
    contrib = pb2.PluginMsg(id="y", contribute_rois=pb2.ContributeRois(rois=[pb2.RoiContribution(contrib_id="a", roi=roi)]))
    over = _over_bound(contrib)
    assert over is not None and "Roi.pose_stretch" in over[1], over
    return f"ver/flags/reserved classed in order; {old_limit} stretch frames accepted in one extension, {frames} refused in an extension and in a contribution alike"


def check_id_rules() -> str:
    """The emulator's id rules, checked without a plugin on a bare reader:
    a first message that is not `Ready` faults the session (the unknown-kind
    rule applies only after the handshake); an unknown kind is never a reply,
    so under the id of a request in flight it faults the session, and under
    any other id it is answered UNSUPPORTED; a known reply of a kind its
    request does not take faults the session, and so does a `CommandDone`
    whose `command_id` is not the invoked command's; a byte-identical re-send is
    dropped; an id reused with different content faults the session."""
    import queue

    from vizi_hostsim import _IdLedger

    def bare():
        h = Host.__new__(Host)
        h.ready, h.session_faults, h.deduplicated, h.unknown_kinds = None, [], [], []
        h.diagnostics, h.unhandled, h.subscriptions, h._waiters, h._asked = [], [], set(), {}, {}
        h._invoked = {}
        h._lock, h._ids, h.sent, h.shutdowns = threading.Lock(), _IdLedger(), [], []
        h.protocol, h.handshake_refusal = (1, 0), None  # §3.1, as Host() sets them
        h.theme = None  # a host that sends no appearance, so `sent` holds only replies
        h.send = h.sent.append
        h.shutdown = lambda **kw: h.shutdowns.append(kw)
        return h

    def settle(h):
        deadline = time.monotonic() + 2
        while h.session_faults and not h.shutdowns and time.monotonic() < deadline:
            time.sleep(0.01)

    unknown = pb2.PluginMsg.FromString(pb2.PluginMsg(id="q1").SerializeToString() + b"\xba\x3e\x00")
    ready = pb2.PluginMsg(id="Ready; I am selfcheck", ready=pb2.Ready(protocol_major=1, plugin_id="x"))
    for first in (unknown, pb2.PluginMsg(id="q1", get_view_state=pb2.GetViewState())):
        h = bare()
        h._on_message(first)
        settle(h)
        assert h.session_faults and "not Ready" in h.session_faults[0], h.session_faults
        assert not h.sent and h.shutdowns[0]["reason"] == pb2.SHUTDOWN_REASON_SESSION_FAULT, (h.sent, h.shutdowns)

    # An unknown kind under the id of the host's own request in flight is not
    # that request's reply (§4 rule 8): the session faults, the waiter gets
    # Error{PROTOCOL}, and nothing is answered UNSUPPORTED.
    h = bare()
    h._on_message(ready)
    waiter = queue.Queue()
    h._waiters["r3"], h._asked["r3"] = waiter, "invoke_command"
    h._on_message(pb2.PluginMsg.FromString(pb2.PluginMsg(id="r3").SerializeToString() + b"\xba\x3e\x00"))
    settle(h)
    fault = h.session_faults[0] if h.session_faults else ""
    assert "'r3'" in fault and "invoke_command" in fault, h.session_faults
    got = waiter.get_nowait()
    assert got.WhichOneof("msg") == "error" and got.error.kind == pb2.Error.PROTOCOL and "r3" not in h._waiters, got
    assert not h.sent and h.shutdowns and h.shutdowns[0]["reason"] == pb2.SHUTDOWN_REASON_SESSION_FAULT, (h.sent, h.shutdowns)

    # Under an id that names nothing in flight it is answered UNSUPPORTED and
    # the session continues; a request in flight beside it is untouched.
    h = bare()
    h._on_message(ready)
    waiter = queue.Queue()
    h._waiters["r4"] = waiter
    h._on_message(pb2.PluginMsg.FromString(pb2.PluginMsg(id="q3").SerializeToString() + b"\xba\x3e\x00"))
    assert [(m.id, m.host_error.kind) for m in h.sent] == [("q3", pb2.Error.UNSUPPORTED)] and not h.session_faults, (h.sent, h.session_faults)
    assert waiter.empty() and "r4" in h._waiters
    done = pb2.PluginMsg(id="r4", command_done=pb2.CommandDone(ok=True))
    h._on_message(done)
    assert waiter.get_nowait() == done and "r4" not in h._waiters
    h._on_message(done)
    assert h.deduplicated == [("r4", "command_done")] and waiter.empty() and not h.unhandled, (h.deduplicated, h.unhandled)
    h._on_message(pb2.PluginMsg(id="r4", command_done=pb2.CommandDone(ok=False)))
    settle(h)
    assert h.session_faults and "'r4'" in h.session_faults[0], h.session_faults

    # The ledger keeps the ids of messages that deserve a reply apart from the
    # ids of replies, so an unknown kind under an id and a reply under the
    # same id are never one id reused.
    split = _IdLedger()
    unknown_r9 = pb2.PluginMsg.FromString(pb2.PluginMsg(id="r9").SerializeToString() + b"\xba\x3e\x00")
    assert [split.observe(unknown_r9, None), split.observe(pb2.PluginMsg(id="r9", command_done=pb2.CommandDone(ok=True)), "command_done")] == ["new", "new"]
    assert "r9" in split.requests and "r9" in split.replies

    # A pending request's id belongs to its reply alone: the kinds its request
    # takes, or Error, reach the waiter; any other known reply faults.
    h = bare()
    h._on_message(ready)
    for rid, asked, reply in (
        ("r5", "open_reader", pb2.PluginMsg(id="r5", series_list=pb2.SeriesList())),
        ("r6", "read_plane", pb2.PluginMsg(id="r6", error=pb2.Error(kind=pb2.Error.NOT_FOUND))),
    ):
        waiter = queue.Queue()
        h._waiters[rid], h._asked[rid] = waiter, asked
        h._on_message(reply)
        assert waiter.get_nowait() == reply and rid not in h._waiters, rid
    waiter = queue.Queue()
    h._waiters["r7"], h._asked["r7"] = waiter, "read_plane"
    h._on_message(pb2.PluginMsg(id="r7", command_done=pb2.CommandDone(ok=True)))
    settle(h)
    fault = h.session_faults[0] if h.session_faults else ""
    assert all(name in fault for name in ("'r7'", "read_plane", "command_done")), h.session_faults
    got = waiter.get_nowait()
    assert got.WhichOneof("msg") == "error" and got.error.kind == pb2.Error.PROTOCOL, got
    assert h.shutdowns and h.shutdowns[0]["reason"] == pb2.SHUTDOWN_REASON_SESSION_FAULT, h.shutdowns

    # §8.3: a CommandDone echoes the invoked command's id, on
    # success, on failure and on an argument-validation failure alike; one
    # without it, or naming another command, faults the session.
    for rid, named in (("r8", ""), ("r9", "fixture.other")):
        h = bare()
        h._on_message(ready)
        good, waiter = queue.Queue(), queue.Queue()
        h._waiters["r10"], h._asked["r10"], h._invoked["r10"] = good, "invoke_command", "fixture.set"
        failed = pb2.PluginMsg(id="r10", command_done=pb2.CommandDone(command_id="fixture.set", ok=False, message="value: missing"))
        h._on_message(failed)
        assert good.get_nowait() == failed and not h.session_faults, h.session_faults
        h._waiters[rid], h._asked[rid], h._invoked[rid] = waiter, "invoke_command", "fixture.set"
        h._on_message(pb2.PluginMsg(id=rid, command_done=pb2.CommandDone(command_id=named, ok=False, message="value: missing")))
        settle(h)
        fault = h.session_faults[0] if h.session_faults else ""
        assert f"{rid!r}" in fault and "'fixture.set'" in fault and (named or "without command_id") in fault, h.session_faults
        got = waiter.get_nowait()
        assert got.WhichOneof("msg") == "error" and got.error.kind == pb2.Error.PROTOCOL, got
        assert h.shutdowns and h.shutdowns[0]["reason"] == pb2.SHUTDOWN_REASON_SESSION_FAULT, h.shutdowns

    ledger = _IdLedger()
    request = pb2.PluginMsg(id="q7", get_view_state=pb2.GetViewState())
    seen = [ledger.observe(request, "get_view_state"), ledger.observe(request, "get_view_state"),
            ledger.observe(pb2.PluginMsg(id="q7", get_rois=pb2.GetRois()), "get_rois")]
    assert seen == ["new", "resent", "reused"], seen
    window = _IdLedger()
    for i in range(_IdLedger.WINDOW + 1):
        assert window.observe(pb2.PluginMsg(id=f"q{i}", get_view_state=pb2.GetViewState()), "get_view_state") == "new"
    assert len(window.requests) == _IdLedger.WINDOW
    assert window.observe(pb2.PluginMsg(id="q0", get_rois=pb2.GetRois()), "get_rois") == "new", "older than the window"
    assert window.observe(pb2.PluginMsg(id="q2", get_rois=pb2.GetRois()), "get_rois") == "reused"
    return "the last 65,536 ids remembered; a first message other than Ready faults; an unknown kind under a waiter's id faults, elsewhere is refused UNSUPPORTED; request and reply ids kept apart; a reply of a kind its request does not take faults, as does a CommandDone without the invoked command_id or naming another; a re-send dropped, a reuse faults naming the id"


def check_hang_up() -> str:
    """`doorbell_protocol.md` §10's `peer_closed` row, §8.13: a
    plugin that hangs up its doorbell with no fault gets
    `DoorbellShutdown{peer_closed}` -- no `failure_class` -- then, after the
    window, `Shutdown{session_fault}` naming the hang-up.
    Its own session, since it ends one; Linux only (a
    `DGRAM` peer's close is not observable on macOS).  The settings folder
    this session never asked for is never created."""
    host = Host(FIXTURE, DATASET)
    with host:
        host.check_handshake()
        done = host.invoke("fixture.hang_up")
        assert done.ok and done.message == "hung up", done.message
        assert host.stream_ended.wait(DOORBELL_WINDOW_MS / 1000.0 + SHUTDOWN_WINDOW_S + 2.0), "the session did not end"
        notice = host.doorbell_shutdowns[0]
        assert notice.reason == pb2.DOORBELL_SHUTDOWN_PEER_CLOSED, pb2.DoorbellShutdownReason.Name(notice.reason)
        assert not notice.HasField("failure_class"), notice
        assert host.doorbell_end_reason == pb2.DOORBELL_SHUTDOWN_PEER_CLOSED and "hung up" in host.doorbell_end, host.doorbell_end
        assert not host.plugin_doorbell_faults and not host.unhandled, (host.plugin_doorbell_faults, host.unhandled)
        assert not host.settings_dir.exists(), "a settings folder nobody asked for was created"
    return f"DoorbellShutdown{{peer_closed}}, no failure_class, then Shutdown{{session_fault}} ({host.doorbell_end!r})"


def check_stop_plugin() -> str:
    """§8.16: work that does not answer a cancel is stopped with
    `Shutdown{USER_STOPPED}` -- vizi offers *Stop plugin* after 10 s; the
    person here is the scenario, which does not wait that long.  Its own
    session, since it ends one."""
    host = Host(FIXTURE, DATASET)
    with host:
        host.check_handshake()
        pending = host.invoke_async("fixture.long_ignores")
        host.await_work(("request", pending.id), lambda w: w.text == "not listening")
        host.cancel_work(request_id=pending.id)
        time.sleep(0.5)
        assert pending.id in host._waiters, "the ignored cancel was answered"
        started = time.monotonic()
        assert host.stop_plugin(deadline_ms=1000), "the stream did not end within the window after USER_STOPPED"
        took = time.monotonic() - started
    return f"an unanswered cancel, then Shutdown{{user_stopped}}: the stream ended {took:.1f} s later"


def check_tool_causes() -> str:
    """§8.15: a FIRE of a tool that declares `view_targets` is a
    cause, keyed `(tool, interaction)` and judged as a panel's cause is.  The
    person's switch is the first row of the plugin's panel and counts only
    while that panel is open: NOT_ENABLED before `aim` opens and with the
    switch off, APPLIED otherwise.  `look_at` moves the camera and becomes the
    orbit point; a second answer to a spent FIRE is CONSUMED.  And the
    manifest rule: a tool with `view_targets` and no panel with them is
    rejected, in vizi's words."""
    host = Host(HERE / "fixture_tool", DATASET)
    start_t = DATASET.t
    with host:
        host.check_handshake()
        handle = host.arrange("aim")
        serial = iter(range(1, 100))

        def fire(**ask):
            n = next(serial)
            assert host.invoke("aim.next", n=n, **ask).ok
            interaction = host.fire(handle, VIEW_2D, None)
            assert (handle, interaction) in host._tool_causes, (handle, interaction)
            return json.loads(host.invoke("aim.got", n=n).message)

        assert fire(how="t", t=2) == ["REFUSED", "NOT_ENABLED"], "the panel is not open, so its switch enables nothing"
        host.open_panel("aim")
        assert fire(how="t", t=2) == ["REFUSED", "NOT_ENABLED"], "the switch is off at every open"
        host.set_view_toggle("aim", True)
        assert fire(how="t", t=2) == ["APPLIED", "NONE"] and DATASET.t == 2, DATASET.t
        assert fire(how="look") == ["APPLIED", "NONE"]
        assert list(host.camera["look_at"].target_zyx) == [1.0, 2.0, 3.0], host.camera
        assert list(host.camera["orbit_point"].zyx) == [1.0, 2.0, 3.0], host.camera
        assert fire(how="decline") == ["DECLINED", "NONE"]
        host.view_toggle = False
        assert fire(how="t", t=1) == ["REFUSED", "NOT_ENABLED"]
        host.view_toggle = True
        assert not host.unhandled and not host.violations, (host.unhandled, host.violations)
    DATASET.t = start_t
    manifest = json.loads((HERE / "fixture_tool" / "vizi-plugin.json").read_text())
    del manifest["contributions"]["panels"][0]["view_targets"]
    with tempfile.TemporaryDirectory() as bad:
        (Path(bad) / "vizi-plugin.json").write_text(json.dumps(manifest))
        try:
            Host(Path(bad), DATASET)
        except ManifestError as exc:
            want = "tool 'aim' declares view_targets but no panel does; the move-the-view row lives on the plugin's panel"
            assert want in str(exc), str(exc)
        else:
            raise AssertionError("a tool's view_targets without a panel's was accepted")
    return "NOT_ENABLED unopened and switched off; t and look_at APPLIED (orbit point follows); a decline; the manifest rule"


def check_region_rules() -> str:
    """§8.8: what a contributed region may be, checked
    without a plugin -- each refusal names its rule, and the shapes the
    rules admit are admitted."""
    from vizi_hostsim import regions
    from vizi_plugin_sdk import roi_pb2 as r

    corners = [(10, 10, 10), (10, 30, 12), (28, 18, 14), (16, 20, 32)]
    centre = tuple(np.mean(corners, axis=0))
    ident = [1.0, 0, 0, 0, 1, 0, 0, 0, 1]

    def v(p, **kw):
        return r.Vertex(position=r.Vec3(z=p[0], y=p[1], x=p[2]), **kw)

    def star(points, root_at=centre) -> "r.Roi":
        return r.Roi(type_name="blob", root=len(points), auto_surface=r.AUTO_SURFACE_STAR,
                     vertices=[v(p, enclosing=True) for p in points] + [v(root_at)])

    tris = [r.Facet(vertices=f) for f in ([0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3])]

    def compiled(coeff: float) -> "r.Roi":
        """One enclosing control behind a compile operator whose second
        row is ``coeff`` x it: exposed only when ``coeff`` is exactly 1."""
        roi = r.Roi(type_name="x", vertices=[v(centre, enclosing=True)])
        roi.compile.num_display = 2
        roi.compile.controls.append(0)
        roi.compile.triplets.add(display_vertex=0, control=0, coeff=0.5)
        roi.compile.triplets.add(display_vertex=1, control=0, coeff=coeff)
        return roi
    cases = [
        (r.Roi(type_name="vizi.thing", vertices=[v(centre)]), "reserved"),
        (r.Roi(type_name="x", vertices=[v(centre)], inclusion=r.Inclusion(mode=r.INCLUDE_SHELL)), "INCLUDE_SHELL"),
        (r.Roi(type_name="x", vertices=[v(centre)], inclusion=r.Inclusion(mode=r.INCLUDE_POINTS)), "INCLUDE_POINTS"),
        (r.Roi(type_name="x", vertices=[v(p) for p in corners], facets=tris[:3]), "not closed"),
        (star(corners[:3]), "at least 4 points"),
        (star(corners, root_at=corners[0]), "coincides with the root"),
        (star(corners + [tuple(2 * np.subtract(corners[0], centre) + centre)]), "one ray"),
        (star(corners, root_at=(0, 0, 0)), "strictly surround"),
        (r.Roi(type_name="tetra", vertices=[v(centre, direction=r.Vec3(z=1), direction_handle=r.DirectionHandle())]), "DirectionHandle"),
        (r.Roi(type_name="x", vertices=[v(centre, handle=r.HandleSpec(frame=[1.0]))]), "9 values"),
        (r.Roi(type_name="x", vertices=[v(centre)], root=3), "out of range"),
        (r.Roi(type_name="x", vertices=[v(centre)], views=[r.CameraView()]), "viewpoints"),
        # Rule 0: an enclosing control no compile row exposes.
        (compiled(0.5), "rule 0"),
    ]
    for roi, want in cases:
        why = regions.contribution_refusal(roi, {"tetra"})
        assert why is not None and want in why, (want, why)
    admitted = [
        r.Roi(type_name="x", vertices=[v(p) for p in corners], facets=tris),
        star(corners),
        r.Roi(type_name="x", vertices=[v(p) for p in corners], facets=tris[:3], inclusion=r.Inclusion(mode=r.INCLUDE_INTERSECTED)),
        r.Roi(type_name="x", vertices=[v(centre, enclosing=True, handle=r.HandleSpec(frame=ident))]),
        r.Roi(type_name="box", vertices=[v((0, 0, 0)), v((1, 1, 1))]),
        compiled(1.0),
        # A side (a, a) of one repeated vertex is no side.
        r.Roi(type_name="x", vertices=[v(p) for p in corners], facets=[r.Facet(vertices=[0, 1, 1, 2]), *tris[1:]]),
    ]
    for roi in admitted:
        assert regions.contribution_refusal(roi, {"tetra"}) is None, (roi, regions.contribution_refusal(roi, {"tetra"}))
    assert len(regions.star_triangles(star(corners))) == 4
    assert regions.exposing_rows(compiled(1.0)) == {0: 1}
    # The surface rule an asked shape is held to -- ENCLOSED only.
    three = r.Roi(type_name="x", vertices=[v(p, enclosing=True) for p in corners[:3]])
    assert "fewer than 4" in (regions.surface_problem(three) or ""), regions.surface_problem(three)
    three.inclusion.mode = r.INCLUDE_INTERSECTED
    assert regions.surface_problem(three) is None
    assert "rule 0" in (regions.surface_problem(compiled(0.5)) or "")
    return f"{len(cases)} refusals, each naming its rule; {len(admitted)} admitted (a closed surface, STAR, an open INTERSECTED sheet, a handle, a box, a compiled control its row exposes, a facet with a repeated vertex)"


def check_shape_refused() -> str:
    """§8.8: an asked shape that leaves its region no inside is
    a bad shape.  The fixture's `fixture.blob` is STAR about its root, which
    is a handle its shaper puts wherever it is dragged: a small move is
    shown and committed; a move out of the corners is `shape_refused` as a
    proposal (the last shape stays) and `REVERTED` as the answer, each
    naming the rule.  Its own session, since it provokes violations."""
    from vizi_hostsim import regions

    host = Host(FIXTURE, DATASET)
    with host:
        host.check_handshake()
        done = host.invoke("fixture.blob")
        assert done.ok, done.message
        region_id = next(rid for rid, rec in host.rois.items() if rec.roi.type_name == "fixture.blob")
        rest = regions.positions(host.rois[region_id].roi)
        inside, outside = rest[4] + (0.5, 0.5, 0.5), rest[4] + (0, 0, 60)

        drag = host.begin_shape_drag(region_id, 4)
        drag.move(inside)
        assert np.allclose(drag.await_proposal()[4], inside), drag.on_screen
        ended = drag.release()
        assert ended.outcome == pb2.ShapeEditEnded.COMMITTED, ended
        moved = regions.positions(host.rois[region_id].roi)
        assert np.allclose(moved[4], inside), moved

        drag = host.begin_shape_drag(region_id, 4)
        drag.move(outside)
        refused = wait_until(lambda: [d for s, d in host.refusals if s == "shape_refused"])
        assert "no inside" in refused[0] and "STAR" in refused[0], refused
        assert drag.on_screen_seq == 0 and np.allclose(drag.on_screen, moved), "the refused shape was shown"
        ended = drag.release()
        assert ended.outcome == pb2.ShapeEditEnded.REVERTED and "no inside" in ended.detail and "STAR" in ended.detail, ended
        assert np.allclose(regions.positions(host.rois[region_id].roi), moved), "the refused shape was committed"
        sites = [s for s, _ in host.refusals]
        assert sites == ["shape_refused", "shape_refused"], host.refusals
    return f"inside: COMMITTED; outside: proposal refused, answer REVERTED ({ended.detail!r})"


def check_launch_budget() -> str:
    """§9: one launch budget from spawn to `Ready`, host
    policy -- 60 s, or the manifest's `entry.startup_s` held to 10-300 s.
    A copy of the fixture declaring `startup_s` gets its budget and starts
    late inside it; with a short budget (vizi's explicit override, since 10 s
    is the least a manifest gets) the same late start is killed."""
    from vizi_hostsim import ENV_INIT_TIMEOUT, LAUNCH_BUDGET_S, launch_budget

    saved = os.environ.pop(ENV_INIT_TIMEOUT, None)
    try:
        assert launch_budget(None) == LAUNCH_BUDGET_S == 60.0
        assert [launch_budget(v) for v in (2, 12, 120, 1e6)] == [10.0, 12.0, 120.0, 300.0]
        assert launch_budget(120, timeout=1.5) == 1.5
    finally:
        if saved is not None:
            os.environ[ENV_INIT_TIMEOUT] = saved
    with tempfile.TemporaryDirectory(prefix="hostsim-startup-") as tmp:
        manifest = json.loads((FIXTURE / "vizi-plugin.json").read_text(encoding="utf-8"))
        # `.venv/bin/python` relative, as the fixture's own manifest names it: the
        # copy has none, so the emulator substitutes its interpreter (CI's too).
        manifest["entry"] = {"command": [".venv/bin/python", str(FIXTURE / "plugin.py")], "startup_s": 12}
        (Path(tmp) / "vizi-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        os.environ["VIZI_FIXTURE_START_DELAY_S"] = "1.5"
        try:
            slow = Host(Path(tmp), DATASET)
            assert slow.launch_budget == 12.0, slow.launch_budget
            with slow:
                slow.check_handshake()
            short = Host(Path(tmp), DATASET, launch_timeout=0.5)
            started = time.monotonic()
            try:
                with short:
                    short.check_handshake()
            except RuntimeError as exc:
                why = str(exc)
            else:
                raise AssertionError("a start past its budget was not refused")
            took = time.monotonic() - started
            assert "within 0.5s" in why and took < 1.4, (why, took)
            assert short._plugin.poll() is not None, "the plugin past its budget was not killed"
        finally:
            os.environ.pop("VIZI_FIXTURE_START_DELAY_S", None)
    return f"60 s, startup_s held to 10-300 s; startup_s 12 -> a 1.5 s start passes; a 0.5 s budget kills it ({why!r})"


def check_raw_and_pitch() -> str:
    """With a filter shared, a read is its output and names it,
    and a `raw` read is the source with no `filter`; a partly
    recorded file's `ViewState` pitch is per axis, NaN where unrecorded,
    `FILE`, while its `Geometry` stays the finite display fallback.
    Its own session, on its own dataset."""
    import dataclasses

    shared = pb2.AppliedFilter(plugin_id="org.example.other", plugin_version="1.0", stage_id="double", channels=[0])
    dataset = dataclasses.replace(DATASET, applied_filter=shared, filter_fn=lambda v, t, c: v * 2, spacing_zyx=(None, 0.3208, 0.3208))
    state = dataset.view_state()
    assert np.isnan(state.spacing_zyx[0]) and list(state.spacing_zyx[1:]) == [0.3208, 0.3208], list(state.spacing_zyx)
    assert state.spacing_unit == "um" and state.spacing_provenance == pb2.SPACING_PROVENANCE_FILE, state
    geometry = dataset.geometry()
    assert (geometry.spacing_z, geometry.spacing_y, geometry.spacing_x) == (1.0, 1.0, 1.0), geometry
    assert geometry.spacing_unit == "" and geometry.spacing_provenance == pb2.SPACING_PROVENANCE_DISPLAY_FALLBACK, geometry
    for spacing in (None, (2.0, 0.5, 0.5)):  # none and every axis: as before, never NaN
        whole = dataclasses.replace(DATASET, spacing_zyx=spacing).view_state()
        assert all(np.isfinite(whole.spacing_zyx)), (spacing, list(whole.spacing_zyx))
    host = Host(FIXTURE, dataset)
    with host:
        host.check_handshake()
        done = host.invoke("fixture.raw_read")
        assert done.ok, done.message
        got = json.loads(done.message)
        source = float(ramp(0, 0).astype(np.float64).sum())
        assert got["shared"] == [2 * source, "double"] and got["raw"] == [source, None], got
        assert got["view_filter"] == "double", got
        assert got["known"] == [False, True, True] and got["measured"] == [None, 0.3208, 0.3208] and got["unit"] == "um", got
        assert [r.raw for r in host.read_requests] == [False, True], host.read_requests
    return f"shared read doubled and named; raw read the source, no filter; pitch {got['measured']} um, Geometry the fallback"


def check_protocol_minors() -> str:
    """§3.1's version gating, with this emulator presenting minors 0 and 1:
    the plugin's `Ready` minor never exceeds the host's `Hello` minor (one
    above it is refused with the host's sentence; one below it is the plugin
    clamping and runs), and `engines.vizi` is the floor -- a manifest asking
    for a newer host than this one is refused before launch."""
    from vizi_hostsim import ENV_PROTOCOL_MINOR, ready_refusal

    sessions = []
    for minor in (0, 1):
        host = Host(FIXTURE, DATASET, protocol_minor=minor)
        with host:
            host.check_handshake()
            assert host.ready.protocol_minor <= minor, (minor, host.ready)
            assert host.handshake_refusal is None, host.handshake_refusal
            sessions.append(f"host 1.{minor}: Ready 1.{host.ready.protocol_minor}")
    assert ready_refusal(pb2.Ready(protocol_major=1, protocol_minor=3), (1, 1)) == "this plugin speaks protocol 1.3; this vizi speaks 1.1"
    assert ready_refusal(pb2.Ready(protocol_major=1, protocol_minor=0), (1, 1)) is None
    manifest = json.loads((FIXTURE / "vizi-plugin.json").read_text(encoding="utf-8"))
    manifest["engines"] = {"vizi": "1.1"}
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "vizi-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        try:
            Host(Path(tmp), DATASET, protocol_minor=0)
        except ManifestError as exc:
            assert "needs vizi protocol 1.1; this vizi speaks 1.0" in str(exc), exc
        else:
            raise AssertionError("a manifest above the host's minor was launched")
        newer = Host(Path(tmp), DATASET, protocol_minor=1)
        newer._tempdir.cleanup()
        saved = os.environ.get(ENV_PROTOCOL_MINOR)
        os.environ[ENV_PROTOCOL_MINOR] = "1"
        try:
            from_env = Host(FIXTURE, DATASET)
            from_env._tempdir.cleanup()
        finally:
            if saved is None:
                del os.environ[ENV_PROTOCOL_MINOR]
            else:
                os.environ[ENV_PROTOCOL_MINOR] = saved
        assert newer.protocol == (1, 1) and from_env.protocol == (1, 1), (newer.protocol, from_env.protocol)
    return "; ".join(sessions) + "; Ready above Hello refused; engines 1.1 refused by a 1.0 host, accepted by 1.1"


def wait_until(pred, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while not (got := pred()):
        if time.monotonic() > deadline:
            raise AssertionError(f"not within {timeout:g} s")
        time.sleep(0.02)
    return got


def main() -> int:
    check("record encoder", check_encoder)
    check("plugin folder names (§2.3)", check_folder_names)
    check("slider positions and readout (§3.3.1)", check_slider_rendering)
    check("table cells whose kind is not their column's (§8.4)", check_table_cell_kinds)
    check("table ops open and held reach the state of applying them in order; the PanelPatch bound (§8.4)", check_table_replay)
    check("the transport holds a table's ops in order, bounded by 64 MiB per table (§8.4, §9)", check_table_transport)
    check("manifests refused at load: an overlay nothing can start, an integer slider's non-whole step, segment_mib true", check_manifest_refused)
    check("host rules: header classes, §9 on a pose extension", check_host_rules)
    check("ids: Ready first, an unknown kind never a reply (faults under a pending id), a reply of its request's kind, never reused", check_id_rules)
    check("what a contributed region may be (§8.8)", check_region_rules)
    check("replace_all keeps to its namespace (§8.8)", check_replace_all_namespaces)
    if failures:
        return 1
    host = Host(FIXTURE, DATASET)
    # `G` is not here: `reader_writer_contract.md` §1.1 grants it AFTER
    # `ReaderOpened`, sized from the reply (`test_reader`).
    host.add(Segment.host_ring(V_SEG, "V", 1 << 20, host.directory))
    host.add(Segment.host_ring(X_SEG, "X", 1 << 20, host.directory))
    host.add(Segment.overlay(O_SEG, "labels", 4 << 20, host.directory))
    host.add(Segment.plugin_arena(P_SEG, "P", 1 << 20, host.directory, subject="fixture"))
    host.volume_segment = V_SEG
    host.export_segment = X_SEG
    tmp = tempfile.TemporaryDirectory(prefix="hostsim-selfcheck-")

    with host:
        state: dict = {}

        def test_handshake():
            return host.check_handshake()

        def test_grants():
            """`V`, `O` and `P` at session setup.  `G` follows `ReaderOpened`
            and `X` precedes `OpenExport`, each standalone
            (`reader_writer_contract.md` §1.1, §2.1)."""
            for seg in (V_SEG, O_SEG, P_SEG):
                ack = host.grant(seg)
                assert ack.WhichOneof("outcome") == "installed", (seg, ack)
            # The host grants `P` as it OPENS the panel, and refuses a patch
            # to a panel it has not opened (`panel_patch_refused`); the
            # fixture's commands write its status line from here on.
            host.open_panel("fixture")
            return "V, O(labels), P(fixture) installed; panel opened"

        def test_reader():
            """§1.1: open, THEN grant `G` sized from the reply and wait for the
            installed ack, THEN read; a level other than 0 and an index past
            the declared shape are `OUT_OF_BOUNDS` (§1.5, §1.6)."""
            opened = host.open_reader("/data/a.ramp", "ramp")
            assert tuple(opened.shape_tczyx) == SHAPE and opened.dtype == pb2.DTYPE_U16, opened
            state["handle"] = opened.handle
            host.add(Segment.reader_arena(G_SEG, opened, host.directory))
            ack = host.grant(G_SEG)
            assert ack.WhichOneof("outcome") == "installed", ack
            for kind, call in (
                ("level", lambda: host.read_volume(opened.handle, 0, 0, level=1)),
                ("t", lambda: host.read_volume(opened.handle, 3, 0)),
                ("z", lambda: host.read_plane(opened.handle, 0, 4, 0)),
            ):
                try:
                    call()
                except PluginError as exc:
                    assert exc.kind == pb2.Error.OUT_OF_BOUNDS and kind in exc.args[0], (kind, exc.kind, exc.args)
                else:
                    raise AssertionError(f"an out-of-range {kind} was answered with data")
            got = host.read_volume(opened.handle, 1, 0)
            assert got.ref.segment_id == G_SEG, got.ref
            assert np.array_equal(got.array, ramp(1, 0)), "the deposit is not the volume"
            plane = host.read_plane(opened.handle, 2, 3, 1)
            assert plane.array.shape == (1, 8, 8) and np.array_equal(plane.array[0], ramp(2, 1)[3]), plane.array.shape
            doc = host.metadata(opened.handle)
            assert doc.form == "ome-xml" and "a.ramp" in doc.text, doc
            # The reader was constructed knowing its file's folder, and
            # nobody created it.
            state_dir = host.file_state_for("/data/a.ramp")
            assert f"state={state_dir!r}" in doc.text, (doc.text, state_dir)
            assert not Path(state_dir).exists(), "the per-file folder was created on open"
            released = [r for r in host.loans_released if r[0] == G_SEG]
            assert len(released) == 2, released
            return f"opened {SHAPE}; G granted after the open, sized for one volume; level 1 / t=3 / z=4 refused OUT_OF_BOUNDS; a volume and a plane read out of G and released; metadata {doc.form}"

        def test_reads_advance_and_release():
            """The host releases each loan once it has read it, so the arena
            reuses the space: sequential reads land at the same offset, and
            every one is released -- a plugin that never reused would run its
            arena out."""
            handle = state["handle"]
            before = len(host.loans_released)
            offsets = [host.read_volume(handle, t, 1).ref.offset for t in range(3)]
            assert offsets == [32, 32, 32], offsets
            assert len(host.loans_released) == before + 3
            return "3 deposits, each released and its space reused"

        def test_close_reader():
            """§1.8, a deliberate close: `CloseReader` for the open handle,
            then the host retires its `G`; the close is idempotent and a read
            after it is `NOT_FOUND` naming the handle."""
            handle = state["handle"]
            closed = host.close_reader(handle)
            assert closed.handle == handle
            assert host.close_reader(handle).handle == handle, "a second CloseReader was not answered Closed"
            try:
                host.read_volume(handle, 0, 0)
            except PluginError as exc:
                assert exc.kind == pb2.Error.NOT_FOUND and handle in exc.args[0], (exc.kind, exc.args)
            else:
                raise AssertionError("a read on a closed handle was answered")
            released = host.retire_segment(G2_SEG)
            assert released.segment_id == G2_SEG
            return f"closed twice; a read after close NOT_FOUND {handle!r}; G retired"

        def test_second_open_replaces():
            """§1.2's handshake: the next file's `OpenReader` goes
            while the last reader is open (H1); the plugin opens B, closes A
            and answers a NEW handle (H2); the new `G` is granted superseding
            the old, which retires on the install with `SegmentReleased`
            (H3-H5) -- no `CloseReader`, no `RetireSegment` for A.  Then I3:
            a request naming A's handle is `NOT_FOUND` naming it; §1.8: a
            `CloseReader` for A closes the NAMED handle only, B reads on;
            and the reload exception: reopening B's own URI closes first and
            answers yet another handle."""
            old_handle = state["handle"]
            opened = host.open_reader("/data/b.ramp", "ramp")
            assert tuple(opened.shape_tczyx) == SHAPE, opened
            assert opened.handle != old_handle, f"handle {opened.handle!r} reused across opens (§1.2 I1)"
            ack = host.supersede(G_SEG, Segment.reader_arena(G2_SEG, opened, host.directory))
            assert ack.WhichOneof("outcome") == "installed", ack
            assert G_SEG not in host.segments and host.retired[G_SEG].segment_id == G_SEG
            assert not [d for d in host.diagnostics if d.startswith("supersede:")], host.diagnostics
            got = host.read_volume(opened.handle, 2, 1)
            assert got.ref.segment_id == G2_SEG and np.array_equal(got.array, ramp(2, 1)), got.ref
            doc = host.metadata(opened.handle)
            assert "b.ramp" in doc.text, doc.text
            try:
                host.read_volume(old_handle, 0, 0)
            except PluginError as exc:
                assert exc.kind == pb2.Error.NOT_FOUND and old_handle in exc.args[0], (exc.kind, exc.args)
            else:
                raise AssertionError("a read naming the replaced handle was answered (§1.2 I3)")
            assert host.close_reader(old_handle).handle == old_handle, "CloseReader for a replaced handle is the idempotent Closed"
            assert np.array_equal(host.read_volume(opened.handle, 0, 1).array, ramp(0, 1)), "closing the replaced handle closed the open reader"
            again = host.open_reader("/data/b.ramp", "ramp")
            assert again.handle not in (old_handle, opened.handle), again.handle
            assert "b.ramp" in host.metadata(again.handle).text
            state["handle"] = again.handle
            return f"OpenReader while open: {old_handle} -> {opened.handle}; G superseded and released; read naming {old_handle} NOT_FOUND; CloseReader({old_handle}) idempotent, B reads on; reload -> {again.handle}"

        def test_series():
            """`reader_writer_contract.md` §1.2a: an `OpenReader` with no
            series is answered `SeriesList` for a file of several images, which
            changed nothing, and the second `OpenReader` naming one opens it on
            the instance that listed it; a named open with no listing before it
            (a recent-files reopen) opens fresh; ``""`` is an id; an unknown id
            is `NOT_FOUND`; a one-entry list opens directly; 70 000 entries
            pass the 65 536 bound they are exempt from; a list over the message
            ceiling arrives trimmed and `truncated`; a list answered to a named
            series is refused; listing the open reader's own URI closed it; a
            reader of one image ignores the field; `ViewState.dataset_series`."""
            arena = [None]

            def granted(opened):
                """vizi grants a `G` after every `ReaderOpened` (§1.1), and the
                SDK holds the reader's requests until it is installed."""
                sid = host.new_segment_id()
                seg = Segment.reader_arena(sid, opened, host.directory)
                if arena[0] is None:
                    host.add(seg)
                    ack = host.grant(sid)
                else:
                    ack = host.supersede(arena[0], seg)
                assert ack.WhichOneof("outcome") == "installed", ack
                arena[0] = sid
                return opened

            try:
                host.open_reader("/data/a.stack", "stack")
            except SeriesChoiceRequired as exc:
                choice = exc
            else:
                raise AssertionError("a file of three images opened without a choice")
            ids = [e.id for e in choice.entries]
            assert ids == ["s0", "s1", ""] and not choice.truncated, (ids, choice.truncated)
            first, unnamed = choice.entries[0], choice.entries[2]
            assert tuple(first.shape_tczyx) == (1, 1, 2, 4, 4) and first.dtype == pb2.DTYPE_U8 and first.label == "first", first
            assert not unnamed.shape_tczyx and unnamed.dtype == pb2.DTYPE_UNSPECIFIED, unnamed
            opened = granted(choice.choose("s1"))
            assert tuple(opened.shape_tczyx) == (2, 1, 2, 4, 4), opened
            doc = json.loads(host.metadata(opened.handle).text)
            assert doc["series"] == "s1" and doc["by_listing"], doc
            got = host.read_volume(opened.handle, 1, 0)
            assert got.array.shape == (2, 4, 4) and (got.array == 2).all(), got.array
            fresh = granted(host.open_reader("/data/b.stack", "stack", series=""))
            assert tuple(fresh.shape_tczyx) == (1, 1, 1, 4, 4), fresh
            doc = json.loads(host.metadata(fresh.handle).text)
            assert doc["series"] == "" and not doc["by_listing"], doc
            try:
                host.open_reader("/data/b.stack", "stack", series="s9")
            except PluginError as exc:
                assert exc.kind == pb2.Error.NOT_FOUND and "s9" in exc.args[0], (exc.kind, exc.args)
            else:
                raise AssertionError("an unknown series opened")
            lists = len(host.series_lists)
            only = granted(host.open_reader("/data/one.stack", "stack"))
            assert json.loads(host.metadata(only.handle).text)["series"] == "only" and len(host.series_lists) == lists, "a one-entry list was sent"
            for stem, count, cut in (("many", 70_000, False), ("huge", None, True)):
                try:
                    host.open_reader(f"/data/{stem}.stack", "stack")
                except SeriesChoiceRequired as exc:
                    n = len(exc.entries)
                    assert exc.truncated == cut and (n == count if count else 0 < n < 400), (stem, n, exc.truncated)
                    if stem == "huge":
                        huge = n
                else:
                    raise AssertionError(f"{stem}.stack opened")
            try:
                host.open_reader("/data/defect.stack", "stack", series="a")
            except PluginError as exc:
                assert exc.kind == pb2.Error.INVALID_ARGUMENT and "answered with a list" in exc.args[0], (exc.kind, exc.args)
            else:
                raise AssertionError("a list answered to a named series was accepted")
            held = granted(host.open_reader("/data/c.stack", "stack", series="s0"))
            try:
                host.open_reader("/data/c.stack", "stack")
            except SeriesChoiceRequired:
                pass
            assert held.handle not in host.readers, "a listing of the open reader's URI left it open at the host"
            try:
                host.metadata(held.handle)
            except PluginError as exc:
                assert exc.kind == pb2.Error.NOT_FOUND, (exc.kind, exc.args)
            else:
                raise AssertionError("the reader a same-URI listing closed still answered")
            ramp_opened = granted(host.open_reader("/data/c.ramp", "ramp", series="zz"))
            assert tuple(ramp_opened.shape_tczyx) == SHAPE, ramp_opened
            host.close_reader(ramp_opened.handle)
            # One id listed twice is the sender's defect,
            # refused by the SDK before it is sent ...
            try:
                host.open_reader("/data/dup.stack", "stack")
            except PluginError as exc:
                assert exc.kind == pb2.Error.INVALID_ARGUMENT and "listed twice" in exc.args[0], (exc.kind, exc.args)
            else:
                raise AssertionError("a list naming one id twice was sent")
            # ... and the host refuses what a reader sends anyway: a malformed
            # list fails the open with no choice, more names
            # than channels fails it too.  No SDK sends these, so the
            # host's checks are driven directly.
            E = pb2.SeriesEntry
            for bad, word in (
                (pb2.SeriesList(), "no entries"),
                (pb2.SeriesList(entries=[E(id="a", shape_tczyx=[1, 1, 4, 4])]), "five positive"),
                (pb2.SeriesList(entries=[E(id="a", shape_tczyx=[1, 0, 1, 4, 4])]), "five positive"),
                (pb2.SeriesList(entries=[E(id=""), E(id="b"), E(id="")]), "listed twice"),
            ):
                why = series_list_problem(bad)
                assert why is not None and word in why, (bad, why)
            assert series_list_problem(pb2.SeriesList(entries=[E(id=""), E(id="b", dtype=99)])) is None, "an unknown dtype is shown as unknown, not refused"
            assert "3 channels of 2" in (reader_opened_problem(pb2.ReaderOpened(shape_tczyx=[1, 2, 1, 1, 1], channel_names=["a", "b", "c"])) or "")
            assert reader_opened_problem(pb2.ReaderOpened(shape_tczyx=[1, 2, 1, 1, 1], channel_names=["a"])) is None
            named_opened = granted(host.open_reader("/data/named.stack", "stack"))
            assert list(named_opened.channel_names) == ["GCaMP", ""], ("ReaderOpened.channel_names", named_opened.channel_names)
            host.close_reader(named_opened.handle)
            host.retire_segment(arena[0])
            kept = DATASET.dataset_series, DATASET.channel_names
            try:
                DATASET.channel_names = ("GCaMP",)
                DATASET.dataset_series = "s1"
                named = host.invoke("fixture.series").message
                DATASET.dataset_series = ""
                blank = host.invoke("fixture.series").message
                DATASET.dataset_series = None
                absent = host.invoke("fixture.series").message
            finally:
                DATASET.dataset_series, DATASET.channel_names = kept
            channels = "channels=GCaMP:FILE,Channel 2:PLACEHOLDER"
            assert (named, blank, absent) == (f"series='s1' {channels}", f"series='' {channels}", f"series=None {channels}"), (named, blank, absent)
            return f"listed 3, chose s1 on the listing instance; '' opened fresh; s9 NOT_FOUND; one entry opened directly; 70 000 untrimmed; {huge} of 400 then truncated; a list to a named series INVALID_ARGUMENT; same-URI listing closed the reader; ramp ignored series; a duplicate id refused by the SDK, and malformed lists and too many channel names by the host; ReaderOpened.channel_names carried; dataset_series absent, '' and named told apart on ViewState, with ViewState.channels and their sources"

        def test_exporter():
            """`reader_writer_contract.md` §2.1: `X` granted standalone BEFORE
            `OpenExport`, the host waiting for nothing -- here the `M` half is
            deliberately late, so the first chunk is held until both halves
            land (§8.12); a deposit chunk and an inline one; the plugin
            releases the deposit; finish writes the artifact."""
            host.grant_install(X_SEG)
            path = str(Path(tmp.name) / "out.csv")
            kept_names = DATASET.channel_names
            DATASET.channel_names = ("GFP",)
            try:
                # The SOURCE's document, the artifact's one
                # channel (source channel 1, which the file does not name),
                # and the host's record built at open.
                opened = host.open_export("stats", path, (2, 1, 4, 8, 8), pb2.DTYPE_U16, source_metadata=("ome-xml", "<OME/>"), channels=[1])
            finally:
                DATASET.channel_names = kept_names
            assert X_SEG not in host.acks, "the install half alone must not be acknowledged installed"
            late_m = threading.Timer(0.2, host.grant_capability, args=(X_SEG,))
            late_m.start()
            written = host.export_chunk(opened.handle, 0, [0], [ramp(0, 0)], dtype=pb2.DTYPE_U16)
            assert written.total_bytes == ramp(0, 0).nbytes, written
            ack = host.await_ack(X_SEG)
            assert ack.WhichOneof("outcome") == "installed", ack
            written = host.export_chunk(opened.handle, 1, [0], [ramp(1, 0)], dtype=pb2.DTYPE_U16, inline=True)
            assert written.total_bytes == ramp(1, 0).nbytes
            done = host.finish_export(opened.handle)
            assert done.handle == opened.handle
            deposits = [r for r in host.released if r[0] == X_SEG]
            assert len(deposits) == 1, deposits
            lines = Path(path).read_text().splitlines()
            assert lines[0].startswith("# ome-xml") and len(lines) == 4, lines
            assert lines[0].endswith(f"state={host.file_state_dir}"), ("the export's folder is the SOURCE's", lines[0])
            head, _, record = lines[1].partition(" provenance=")
            assert head == "# channels=1:Channel 2:PLACEHOLDER", ("OpenExport.channels", lines[1])
            record = json.loads(record)
            # vizi's shape: a channel the source does not
            # name is "" in the record, never its "Channel N" placeholder.
            assert record["manifest_version"] == 1 and record["selection"]["channels"] == [1] and record["selection"]["channel_names"] == [""], record
            assert record["source"]["shape_tczyx"] == list(SHAPE) and record["source"]["reader"] == {"kind": "emulated"}, record
            assert set(record["selection"]["crop_physical_xyz"]) == {"x", "y", "z"} and record["processing"]["filters"] == [], record
            assert record["output"] == {"format": f"plugin:{host.plugin_id}:stats", "shape_tczyx": [2, 1, 4, 8, 8], "dtype": record["source"]["dtype"]}, record
            return f"X standalone before OpenExport, chunk 0 held until the late M; one X deposit released; inline chunk; {len(lines) - 2} rows written; source_metadata, channels (a PLACEHOLDER) and the provenance record delivered"

        def test_abort():
            path = str(Path(tmp.name) / "partial.csv")
            opened = host.open_export("stats", path, (1, 1, 4, 8, 8), pb2.DTYPE_U16)
            host.export_chunk(opened.handle, 0, [0], [ramp(0, 0)], dtype=pb2.DTYPE_U16)
            done = host.abort_export(opened.handle)
            assert done.handle == opened.handle and not Path(path).exists(), "the partial artifact survived the abort"
            return "AbortExport answered ExportDone, partial file deleted"

        def test_chunk_cancelled_aborts():
            """§8.6: an export ends early however a chunk ends in
            an `Error`, and the host then sends `AbortExport`, so no partial
            file survives.  The fixture holds a chunk to `cancel-me.csv` until
            the activity row's Cancel reaches it, and answers CANCELLED."""
            path = str(Path(tmp.name) / "cancel-me.csv")
            opened = host.open_export("stats", path, (1, 1, 4, 8, 8), pb2.DTYPE_U16)
            assert Path(path).exists()
            pending = host.export_chunk_async(opened.handle, 0, [0], [ramp(0, 0)], dtype=pb2.DTYPE_U16, inline=True)
            time.sleep(0.2)
            host.cancel_work(request_id=pending.id)
            try:
                pending.result(15.0)
            except PluginError as exc:
                assert exc.kind == pb2.Error.CANCELLED, (pb2.Error.Kind.Name(exc.kind), exc.args)
            else:
                raise AssertionError("the canceled chunk was written")
            handle, kind, outcome = host.export_aborts[-1]
            assert handle == opened.handle and kind == pb2.Error.CANCELLED, host.export_aborts
            assert isinstance(outcome, pb2.ExportDone) and outcome.handle == opened.handle, outcome
            assert not Path(path).exists(), "the partial file survived the canceled chunk"
            return "a chunk answered CANCELLED; the host sent AbortExport (ExportDone); the partial file is gone"

        def test_grant_recovery():
            """§8.12's backstop, as `doorbell_M_memory.md` §7's table says the
            host acts on each answer.  An installed grant answers `installed`
            with its proof.  A grant whose `M` was lost answers `not_present`,
            and the host re-sends the SAME grant -- the `M` byte for byte, same
            doorbell id -- which then installs.  The original arriving late is
            a duplicate, answered again with the same answer, never a
            conflict.  A
            grant with only its install answers `pending` naming that half,
            and the host sends nothing."""
            known = host.query_grant(V_SEG)
            assert known.state == pb2.GRANT_STATE_INSTALLED and known.identity_proof == host.segments[V_SEG].proof, known
            lost = host.add(Segment.host_ring(host.new_segment_id(), "V", 4096, host.directory)).segment_id
            host.grant_capability(lost, lost=True)
            state = host.recover_grant(lost)
            assert state.state == pb2.GRANT_STATE_NOT_PRESENT, state
            ack = host.await_ack(lost)
            assert ack.WhichOneof("outcome") == "installed", ack
            host.resend_capability(lost)  # the "undrained original", late
            again = host.query_grant(lost)
            assert again.state == pb2.GRANT_STATE_INSTALLED and again.identity_proof == host.segments[lost].proof, again
            assert host.acks[lost].WhichOneof("outcome") == "installed", host.acks[lost]
            half = host.add(Segment.host_ring(host.new_segment_id(), "V", 4096, host.directory)).segment_id
            host.grant_install(half)
            state = host.recover_grant(half)
            assert state.state == pb2.GRANT_STATE_PENDING and state.have_install and not state.have_capability, state
            assert host.recoveries[-1][2] == "nothing", host.recoveries
            host.grant_capability(half)  # the M that was only slow
            assert host.await_ack(half).WhichOneof("outcome") == "installed"
            for seg in (lost, half):
                host.retire_segment(seg)
            return "installed answers with its proof; not_present -> the same M re-sent and installed, the late original a duplicate; pending(install) -> nothing sent"

        def test_per_export_x():
            """The `1.0` host's `X`: a fresh arena per export, sized to one
            chunk, granted standalone before `OpenExport` with no ack awaited,
            every chunk at the same offset, retired after the job (legal at
            any time under §9)."""
            volume = ramp(0, 0)
            first = host.grant_export_arena(volume.nbytes)
            path = str(Path(tmp.name) / "per-export.csv")
            opened = host.open_export("stats", path, (2, 1, 4, 8, 8), pb2.DTYPE_U16)
            for t in range(2):
                host.export_chunk(opened.handle, t, [0], [ramp(t, 0)], dtype=pb2.DTYPE_U16)
            host.finish_export(opened.handle)
            host.retire_export_arena()
            second = host.grant_export_arena(volume.nbytes)
            opened = host.open_export("stats", str(Path(tmp.name) / "second.csv"), (1, 1, 4, 8, 8), pb2.DTYPE_U16)
            host.export_chunk(opened.handle, 0, [0], [volume], dtype=pb2.DTYPE_U16)
            host.finish_export(opened.handle)
            host.retire_export_arena()
            assert first != second and first not in host.segments and second not in host.segments
            offsets = {r[2] for r in host.released if r[0] in (first, second)}
            assert offsets <= {32}, offsets
            return f"two jobs, two X arenas ({first & 0xFFFFFFFF}, {second & 0xFFFFFFFF}), each sized to one chunk, chunks at offset 32, both retired"

        def test_v_grows_the_hosts_way():
            """The host's `V`: none until the first read, then sized to that
            deposit, then a new ring at least twice the size naming the old in
            `supersedes`; the plugin reads through each, releases the old ring
            when the new one is installed, and every value is right."""
            host.volume_segment = None  # hand V to the emulator's policy
            done = host.invoke("fixture.reads")
            assert done.ok, done.message
            expected = f"{int(ramp(1, 0)[0, 0, 0])} {int(ramp(1, 0).sum())} {int(ramp(2, 1).sum())}"
            assert done.message == expected, (done.message, expected)
            rings = list(host.v_rings)
            assert len(rings) >= 2, rings
            newest = host.segments[rings[-1]]
            assert newest.supersedes in (rings[-2], 0), "the growth named a ring it did not outgrow"
            for old in rings[:-1]:
                host._await(lambda old=old: host.retired.get(old), 5.0, f"no SegmentReleased for superseded V {old}")
                assert old not in host.segments
            for ring in rings:
                assert host.acks[ring].WhichOneof("outcome") == "installed", host.acks[ring]
            how = "superseding it" if newest.supersedes else "the old one retired once drained"
            return f"{len(rings)} rings, the last {how}; every outgrown ring released; values right"

        def test_v_under_concurrent_reads():
            """Four reads in flight at once into the host's `V`: a reply into
            a ring whose installation already rode an earlier reply goes BARE
            and the plugin must hold it, and the ring can grow while deposits
            in the old one are lent.  The host and this
            emulator both do what the spec says, so a plugin that fails
            here fails the spec and vizi."""
            before, bare = len(host.v_rings), host.bare_replies
            done = host.invoke("fixture.reads_together")
            assert done.ok, done.message
            expected = " ".join(f"{t}{c}:{int(ramp(t, c).sum())}" for t in range(2) for c in range(2))
            assert done.message == expected, (done.message, expected)
            rings = list(host.v_rings)
            for old in rings[:-1]:
                host._await(lambda old=old: host.retired.get(old), 5.0, f"no SegmentReleased for superseded V {old}")
            return f"4 concurrent reads, all values right; {len(rings) - before} more ring(s) granted, {host.bare_replies - bare} reply(ies) bare into a pending ring, {len(host.v_retired_on_drain)} outgrown ring(s) retired once drained"

        def test_compile_and_masks():
            """The push and the compile naming ONE reservation with ``uses=2``
            (§5.6): released once, after the second disposition.  Three masks
            -- label 256 wraps onto LUT row 1 (§8.8's rule) with its own name
            -- the rois, and a mask read back out of the V ring."""
            done = host.invoke("fixture.compile")
            assert done.ok, done.message
            assert "compiled 3" in done.message, done.message
            assert "('label:1', 'nucleus'), ('label:2', 'membrane'), ('label:256', 'wrapped')" in done.message, done.message
            assert "mask label:1: 4 voxels of (1, 2, 2) at (1, 2, 3)" in done.message, done.message
            pushes = [p for p in host.pushes if p.overlay_id == "labels"]
            assert pushes and len(pushes[-1].loans) == 2, "the display push named two loans (labels, LUT)"
            assert all(loan.uses == 2 for loan in pushes[-1].loans), "the push declared uses=2 on both"
            assert len(host.compiles) == 1
            by_label = {m.label: m for m in host.masks.values()}
            assert sorted(by_label) == [1, 2, 256]
            assert by_label[256].color == by_label[1].color, "label 256 draws with row 1, never row 0"
            assert by_label[256].name == "wrapped" and by_label[1].name == "nucleus", "names are keyed by id, not row"
            o_released = [r for r in host.loans_released if r[0] == O_SEG]
            assert len(o_released) == 2, "one release per reservation (labels, LUT), at the second use"
            assert not host.loan_uses, "no reservation is still pending a use"
            assert not host.diagnostics, host.diagnostics
            mask_reads = [r for r in host.released if r[0] == V_SEG]
            assert mask_reads, "the mask deposit was not released"
            return f"{done.message}; 2 O loans released once each, after both uses; mask deposit released"

        def test_chunked_overlay():
            """§5.7: the chunked overlay's arena is granted when a dataset is
            open, HOST-SIZED from it (one level-0 u16 volume plus one at the
            first coarser level); the host names the volumes it wants and
            the plugin's `@chunk_source` answers each once, in the host's
            order, segment-backed into that arena; the loans come back in
            the tick that applies them."""
            seg = host.add(Segment.chunk_arena(C_SEG, "chunks", DATASET, host.directory))
            assert seg.arena.extent == Segment.chunk_arena_bytes(DATASET) == 2 * 4 * 8 * 8 + 2 * 2 * 4 * 4, seg.arena.extent
            ack = host.grant(C_SEG)
            assert ack.WhichOneof("outcome") == "installed", ack
            host.want_chunks("chunks", [(0, 1), (0, 2)])
            held = host.await_chunks("chunks", [(0, 1), (0, 2)])
            assert [c.key for c in host.chunk_log] == [("chunks", 0, 1), ("chunks", 0, 2)], [c.key for c in host.chunk_log]
            first = held[("chunks", 0, 1)]
            assert first.loan is not None and first.loan.segment == C_SEG and not first.inline, first
            assert first.labels.dtype == np.uint8 and first.shape == (1, 2, 2) and int(first.labels[0, 0, 0]) == 2, first
            assert first.position_zyx == (0, 1, 0) and held[("chunks", 0, 2)].position_zyx == (0, 2, 0)
            released = [r for r in host.loans_released if r[0] == C_SEG]
            assert len(released) == 2, released
            assert not host.loan_uses, "every chunk loan is released once applied"
            # The set restated unchanged: nothing is served twice.
            host.want_chunks("chunks", [(0, 1), (0, 2)])
            time.sleep(0.3)
            assert len(host.chunk_log) == 2, [c.key for c in host.chunk_log]
            # The set moves: (0, 1) leaves and is evicted, (0, 0) arrives and is served.
            host.want_chunks("chunks", [(0, 2), (0, 0)])
            host.await_chunks("chunks", [(0, 2), (0, 0)])
            assert ("chunks", 0, 1) not in host.chunks and host.evicted == [("chunks", 0, 1)], host.evicted
            assert [c.key for c in host.chunk_log] == [("chunks", 0, 1), ("chunks", 0, 2), ("chunks", 0, 0)]
            return f"arena host-sized at {seg.arena.extent} B; 2 wants served in order, 2 loans released; restated: nothing re-served; moved: 1 evicted, 1 served"

        def test_chunk_misuse():
            """The dispositions a conforming plugin never provokes: an
            `OverlayUpdate` to a chunked overlay refused on site 30 and its
            loans released; a chunk for a key the host did not name dropped
            and counted; a compile of a chunked overlay `INVALID_ARGUMENT`;
            and the label table applied, keyed by id."""
            before = len(host.refusals)
            done = host.invoke("fixture.chunk_misuse")
            assert done.ok and done.message.startswith("compile refused:") and "chunked" in done.message, done.message
            refused = host.refusals[before:]
            assert [site for site, _ in refused] == ["overlay_chunk_refused"], refused
            assert "OverlayUpdate to chunked overlay 'chunks'" in refused[0][1], refused
            assert host.dropped_chunks == [("chunks", 0, 99)], host.dropped_chunks
            table = host.label_tables["chunks"]
            assert tuple(table.colormap[1]) == (255, 0, 0, 255) and table.label_names == {1: "one", 2: "two"}
            assert table.label_visible == {2: False} and table.style.opacity == 0.5, table
            assert not host.loan_uses, f"a refused push's loans are released: {host.loan_uses}"
            assert not host.diagnostics, host.diagnostics
            return "OverlayUpdate refused (site 30); unwanted chunk dropped and counted; compile INVALID_ARGUMENT; table applied"

        def test_contribute_and_list():
            done = host.invoke("fixture.rois")
            assert done.ok and "4 rois after the box" in done.message, done.message
            box = [r for r in host.rois.values() if r.contrib_id == "box"]
            assert len(box) == 1 and box[0].name == "A box" and box[0].style.color == "#123456"
            return f"{done.message}; stored as {box[0].region_id}"

        def test_epochs_subscription():
            """This fixture declares NO tool, so §8.7's rule
            alone would leave it off the chain.  It asks for `"epochs"` in
            `subscriptions`, so the host arranges one -- and then holds it to
            §8.10's acknowledgement contract exactly like a tool session,
            which is why asking is opt-in rather than folded into a
            permission.  The unknown name beside it is warned and ignored."""
            assert host.subscriptions == {"epochs", "view", "regions"}, host.subscriptions
            assert host.epochs_arranged, "a session that asked is not on the chain"
            assert any("weather" in w for w in host.manifest_warnings), host.manifest_warnings
            host.arrange_time()                     # an epoch naming an unheld mapping is not acked
            epoch = host.arrange_pane(VIEW_2D)      # raises if the ack does not come
            return f"no tool declared; epochs subscribed, pane arranged at epoch {epoch} and acknowledged; 'weather' ignored"

        def test_progress():
            """A progress widget is the one kind the host
            drives and the user cannot, so nothing is echoed back."""
            host.open_panel("fixture")   # a patch to a closed panel goes nowhere
            done = host.invoke("fixture.progress")
            assert done.ok, done.message
            # A panel patch is a coalescing push, so the run of values the
            # plugin sent may arrive as one and the reply can overtake them:
            # wait for the state, and assert the state, never the count.
            final = host.await_progress("fixture", "bar", until=lambda v: v is None)
            assert final is None, f"indeterminate should outrank the value, got {final}"
            steps = [v for _, widget, v in host.progress_log if widget == "bar"]
            assert steps and steps[-1] is None, steps
            assert all(s is None or 0.0 <= s <= 1.0 for s in steps), steps
            # nothing can come back: the kind binds no command, which is the
            # whole reason it exists rather than a slider driven for progress
            bar = [w for w in host.manifest["contributions"]["panels"][0]["root"]["children"] if w["id"] == "bar"][0]
            assert bar["kind"] == "progress" and "command" not in bar, bar
            return f"arrived {steps} (None = indeterminate), coalescing allowed; nothing echoed"

        def test_pose_window_and_extension():
            """The delta form, the window it reads back, and
            the gap that is refused because a pose is dense by contract."""
            done = host.invoke("fixture.track")
            assert done.ok, done.message
            assert "extended 1 region over 2 frames" in done.message, done.message
            # the window is frames 1..2 of the domain 0..2 (T=3: every frame
            # of a pose lies inside [0, T)), so two rows of three
            assert "window [1.0, 1.0, 1.0, 2.0, 2.0, 2.0] of domain (0, 2)" in done.message, done.message
            assert "gap refused" in done.message, done.message
            # the host's shapes: a window missing
            # the domain is timeless, an inverted one refused; a batch naming
            # a region twice, an empty one, a component-less one refused
            assert "missed window timeless=True" in done.message, done.message
            assert "inverted window refused (INVALID_ARGUMENT)" in done.message, done.message
            assert "batch refusals: twice INVALID_ARGUMENT, empty INVALID_ARGUMENT, no component INVALID_ARGUMENT" in done.message, done.message
            track = [r for r in host.rois.values() if r.contrib_id == "track"][0]
            assert host.pose_domains[track.region_id] == (0, 2)
            assert len(track.roi.pose_translation) == 9  # three frames, dense
            # and without a window the rows are not in the reply at all
            # nor t_start/t_end -- the record is posed, so timeless --
            # while the domain pair still says where the pose is
            assert "bare cleared=True timeless=True domain carried=True" in done.message, done.message
            assert "window reads as a slice=True" in done.message, done.message
            return f"{done.message}; domain {host.pose_domains[track.region_id]}"

        def test_roi_inclusion_and_oversized_window():
            """The host answers which voxels a region covers at a
            frame, and refuses an oversized windowed `GetRois` rather
            than clipping it.  As the `1.0` host answers inclusion:
            a `t` past the DATASET is
            `OUT_OF_BOUNDS`; past the POSE domain a region is HELD at its end
            row (HOLD is the wire default) and only a HIDE one is absent; a
            region clipped away is present with nothing to deposit."""
            done = host.invoke("fixture.inclusion")
            assert done.ok, done.message
            assert "t=2 present=True voxels=1" in done.message, done.message
            assert "t=99 refused OUT_OF_BOUNDS" in done.message, done.message
            assert "held past its domain: present voxels=1 origin=(1, 1, 1)" in done.message, done.message
            assert "hidden past its domain: absent" in done.message, done.message
            assert "clipped away: present shape=(0, 0, 0)" in done.message, done.message
            assert "hidden listed unwindowed at t=1: False" in done.message, done.message
            assert host.inclusion_requests, "the host was never asked"
            # the deposit is released like any other (no leak at teardown)
            # lowered so the refusal runs without 32 MiB of poses in a smoke
            # test; the host's rule is the same at either size
            host.max_message_bytes = 64
            big = host.invoke("fixture.big_window")
            host.max_message_bytes = MAX_MESSAGE_BYTES
            assert big.ok and "refused" in big.message, big.message
            assert "OUT_OF_BOUNDS" in big.message or "past the" in big.message, big.message
            return f"{done.message}; oversized window {big.message.split(';')[0]}"

        def test_inclusion_clip_box():
            """§8.7's table: `ReadRoiInclusion`
            clipped to a level-0 box with `ReadDataVolume`'s semantics -- the
            region's voxels inside it, a box missing the region present with
            nothing to deposit -- and the refusals: an empty box and one
            leaving the dataset `OUT_OF_BOUNDS`, a malformed one
            `INVALID_ARGUMENT`, a region whose clipped box is past the host's
            voxel budget `OUT_OF_BOUNDS`."""
            done = host.invoke("fixture.inclusion_box")
            assert done.ok, done.message
            assert "clipped: 2 voxels of (1, 1, 2) at (1, 2, 3)" in done.message, done.message
            assert "missed: present shape=(0, 0, 0)" in done.message, done.message
            assert "empty OUT_OF_BOUNDS" in done.message, done.message
            assert "past the dataset OUT_OF_BOUNDS" in done.message, done.message
            assert "malformed INVALID_ARGUMENT" in done.message, done.message
            host.inclusion_voxel_budget = 3  # the mask's box is 4 voxels
            try:
                budget = host.invoke("fixture.inclusion_budget")
            finally:
                host.inclusion_voxel_budget = INCLUSION_VOXEL_BUDGET
            assert budget.ok and budget.message == "over budget OUT_OF_BOUNDS", budget.message
            return f"{done.message}; {budget.message}"

        def test_roi_refusals():
            """§8.8: a NaN or infinite position, direction, pose
            row or frame basis is refused `INVALID_ARGUMENT` in
            `ContributeRois`, and in any `ExtendRoiPoses` row; a pose domain
            outside `[0, T)` is refused in both, and so is a `contrib_id` with
            a compiled label's `label:` prefix.  Nothing is stored."""
            done = host.invoke("fixture.roi_refusals")
            assert done.ok, done.message
            for label in ("nan position", "inf direction", "nan pose row", "inf frame basis", "domain past T",
                          "label prefix", "extension nan", "extension past T"):
                assert f"{label} INVALID_ARGUMENT" in done.message, (label, done.message)
            assert done.message.endswith("stored=False"), done.message
            return done.message

        def test_held_picker_and_status():
            """A plugin waiting on a pick can tell a browsing
            person from a wedged host, and the session keeps working while the
            host's dialog is up.

            The fixture picks on a background thread, not in the handler, which
            is what this repository's own guide tells authors to do -- so what
            this proves is the query and the host's concurrency, not the held
            command deadline.  Nothing here waits on that deadline."""
            weights = Path(tmp.name) / "held.bin"
            weights.write_bytes(b"held")
            host.assets["weights"] = weights
            host.hold_picker()
            done = host.invoke("fixture.picker_status")
            assert done.ok and "picking in the background" in done.message, done.message
            subject = host.await_picker()      # the dialog is up, the command already answered
            status = host.await_status("fixture", "awaiting_user=True", timeout=5.0)
            # §8.9: one picker per session at a time -- a second
            # `RequestAsset` while this one is up is OUT_OF_BOUNDS and opens nothing
            asked = len(host.asset_requests)
            second = host.invoke("fixture.asset")
            assert second.ok and second.message.startswith("refused: OUT_OF_BOUNDS"), second.message
            assert len(host.asset_requests) == asked and len(host.picker_refusals) == 1, (host.asset_requests, host.picker_refusals)
            # the host keeps serving while its own modal is on screen
            assert host.invoke("fixture.rois").ok, "a command was not answered while the picker was up"
            host.answer_picker()
            picked = host.await_status("fixture", "yes", widget="picked", timeout=10.0)
            assert host.status_queries, "the plugin never asked about its own request"
            return f"picked in the background on {subject}; '{status}'; a second picker refused OUT_OF_BOUNDS; a command answered meanwhile; picked {picked}"

        def test_asset_picked_declined_refused():
            weights = Path(tmp.name) / "weights.bin"
            weights.write_bytes(b"model")
            host.assets["weights"] = weights
            done = host.invoke("fixture.asset")
            assert done.ok and done.message == f"asset at {weights}", done.message
            assert host.asset_requests[-1].slot == "weights"
            host.assets["weights"] = None
            done = host.invoke("fixture.asset")
            assert done.ok and done.message == "declined", done.message
            host.permissions.discard("data_access")
            try:
                done = host.invoke("fixture.asset")
            finally:
                host.permissions.add("data_access")
            assert done.ok and done.message.startswith("refused: UNSUPPORTED") and "data_access" in done.message, done.message
            return "picked, then declined, then refused without data_access"

        def test_activation_refused():
            """An `ActivateTool` the host refuses -- a tool the manifest
            does not declare is `INVALID_ARGUMENT`, and nothing is bound."""
            before = len(host.activation_refusals)
            done = host.invoke("fixture.activate")
            assert done.ok and done.message.startswith("refused INVALID_ARGUMENT"), done.message
            assert len(host.activation_refusals) == before + 1 and host.engaged is None, host.activation_refusals
            return done.message

        def test_panel_patch_refusals():
            """A patch applied op by op, as the host does: an undeclared widget, an op
            its kind does not take, each refused `panel_patch_refused` to the
            plugin; the good op beside them applied."""
            before = len(host.refusals)
            done = host.invoke("fixture.bad_patches")
            assert done.ok, done.message
            host._await(lambda: True if len(host.refusals) >= before + 3 else None, 5.0, "three panel_patch_refused")
            refused = host.refusals[before:]
            assert [site for site, _ in refused] == ["panel_patch_refused"] * 3, refused
            assert "'nope' is not in this panel" in refused[0][1], refused
            assert host.await_status("fixture", "applied", widget="picked") == "applied"
            return f"{len(refused)} ops refused, the good one applied"

        def test_status_of_a_held_read():
            """The ledger is every request, not only the picker's: a read
            the host is sitting on (`hold_reads`) is PENDING, awaiting no one."""
            host.hold_reads()
            try:
                done = host.invoke("fixture.read_status")
                assert done.ok, done.message
                host.await_reads(1)
                status = host.await_status("fixture", "read pending=", timeout=5.0)
            finally:
                host.release_reads()
            assert status == "read pending=True awaiting_user=False", status
            return status

        def test_no_violations():
            # The retirement scenario's `ReleaseLoan`, which
            # crosses the SDK's own `SegmentReleased`, is counted, not reported.
            assert not host.violations, [(pb2.ViolationSite.Name(v.where), v.detail) for v in host.violations]
            assert not host.unhandled, host.unhandled
            provoked = ("'nope'", "set_progress", "'bar'", "needs a row_key", "not a list's cell", "holds no row 'zzz'", "refused whole", "is multiple", "channels [9]", "'nonesuch' is not",
                        "INVALID_ARGUMENT: table 'stats'", "INVALID_ARGUMENT: table 'held' row 'wide'", "the PanelPatch carrying this table op", "has no row 'nonesuch'", "'plain' is not selectable", "widget 'plain' (kind 'table')")
            panel = [r for r in host.refusals if r[0] == "panel_patch_refused" and not any(p in r[1] for p in provoked)]
            assert not panel, panel
            return "no ContractViolation, nothing unhandled, no panel patch refused but those provoked"

        def test_command_id_on_refused_arguments():
            """§8.3: `CommandDone.command_id`
            echoes the invoked command's id on success, on failure and on an
            argument-validation failure alike.  The emulator faults a session on
            one that does not (`check_id_rules`); here the plugin's own answers
            to a missing and an ill-typed argument are checked, and the session
            goes on."""
            seen = []
            for args, ok in (({"value": 2.5}, True), ({}, False), ({"value": "two"}, False)):
                done = host.invoke("fixture.needs_value", **args)
                assert done.command_id == "fixture.needs_value" and done.ok == ok, (args, done)
                seen.append(done.message or "ok")
            assert not host.session_faults, host.session_faults
            return "; ".join(seen) + " -- each CommandDone named fixture.needs_value"

        def test_command_outcomes():
            """The command outcomes, the same in every SDK: an undeclared command is `Error{NOT_FOUND}`; a declared
            command with no handler (`fixture.unhandled`) is
            `CommandDone{ok=false}` naming it; a canceled one is
            `Error{CANCELLED}`.  A case listed in `PENDING_VIZI` that the
            Python SDK answers as listed is reported, not failed."""

            def outcome(pending) -> str:
                try:
                    done = pending.result(10.0).command_done
                except PluginError as exc:
                    return f"Error{{{pb2.Error.Kind.Name(exc.kind)}}}"
                if done.command_id != command_of[pending.id]:
                    return f"CommandDone naming {done.command_id!r}"
                return f"CommandDone{{ok={str(done.ok).lower()}}}"

            command_of = {}

            def invoke(command_id: str):
                p = host.invoke_async(command_id)
                command_of[p.id] = command_id
                return p

            cancelled = invoke("fixture.long_honours")
            host.await_work(("request", cancelled.id), lambda w: w.HasField("progress") and w.progress.value > 0)
            host.cancel_work(request_id=cancelled.id)
            got = {
                "canceled": outcome(cancelled),
                "undeclared command": outcome(invoke("fixture.nobody_declared_this")),
                "declared, no handler": outcome(invoke("fixture.unhandled")),
            }
            want = {"canceled": "Error{CANCELLED}", "undeclared command": "Error{NOT_FOUND}", "declared, no handler": "CommandDone{ok=false}"}
            wrong, seen = [], []
            for case, expected in want.items():
                pending = PENDING_VIZI.get(case)
                if got[case] == expected:
                    seen.append(f"{case} {expected}")
                    if pending:
                        print(f"      pending vizi, now passes (remove it from PENDING_VIZI): {case}")
                elif pending and got[case] == pending[0]:
                    seen.append(f"{case} {got[case]} (pending vizi)")
                    print(f"      pending vizi: {case}: {pending[1]}")
                else:
                    wrong.append(f"{case}: want {expected}, got {got[case]}")
            assert not wrong, wrong
            assert not host.session_faults, host.session_faults
            assert host.invoke("fixture.series").ok, "the session did not go on"
            return "; ".join(seen)

        def test_folders():
            """`control_plane_protocol.md` §2.3: the
            settings folder reaches the plugin as `VIZI_PLUGIN_SETTINGS_DIR`
            and is readable outside a command context; the dataset's per-file
            folder arrives on `ViewState`, is NOT created by the SDK, and an
            EMPTY field reads as none -- never as a path.  The host
            NAMES the settings folder and does not create it; the SDK's
            `settings_dir()` creates it on first use -- so it is absent until
            the fixture asks, and present after."""
            assert host.settings_dir is not None and not host.settings_dir.exists(), "the settings folder existed before the plugin asked for it"
            done = host.invoke("fixture.folders")
            assert host.settings_dir.is_dir(), "settings_dir() did not create the folder on first use"
            assert done.ok, done.message
            expected = f"settings={host.settings_dir} state={host.file_state_dir}"
            assert done.message == expected, (done.message, expected)
            assert not Path(host.file_state_dir).exists(), "the per-file folder was created"
            kept = host.file_state_dir
            host.file_state_dir = ""  # a dataset with no local path
            try:
                done = host.invoke("fixture.folders")
            finally:
                host.file_state_dir = kept
            assert done.ok and done.message.endswith("state=None"), done.message
            return "settings folder absent until asked, created by settings_dir() on first use; per-file folder named, not created; empty read as None"

        def test_unknown_kind_answered():
            """`control_plane_protocol.md` §11: a request of a
            kind the plugin does not know is answered `Error{UNSUPPORTED}` echoing
            its id -- never silence, which costs the sender its timeout."""
            reply = host.request_unknown_kind()
            assert reply.WhichOneof("msg") == "error" and reply.error.kind == pb2.Error.UNSUPPORTED, reply
            return f"UNSUPPORTED: {reply.error.message!r}"

        def test_zero_length_frame_ends_the_session():
            """A 0-byte record carrying a descriptor
            is a FRAME (`doorbell_protocol.md` §8), not a hang-up: the plugin
            faults it `bad_length` and closes the descriptor.  Its
            `DoorbellFault` then ends the session politely (§8.13): the host
            answers `DoorbellShutdown`, and after the window
            `Shutdown{session_fault}` -- the plugin does not end it itself.
            LAST before teardown, because it ends the session."""
            fd = os.open(os.devnull, os.O_RDONLY)
            try:
                host.send_raw_doorbell(b"", [fd])
            finally:
                os.close(fd)
            fault = host._await(lambda: host.plugin_doorbell_faults[0] if host.plugin_doorbell_faults else None, 5.0, "no DoorbellFault")
            assert fault.failure_class == pb2.DOORBELL_FAILURE_BAD_LENGTH, pb2.DoorbellFailureClass.Name(fault.failure_class)
            assert host.stream_ended.wait(DOORBELL_WINDOW_MS / 1000.0 + SHUTDOWN_WINDOW_S + 1.0), "the session did not end"
            assert host.shutdown_report is not None, "no ShutdownReport before the close"
            # Doorbell §10: the plugin closes its socket as it
            # reports, and the two race.  If the EOF won, the window opened
            # `peer_closed` and the report inside it upgraded the ending to
            # `peer_fault`; either way the ending names the plugin's report.
            first = host.doorbell_shutdowns[0].reason
            assert first in (pb2.DOORBELL_SHUTDOWN_PEER_FAULT, pb2.DOORBELL_SHUTDOWN_PEER_CLOSED), first
            assert host.doorbell_end_reason == pb2.DOORBELL_SHUTDOWN_PEER_FAULT, host.doorbell_end_reason
            assert "BAD_LENGTH" in host.doorbell_end, host.doorbell_end
            opened = pb2.DoorbellShutdownReason.Name(first)
            return f"plugin faulted bad_length; DoorbellShutdown({opened}) then Shutdown{{session_fault}}; ended as peer_fault ({host.doorbell_end!r})"

        def wait_for(pred, what: str, timeout: float = 5.0):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                got = pred()
                if got:
                    return got
                time.sleep(0.02)
            raise AssertionError(f"{what} within {timeout:g}s")

        def plugin_saw():
            done = host.invoke("fixture.subs")
            assert done.ok, done.message
            return json.loads(done.message)

        def test_subscribed_view():
            """§8.7: `ViewChanged` sent at the session's
            start and again only when the view changed, at most once per
            pump tick; the plugin's newest-wins handler sees the latest.
            The frame interval and the selected region on `ViewState` too."""
            assert host.view_pushes, "no ViewChanged at the session's start"
            first = host.view_pushes[0]
            assert first.open_generation == DATASET.open_generation and first.t == DATASET.t, first
            count = len(host.view_pushes)
            time.sleep(0.2)
            assert len(host.view_pushes) == count, "a ViewChanged was sent with nothing changed"
            DATASET.t = 2
            wait_for(lambda: plugin_saw()["views"][-1][1] == 2, "the plugin seeing t=2")
            assert len(host.view_pushes) == count + 1, [v.t for v in host.view_pushes]
            view = DATASET.view_state()
            assert view.time_increment == DATASET.time_increment_s and view.time_unit == "s", view
            DATASET.t = 1
            wait_for(lambda: plugin_saw()["views"][-1][1] == 1, "the plugin seeing t=1 again")
            return f"{len(host.view_pushes)} pushes: at start, then one per change; the handler holds the newest"

        def test_subscribed_regions():
            """`RegionsChanged` -- `reset` first, with every region;
            a person's region, the plugin's own contribution, an edit and a
            deletion each reported with `origin` and a never-reused
            `revision`; `GetRois{region_ids}` returns only those named, with
            the same `revision` and a 16-hex `content_hash`; the selection
            reaches `ViewChanged`."""
            assert host.region_pushes and host.region_pushes[0].reset, host.region_pushes[:1]
            # A region the person drew: any shape will do for the push, so it
            # borrows an existing region's (the emulator stores, never draws).
            shape = host.rois[next(iter(host.rois))].roi if host.rois else pb2.RoiRecord().roi
            user = host.add_region(shape, name="drawn")
            got = wait_for(lambda: [c for c in plugin_saw()["regions"] if any(ch[0] == user for ch in c[1])], "the plugin hearing of the drawn region")
            change = next(ch for ch in got[-1][1] if ch[0] == user)
            assert change[2] == "user" and change[1] > 0 and not change[3], change
            rev = change[1]
            done = host.invoke("fixture.rois_by_id", ids=f"{user},nonesuch")
            records = json.loads(done.message)
            assert [r[0] for r in records] == [user], records  # the missing id is simply absent
            assert records[0][1] == rev and len(records[0][2]) == 16, records
            host.select_region(user)
            wait_for(lambda: plugin_saw()["views"][-1][4] == user, "the selection in ViewChanged")
            host.delete_region(user)
            wait_for(lambda: any(ch[0] == user and ch[3] and ch[1] == 0 and ch[2] == "" for c in plugin_saw()["regions"] for ch in c[1]), "the deletion")
            host.select_region("")
            origins = {ch.origin for push in host.region_pushes for ch in push.changes if not ch.deleted}
            return f"reset first; {len(host.region_pushes)} pushes; origins seen {sorted(origins)}; region_ids filtered; revision {rev} matches GetRois"

        def test_colormap():
            """§8.4: `GetColormap` answers 1024 bytes of RGBA for a
            known name (gray is exact), `NOT_FOUND` for another; no
            permission and no dataset needed."""
            got = json.loads(host.invoke("fixture.colormap", name="gray").message)
            assert got == {"shape": [256, 4], "dtype": "uint8", "mid": [128, 128, 128, 255]}, got
            refused = host.invoke("fixture.colormap", name="no-such-map").message
            assert refused.startswith("refused: NOT_FOUND"), refused
            return f"gray (256, 4) u8, entry 128 = [128, 128, 128, 255]; unknown -> NOT_FOUND"

        def test_save_path():
            """§8.9: a path the person picks, the extension
            appended; declined; a name with a separator `INVALID_ARGUMENT`."""
            host.save_path_answer = str(Path(tempfile.gettempdir()) / "chosen")
            picked = host.invoke("fixture.save_path").message
            assert picked == f"path {Path(tempfile.gettempdir()) / 'chosen.csv'}", picked
            host.save_path_answer = None
            assert host.invoke("fixture.save_path").message == "declined"
            bad = host.invoke("fixture.save_path", name="a/b").message
            assert bad.startswith("refused: INVALID_ARGUMENT"), bad
            assert [r.extension for r in host.save_requests] == ["csv", "csv"], host.save_requests
            return f"{picked!r}; declined; a path as the name refused"

        def test_pickers():
            """Manifest §3.3.1: the four picker kinds parse; a
            multi-select `channel_picker` fires `Value.ints` and the plugin
            reads a list; `set_value` on each picker is applied, and a
            color that is not #RRGGBB is refused."""
            fired = json.loads(host.invoke("fixture.pick", value=[0, 1]).message)
            assert fired == {"value": [0, 1]}, fired
            before = len(host.refusals)
            host.open_panel("fixture")
            roi = next(iter(host.rois), "")
            assert host.invoke("fixture.pickers_set", roi=roi).ok
            time.sleep(0.3)
            assert len(host.refusals) == before, host.refusals[before:]
            # Checked against the host's own choices.
            assert host.invoke("fixture.pickers_bad").ok
            assert host.invoke("fixture.pickers_bad", scalar=True).ok  # its own patch: ops coalesce by widget
            wait_for(lambda: len(host.refusals) >= before + 4, "four picker refusals")
            reasons = [d for _, d in host.refusals[before:]]
            for needle in ("is multiple", "channels [9]", "'nonesuch' is not a colormap", "'nonesuch' is not a region"):
                assert any(needle in r for r in reasons), (needle, reasons)
            return "Value.ints both ways; every picker's set_value applied; four values the host does not hold refused"

        def test_lists():
            """§8.4: `set_rows` gives the whole ordered key
            list; a row-addressed op sets one row's cell; a selectable list
            takes a key; and the host refuses, op by op, a cell op with no
            row, a row_key off a cell, a row it does not hold, and more rows
            than `max_rows` (whole, never truncated).  A row's event carries
            `args["row"]`."""
            host.open_panel("fixture")
            before, rbefore = len(host.refusals), len(host.row_patches)
            done = host.invoke("fixture.list_ops")
            assert done.ok, done.message
            wait_for(lambda: host.list_selection.get(("fixture", "results")) == "b", "the rows, cells and selection")
            assert len(host.refusals) == before, host.refusals[before:]
            assert host.invoke("fixture.list_bad").ok
            time.sleep(1.0)
            assert len(host.refusals) >= before + 4, [d for _, d in host.refusals[before:]]
            reasons = [d for _, d in host.refusals[before:]]
            for needle in ("needs a row_key", "which is not a list's cell", "holds no row 'zzz'", "refused whole"):
                assert any(needle in r for r in reasons), (needle, reasons)
            assert host.list_rows[("fixture", "results")] == ["a", "b", "c"], host.list_rows
            cells = {(w, r): v for _, w, r, v in host.row_patches[rbefore:]}
            assert cells.get(("rname", "a")) == "Alpha" and cells.get(("rshow", "b")) is True, cells
            assert host.list_selection[("fixture", "results")] == "b", host.list_selection
            fired = json.loads(host.invoke("fixture.pick", row="b", value=True).message)
            assert fired == {"row": "b", "value": True}, fired
            return "rows a b c; cells by row; selection b; four refusals; args['row'] reaches the handler"

        def test_table():
            """§8.4's `table`, as the host applies it: rows whole,
            then a keyed patch (deletes first, a key in place replaced there,
            a new one appended); a missing trailing cell and a ``None`` blank;
            a selection set by `set_value` that fires nothing; the person's
            selection firing ``{"rows": [...]}``; a sort numeric on a number
            column with blanks last; every refusal whole, `INVALID_ARGUMENT`
            for the table ops; a cell of a kind the host does not show,
            blank and not refused; a burst of 1,000 small patches, held by
            the host's transport and applied in order, none refused; 100,000
            rows through the SDK's split, and one more refused."""
            host.open_panel("grid")
            before = len(host.refusals)
            done = host.invoke("fixture.table_fill")
            assert done.ok, done.message
            wait_for(lambda: host.table_selection.get(("grid", "stats")) == ["a", "e"], "the rows and the selection")
            assert len(host.refusals) == before, host.refusals[before:]
            rows = host.table_rows("grid", "stats")
            assert rows == [
                ("a", ("Alpha", 12.5, True)),
                ("b", ("Beta", 7.25, False)),
                ("c", ("Gamma", 3.0, None)),
                ("e", ("Epsilon", None, None)),
            ], rows
            shown = dict(host.table_shown("grid", "stats"))
            assert shown["a"] == ("Alpha", "12.50", "✓") and shown["c"] == ("Gamma", "3.00", ""), shown
            # Numerically (as text 12.5 would sort first), the blank last;
            # a second click reverses the comparison, as Qt does.
            assert host.table_sorted("grid", "stats", "area") == ["c", "b", "a", "e"]
            assert host.table_sorted("grid", "stats", "area", descending=True) == ["a", "b", "c", "e"], "blanks are last both ways"
            assert host.table_sorted("grid", "stats", "name") == ["a", "b", "e", "c"]
            assert [k for k, _ in host.table_rows("grid", "stats")] == ["a", "b", "c", "e"], "sorting moved the plugin's order"
            fired = json.loads(host.select_table_rows("grid", "stats", ["c", "a"]).message)
            assert fired == {"rows": ["a", "c"]}, fired
            assert json.loads(host.select_table_rows("grid", "stats", []).message) == {"rows": []}
            # The bad ops, one command each: one table op would merge another away.
            needles = (
                "INVALID_ARGUMENT: table 'stats' row 'x' has 4 cells for 3 columns",
                "INVALID_ARGUMENT: table 'stats' row keys must be non-empty and at most 128 characters",
                "INVALID_ARGUMENT: table 'stats' row keys must be distinct",
                "INVALID_ARGUMENT: the PanelPatch carrying this table op is",
                "table 'stats' has no row 'nonesuch'",
                "table 'plain' is not selectable, so takes no set_value",
                "set_rows is not supported by widget 'plain' (kind 'table')",
            )
            for which, needle in enumerate(needles):
                assert host.invoke("fixture.table_bad", which=which).ok
                wait_for(lambda: len(host.refusals) > before + which, f"bad table op {which} refused")
                detail = host.refusals[before + which][1]
                assert needle in detail, (which, detail)
            assert host.table_rows("grid", "stats") == rows, "a refused op changed the table"
            assert host.invoke("fixture.table_bad", which=99).ok
            wait_for(lambda: host.table_rows("grid", "plain") == [("odd", (None,))], "the unknown cell, blank")
            assert "ints" in host.table_blank_kinds and len(host.refusals) == before + len(needles), host.refusals[before:]
            # 1,000 small patches through the SDK, the host's drain held:
            # every op the SDK sends waits in order and none is refused.
            host.hold_table_drain = True
            try:
                burst = host.invoke("fixture.table_burst", 60.0, n=1000)
            finally:
                host.hold_table_drain = False
            assert burst.ok, burst.message
            held_ops = len(host.table_transport)
            host.drain_tables()
            wait_for(lambda: len(host.table_rows("grid", "stats")) == len(rows) + 1000, "the 1,000 patches applied", timeout=30.0)
            burst_keys = [k for k, _ in host.table_rows("grid", "stats")][len(rows):]
            assert burst_keys == [f"burst{i:04d}" for i in range(1000)], burst_keys[:5]
            assert len(host.refusals) == before + len(needles) and not host.session_faults, host.refusals[before + len(needles):]
            # 100,000 rows: the SDK splits at 1 MiB into a set_table and appends.
            mark = len(host.patches)
            assert host.invoke("fixture.table_big", 60.0, rows=100_000).ok
            wait_for(lambda: len(host.table_rows("grid", "stats")) == 100_000, "100,000 rows", timeout=60.0)
            ops = [p[2].WhichOneof("op") for p in host.patches[mark:] if p[1] == "stats" and isinstance(p[2], pb2.PatchOp)]
            assert ops[0] == "set_table" and ops.count("set_table") == 1 and "patch_table" in ops, ops
            assert host.table_rows("grid", "stats")[99_999][0] == "r099999"
            assert host.table_selection[("grid", "stats")] == [], "a replaced row kept its selection"
            assert host.invoke("fixture.table_bad", which=len(needles)).ok
            wait_for(lambda: len(host.refusals) > before + len(needles), "the 100,001st row refused")
            assert "INVALID_ARGUMENT: table 'stats' holds at most 100000 rows, not 100001" in host.refusals[-1][1], host.refusals[-1]
            return f"set and patched by key; selection set without an echo and fired as rows; sorted numerically, blanks last; {len(needles) + 1} ops refused whole; an unknown cell blank; 1,000 patches as {held_ops} held ops, none refused; 100,000 rows in {len(ops)} ops"

        def test_table_held_until_open():
            """A panel that has never opened: its table starts EMPTY, and each
            table op is validated as it arrives against the table the held ops
            leave -- a 60,000-row table that spans several messages, three
            patches and a selection, each sent on its own.  The hold keeps ONE
            `set_table`, the table's whole state, and the selection, and shows
            them when the panel opens, the selection after the rows.  The held
            `set_table` passes 1 MiB and is applied whole all the same.  Then,
            still closed, a row with too many cells and a selection naming a
            row the table will not hold are each refused AT ONCE and alone,
            and the good patch sent between them is held and shown."""
            before = len(host.refusals)
            assert host.invoke("fixture.table_held", 30.0).ok
            wait_for(lambda: ("held", "set_value", "") in host._held_patches.get("shelf", {}), "the selection held", timeout=30.0)
            assert len(host.refusals) == before, host.refusals[before:]
            held = [op for k, op in host._held_patches["shelf"].items() if k[1] == "table"]
            assert len(held) == 1 and held[0].WhichOneof("op") == "set_table", [op.WhichOneof("op") for op in held]
            assert len(held[0].set_table.rows) == 60_000, len(held[0].set_table.rows)
            size = held[0].ByteSize()
            assert size > (1 << 20), size
            assert host.invoke("fixture.table_held_bad", 30.0).ok
            wait_for(lambda: len(host.refusals) >= before + 2, "both bad ops refused while closed", timeout=30.0)
            reasons = [d for _, d in host.refusals[before:]]
            assert len(reasons) == 2, reasons
            assert "INVALID_ARGUMENT: table 'held' row 'wide' has 3 cells for 2 columns" in reasons[0], reasons
            assert "table 'held' has no row 'nonesuch'" in reasons[1], reasons
            assert "shelf" not in host.opened_panels, "refused before the panel opened"
            wait_for(lambda: any(row.key == "extra3" for row in host._held_patches["shelf"][("held", "table", "")].set_table.rows), "the good patch held", timeout=30.0)
            host.open_panel("shelf")
            rows = host.table_rows("shelf", "held")
            assert len(rows) == 60_001, len(rows)
            assert rows[0] == ("r00001", ("one", -1.0)) and rows[1] == ("r00002", ("two", 2.5)), rows[:2]
            assert rows[-3] == ("r59999", ("row 59999", 59999.0)) and rows[-2] == ("extra2", ("second extra", 2.0)), rows[-3:]
            assert rows[-1] == ("extra3", ("third extra", 3.0)) and not {"extra1", "wide"} & {k for k, _ in rows}
            assert host.table_selection[("shelf", "held")] == ["r00001", "extra2"], host.table_selection.get(("shelf", "held"))
            assert len(host.refusals) == before + 2, host.refusals[before:]
            return (f"60,000 rows, three patches and a selection held as one set_table of {size} bytes; a too-wide row "
                    "and an unknown selection refused at once while closed, the patch between them kept; all shown at open")

        def test_widget_geometry():
            """§8.4's `WidgetGeometry`: every image widget's size right after
            `PanelOpened` (the `grid` panel's `plot`, at the emulator's
            default), a resize sent once and the same size not again, nothing
            while the dock is hidden, and the size told again at a reopen.
            The plugin's handler hears each, with `pixels` the 1:1 size --
            once it has settled, since the SDK coalesces this push and two
            sizes in quick succession may reach the handler as the newest.
            At a fractional pixel ratio the host sends the size and ratio as
            they are, and the 1:1 size is each product rounded half to even:
            403 x 301 at 1.5 is 604.5 x 451.5, so 604 x 452."""

            def hears(entry):
                return wait_for(lambda: (h := json.loads(host.invoke("fixture.geometry").message)) and h[-1] == entry and h, f"the handler hearing {entry}")

            sent = [g for g in host.geometry_sent if g.panel_id == "grid"]
            assert [(g.widget_id, g.width_px, g.height_px, g.dpr) for g in sent] == [("plot", 320, 240, 1.0)], sent
            hears(["plot", 320, 240, 1.0, [320, 240]])
            assert host.resize("grid", "plot", 400, 300, 2.0)
            assert not host.resize("grid", "plot", 400, 300, 2.0), "an unchanged size was sent again"
            hears(["plot", 400, 300, 2.0, [800, 600]])
            host.close_panel("grid")
            assert not host.resize("grid", "plot", 500, 300, 1.5), "a hidden dock's size was sent"
            host.open_panel("grid")
            hears(["plot", 500, 300, 1.5, [750, 450]])
            assert host.resize("grid", "plot", 403, 301, 1.5)
            assert (host.geometry_sent[-1].width_px, host.geometry_sent[-1].height_px, host.geometry_sent[-1].dpr) == (403, 301, 1.5)
            heard = hears(["plot", 403, 301, 1.5, [604, 452]])
            assert len(heard) == 4, heard
            return f"{len(heard)} sizes heard, the last {heard[-1][1]}x{heard[-1][2]} at dpr {heard[-1][3]} = {heard[-1][4]} pixels"

        def test_appearance():
            """§8.4's `HostAppearance`: sent once right after `Ready`, before
            any panel opened, and not again for the panels since; a theme
            switch sends it, the same theme again does not.  The handler hears
            each as `(r, g, b, a)` and `#rrggbb` -- once settled, since the SDK
            coalesces this push -- and `ctx.appearance` holds the newest."""

            def hears(theme):
                return wait_for(
                    lambda: (h := json.loads(host.invoke("fixture.appearance").message))["heard"]
                    and h["heard"][-1][0] == theme and h["newest"] == theme and h,
                    f"the handler hearing {theme}",
                )

            dark, light = APPEARANCE_PALETTES["dark"], APPEARANCE_PALETTES["light"]
            assert [a.theme for a in host.appearance_sent] == ["dark"], host.appearance_sent
            first = hears("dark")["heard"][0]
            assert first[0] == "dark" and first[1]["text"] == f"#{dark['text'] >> 8:06x}", first
            assert first[2] == [(dark["text"] >> s) & 0xFF for s in (24, 16, 8, 0)], first
            assert not host.set_theme("dark"), "an unchanged theme was sent again"
            assert host.set_theme("light")
            assert not host.set_theme("light"), "an unchanged theme was sent again"
            heard = hears("light")["heard"]
            assert heard[-1][1]["window_bg"] == f"#{light['window_bg'] >> 8:06x}", heard[-1]
            assert host.set_theme("dark")
            heard = hears("dark")["heard"]
            assert [a.theme for a in host.appearance_sent] == ["dark", "light", "dark"], host.appearance_sent
            assert len(heard) == 3, heard
            return f"{len(host.appearance_sent)} pushes for 5 theme settings; heard {[h[0] for h in heard]}"

        def test_state_kept_until_open():
            """A declared panel
            that is not open keeps the latest op per (widget, property) and
            shows it when it opens; an undeclared widget is still refused."""
            before = len(host.refusals)
            assert host.invoke("fixture.late_patch").ok
            wait_for(lambda: len(host.refusals) >= before + 1, "the undeclared widget refused")
            assert "'nope' is not in this panel" in host.refusals[before][1], host.refusals[before:]
            assert not [p for p in host.patches if p[0] == "late"], "applied before the panel opened"
            host.open_panel("late")
            assert host.status_of("late", "lbl") == "kept", host.status_of("late", "lbl")
            return "held while closed, the newer value shown at open; the undeclared widget refused"

        def test_retire_between_reserve_and_push():
            """§9.1: a loaned reservation is in flight
            from `reserve` until the message naming it is sent, and a
            retirement waits for it.  The `labels` arena is superseded while
            an item's reservation is parked: the plugin must not release the
            old arena yet; the parked push, naming the retiring segment, is
            dropped quietly by the host, its loan returned;
            only then does `SegmentReleased` come; and the next push goes to
            the successor.  No refusal, no error."""
            new_seg = segment_id(20)
            assert host.invoke("fixture.reserve_parked").message == "reserved"
            refusals_before, pushes_before = len(host.refusals), len(host.pushes)
            result = {}
            worker = threading.Thread(target=lambda: result.update(ack=host.supersede(
                O_SEG, Segment.overlay(new_seg, "labels", 4 << 20, host.directory), release_timeout=10.0)))
            worker.start()
            deadline = time.monotonic() + 10
            while O_SEG not in host.retiring_segments or new_seg not in host.acks:
                assert time.monotonic() < deadline, "the successor was never installed"
                time.sleep(0.01)
            time.sleep(0.3)
            assert O_SEG not in host.retired, "the old arena was released with a reservation still unsent"
            sent = json.loads(host.invoke("fixture.release_parked").message)
            worker.join(15)
            assert result.get("ack") is not None and result["ack"].WhichOneof("outcome") == "installed", result
            assert sent == [["parked", "sent"], ["fresh", "sent"]], sent
            assert ("overlay labels", O_SEG) in host.dropped_retiring, host.dropped_retiring
            assert host.drops_at_release.get(O_SEG, 0) >= 1, "SegmentReleased came before the parked push"
            fresh = [p for p in host.pushes[pushes_before:] if p.overlay_id == "labels"]
            assert fresh and all(loan.segment == new_seg for p in fresh for loan in p.loans), [(p.overlay_id, [l.segment for l in p.loans]) for p in fresh]
            assert len(host.refusals) == refusals_before, host.refusals[refusals_before:]
            assert not [d for d in host.diagnostics if "supersede" in d], host.diagnostics
            return f"old arena held until the parked push went; that push dropped quietly; SegmentReleased after; the next push in {new_seg:#x}"

        def test_follow():
            """§8.15, as the host runs Follow:
            an offer is kept for an unopened panel, but the button can only be
            pressed on an open one; a running Follow refuses a `ViewRequest`
            touching its targets `FOLLOW_ACTIVE`; the person's input on
            something it does not drive leaves it running, and on something
            it drives ends it `USER_TOOK_CONTROL` (then Resume starts it
            `resumed`); the switch off is `STOPPED`; returning is
            `PLUGIN_ENDED`; t=99 is `INVALID` in vizi's words; a second press
            is `REPLACED`; withdrawing is `WITHDRAWN`; closing the panel is
            `PANEL_CLOSED`; an undeclared target is refused by the SDK."""
            def until(pred, what, timeout=5.0):
                deadline = time.monotonic() + timeout
                while not pred():
                    assert time.monotonic() < deadline, f"{what} within {timeout:g}s"
                    time.sleep(0.01)
            def mode(**args):
                assert host.invoke("fixture.follow_mode", **args).ok
            def ended(n, reason):
                until(lambda: len(host.follows_ended) >= n, f"FollowEnded #{n}")
                got = host.follows_ended[n - 1]
                assert pb2.FollowEndReason.Name(got.reason) == f"FOLLOW_END_{reason}", (pb2.FollowEndReason.Name(got.reason), got.detail)
                return got
            def log():
                return json.loads(host.invoke("fixture.follow_log").message)
            start_t = DATASET.t
            mode(mode="hold", offer="Tour the frames")
            until(lambda: host.follow_offers.get("tour") == "Tour the frames", "the offer")
            assert host.press_follow("tour") is None, "pressed Follow on an unopened panel"
            host.open_panel("tour")
            assert host.press_follow("tour") is None, "pressed Follow with the switch off (it is off at every open)"
            host.set_view_toggle("tour", True)
            first = host.press_follow("tour")
            started = host.follows_started[-1]
            assert started.follow_id == first and list(started.targets) == ["t", "z"] and not started.resumed, started
            until(lambda: any(a[0] == first and a[2] == {"t": 1} for a in host.follow_applied), "the first state applied")
            assert DATASET.t == 1
            done = host.invoke_widget("fixture", "jump", how="move", t=2)
            assert json.loads(done.message)["got"] == ["REFUSED", "FOLLOW_ACTIVE"], done.message
            host.person_touches("camera")  # the Follow drives t and z, not the camera
            assert host.follow is not None and host.follow["follow_id"] == first
            host.person_touches("t")
            ended(1, "USER_TOOK_CONTROL")
            until(lambda: log() and log()[-1].get("reason") == "USER_TOOK_CONTROL", "the plugin told")
            assert host.resumable == "tour"
            resumed = host.resume_follow()
            assert resumed and host.follows_started[-1].resumed
            host.view_toggle = False
            ended(2, "STOPPED")
            host.view_toggle = True
            mode(mode="steps")
            steps = host.press_follow("tour")
            ended(3, "PLUGIN_ENDED")
            seqs = [a[1] for a in host.follow_applied if a[0] == steps]
            assert seqs and seqs == sorted(set(seqs)), seqs
            mode(mode="bad")
            host.press_follow("tour")
            bad = ended(4, "INVALID")
            assert bad.detail == "t=99 is outside 0..2", bad.detail
            mode(mode="hold")
            a = host.press_follow("tour")
            until(lambda: any(x[0] == a for x in host.follow_applied), "the held Follow's state")
            # The running panel's button is Stop, so a second press
            # ends it STOPPED; REPLACED comes only from another panel or plugin.
            assert host.press_follow("tour") is None
            assert ended(5, "STOPPED").follow_id == a
            b = host.press_follow("tour")
            assert b != a
            mode(offer="")
            assert ended(6, "WITHDRAWN").follow_id == b
            mode(offer="Tour again")
            until(lambda: host.follow_offers.get("tour") == "Tour again", "the new offer")
            c = host.press_follow("tour")
            host.close_panel("tour")
            assert ended(7, "PANEL_CLOSED").follow_id == c
            host.open_panel("tour")
            host.set_view_toggle("tour", True)
            mode(mode="undeclared")
            host.press_follow("tour")
            ended(8, "PLUGIN_ENDED")
            until(lambda: "refused" in log()[-1], "the local refusal")
            assert "does not drive select_region" in log()[-1]["refused"], log()[-1]
            assert len(host.follows_ended) == 8, [pb2.FollowEndReason.Name(e.reason) for e in host.follows_ended]
            DATASET.t = start_t
            return f"{len(host.follows_started)} Follows, ended " + ", ".join(pb2.FollowEndReason.Name(e.reason)[11:] for e in host.follows_ended) + "; FOLLOW_ACTIVE; one refused locally"

        def test_set_view():
            """§8.15, judged as the host judges it: a
            `moves_view` widget's interaction carries a fresh cause; the
            plugin's one answer is APPLIED (the view moves), DECLINED, or
            REFUSED for NOT_DECLARED, OUT_OF_RANGE, UNKNOWN_REGION, NOT_ENABLED,
            SUPERSEDED (the person touched `t` meanwhile), STALE (past 2 s)
            or CONSUMED (a different request under a spent cause, while the
            same one repeated gets the same answer); a cause the host never
            minted is INVALID; and a scripted outcome reaches any result."""
            host.open_panel("fixture")
            host.set_view_toggle("fixture", True)
            def jump(**args):
                done = host.invoke_widget("fixture", "jump", **args)
                assert done.ok, done.message
                return json.loads(done.message)
            start_t = DATASET.t
            got = jump(how="move", t=2)
            assert got["cause"] == host.last_cause != 0 and got["got"] == ["APPLIED", "NONE"], got
            assert DATASET.t == 2 and host.view_moves[-1] == {"t": 2}, (DATASET.t, host.view_moves[-1:])
            DATASET.t = start_t
            assert jump(how="decline")["got"] == ["DECLINED", "NONE"]
            assert jump(how="undeclared")["got"][1] == "NOT_DECLARED"
            assert jump(how="both", a="x", b="y")["got"] == ["REFUSED", "INVALID"], "frame_region beside a different select_region"
            assert jump(how="move", t=99)["got"][1] == "OUT_OF_RANGE"
            assert jump(how="region", id="nonesuch")["got"][1] == "UNKNOWN_REGION"
            assert jump(how="twice")["got"] == [["APPLIED", "NONE"], ["APPLIED", "NONE"], ["REFUSED", "CONSUMED"]]
            DATASET.t = start_t
            assert json.loads(host.invoke("fixture.forge_cause").message) == ["REFUSED", "INVALID"]
            host.view_toggle = False
            assert jump(how="move", t=1)["got"][1] == "NOT_ENABLED"
            host.view_toggle = True
            result = {}
            waiter = threading.Thread(target=lambda: result.update(jump(how="wait", wait=0.6, t=2)))
            waiter.start()
            time.sleep(0.2)
            host.person_touches("t")  # the person scrubs t after the click
            waiter.join(10)
            assert result.get("got", [None, None])[1] == "SUPERSEDED", result
            assert jump(how="wait", wait=2.3, t=2)["got"][1] == "STALE"
            host.view_script = ("REFUSED", "EXPORT_ACTIVE", "scripted")
            assert jump(how="move", t=1)["got"] == ["REFUSED", "EXPORT_ACTIVE"]
            host.view_script = None
            DATASET.t = start_t
            return f"{len(host.view_requests)} requests judged: APPLIED moved t; DECLINED; eight refusals by rule; a scripted one"

        def test_host_ui_rules():
            """Host rules the emulator mirrors: a
            `follows_selection` picker's choice is the person's selection and
            sends its name; an in-place edit bumps a region's revision,
            not its content_hash; a closed dock is hidden, its patches
            still applied, and reopening it is an open with the switch off;
            a cause past 20 s is forgotten, so INVALID;
            with no dataset open no cause is minted and ViewChanged says
            generation 0; an applied filter reaches ViewState and
            ViewChanged."""
            rid = next(iter(host.rois))
            touched = host._touched.get("selection", 0)
            fired = json.loads(host.invoke_widget("fixture", "roi", value=rid).message)
            assert fired == {"value": rid, "name": host.rois[rid].name}, fired
            assert DATASET.selected_region_id == rid and host._touched["selection"] > touched
            before = host.region_revisions()[rid]
            hashed = content_hash(host.rois[rid])
            host.edit_region(rid, name="renamed", color="#102030", visible=False)
            assert host.region_revisions()[rid] > before and content_hash(host.rois[rid]) == hashed
            host.close_panel("fixture")
            count, refused = len(host.patches), len(host.refusals)
            assert host.invoke("fixture.pickers_set", roi=rid).ok
            wait_for(lambda: len(host.patches) > count, "patches applied to a hidden panel")
            assert len(host.refusals) == refused, host.refusals[refused:]
            host.open_panel("fixture")
            assert not host.view_toggles["fixture"], "the switch survived a reopen"
            host.set_view_toggle("fixture", True)
            done = host.invoke_widget("fixture", "jump", how="move", t=DATASET.t)
            aged = host.last_cause
            host._causes[aged]["minted_at"] -= 25
            host._forget_expired_causes()
            outcome, reason, _, _ = host._judge_view(pb2.ViewRequest(cause=aged, t=0))
            assert (outcome, reason) == ("REFUSED", "INVALID"), (outcome, reason, done.message)
            DATASET.applied_filter = pb2.AppliedFilter(plugin_id="org.example.f", plugin_version="1", stage_id="blur", channels=[0])
            wait_for(lambda: host.view_pushes and host.view_pushes[-1].HasField("filter"), "ViewChanged.filter")
            assert DATASET.view_state().filter.stage_id == "blur"
            DATASET.applied_filter = None
            host.close_dataset()
            host.invoke_widget("fixture", "jump", how="decline")
            assert host.last_cause == 0, host.last_cause
            wait_for(lambda: host.view_pushes and not host.view_pushes[-1].ListFields(), "the close's ViewChanged")
            generation = DATASET.open_generation
            host.open_dataset()
            assert DATASET.open_generation == generation + 1 and host.dataset_open
            return "picker selection and name; revision without content_hash; hidden panel still patched, reopened with the switch off; a forgotten cause INVALID; the filter pushed; no dataset, no cause"

        def test_long_work():
            """§8.16: a command's progress keyed by its request
            id, canceled and answered `CANCELLED` (no fault), keeping the slot
            until then; one that ignores the cancel answering what it had; a
            background task that blocks nothing, canceled by its task id and
            ended; a reader read canceled mid-way; cancels naming nothing are
            no-ops; a report for a request no longer waited on is dropped."""
            p = host.invoke_async("fixture.long_honours")
            first = host.await_work(("request", p.id), lambda w: w.HasField("progress") and w.progress.value > 0)
            assert first.text == "working" and not first.done, first
            host.cancel_work(request_id="h-nothing")  # no-ops, answered by nothing
            host.cancel_work(task_id="t-nothing")
            host.cancel_work(request_id=p.id)
            try:
                p.result(10.0)
            except PluginError as exc:
                assert exc.kind == pb2.Error.CANCELLED, (pb2.Error.Kind.Name(exc.kind), exc.args)
            else:
                raise AssertionError("a canceled command answered normally")
            assert host.invoke("fixture.series").ok, "the session did not go on after a CANCELLED answer"

            q = host.invoke_async("fixture.long_ignores", seconds=1.0)
            host.await_work(("request", q.id), lambda w: w.text == "not listening")
            host.cancel_work(request_id=q.id)
            done = q.result(10.0).command_done
            assert done.ok and done.message == "finished anyway", done
            dropped = len(host.work_dropped)

            started = host.invoke("fixture.background")
            assert started.ok and started.message.startswith("started t"), started.message
            task_id = started.message.split()[-1]
            begun = host.await_work(("task", task_id))
            assert begun.title == "fixture background work" and begun.cancellable, begun
            assert host.invoke("fixture.series").ok, "a background task held the slot"
            host.await_work(("task", task_id), lambda w: w.text.startswith("step"))
            host.cancel_work(task_id=task_id)
            ended = host.await_work(("task", task_id), lambda w: w.done)
            assert ended.text == "canceled", ended
            started = host.invoke("fixture.background")
            second = started.message.split()[-1]
            host.await_work(("task", second), lambda w: w.text.startswith("step"))
            host.invoke("fixture.background", finish=True)
            assert host.await_work(("task", second), lambda w: w.done).text == "done"

            opened = host.open_reader("/data/a.slow", "slow")
            sid = host.new_segment_id()
            host.add(Segment.reader_arena(sid, opened, host.directory))
            assert host.grant(sid).WhichOneof("outcome") == "installed"
            r = host.request_async(read_volume=pb2.ReadVolume(handle=opened.handle, t=0, c=0, level=0))
            host.await_work(("request", r.id), lambda w: w.text.startswith("reading"))
            host.cancel_work(request_id=r.id)
            try:
                r.result(10.0)
            except PluginError as exc:
                assert exc.kind == pb2.Error.CANCELLED, (pb2.Error.Kind.Name(exc.kind), exc.args)
            else:
                raise AssertionError("a canceled read answered normally")
            host.close_reader(opened.handle)
            host.retire_segment(sid)
            late = len(host.work_dropped) - dropped
            return f"a command canceled mid-way answered CANCELLED and the session went on; one ignoring the cancel answered 'finished anyway'; two background tasks, one canceled and one done, holding no slot; a read canceled mid-way; {late} late request report(s) dropped"

        def test_teardown():
            assert host.shutdown(), "the stream did not end inside the window"
            assert host.shutdown_report is not None and host.shutdown_report.outcome == pb2.ShutdownReport.NO_HOOK
            return "clean end of stream"

        check("handshake", test_handshake)
        if failures:
            return 1
        check("grants", test_grants)
        check("reader", test_reader)
        check("reads advance and release", test_reads_advance_and_release)
        check("second OpenReader replaces: the §1.2 handshake", test_second_open_replaces)
        check("CloseReader, then G retired", test_close_reader)
        check("series: a file holding several images", test_series)
        check("exporter", test_exporter)
        check("abort", test_abort)
        check("a canceled chunk ends its export with AbortExport", test_chunk_cancelled_aborts)
        check("grant recovery: GrantStateQuery and the same-grant re-send", test_grant_recovery)
        check("per-export X", test_per_export_x)
        check("compile and masks", test_compile_and_masks)
        check("V grows the host's way", test_v_grows_the_hosts_way)
        check("V under concurrent reads", test_v_under_concurrent_reads)
        check("chunked overlay: host-sized arena, wants served once, eviction", test_chunked_overlay)
        check("chunked overlay: refused, dropped, never compiled; the table", test_chunk_misuse)
        check("contribute and list", test_contribute_and_list)
        check("subscriptions: a tool-less session on the epoch chain", test_epochs_subscription)
        check("progress: the host drives it, nothing echoes", test_progress)
        check("pose: extend, window, and the refused gap", test_pose_window_and_extension)
        check("inclusion, and an oversized window refused", test_roi_inclusion_and_oversized_window)
        check("inclusion clipped to a box, and its refusals", test_inclusion_clip_box)
        check("ROI values refused at the wire: non-finite, domain past T, a label: id", test_roi_refusals)
        check("picker: a pick in the background, asked about while it waits", test_held_picker_and_status)
        check("asset: picked, declined, refused", test_asset_picked_declined_refused)
        check("an argument-validation failure's CommandDone carries command_id (§8.3)", test_command_id_on_refused_arguments)
        check("command outcomes: undeclared NOT_FOUND, no handler ok=false, canceled CANCELLED", test_command_outcomes)
        check("the plugin's folders", test_folders)
        check("unknown kind answered", test_unknown_kind_answered)
        check("ActivateTool refused for an undeclared tool", test_activation_refused)
        check("panel patches refused op by op", test_panel_patch_refusals)
        check("RequestStatus: a held read is PENDING", test_status_of_a_held_read)
        check("ViewChanged, at start and on change", test_subscribed_view)
        check("RegionsChanged, revision, content_hash, region_ids, selection", test_subscribed_regions)
        check("GetColormap", test_colormap)
        check("RequestSavePath", test_save_path)
        check("host pickers and Value.ints", test_pickers)
        check("the list widget", test_lists)
        check("a panel's state kept until it opens", test_state_kept_until_open)
        check("the table widget", test_table)
        check("a table held until its panel opens, validated on arrival, a bad op refused alone", test_table_held_until_open)
        check("widget geometry", test_widget_geometry)
        check("appearance: once after Ready, again on a theme switch only", test_appearance)
        check("set-view, judged as vizi judges it", test_set_view)
        check("Follow, as vizi runs it", test_follow)
        check("a retirement waits for a reservation in flight", test_retire_between_reserve_and_push)
        check("selection, in-place edits, hidden docks, causes and the applied filter", test_host_ui_rules)
        check("long-running work, progress and cancellation", test_long_work)
        check("no violations", test_no_violations)
        check("a 0-byte frame with a descriptor ends the session", test_zero_length_frame_ends_the_session)
        check("teardown", test_teardown)
    tmp.cleanup()
    check("a tool's FIRE is a cause, on the panel's switch", check_tool_causes)
    check("Stop plugin after an unanswered cancel", check_stop_plugin)
    check("an asked shape with no inside is refused (§8.8)", check_shape_refused)
    check("one launch budget, spawn to Ready (§9)", check_launch_budget)
    check("raw reads and per-axis pitch", check_raw_and_pitch)
    check("version gating: host minors 0 and 1 (§3.1)", check_protocol_minors)
    if sys.platform != "darwin":
        check("a plain hang-up opens the window as peer_closed", check_hang_up)
    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("\nemulator self-check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
