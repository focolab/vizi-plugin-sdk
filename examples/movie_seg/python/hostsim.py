#!/usr/bin/env python3
"""Drive the movie-seg plugin end to end over the real transport, with no vizi.

    .venv/bin/python hostsim.py                       # the Python implementation
    .venv/bin/python hostsim.py --plugin-dir ../rust  # any other

A CHUNKED overlay (`control_plane_protocol.md` §5.7): this host grants each chunked overlay an arena it sizes from
the open dataset, names the ``(level, t)`` volumes it wants (`ChunksWanted`)
and holds exactly those, applies the plugin's `OverlayChunk`s -- copied out
of the arena, the loan released in the same tick -- and its
`OverlayLabelTable`s, evicts what it stops wanting, re-grants the arena when
the dataset changes (`supersedes`), and, by holding its releases, watches the
plugin DEFER a chunk the arena cannot hold rather than fail it.  Two
datasets: a blob moving through a three-level movie for the serving,
eviction, static-overlay, re-serve and table scenarios, then a small dense
one whose whole-volume chunks fill the host-sized arena two at a time, for
the deferral.

Every expectation here is computed from the emulator's dataset with the
driver's own arithmetic (`SPEC.md` §2), never imported from the plugin.
The emulator is ``examples/vizi_hostsim``; this file is the scenarios, in
`SPEC.md`'s order.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # examples/, for vizi_hostsim

from vizi_hostsim import (  # noqa: E402
    DOORBELL_WINDOW_MS,
    SHUTDOWN_WINDOW_S,
    Dataset,
    Host,
    Segment,
    check,
    check_encoder,
    failures,
    segment_id,
)
from vizi_plugin_sdk import pb2  # noqa: E402

BANDS, FOOT = "bands", "footprint"
PANEL = "movie_seg"
BANDS_SEG, FOOT_SEG, _, BANDS2_SEG, FOOT2_SEG = (segment_id(i) for i in range(1, 6))

# -- the two datasets -------------------------------------------------------

SHAPE_A = (6, 1, 8, 24, 32)


def blob_volume(t: int, c: int) -> np.ndarray:
    """A bright Gaussian blob on a dim floor, drifting +4 voxels in x per
    frame: peak 1000 at ``(3, 12, 6 + 4t)``, floor 100, sigma 3."""
    z, y, x = np.indices(SHAPE_A[2:])
    d2 = (z - 3) ** 2 + (y - 12) ** 2 + (x - (6 + 4 * t)) ** 2
    return (100 + np.rint(900.0 * np.exp(-d2 / 18.0))).astype(np.uint16)


DATASET_A = Dataset(
    shape_tczyx=SHAPE_A,
    np_dtype=np.dtype(np.uint16),
    wire_dtype=pb2.DTYPE_U16,
    open_generation=11,
    volume=blob_volume,
    t=2,
    levels=3,
    dataset_uri="hostsim://blob-movie",
    dataset_name="blob movie",
)

SHAPE_B = (4, 1, 4, 8, 8)


def dense_volume(t: int, c: int) -> np.ndarray:
    """Every voxel at or above the default threshold, in all three bands:
    ``400 + 20 (z + y + x + t)``, so a level-0 chunk is the whole volume."""
    z, y, x = np.indices(SHAPE_B[2:])
    return (400 + 20 * (z + y + x + t)).astype(np.uint16)


DATASET_B = Dataset(
    shape_tczyx=SHAPE_B,
    np_dtype=np.dtype(np.uint16),
    wire_dtype=pb2.DTYPE_U16,
    open_generation=12,
    volume=dense_volume,
    t=1,
    levels=2,
    dataset_uri="hostsim://dense",
    dataset_name="dense",
)

# -- the driver's own arithmetic (`SPEC.md` §2) ------------------------------

THRESHOLD, BAND_WIDTH, BAND_COUNT = 400.0, 200.0, 3
BAND_NAMES = {1: "dim", 2: "mid", 3: "bright"}
BAND_ROWS = {1: (60, 120, 255, 255), 2: (255, 200, 0, 255), 3: (255, 60, 60, 255)}
FOOT_ROW = (200, 200, 200, 255)


def bands_of(volume: np.ndarray, threshold: float) -> np.ndarray:
    v = volume.astype(np.float64)
    labels = np.zeros(v.shape, dtype=np.uint8)
    fg = v >= threshold
    labels[fg] = np.minimum(1 + np.floor((v[fg] - threshold) / BAND_WIDTH), BAND_COUNT).astype(np.uint8)
    return labels


def tight(labels: np.ndarray):
    nz = np.nonzero(labels)
    if nz[0].size == 0:
        return None
    lo = [int(a.min()) for a in nz]
    hi = [int(a.max()) + 1 for a in nz]
    return labels[lo[0] : hi[0], lo[1] : hi[1], lo[2] : hi[2]], tuple(lo)


def expected_bands(dataset: Dataset, level: int, t: int, threshold: float = THRESHOLD):
    return tight(bands_of(dataset.volume_at(t, 0, level), threshold))


def expected_footprint(dataset: Dataset, level: int, threshold: float = THRESHOLD):
    ever = None
    for t in range(dataset.shape_tczyx[0]):
        mask = dataset.volume_at(t, 0, level).astype(np.float64) >= threshold
        ever = mask if ever is None else ever | mask
    return tight(ever.astype(np.uint8))


def assert_chunk(chunk, expected, *, where: str) -> str:
    """One applied chunk against ``(labels, origin)`` or ``None`` (empty)."""
    if expected is None:
        assert chunk.labels is None, f"{where}: expected an empty chunk, got shape {chunk.shape}"
        return "empty"
    labels, origin = expected
    assert chunk.labels is not None, f"{where}: expected shape {labels.shape} at {origin}, got an empty chunk"
    assert chunk.labels.dtype == np.uint8, f"{where}: labels are {chunk.labels.dtype}, expected uint8"
    assert chunk.shape == labels.shape, f"{where}: shape {chunk.shape}, expected {labels.shape}"
    assert chunk.position_zyx == origin, f"{where}: origin {chunk.position_zyx}, expected {origin}"
    assert np.array_equal(chunk.labels, labels), f"{where}: labels differ ({int((chunk.labels != labels).sum())} voxels)"
    return f"{labels.shape} at {origin}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-dir", type=Path, default=HERE)
    plugin_dir = parser.parse_args().plugin_dir

    check("record encoder", check_encoder)
    if failures:
        print("\nthe encoder disagrees with the corpus; nothing else was run", file=sys.stderr)
        return 1

    host = Host(plugin_dir, DATASET_A)
    # No `V` here: the emulator grants it the `1.0` host's way -- at the first
    # read, sized to it, grown by supersede (`Host._v_for`).
    host.add(Segment.chunk_arena(BANDS_SEG, BANDS, DATASET_A, host.directory))
    host.add(Segment.chunk_arena(FOOT_SEG, FOOT, DATASET_A, host.directory))

    with host:

        def served() -> int:
            return len(host.chunk_log)

        def quiet(seconds: float, before: int, what: str) -> None:
            time.sleep(seconds)
            assert served() == before, f"{what}: {served() - before} chunk(s) arrived: {[c.key for c in host.chunk_log[before:]]}"

        def test_handshake():
            return host.check_handshake()

        def test_grants():
            """§5.7 rule 4: the two chunked overlays' arenas are HOST-SIZED
            from the open dataset -- one level-0 u16 volume plus one at the
            first coarser level -- and the manifest declares no `segment_mib`
            for them.  Each installed.  (`V` is the emulator's, granted at
            the first read as the host grants it: the two chunk sources read
            concurrently.)"""
            for seg in (BANDS_SEG, FOOT_SEG):
                ack = host.grant(seg)
                assert ack.WhichOneof("outcome") == "installed", (seg, ack)
            size = Segment.chunk_arena_bytes(DATASET_A)
            assert host.segments[BANDS_SEG].arena.extent == size == 2 * 8 * 24 * 32 + 2 * 4 * 12 * 16, size
            assert host.chunked == {BANDS: "per_t", FOOT: "static"}, host.chunked
            return f"O({BANDS}), O({FOOT}) installed; each chunk arena {size} B from the dataset"

        def test_wants_served_in_order():
            """§5.7 rules 1-2 (SPEC §4, acceptance 1-3): three wants, served
            once each in the host's order, each a bbox-tight u8 volume on the
            wanted level's lattice, segment-backed into the overlay's own
            arena with `t` present, the loan released in the tick that
            applied it."""
            wants = [(0, 2), (2, 2), (0, 3)]
            host.want_chunks(BANDS, wants)
            held = host.await_chunks(BANDS, wants)
            keys = [(c.level, c.t) for c in host.chunk_log]
            assert keys == wants, f"served {keys}, the host asked in order {wants}"
            notes = []
            for level, t in wants:
                chunk = held[(BANDS, level, t)]
                assert chunk.loan is not None and not chunk.inline, f"({level}, {t}) is not segment-backed"
                assert chunk.loan.segment == BANDS_SEG, f"({level}, {t}) rides segment {chunk.loan.segment}, not the overlay's"
                assert chunk.t == t, f"({level}, {t}) arrived with t={chunk.t}"
                notes.append(f"({level}, {t}) " + assert_chunk(chunk, expected_bands(DATASET_A, level, t), where=f"bands ({level}, {t})"))
            released = [r for r in host.loans_released if r[0] == BANDS_SEG]
            assert len(released) == 3 and not host.loan_uses, (released, host.loan_uses)
            reads = [(r.t, r.level) for r in host.read_requests]
            for level, t in wants:
                assert (t, level) in reads, f"no read of (t={t}, level={level}) for the chunk: {reads}"
            return "; ".join(notes) + "; 3 loans released"

        def test_bands_table():
            """SPEC §3 (acceptance 4): the table once per overlay -- row 0
            clear, one row per band, names by id, every band's visibility,
            opacity 0.6 -- inline."""
            table = host._await(lambda: host.label_tables.get(BANDS), 5.0, "no label table for bands")
            assert not np.any(table.colormap[0]), f"row 0 is not clear: {table.colormap[0]}"
            for label, rgba in BAND_ROWS.items():
                assert tuple(table.colormap[label]) == rgba, (label, tuple(table.colormap[label]))
            assert not np.any(table.colormap[4:]), "rows past the bands are not clear"
            assert table.label_names == BAND_NAMES, table.label_names
            assert table.label_visible == {1: True, 2: True, 3: True}, table.label_visible
            assert abs(table.style.opacity - 0.6) < 1e-9 and not table.style.hidden, table.style
            assert table.style.draw_priority == 1, f"bands draw_priority {table.style.draw_priority}, not 1 (above the footprint)"
            assert len([t for t in host.table_log if t.overlay_id == BANDS]) == 1, "the table went out more than once"
            return "rows 1-3 as pinned, names dim/mid/bright, all visible, opacity 0.6; sent once"

        def test_restated_set_serves_nothing():
            """§5.7 rule 2 (acceptance 5): the same set restated is a
            no-op -- nothing is served twice while its want stands."""
            before = served()
            host.want_chunks(BANDS, [(0, 2), (2, 2), (0, 3)])
            quiet(0.4, before, "restating the same set")
            return "the same three wants restated: nothing served again"

        def test_eviction_when_t_moves():
            """§5.7 (acceptance 6): the set moves with t -- the host evicts
            what left, keeps what stayed (not served again), and the plugin
            serves only what arrived."""
            before = served()
            host.want_chunks(BANDS, [(0, 3), (2, 3), (0, 4)])
            host.await_chunks(BANDS, [(2, 3), (0, 4)])
            new = [(c.level, c.t) for c in host.chunk_log[before:]]
            assert new == [(2, 3), (0, 4)], f"served {new}; (0, 3) stayed in the set and must not be served again"
            assert set(host.evicted) == {(BANDS, 0, 2), (BANDS, 2, 2)}, host.evicted
            assert sorted((k[1], k[2]) for k in host.chunks if k[0] == BANDS) == [(0, 3), (0, 4), (2, 3)]
            notes = [assert_chunk(host.chunks[(BANDS, level, t)], expected_bands(DATASET_A, level, t), where=f"bands ({level}, {t})") for level, t in new]
            return f"(2, 3) {notes[0]} and (0, 4) {notes[1]} served; (0, 2), (2, 2) evicted; (0, 3) kept"

        def test_t_zero_is_a_t():
            """A per_t chunk at t = 0 carries `t` -- the optional field is
            PRESENT with value 0, never absent (`ChunkWant.t`, `OverlayChunk.t`)."""
            host.want_chunks(BANDS, [(0, 0)])
            chunk = host.await_chunks(BANDS, [(0, 0)])[(BANDS, 0, 0)]
            assert chunk.t == 0 and chunk.t is not None, chunk.t
            return "(0, 0) " + assert_chunk(chunk, expected_bands(DATASET_A, 0, 0), where="bands (0, 0)") + "; t present and 0"

        def test_static_overlay():
            """SPEC §2, §3 (acceptance 7): the static overlay's wants carry no
            `t`; each answer is one volume per level -- foreground at ANY
            timepoint -- with `t` ABSENT on the chunk; every timepoint read at
            the wanted level; its own table, one row, no visibility list."""
            host.want_chunks(FOOT, [(0, None), (1, None)])
            held = host.await_chunks(FOOT, [(0, None), (1, None)], timeout=15.0)
            notes = []
            for level in (0, 1):
                chunk = held[(FOOT, level, None)]
                assert chunk.t is None, f"a static chunk arrived with t={chunk.t}"
                assert chunk.loan is not None and chunk.loan.segment == FOOT_SEG, chunk.loan
                notes.append(f"level {level} " + assert_chunk(chunk, expected_footprint(DATASET_A, level), where=f"footprint level {level}"))
            for level in (0, 1):
                read = {r.t for r in host.read_requests if r.level == level}
                assert read >= set(range(SHAPE_A[0])), f"level {level}: timepoints read {sorted(read)}, the footprint needs all {SHAPE_A[0]}"
            table = host._await(lambda: host.label_tables.get(FOOT), 5.0, "no label table for footprint")
            assert tuple(table.colormap[1]) == FOOT_ROW and not np.any(table.colormap[0]) and not np.any(table.colormap[2:]), table.colormap[:3]
            assert table.label_names == {1: "ever"} and table.label_visible == {}, (table.label_names, table.label_visible)
            assert abs(table.style.opacity - 0.3) < 1e-9, table.style
            assert table.style.draw_priority == 0, f"footprint draw_priority {table.style.draw_priority}, not 0 (under the bands)"
            assert not host.loan_uses
            return "; ".join(notes) + "; every t read at each level; table: one row 'ever', opacity 0.3"

        def test_threshold_reserves_every_want():
            """SPEC §5 (acceptance 8): a threshold change re-serves every
            volume the host currently wants, on both overlays, with the new
            labels; the reply names the threshold and the count."""
            before = served()
            done = host.invoke("movie_seg.threshold", value=800.0)
            assert done.ok, done.message
            assert done.message.startswith("threshold 800;"), done.message
            assert "3 wanted" in done.message, done.message
            host.await_chunk_log(before + 3, timeout=15.0)
            new = sorted((c.overlay_id, c.level, c.t) for c in host.chunk_log[before:])
            assert new == [(BANDS, 0, 0), (FOOT, 0, None), (FOOT, 1, None)], new
            quiet(0.3, before + 3, "after the re-serve")
            notes = [
                assert_chunk(host.chunks[(BANDS, 0, 0)], expected_bands(DATASET_A, 0, 0, 800.0), where="bands (0, 0) at 800"),
                assert_chunk(host.chunks[(FOOT, 0, None)], expected_footprint(DATASET_A, 0, 800.0), where="footprint 0 at 800"),
                assert_chunk(host.chunks[(FOOT, 1, None)], expected_footprint(DATASET_A, 1, 800.0), where="footprint 1 at 800"),
            ]
            return f"{done.message!r}; re-served {notes}"

        def test_empty_chunks():
            """SPEC §2 (acceptance 9): a threshold nothing reaches makes every
            chunk EMPTY -- labels absent, a state, no loan."""
            before = served()
            done = host.invoke("movie_seg.threshold", value=5000)
            assert done.ok and done.message.startswith("threshold 5000;"), done.message
            host.await_chunk_log(before + 3, timeout=15.0)
            for chunk in host.chunk_log[before:]:
                assert chunk.labels is None and chunk.loan is None, (chunk.key, chunk.shape)
            assert all(host.chunks[k].labels is None for k in host.chunks), "a held chunk is not empty"
            assert not host.loan_uses
            return "three empty chunks, no loans"

        def test_band_visibility():
            """SPEC §3, §5 (acceptance 10): hiding a band re-sends the bands
            table with that band's visibility false and nothing else changed;
            showing it again restores it.  Coalescing per overlay: latest wins."""
            count = len([t for t in host.table_log if t.overlay_id == BANDS])
            done = host.invoke("movie_seg.band_visible", band=2, visible=False)
            assert done.ok and done.message == "band 2 (mid) hidden", done.message
            host._await(lambda: True if len([t for t in host.table_log if t.overlay_id == BANDS]) >= count + 1 else None, 5.0, "no re-sent table")
            table = host.label_tables[BANDS]
            assert table.label_visible == {1: True, 2: False, 3: True}, table.label_visible
            # The host draws from the LUT, so a hidden band's row is
            # clear; `label_visible` still goes, for the compile.
            assert tuple(table.colormap[2]) == (*BAND_ROWS[2][:3], 0), f"hidden band 2's row {tuple(table.colormap[2])}: alpha must be 0"
            assert tuple(table.colormap[1]) == BAND_ROWS[1] and tuple(table.colormap[3]) == BAND_ROWS[3]
            assert table.label_names == BAND_NAMES
            done = host.invoke("movie_seg.band_visible", band=2, visible=True)
            assert done.ok and done.message == "band 2 (mid) visible", done.message
            host._await(lambda: True if len([t for t in host.table_log if t.overlay_id == BANDS]) >= count + 2 else None, 5.0, "no re-sent table")
            assert host.label_tables[BANDS].label_visible == {1: True, 2: True, 3: True}
            assert tuple(host.label_tables[BANDS].colormap[2]) == BAND_ROWS[2], "a shown band's row is its colour again"
            return "hidden then shown; the table re-sent twice, visibility as pinned, the hidden row's alpha 0 then restored"

        def test_bad_arguments_refused():
            """SPEC §5 (acceptance 11): a bad threshold or band is refused
            (`ok = false`) and changes nothing."""
            before = served()
            tables = len(host.table_log)
            for command, args in (
                ("movie_seg.threshold", {"value": -1.0}),
                ("movie_seg.threshold", {"value": "high"}),
                ("movie_seg.band_visible", {"band": 7, "visible": False}),
                ("movie_seg.band_visible", {"band": 1, "visible": "no"}),
            ):
                done = host.invoke(command, **args)
                assert not done.ok and done.message, (command, args, done)
            quiet(0.3, before, "after refused commands")
            assert len(host.table_log) == tables, "a refused command re-sent a table"
            return "four refusals; nothing served, no table"

        def test_nothing_wanted():
            """§5.7 rule 2: an empty set is a state -- nothing wanted; the host
            evicts everything and the plugin serves nothing."""
            before = served()
            host.want_chunks(BANDS, [])
            host.want_chunks(FOOT, [])
            assert not host.chunks, list(host.chunks)
            quiet(0.3, before, "with nothing wanted")
            return "both sets emptied; every held chunk evicted; nothing served"

        def test_dataset_change_regrants():
            """§5.7 rule 4 / §8.12 / `shared_memory_protocol.md` §9.5 (acceptance
            12): the dataset changes, so the host re-grants each chunk arena at
            a NEW id sized for the new dataset, naming the old in `supersedes`;
            the old retires when the new is installed (`SegmentReleased`)."""
            host.dataset = DATASET_B
            size = Segment.chunk_arena_bytes(DATASET_B)
            assert size == 512 + 64, size
            for old, new, subject in ((BANDS_SEG, BANDS2_SEG, BANDS), (FOOT_SEG, FOOT2_SEG, FOOT)):
                ack = host.supersede(old, Segment.chunk_arena(new, subject, DATASET_B, host.directory))
                assert ack.WhichOneof("outcome") == "installed", (new, ack)
                assert old in host.retired and old not in host.segments and new in host.segments
            done = host.invoke("movie_seg.threshold", value=400)
            assert done.ok and done.message.startswith("threshold 400;") and "0 wanted" in done.message, done.message
            # §9: a superseded segment owes `SegmentReleased` exactly as a
            # retired one does.  The emulator falls back to `RetireSegment`
            # so the run continues, and says so here.
            lapses = [d for d in host.diagnostics if d.startswith("supersede:")]
            assert not lapses, "; ".join(lapses)
            return f"both arenas re-granted at {size} B superseding the old; both old released with SegmentReleased; threshold back to 400"

        def test_deferred_publish():
            """§5.7's one rule that differs (acceptance 13): the host-sized
            arena holds two whole-volume chunks; the third is DEFERRED -- not
            failed, no `SegmentShortfall` -- until the host's release, then
            published with the same answer."""
            before = served()
            shortfalls = len(host.shortfalls)
            host.hold_chunk_releases = True
            try:
                host.want_chunks(BANDS, [(0, 1), (0, 2), (0, 3)])
                host.await_chunk_log(before + 2)
                first = [(c.level, c.t) for c in host.chunk_log[before:]]
                assert first == [(0, 1), (0, 2)], first
                for level, t in first:
                    chunk = host.chunks[(BANDS, level, t)]
                    assert chunk.shape == SHAPE_B[2:] and chunk.position_zyx == (0, 0, 0), (chunk.shape, chunk.position_zyx)
                    assert_chunk(chunk, expected_bands(DATASET_B, level, t), where=f"dense ({level}, {t})")
                quiet(0.8, before + 2, "with the arena full and the releases held")
                assert len(host.shortfalls) == shortfalls, f"a deferred publish is no shortfall: {host.shortfalls[shortfalls:]}"
                assert not host.violations, [(pb2.ViolationSite.Name(v.where), v.detail) for v in host.violations]
                assert host.release_held_chunks() == 2
                host.await_chunks(BANDS, [(0, 3)])
                third = host.chunks[(BANDS, 0, 3)]
                assert [(c.level, c.t) for c in host.chunk_log[before:]] == [(0, 1), (0, 2), (0, 3)]
                assert_chunk(third, expected_bands(DATASET_B, 0, 3), where="dense (0, 3)")
                assert host.release_held_chunks() == 1
            finally:
                host.hold_chunk_releases = False
            assert not host.loan_uses
            return "two whole-volume chunks fill the arena; the third waited 0.8 s with no shortfall, then followed the releases"

        def test_deferred_want_leaves_and_returns():
            """§5.7 (acceptance 14): a want that leaves the set while its
            publish is deferred is forgotten -- never published, never dropped
            by the host -- and served again when it returns."""
            host.want_chunks(BANDS, [])
            before = served()
            host.hold_chunk_releases = True
            try:
                host.want_chunks(BANDS, [(0, 0), (0, 1), (0, 2)])
                host.await_chunk_log(before + 2)
                assert [(c.level, c.t) for c in host.chunk_log[before:]] == [(0, 0), (0, 1)]
                host.want_chunks(BANDS, [(0, 0), (0, 1)])  # the deferred (0, 2) leaves
                assert host.release_held_chunks() == 2
                quiet(0.6, before + 2, "after the deferred want left the set")
                assert not host.dropped_chunks, f"the plugin published a chunk the host no longer wanted: {host.dropped_chunks}"
                host.want_chunks(BANDS, [(0, 0), (0, 1), (0, 2)])  # it returns
                host.await_chunks(BANDS, [(0, 2)])
                assert [(c.level, c.t) for c in host.chunk_log[before:]] == [(0, 0), (0, 1), (0, 2)]
                assert_chunk(host.chunks[(BANDS, 0, 2)], expected_bands(DATASET_B, 0, 2), where="dense (0, 2)")
                host.release_held_chunks()
            finally:
                host.hold_chunk_releases = False
            return "(0, 2) deferred, left the set: not published; returned: served"

        def test_sets_back_to_back():
            """§5.7 rule 2 (acceptance 15): the served-set
            follows EVERY set as it arrives — an empty set and a returning set
            sent back to back, closer than a pump tick, serve the returned
            wants again.  A server that reconciles only when it next wakes
            sees one set and serves nothing."""
            host.want_chunks(BANDS, [(0, 0), (0, 1)])  # (0, 2) leaves; both held and served
            before = served()
            tick, host.want_tick = host.want_tick, 0.0
            try:
                host.want_chunks(BANDS, [])
                host.want_chunks(BANDS, [(0, 0), (0, 1)])
            finally:
                host.want_tick = tick
            host.await_chunk_log(before + 2)
            again = sorted((c.level, c.t) for c in host.chunk_log[before:])
            assert again == [(0, 0), (0, 1)], again
            quiet(0.3, before + 2, "after the back-to-back sets")
            return "[] then [(0, 0), (0, 1)] in one tick: both served again"

        def test_coarser_level_on_the_new_dataset():
            """The new dataset's level 1 (acceptance 16): a per_t chunk on the
            coarser lattice and the static overlay's level-1 volume, both
            inside level 1's shape."""
            host.want_chunks(BANDS, [(1, 3)])
            host.want_chunks(FOOT, [(1, None)])
            host.await_chunks(BANDS, [(1, 3)])
            host.await_chunks(FOOT, [(1, None)], timeout=10.0)
            a = assert_chunk(host.chunks[(BANDS, 1, 3)], expected_bands(DATASET_B, 1, 3), where="dense (1, 3)")
            b = assert_chunk(host.chunks[(FOOT, 1, None)], expected_footprint(DATASET_B, 1), where="dense footprint 1")
            assert host.chunks[(FOOT, 1, None)].t is None
            return f"bands (1, 3) {a}; footprint level 1 {b}"

        def test_accounting():
            """Nothing refused by this host, nothing dropped, nothing reported
            by the plugin, no shortfall, no loan outstanding, nothing
            unhandled either way."""
            assert not host.refusals, host.refusals
            assert not host.dropped_chunks, host.dropped_chunks
            assert not host.violations, [(pb2.ViolationSite.Name(v.where), v.detail) for v in host.violations]
            assert not host.shortfalls, host.shortfalls
            assert not host.loan_uses, host.loan_uses
            assert not [d for d in host.diagnostics if not d.startswith("supersede:")], host.diagnostics
            assert not host.unhandled, host.unhandled
            return f"{served()} chunks applied, {len(host.table_log)} tables, {len(host.evicted)} evictions; no refusal, drop, violation or shortfall"

        def test_panel():
            """SPEC §5a (acceptance 17): the panel, which is how a person starts
            this plugin in vizi.  Its box sends the threshold as TEXT, and each
            checkbox fires its own band command with a bare bool; the panel
            shows the plugin's state, whichever form changed it."""
            # Changed while the panel is CLOSED: the host
            # keeps a declared panel's state and shows it at open, so the box
            # and the checkbox must say so the moment it opens.
            assert PANEL not in host.opened_panels
            done = host.invoke("movie_seg.threshold", value=" 800 ")
            assert done.ok and done.message.startswith("threshold 800;"), done.message
            tables = len([t for t in host.table_log if t.overlay_id == BANDS])
            done = host.invoke("movie_seg.band_2", value=False)
            assert done.ok and done.message == "band 2 (mid) hidden", done.message
            host._await(lambda: True if len([t for t in host.table_log if t.overlay_id == BANDS]) > tables else None, 5.0, "no re-sent table")
            assert host.label_tables[BANDS].label_visible == {1: True, 2: False, 3: True}, host.label_tables[BANDS].label_visible
            time.sleep(0.3)  # the patches travel; the host holds them for the closed panel
            host.open_panel(PANEL)
            assert host.status_of(PANEL, "threshold") == "800", ("not shown at open", host.status_of(PANEL, "threshold"))
            assert host.status_of(PANEL, "band_2") == "False", ("not shown at open", host.status_of(PANEL, "band_2"))
            done = host.invoke("movie_seg.band_visible", band=2, visible=True)
            assert done.ok, done.message
            assert host.await_status(PANEL, "True", widget="band_2") == "True", "the checkbox did not follow the scripted form"
            for command, args in (("movie_seg.threshold", {"value": "high"}), ("movie_seg.band_3", {"value": "no"})):
                done = host.invoke(command, **args)
                assert not done.ok and done.message, (command, args, done)
            done = host.invoke("movie_seg.threshold", value="400")
            assert done.ok and done.message.startswith("threshold 400;"), done.message
            assert host.await_status(PANEL, "400", widget="threshold") == "400"
            assert not host.refusals, host.refusals
            return "threshold as text; band_2 from its checkbox; each widget follows the state; two refusals"

        def test_zero_length_frame_ends_the_session():
            """`doorbell_protocol.md` §8.  A
            0-byte record carrying a descriptor is a FRAME, not a hang-up: the
            plugin faults it `bad_length` (ctrunc and wrong_fd_count are
            checked first and do not apply), closes the descriptor, reports
            `DoorbellFault` -- and then keeps its stream open, because a
            doorbell that closes ends the session from the HOST's side: this
            emulator answers `DoorbellShutdown` and, after the window,
            `Shutdown{session_fault}`.  Here because movie_seg's doorbell
            carries no tool, so nothing else is lost; LAST before teardown,
            because it ends the session."""
            fd = os.open(os.devnull, os.O_RDONLY)
            try:
                host.send_raw_doorbell(b"", [fd])
            finally:
                os.close(fd)
            fault = host._await(lambda: host.plugin_doorbell_faults[0] if host.plugin_doorbell_faults else None, 5.0, "no DoorbellFault for a 0-byte record carrying a descriptor")
            name = pb2.DoorbellFailureClass.Name(fault.failure_class)
            assert fault.failure_class == pb2.DOORBELL_FAILURE_BAD_LENGTH, f"faulted {name}, not bad_length"
            ended = host.stream_ended.wait(DOORBELL_WINDOW_MS / 1000.0 + SHUTDOWN_WINDOW_S + 1.0)
            assert ended, "the session did not end on the host's Shutdown{session_fault}"
            assert host.shutdown_report is not None, "no ShutdownReport before the close"
            return f"faulted {name}; the stream stayed open until Shutdown{{session_fault}}, then closed"

        def test_teardown():
            assert host.shutdown(), "the stream did not end inside the window"
            report = host.shutdown_report
            assert report is not None and report.outcome in (pb2.ShutdownReport.COMPLETED, pb2.ShutdownReport.NO_HOOK), report
            return f"ShutdownReport {pb2.ShutdownReport.Outcome.Name(report.outcome)}, then a clean end of stream"

        check("handshake", test_handshake)
        if failures:
            return 1
        check("grants: host-sized chunk arenas", test_grants)
        check("1-3. three wants served once, in order, segment-backed, released", test_wants_served_in_order)
        check("4. the bands table", test_bands_table)
        check("5. a restated set serves nothing", test_restated_set_serves_nothing)
        check("6. eviction when t moves", test_eviction_when_t_moves)
        check("t = 0 is a t", test_t_zero_is_a_t)
        check("7. the static overlay: no t, every timepoint, its own table", test_static_overlay)
        check("8. a threshold change re-serves every want", test_threshold_reserves_every_want)
        check("9. empty chunks", test_empty_chunks)
        check("10. band visibility re-sends the table", test_band_visibility)
        check("11. bad arguments refused", test_bad_arguments_refused)
        check("nothing wanted", test_nothing_wanted)
        check("12. dataset change: arenas re-granted, superseding", test_dataset_change_regrants)
        check("13. a full arena defers the publish, no shortfall", test_deferred_publish)
        check("14. a deferred want leaves and returns", test_deferred_want_leaves_and_returns)
        check("15. two sets back to back: the served-set follows each", test_sets_back_to_back)
        check("16. level 1 on the new dataset, per_t and static", test_coarser_level_on_the_new_dataset)
        check("accounting", test_accounting)
        check("17. the panel: text threshold, a checkbox per band, state shown", test_panel)
        check("a 0-byte frame with a descriptor ends the session", test_zero_length_frame_ends_the_session)
        check("teardown", test_teardown)

    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("\nhost emulation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
