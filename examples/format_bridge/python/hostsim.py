#!/usr/bin/env python3
"""Drive the crate format bridge end to end over the real transport, with no vizi.

    .venv/bin/python hostsim.py                       # this implementation
    .venv/bin/python hostsim.py --plugin-dir ../rust  # any other implementation of SPEC.md

``selftest.py`` checks the format and the pieces that need no host.  This
checks the protocol: it launches the plugin as the Broker would (the
manifest's ``entry.command`` in the plugin's own directory, gRPC on a Unix
domain socket, an inherited doorbell, segments granted over ``SCM_RIGHTS``)
and plays host in ``docs/spec/reader_writer_contract.md``'s order — open,
THEN the ``G`` grant sized from the reply and acknowledged before the first
read; ``X`` standalone BEFORE ``OpenExport`` with the host waiting for
nothing; chunks addressed by SOURCE ``t`` and channel; the replacing second
open; a deposit that does not fit; both provisioning paths for the
calibration table; the two commands that wait for a human; the panel; the
re-entrancy guard's reply; teardown.

**It drives any implementation of ``SPEC.md``, not only this one.**  The
scenarios are the specification's behavior, so ``--plugin-dir ../<lang>`` is
the same run against that implementation: what it reads is its
``vizi-plugin.json`` and what it launches is that manifest's command.  Two
sessions run: one with the calibration asset published through
``VIZI_PLUGIN_ASSETS`` (the manifest-asset path), and a short one without it.

The emulator itself is ``examples/vizi_hostsim``; what is here is this
example's fixtures, its dataset for the host-data-access branch, and its
scenarios.  Where the emulator cannot play the host — it has no self-serve
guard — the scenario says so and provokes the same reply the host would
send.  Unix only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))  # examples/, for vizi_hostsim

from crate.format import Crate  # noqa: E402  (offline checks of what the plugin wrote)
from vizi_hostsim import (  # noqa: E402
    SHUTDOWN_WINDOW_S,
    Dataset,
    Host,
    PluginError,
    Segment,
    check,
    check_encoder,
    failures,
    segment_id,
)
from vizi_plugin_sdk import pb2  # noqa: E402

PANEL = "crate"
V_SEG = segment_id(1)
X_SEG = segment_id(2)
NO_SPACING_FROM_HOST = "unknown — the host publishes no voxel size to a plugin without a tool"
SAMPLE_LABEL = "2.000 × 0.325 × 0.325 µm (z × y × x), Δt 1.5 s"


def fixture(name: str) -> str:
    return str(FIXTURES / name)


def expected_volume(t: int, c: int) -> np.ndarray:
    """SPEC.md's formula, written here rather than imported from anywhere."""
    z, y, x = np.meshgrid(np.arange(4), np.arange(6), np.arange(5), indexing="ij")
    return (10000 * t + 1000 * c + 100 * z + 10 * y + x).astype(np.uint16)


def sample_document() -> tuple[str, str]:
    with Crate.open(FIXTURES / "sample.crate") as crate:
        return crate.metadata()


def document_of(name: str) -> tuple[str, str]:
    with Crate.open(FIXTURES / name) as crate:
        return crate.metadata()


# --- the dataset the emulated host serves natively ----------------------------------

HOST_SHAPE = (2, 2, 3, 4, 5)


def host_volume(t: int, c: int) -> np.ndarray:
    _, _, depth, height, width = HOST_SHAPE
    z, y, x = np.meshgrid(np.arange(depth), np.arange(height), np.arange(width), indexing="ij")
    return (500 * t + 100 * c + 20 * z + 5 * y + x).astype(np.uint16)


DATASET = Dataset(
    shape_tczyx=HOST_SHAPE,
    np_dtype=np.dtype(np.uint16),
    wire_dtype=pb2.DTYPE_U16,
    open_generation=41,
    volume=host_volume,
    t=1,
    # `c` is deprecated and always 0 on the host; `crate.describe`
    # reads `visible_channels[0]` (SPEC.md).  Different on purpose.
    c=0,
    visible_channels=(1,),
    dataset_uri="hostsim://native",
    dataset_name="native",
)


# --- helpers ---------------------------------------------------------------------------


def open_files(host: Host) -> set[str] | None:
    """What the plugin process holds open — Linux ``/proc`` only, ``None``
    elsewhere.  The emulator keeps its ``Popen`` private; this is the one
    thing a driver needs from it, and it is a check on the plugin, not on
    the emulator."""
    pid = host._plugin.pid  # noqa: SLF001
    fd_dir = Path(f"/proc/{pid}/fd")
    if not fd_dir.is_dir():
        return None
    links = set()
    for entry in fd_dir.iterdir():
        try:
            links.add(os.readlink(entry))
        except OSError:
            pass
    return links


def holds(host: Host, path: str) -> bool | None:
    files = open_files(host)
    return None if files is None else str(Path(path).resolve()) in files


def await_widget(host: Host, widget: str, needle: str, timeout: float = 5.0) -> str:
    return host.await_status(PANEL, needle, timeout, widget=widget)


def await_change(host: Host, widget: str, previous: str, timeout: float = 5.0) -> str:
    """The label once it no longer says ``previous``, or whatever it says at
    the deadline.  For outcomes SPEC.md says reach the panel without pinning
    the words -- the driver asserts that *something* was reported, not what."""
    deadline = time.monotonic() + timeout
    while host.status_of(PANEL, widget) == previous and time.monotonic() < deadline:
        time.sleep(0.02)
    return host.status_of(PANEL, widget)


def assert_describe_stats(status: str, expected: np.ndarray) -> None:
    """SPEC.md: "Report min, max and mean to `status`".  The integers must
    appear as written; the mean's precision and the layout are the plugin's."""
    low = status.lower()
    assert "min" in low and "max" in low and "mean" in low, status
    assert str(int(expected.min())) in status and str(int(expected.max())) in status, (status, expected.min(), expected.max())


def expect_plugin_error(fn, needle: str = "", kind: int | None = None) -> PluginError:
    try:
        fn()
    except PluginError as exc:
        assert needle in exc.args[0], (needle, exc.args[0])
        if kind is not None:
            assert exc.kind == kind, (pb2.Error.Kind.Name(exc.kind), exc.args[0])
        return exc
    raise AssertionError(f"expected an Error reply containing {needle!r}")


# --- the run ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--plugin-dir",
        type=Path,
        default=HERE,
        help="directory holding the vizi-plugin.json to launch (default: this one)",
    )
    plugin_dir = parser.parse_args().plugin_dir

    check("record encoder", check_encoder)
    if failures:
        print("\nthe encoder disagrees with the corpus; nothing else was run", file=sys.stderr)
        return 1

    tmp = tempfile.TemporaryDirectory(prefix="hostsim-crate-")
    out = Path(tmp.name)
    # `VIZI_PLUGIN_ASSETS` is a JSON mapping {asset_id: absolute path}
    # (`control_plane_protocol.md` §2.1); the emulator launches with its own
    # environment, so this is how the manifest asset reaches the plugin.
    os.environ["VIZI_PLUGIN_ASSETS"] = json.dumps({"calibration": fixture("vizi-crate-cal.bin")})
    run_provisioned(plugin_dir, out)
    os.environ.pop("VIZI_PLUGIN_ASSETS", None)
    run_unprovisioned(plugin_dir)
    tmp.cleanup()

    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("\nhost emulation passed")
    return 0


def run_provisioned(plugin_dir: Path, out: Path) -> None:
    host = Host(plugin_dir, DATASET)
    # `G` is not here: it is granted AFTER `ReaderOpened`, sized from the
    # reply; `X` is added at the export scenario, granted BEFORE `OpenExport`.
    host.add(Segment.host_ring(V_SEG, "V", 1 << 20, host.directory))
    host.add(Segment.host_ring(X_SEG, "X", 1 << 20, host.directory))
    host.volume_segment = V_SEG
    host.export_segment = X_SEG
    next_g = iter(range(10, 40))
    state: dict = {}

    def grant_reader_arena(opened: pb2.ReaderOpened) -> int:
        """§1.1's `G`, sized from the reply -- and, when a reader is being
        REPLACED, §1.2's H3-H5: granted with `supersedes` naming
        the current `G`, whose `SegmentReleased` the host awaits; the old
        reader gets no `CloseReader` and no `RetireSegment`."""
        seg = segment_id(next(next_g))
        arena = Segment.reader_arena(seg, opened, host.directory)
        old = state.pop("g", None)
        if old is None:
            host.add(arena)
            ack = host.grant(seg)
        else:
            ack = host.supersede(old, arena)
            assert old not in host.segments and old in host.retired, "the superseded G was not released"
        assert ack.WhichOneof("outcome") == "installed", ack
        state["g"] = seg
        return seg

    def retire_reader_arena() -> None:
        """§1.8, after a deliberate `CloseReader`: the host retires `G`."""
        seg = state.pop("g", None)
        if seg is not None:
            released = host.retire_segment(seg)
            assert released.segment_id == seg

    def open_crate(uri: str) -> pb2.ReaderOpened:
        """§1.2 H1 -- sent while the previous reader is open -- and I1: a
        handle is never reused within a session."""
        opened = host.open_reader(uri, "crate")
        seen = state.setdefault("handles", [])
        assert opened.handle not in seen, f"handle {opened.handle!r} reused within the session (§1.2 I1): {seen}"
        seen.append(opened.handle)
        return opened

    with host:
        def test_handshake():
            return host.check_handshake()

        def test_grants():
            ack = host.grant(V_SEG)
            assert ack.WhichOneof("outcome") == "installed", ack
            return "V installed at session setup; G waits for an open, X for an export"

        def test_panel_before_anything():
            """Open the panel first so the reader's patches below are
            observable -- a patch to a closed panel goes nowhere (SPEC.md,
            "The panel")."""
            host.open_panel(PANEL)
            cal = await_widget(host, "cal", "manifest asset")
            head = host.status_of(PANEL, "head")
            # SPEC.md: `head` "names the open dataset or says none is open"; the words are the plugin's.
            assert head and not any(f.name in head for f in FIXTURES.glob("*.crate")), head
            assert cal == "manifest asset", cal
            return f"head {head!r}, cal {cal!r} (VIZI_PLUGIN_ASSETS resolved)"

        def test_open_unfinalized():
            # SPEC.md §1: an unfinished crate is `IO_ERROR`.
            exc = expect_plugin_error(
                lambda: open_crate(fixture("unfinalized.crate")), "did not complete", pb2.Error.IO_ERROR
            )
            assert "unfinished" in exc.args[0]
            return f"refused: {exc.args[0]!r} (Error.kind {pb2.Error.Kind.Name(exc.kind)})"

        def test_open_sample():
            opened = open_crate(fixture("sample.crate"))
            assert tuple(opened.shape_tczyx) == (3, 2, 4, 6, 5) and opened.dtype == pb2.DTYPE_U16, opened
            assert opened.little_endian
            assert (opened.spacing_z, opened.spacing_y, opened.spacing_x) == (2.0, 0.325, 0.325), opened
            assert opened.spacing_unit == "um" and opened.time_increment == 1.5 and opened.time_unit == "s", opened
            state["handle"] = opened.handle
            head = await_widget(host, "head", "sample.crate")
            assert "sample.crate" in head, head
            vox = await_widget(host, "vox", "µm")
            assert vox == SAMPLE_LABEL, vox
            assert holds(host, fixture("sample.crate")) in (True, None)
            grant_reader_arena(opened)
            return f"ReaderOpened with the header's calibration; head {head!r}; vox {vox!r}; G granted after the open and acknowledged"

        def test_reads():
            handle = state["handle"]
            for t in range(3):
                for c in range(2):
                    got = host.read_volume(handle, t, c)
                    assert got.ref.segment_id == state["g"] and np.array_equal(got.array, expected_volume(t, c)), (t, c)
            plane = host.read_plane(handle, 1, 2, 1)
            assert plane.array.shape == (1, 6, 5) and np.array_equal(plane.array[0], expected_volume(1, 1)[2])
            doc = host.metadata(handle)
            form, text = sample_document()
            assert (doc.form, doc.text) == (form, text), "the document did not come back byte for byte"
            for kind, call in (
                ("level", lambda: host.read_volume(handle, 0, 0, level=1)),
                ("t", lambda: host.read_volume(handle, 3, 0)),
                ("c", lambda: host.read_volume(handle, 0, 2)),
                ("z", lambda: host.read_plane(handle, 0, 4, 0)),
            ):
                # the contract pins the KIND (`reader_writer_contract.md` §1.5); naming the axis is the plugin's
                expect_plugin_error(call, "", pb2.Error.OUT_OF_BOUNDS)
            released = [r for r in host.loans_released if r[0] == state["g"]]
            assert len(released) == 7, len(released)
            return "6 volumes + 1 plane match the formula, each released; metadata verbatim; level 1 / t=3 / c=2 / z=4 OUT_OF_BOUNDS"

        def test_badcrc_and_verify_toggle():
            opened = open_crate(fixture("badcrc.crate"))  # H1: while sample.crate is open
            state["handle"] = opened.handle
            was_open = holds(host, fixture("sample.crate"))
            assert was_open in (False, None), "sample.crate is still open after the replacing OpenReader"
            grant_reader_arena(opened)
            exc = expect_plugin_error(lambda: host.read_volume(opened.handle, 1, 0), "(t=1, c=0)")
            assert "CRC" in exc.args[0], exc.args[0]
            for t, c in ((0, 0), (0, 1), (1, 1), (2, 0), (2, 1)):
                assert np.array_equal(host.read_volume(opened.handle, t, c).array, expected_volume(t, c)), (t, c)
            done = host.invoke("crate.set_verify", value=False)
            assert done.ok, done.message
            wrong = host.read_volume(opened.handle, 1, 0).array
            assert wrong.shape == (4, 6, 5) and not np.array_equal(wrong, expected_volume(1, 0)), "verification off should serve the bad block"
            assert host.invoke("crate.set_verify", value=True).ok
            expect_plugin_error(lambda: host.read_volume(opened.handle, 1, 0), "CRC")
            return f"replaced sample.crate (released: {was_open is False or 'unchecked'}); (t=1, c=0) refused {exc.args[0]!r}; five others clean; served wrong with verification off"

        def test_spacing_fixtures():
            opened = open_crate(fixture("spacing-conflict.crate"))
            assert (opened.spacing_z, opened.spacing_y, opened.spacing_x, opened.time_increment) == (2.0, 0.325, 0.325, 1.5), opened
            assert await_widget(host, "vox", "2.000") == SAMPLE_LABEL
            opened = open_crate(fixture("nospacing.crate"))
            assert (opened.spacing_z, opened.spacing_y, opened.spacing_x, opened.time_increment) == (0.0, 0.0, 0.0, 0.0), opened
            # `0` / `""` mean unknown (`reader_writer_contract.md` §1.7): with every value
            # zero, naming the format's fixed unit and leaving it empty both conform.
            assert opened.spacing_unit in ("", "um"), opened.spacing_unit
            vox = await_widget(host, "vox", "(unknown)")
            assert vox == "(unknown)", vox
            grant_reader_arena(opened)
            assert np.array_equal(host.read_volume(opened.handle, 2, 1).array, expected_volume(2, 1))
            state["handle"] = opened.handle
            return "spacing-conflict: the header's values, not the document's; nospacing: zeros, (unknown), and it reads"

        def test_calibrated_with_manifest_asset():
            opened = open_crate(fixture("sample-calibrated.crate"))
            state["handle"] = opened.handle
            head = await_widget(host, "head", "calibrated")
            cal = await_widget(host, "cal", "manifest asset")
            assert cal == "manifest asset", cal
            grant_reader_arena(opened)
            for t, c in ((0, 0), (0, 1), (2, 1)):
                assert np.array_equal(host.read_volume(opened.handle, t, c).array, expected_volume(t, c)), (t, c)
            return f"opened with the manifest asset ({cal!r}); decodes to sample.crate's values; head {head!r}"

        def test_arena_too_small():
            """A deposit that does not fit fails the read with the shortfall,
            never overwrites, never blocks (SPEC.md §2).  The too-small arena
            is a RE-GRANT of the open reader's `G`, superseding it (§9.5)."""
            small = segment_id(next(next_g))
            ack = host.supersede(state.pop("g"), Segment.plugin_arena(small, "G", 32 + 64, host.directory))
            assert ack.WhichOneof("outcome") == "installed"
            state["g"] = small
            before = len(host.shortfalls)
            exc = expect_plugin_error(lambda: host.read_volume(state["handle"], 0, 0))  # the words are the plugin's; the shortfall below is the check
            deadline = time.monotonic() + 2.0
            while len(host.shortfalls) == before and time.monotonic() < deadline:
                time.sleep(0.02)
            assert len(host.shortfalls) == before + 1, "no SegmentShortfall followed the refusal"
            short = host.shortfalls[-1]
            # The SDK reserves in §7's 64-byte slots, so 240 bytes wanted is a
            # 256-byte reservation on the wire.
            assert short.segment_id == small and 240 <= short.wanted_bytes <= 256 and short.largest_free_bytes < 240, short
            opened = open_crate(fixture("sample-calibrated.crate"))  # H1; its G supersedes the small one
            state["handle"] = opened.handle
            grant_reader_arena(opened)
            assert np.array_equal(host.read_volume(opened.handle, 1, 0).array, expected_volume(1, 0))
            return f"{exc.args[0]!r}; SegmentShortfall wanted {short.wanted_bytes}, free {short.largest_free_bytes}; a sized arena then serves"

        def test_close_reader():
            handle = state["handle"]
            closed = host.close_reader(handle)
            assert closed.handle == handle
            released = holds(host, fixture("sample-calibrated.crate"))
            assert released in (False, None), "the file is still open after CloseReader"
            head = await_change(host, "head", host.status_of(PANEL, "head")) if "sample-calibrated.crate" in host.status_of(PANEL, "head") else host.status_of(PANEL, "head")
            assert "sample-calibrated.crate" not in head, head  # `head` says none is open; the words are the plugin's
            expect_plugin_error(lambda: host.read_volume(handle, 0, 0), handle, pb2.Error.NOT_FOUND)
            expect_plugin_error(lambda: host.metadata(handle), handle, pb2.Error.NOT_FOUND)
            assert host.close_reader(handle).handle == handle, "a second CloseReader was not answered Closed"
            retire_reader_arena()
            return f"Closed; file released ({released is False or 'unchecked'}); read and metadata after close NOT_FOUND; close idempotent"

        def test_describe_via_host():
            done = host.invoke("crate.describe")
            assert done.ok, done.message
            status = await_widget(host, "status", "read via host data access")
            expected = host_volume(DATASET.t, DATASET.visible_channels[0]).astype(np.float64)
            assert_describe_stats(status, expected)
            vox = await_widget(host, "vox", "no voxel size")
            assert vox == NO_SPACING_FROM_HOST, vox
            return f"{status!r}; vox {vox!r}"

        def test_describe_directly():
            """`reader_writer_contract.md` §1.9: the host refuses a read of a
            dataset this plugin's own reader serves (`UNSUPPORTED`, at once)
            rather than deadlock on it.  The emulator's dataset is never
            plugin-served, so `self_serve_guard` makes it send that reply."""
            opened = open_crate(fixture("sample.crate"))
            state["handle"] = opened.handle
            grant_reader_arena(opened)
            host.self_serve_guard = True
            try:
                done = host.invoke("crate.describe")
                assert done.ok, done.message
                status = await_widget(host, "status", "read directly")
            finally:
                host.self_serve_guard = False
            expected = expected_volume(DATASET.t, DATASET.visible_channels[0]).astype(np.float64)
            assert_describe_stats(status, expected)
            assert "unavailable for a dataset this plugin serves" in status, status
            vox = await_widget(host, "vox", "µm")
            assert vox == SAMPLE_LABEL, vox
            return f"{status!r}; vox {vox!r}"

        def test_busy_guard():
            """Two describes with the first still reading: SPEC.md pins no
            guard, so refusing the second or serializing it both conform.
            The host holds the first read (`read_delay`) so the second
            invocation lands inside it, and keeps the self-serve reply up
            until every accepted describe has reported -- an acknowledgement
            means *accepted*, not *done*."""
            before = len(host.patches)
            host.self_serve_guard = True
            host.read_delay = 0.4
            try:
                first = host.invoke("crate.describe")
                second = host.invoke("crate.describe")
                assert first.ok, first.message
                expected = 1 + (1 if second.ok else 0)

                def reported() -> int:
                    return sum(
                        1
                        for panel, widget, value in host.patches[before:]
                        if panel == PANEL and widget == "status" and isinstance(value, str) and "read directly" in value
                    )

                deadline = time.monotonic() + 15.0
                while reported() < expected and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert reported() == expected, f"{reported()} describe(s) reported of {expected} accepted"
            finally:
                host.self_serve_guard = False
                host.read_delay = 0.0
            return "an overlapping describe was refused" if not second.ok else "an overlapping describe was serialized"

        def test_export_deposit_then_inline():
            """`reader_writer_contract.md` §2.1: `X` standalone BEFORE `OpenExport`,
            the host waiting for nothing — the `M` half late so the first chunk
            is held; chunks carry SOURCE `t` and channel; one deposit, one inline."""
            host.grant_install(X_SEG)
            form, text = sample_document()
            path = str(out / "roundtrip.crate")
            # The SOURCE's document, the artifact's one
            # channel as the file named it, and the host's record.
            record = {"manifest_version": 1, "source": {"name": "sample.crate"}, "selection": {"t_indices": [1, 2], "channels": [1], "channel_names": ["GFP"]}}
            opened = host.open_export(
                "crate", path, (2, 1, 4, 6, 5), pb2.DTYPE_U16, source_metadata=(form, text),
                spacing_zyx=(2.0, 0.325, 0.325), spacing_unit="um", time_increment=1.5, time_unit="s",
                channels=[pb2.ChannelInfo(index=1, name="GFP", source=pb2.CHANNEL_NAME_SOURCE_FILE)], provenance=record,
            )
            assert X_SEG not in host.acks, "the install half alone must not be acknowledged installed"
            threading.Timer(0.2, host.grant_capability, args=(X_SEG,)).start()
            written = host.export_chunk(opened.handle, 1, [1], [expected_volume(1, 1)], dtype=pb2.DTYPE_U16)
            assert written.total_bytes == 240, written
            assert host.await_ack(X_SEG).WhichOneof("outcome") == "installed"
            written = host.export_chunk(opened.handle, 2, [1], [expected_volume(2, 1)], dtype=pb2.DTYPE_U16, inline=True)
            assert written.total_bytes == 240
            before_status = host.status_of(PANEL)
            done = host.finish_export(opened.handle)
            assert done.handle == opened.handle
            deposits = [r for r in host.released if r[0] == X_SEG]
            assert len(deposits) == 1, deposits
            status = await_change(host, "status", before_status)
            assert status != before_status, "FinishExport did not move `status`"
            with Crate.open(Path(path)) as crate:
                assert crate.header.shape_tczyx == (2, 1, 4, 6, 5)
                assert np.array_equal(crate.read_block(0, 0), expected_volume(1, 1)), "source t=1 is artifact t=0"
                assert np.array_equal(crate.read_block(1, 0), expected_volume(2, 1))
                assert crate.header.spacing_zyx == (2.0, 0.325, 0.325) and crate.header.time_increment == 1.5
                assert crate.header.derived, "an exported crate is DERIVED (SPEC.md §7)"
                assert crate.metadata() == (form, text), "the source's document was not kept verbatim, as provenance"
                assert crate.channel_names() == ("GFP",), ("OpenExport.channels' FILE name", crate.channel_names())
                assert json.loads(crate.provenance) == record, ("OpenExport.provenance", crate.provenance)
            state["roundtrip"] = path
            return f"held until the late M, one deposit released, one inline; t = 1, 2 -> 0, 1; calibration from the fields; DERIVED, the source's document kept, the FILE name and the record written; {status!r}"

        def test_refused_open_leaves_the_reader():
            """§1.2 H2: the plugin opens the next file BEFORE it
            closes the previous reader, so a refused open changes nothing --
            the open reader still answers, its `G` still stands."""
            handle, g = state["handle"], state["g"]
            expect_plugin_error(lambda: host.open_reader(fixture("unfinalized.crate"), "crate"), "did not complete", pb2.Error.IO_ERROR)
            assert state["g"] == g and g in host.segments
            assert np.array_equal(host.read_volume(handle, 0, 0).array, expected_volume(0, 0)), "the open reader was closed by a refused open"
            return f"unfinalized.crate refused; {handle} still reads through its G"

        def test_export_reopens_in_own_reader():
            """Property 1 and the plugin's own half of property 2: what it
            exported, it opens, with the calibration and the names intact --
            and, being DERIVED, it serves no document: the one it
            keeps is its source's."""
            opened = open_crate(state["roundtrip"])
            assert tuple(opened.shape_tczyx) == (2, 1, 4, 6, 5) and opened.spacing_z == 2.0 and opened.time_increment == 1.5
            assert list(opened.channel_names) == ["GFP"], ("ReaderOpened.channel_names", list(opened.channel_names))
            grant_reader_arena(opened)
            assert np.array_equal(host.read_volume(opened.handle, 1, 0).array, expected_volume(2, 1))
            doc = host.metadata(opened.handle)
            assert (doc.form, doc.text) == ("", ""), ("a DERIVED crate served its source's document as its own", doc.form)
            state["handle"] = opened.handle
            return "the exported crate opens; voxels, calibration and the name GFP survive the plugin's own round trip; its source's document is not served"

        def test_export_multichannel_chunk():
            """Also the `1.0` host's `X`: a FRESH arena for this job, sized to
            its one chunk and granted before `OpenExport` with no ack awaited,
            then retired after it (`Host.grant_export_arena`; the retire is
            legal at any time)."""
            path = str(out / "twochannel.crate")
            job_x = host.grant_export_arena(480)
            # A host placeholder is never written as a name; a USER name is.
            channels = [
                pb2.ChannelInfo(index=1, name="Channel 2", source=pb2.CHANNEL_NAME_SOURCE_PLACEHOLDER),
                pb2.ChannelInfo(index=0, name="DAPI", source=pb2.CHANNEL_NAME_SOURCE_USER),
            ]
            opened = host.open_export("crate", path, (1, 2, 4, 6, 5), pb2.DTYPE_U16, channels=channels, provenance="")
            written = host.export_chunk(opened.handle, 7, [1, 0], [expected_volume(0, 1), expected_volume(0, 0)], dtype=pb2.DTYPE_U16)
            assert written.total_bytes == 480
            host.finish_export(opened.handle)
            host.retire_export_arena()
            assert job_x not in host.segments
            with Crate.open(Path(path)) as crate:
                assert np.array_equal(crate.read_block(0, 0), expected_volume(0, 1)), "source channel 1 named first is artifact channel 0"
                assert np.array_equal(crate.read_block(0, 1), expected_volume(0, 0))
                assert crate.channel_names() == ("", "DAPI"), ("a PLACEHOLDER is no name; USER is", crate.channel_names())
                assert crate.provenance_bytes == b"" and crate.metadata() == ("", ""), "no record and no document were sent"
            status = await_widget(host, "status", "without voxel size")  # the one export line SPEC.md quotes
            assert "without voxel size" in status, status
            return f"a fresh X for the job, retired after; one chunk, two channels in buffer order, source t=7 -> 0; names '' (a placeholder) and DAPI (USER); no calibration: {status!r}"

        def test_export_never_scrapes_the_source_document():
            """`source_metadata` is the SOURCE's document, so with the
            fields unknown the spacing stays unknown, whatever the document
            says -- an exporter writes no measurement it was not given."""
            path = str(out / "unscraped.crate")
            form, text = document_of("spacing-conflict.crate")
            opened = host.open_export("crate", path, (1, 1, 4, 6, 5), pb2.DTYPE_U16, source_metadata=(form, text))
            host.export_chunk(opened.handle, 0, [0], [expected_volume(0, 0)], dtype=pb2.DTYPE_U16, inline=True)
            before_status = host.status_of(PANEL)
            host.finish_export(opened.handle)
            with Crate.open(Path(path)) as crate:
                assert crate.header.spacing_zyx == (0.0, 0.0, 0.0) and crate.header.time_increment == 0.0, ("scraped from the source's document", crate.header)
                assert crate.metadata() == (form, text) and crate.header.derived, "the source's document was not kept as provenance"
            status = await_change(host, "status", before_status)
            assert "without voxel size" in status, status
            return f"no fields, the source's ome-xml document kept and not read: zeros in the header; {status!r}"

        def test_export_chunk_out_of_bounds():
            path = str(out / "toomany.crate")
            opened = host.open_export("crate", path, (1, 1, 4, 6, 5), pb2.DTYPE_U16)
            host.export_chunk(opened.handle, 3, [0], [expected_volume(0, 0)], dtype=pb2.DTYPE_U16, inline=True)
            exc = expect_plugin_error(
                lambda: host.export_chunk(opened.handle, 4, [0], [expected_volume(0, 0)], dtype=pb2.DTYPE_U16, inline=True),
            )  # SPEC.md §8: refuse a chunk that would make the distinct-t list longer than declared; the words are the plugin's
            # §8.6: the export ended early, so the host aborts it.
            handle, _, done = host.export_aborts[-1]
            assert handle == opened.handle and isinstance(done, pb2.ExportDone), ("the export did not stay open for the host's AbortExport", done)
            assert not Path(path).exists(), "the partial artifact survived the abort"
            return f"{exc.args[0]!r}; the export stayed open; the host's abort removed the file"

        def test_finish_incomplete():
            path = str(out / "holes.crate")
            opened = host.open_export("crate", path, (2, 1, 4, 6, 5), pb2.DTYPE_U16)
            host.export_chunk(opened.handle, 0, [0], [expected_volume(0, 0)], dtype=pb2.DTYPE_U16, inline=True)
            before_status = host.status_of(PANEL)
            exc = expect_plugin_error(lambda: host.finish_export(opened.handle))  # SPEC.md §9: a message naming how many slots are missing; the words are the plugin's
            assert not Path(path).exists(), "a finish with holes left a file behind"
            status = await_change(host, "status", before_status)
            assert status != before_status, "a failed finish did not move `status`"
            return f"{exc.args[0]!r}; no file; {status!r}"

        def test_abort():
            path = str(out / "partial.crate")
            opened = host.open_export("crate", path, (1, 1, 4, 6, 5), pb2.DTYPE_U16)
            host.export_chunk(opened.handle, 0, [0], [expected_volume(0, 0)], dtype=pb2.DTYPE_U16, inline=True)
            assert Path(path).exists()
            done = host.abort_export(opened.handle)
            assert done.handle == opened.handle and not Path(path).exists()
            # §2.4: the handle is dead and a request naming it is `Error{NOT_FOUND}`.
            dead = expect_plugin_error(lambda: host.finish_export(opened.handle), "", pb2.Error.NOT_FOUND)
            return f"AbortExport answered ExportDone; the partial file is gone; the handle is dead ({pb2.Error.Kind.Name(dead.kind)}: {dead.args[0]!r})"

        def test_export_contradictory_chunks():
            """§8.6: a chunk that contradicts its
            export is a host error the plugin refuses `INVALID_ARGUMENT`,
            writing nothing from it, which ends the export.  vizi's host
            never sends one, so only a driver can.  Two contradictions, and
            the consistent chunk accepted.  Whether the plugin retires the
            handle itself is not specified: the `AbortExport` that follows
            is answered `ExportDone` or `NOT_FOUND`, and either way no
            artifact survives.  The host sends that abort itself (§8.6)."""
            other = pb2.AppliedFilter(plugin_id="org.example.other", plugin_version="1.0", stage_id="blur", channels=[0])
            refused = []
            for name, filt, marked in (("no filter named", None, [0]), ("a channel not in the chunk", other, [1])):
                path = str(out / f"contradiction-{len(refused)}.crate")
                opened = host.open_export("crate", path, (1, 1, 4, 6, 5), pb2.DTYPE_U16, filter=filt)
                exc = expect_plugin_error(
                    lambda: host.export_chunk(opened.handle, 0, [0], [expected_volume(0, 0)], dtype=pb2.DTYPE_U16, inline=True, filtered_channels=marked),
                    "", pb2.Error.INVALID_ARGUMENT,
                )
                # §8.6: the host aborts the export the refusal ended.
                handle, _, done = host.export_aborts[-1]
                assert handle == opened.handle, host.export_aborts
                assert isinstance(done, pb2.ExportDone) or done == pb2.Error.NOT_FOUND, done
                assert not Path(path).exists(), f"{name}: an artifact survived the refused chunk"
                refused.append(f"{name}: {exc.args[0]!r}")
            path = str(out / "filtered.crate")
            opened = host.open_export("crate", path, (1, 1, 4, 6, 5), pb2.DTYPE_U16, filter=other)
            written = host.export_chunk(opened.handle, 0, [0], [expected_volume(0, 0)], dtype=pb2.DTYPE_U16, inline=True, filtered_channels=[0])
            assert written.total_bytes > 0, written
            host.finish_export(opened.handle)
            assert Path(path).exists(), "a consistent filtered chunk was not written"
            return "; ".join(refused) + "; a consistent filtered chunk exported"

        def test_pick_calibration():
            """Acknowledge before the prompt; the picker's outcome only ever
            reaches the panel.  Chosen, declined, malformed."""
            host.assets["calibration_override"] = Path(fixture("vizi-crate-cal.bin"))
            done = host.invoke("crate.pick_calibration")
            assert done.ok, done  # acknowledged before the prompt (SPEC.md §"Calibration")
            cal = await_widget(host, "cal", "override:")
            assert cal == "override: vizi-crate-cal.bin", cal
            req = host.asset_requests[-1]
            assert req.slot == "calibration_override" and req.force_reprompt, req
            # Declined: "a normal outcome and must not be reported as an error"; Acceptance 9: canceling changes nothing.
            host.assets["calibration_override"] = None
            assert host.invoke("crate.pick_calibration").ok
            time.sleep(0.3)
            assert host.status_of(PANEL, "cal") == cal, host.status_of(PANEL, "cal")
            # Malformed: "treat a malformed table as absent rather than as a crash" -- reported to the panel, session alive.
            junk = out / "junk.bin"
            junk.write_bytes(b"not a table")
            host.assets["calibration_override"] = junk
            before_status = host.status_of(PANEL)
            assert host.invoke("crate.pick_calibration").ok
            status = await_change(host, "status", before_status)
            assert status != before_status, "a malformed table was not reported"
            # Whatever the malformed pick did to the label, the next calibrated open decodes through a *valid*
            # table -- the earlier override or the manifest asset -- and `cal` says which.
            opened = open_crate(fixture("sample-calibrated.crate"))
            grant_reader_arena(opened)
            assert np.array_equal(host.read_volume(opened.handle, 0, 1).array, expected_volume(0, 1))
            live = host.status_of(PANEL, "cal")
            assert live in ("override: vizi-crate-cal.bin", "manifest asset"), live
            state["handle"] = opened.handle
            return f"chosen -> {cal!r}; declined -> unchanged; malformed -> {status!r}; the next crate decodes with cal {live!r}"

        def test_check_file():
            host.assets["crate_check_file"] = Path(fixture("badcrc.crate"))
            done = host.invoke("crate.check_file")
            assert done.ok, done  # acknowledged before the prompt (SPEC.md §"crate.check_file")
            status = await_widget(host, "status", "failed CRC")
            # SPEC.md: count every failure and report the first -- "3 of 6 blocks failed CRC; first: (t=1, c=0)".
            assert "1 of 6 blocks failed CRC" in status and "(t=1, c=0)" in status, status
            assert host.asset_requests[-1].slot == "crate_check_file"
            host.assets["crate_check_file"] = Path(fixture("sample.crate"))
            assert host.invoke("crate.check_file").ok
            ok = await_change(host, "status", status)
            assert ok != status, "a clean check was not reported"  # success reaches the panel; its words are the plugin's
            host.assets["crate_check_file"] = Path(fixture("unfinalized.crate"))
            assert host.invoke("crate.check_file").ok
            unfinished = await_change(host, "status", ok)
            assert unfinished != ok, "an unfinished crate was not reported as a failed check"
            host.assets["crate_check_file"] = None
            assert host.invoke("crate.check_file").ok
            time.sleep(0.3)
            assert host.status_of(PANEL) == unfinished, "a canceled check should say nothing"
            return f"{status!r}; {ok!r}; {unfinished!r}; a cancellation says nothing"

        def test_panel_reopen():
            before = len(host.patches)
            host.open_panel(PANEL)
            deadline = time.monotonic() + 3.0
            while len(host.patches) < before + 5 and time.monotonic() < deadline:
                time.sleep(0.02)
            pushed = {widget: value for panel, widget, value in host.patches[before:] if panel == PANEL}
            assert set(pushed) >= {"head", "vox", "cal", "status", "verify"}, pushed
            assert pushed["verify"] is True and "sample-calibrated.crate" in pushed["head"] and pushed["cal"].startswith("override:"), pushed
            return f"re-pushed {sorted(pushed)}"

        def test_no_violations():
            assert not host.violations, [(pb2.ViolationSite.Name(v.where), v.detail) for v in host.violations]
            return "no ContractViolation about this emulator's traffic"

        def test_teardown():
            closed = host.shutdown()
            assert closed, f"the stream did not end within {SHUTDOWN_WINDOW_S:g} s"
            assert host.stream_error is None, host.stream_error
            report = host.shutdown_report
            assert report is not None, "no ShutdownReport arrived ahead of the close"
            outcome = pb2.ShutdownReport.Outcome.Name(report.outcome)
            # SPEC.md §5 pins that the session's end releases the file, not
            # how: a shutdown hook (COMPLETED) or the SDK's own close of the
            # open reader (NO_HOOK; the Python SDK) both
            # conform, and a hook that failed does not.
            assert report.outcome in (pb2.ShutdownReport.COMPLETED, pb2.ShutdownReport.NO_HOOK), outcome
            how = "a shutdown hook" if report.outcome == pb2.ShutdownReport.COMPLETED else "no hook; the SDK closes the reader itself"
            return f"ShutdownReport {outcome} ({how}), then a clean end of stream"

        check("handshake", test_handshake)
        check("grants", test_grants)
        check("panel first", test_panel_before_anything)
        check("open: unfinalized.crate", test_open_unfinalized)
        check("open: sample.crate, then G", test_open_sample)
        check("reads, plane, metadata, bounds", test_reads)
        check("badcrc.crate and the verify toggle", test_badcrc_and_verify_toggle)
        check("spacing-conflict and nospacing", test_spacing_fixtures)
        check("sample-calibrated.crate with the manifest asset", test_calibrated_with_manifest_asset)
        check("G arena too small", test_arena_too_small)
        check("a refused open leaves the open reader (§1.2 H2)", test_refused_open_leaves_the_reader)
        check("CloseReader", test_close_reader)
        check("describe via host data access", test_describe_via_host)
        check("describe directly (the guard's reply)", test_describe_directly)
        check("busy guard", test_busy_guard)
        check("export: X before OpenExport, deposit then inline", test_export_deposit_then_inline)
        check("export: reopens in the plugin's own reader", test_export_reopens_in_own_reader)
        check("export: a two-channel chunk, no calibration", test_export_multichannel_chunk)
        check("export: the source's document is never scraped", test_export_never_scrapes_the_source_document)
        check("export: one timepoint too many", test_export_chunk_out_of_bounds)
        check("export: finish with a hole", test_finish_incomplete)
        check("export: abort", test_abort)
        check("export: a chunk contradicting its filter is refused", test_export_contradictory_chunks)
        check("pick calibration", test_pick_calibration)
        check("check file", test_check_file)
        check("panel reopen", test_panel_reopen)
        check("no violations", test_no_violations)
        check("teardown", test_teardown)


def run_unprovisioned(plugin_dir: Path) -> None:
    """A second, short session with no `VIZI_PLUGIN_ASSETS`: a calibrated
    crate refuses naming Plugin Manager; an uncalibrated one is unaffected."""
    host = Host(plugin_dir, DATASET)
    host.add(Segment.host_ring(V_SEG, "V", 1 << 20, host.directory))
    host.volume_segment = V_SEG
    with host:
        def test_handshake_again():
            return host.check_handshake()

        def test_calibrated_refused():
            host.open_panel(PANEL)
            assert await_widget(host, "cal", "(not provisioned)") == "(not provisioned)"
            exc = expect_plugin_error(lambda: host.open_reader(fixture("sample-calibrated.crate"), "crate"), "Plugin Manager")
            assert "calibrat" in exc.args[0]
            return f"refused: {exc.args[0]!r}"

        def test_uncalibrated_unaffected():
            opened = host.open_reader(fixture("sample.crate"), "crate")
            seg = segment_id(50)
            host.add(Segment.reader_arena(seg, opened, host.directory))
            assert host.grant(seg).WhichOneof("outcome") == "installed"
            assert np.array_equal(host.read_volume(opened.handle, 0, 1).array, expected_volume(0, 1))
            assert await_widget(host, "cal", "(not provisioned)") == "(not provisioned)"
            return "sample.crate serves with no table at all"

        def test_teardown_again():
            assert host.shutdown() and host.stream_error is None
            return "clean end of stream"

        check("unprovisioned: handshake", test_handshake_again)
        check("unprovisioned: calibrated crate refused", test_calibrated_refused)
        check("unprovisioned: uncalibrated crate serves", test_uncalibrated_unaffected)
        check("unprovisioned: teardown", test_teardown_again)


if __name__ == "__main__":
    raise SystemExit(main())
