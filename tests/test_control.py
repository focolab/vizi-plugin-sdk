"""The 1.0 control plane, plugin side — ``control_plane_protocol.md`` §5–§9.

§13's conformance list is what these are written from, and the ones that fail on
a first implementation are all here: classification before id matching, a
coalescing queue that keeps one item per key rather than the newest message, the
install-before-ack order, and an abandoned reply that still owns a reservation.
"""

from __future__ import annotations

import os
import struct
import tempfile
import unittest
import zlib
from unittest import mock

import vizi_plugin_sdk._shared_atomic as _sa


class _SingleProcessAtomics:
    """The stand-in `tests/test_memory.py` documents; no cross-process claim."""

    def __init__(self, mapping) -> None:
        self._mm = mapping
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def load(self, offset: int) -> int:
        return struct.unpack_from("<Q", self._mm, offset)[0]

    def store(self, offset: int, value: int) -> None:
        struct.pack_into("<Q", self._mm, offset, value)

    def compare_exchange(self, offset: int, expected: int, desired: int):
        seen = self.load(offset)
        if seen == expected:
            self.store(offset, desired)
            return True, seen
        return False, seen


try:  # pragma: no cover - depends on the environment
    import atomicshm  # noqa: F401
except ImportError:
    _sa.SharedAtomicMapping = _SingleProcessAtomics

from vizi_plugin_sdk import control as c  # noqa: E402
from vizi_plugin_sdk import memory as m  # noqa: E402
from vizi_plugin_sdk._proto import vizi_plugin_pb2 as pb2  # noqa: E402
from vizi_plugin_sdk.violations import ViolationReporter  # noqa: E402

PURPOSES = frozenset({"V"})
PERM_RW = m.PERM_PLUGIN_READ | m.PERM_PLUGIN_WRITE


def preamble(segment_id: int, proof: bytes = b"\xa5" * 16) -> bytes:
    head = struct.pack("<QI16s", segment_id, 0, proof)
    return head + struct.pack("<I", zlib.crc32(head))


def make_object(size: int, segment_id: int) -> int:
    # A real file-backed object with a valid preamble; returns an owned fd.
    # Through a file object rather than `mkstemp` + `unlink` + `pwrite`:
    # Windows refuses to unlink a file another handle holds open and has no
    # `os.pwrite`, so the name is dropped by `TemporaryFile` (unlinked at
    # once on unix, deleted at last close on Windows) and the preamble is
    # written through the object.  The dup outlives the object, and its
    # close is the last one.
    with tempfile.TemporaryFile() as tmp:
        tmp.truncate(size)
        tmp.seek(0)
        tmp.write(preamble(segment_id))
        tmp.flush()
        return os.dup(tmp.fileno())


def loaned_region(index: int = 0, offset: int = 32, extent: int = 512, *, host=True):
    return pb2.Region(
        index=index,
        offset=offset,
        extent=extent,
        discipline=pb2.RegionDiscipline.REGION_DISCIPLINE_LOANED,
        producer=(
            pb2.RegionProducer.REGION_PRODUCER_HOST
            if host
            else pb2.RegionProducer.REGION_PRODUCER_PLUGIN
        ),
    )


class _Transport:
    """The doorbell's lifecycle inputs, with its own accept/refuse rules."""

    def __init__(self) -> None:
        self.epochs: dict[int, int] = {}
        self.tools: set[int] = set()
        self.high_water = 0
        self.calls: list[tuple] = []

    def epoch_acknowledged(self, view_id: int, epoch: int) -> bool:
        self.calls.append(("epoch", view_id, epoch))
        if epoch == 0:
            return False
        installed = self.epochs.get(view_id)
        if installed is not None and epoch <= installed:
            return epoch == installed
        self.epochs[view_id] = epoch
        return True

    def tool_activated(self, handle: int) -> bool:
        self.calls.append(("activate", handle))
        if handle == 0:
            return False
        if handle > self.high_water:
            self.high_water = handle
            self.tools.add(handle)
            return True
        return handle in self.tools

    def tool_retired(self, handle: int) -> bool:
        self.calls.append(("retire", handle))
        if handle == 0 or handle > self.high_water:
            return False
        self.tools.discard(handle)
        return True


def _with_unknown(msg):
    """A copy of ``msg`` carrying field 9999, which this build does not know."""
    copy = type(msg)()
    copy.CopyFrom(msg)
    copy.MergeFromString(b"\xf8\xf0\x04\x01")  # field 9999, varint 1
    return copy


# --------------------------------------------------------------------------
# §5: classification
# --------------------------------------------------------------------------


class Classification(unittest.TestCase):
    """§4 rule 3: a message's CLASS is decided by its kind, never by its id."""

    def test_every_wire_arm_has_a_class(self):
        """An arm with no class would take a queue policy nobody gave it."""
        host = {f.name for f in pb2.HostMsg.DESCRIPTOR.fields} - {"id"}
        plugin = {f.name for f in pb2.PluginMsg.DESCRIPTOR.fields} - {"id"}
        self.assertEqual(host - set(c.HOST_CLASSES), set())
        self.assertEqual(plugin - set(c.PLUGIN_CLASSES), set())

    def test_every_request_names_its_reply(self):
        """§4 rule 8: a pending request's id belongs to its reply alone, so
        every request names the reply it takes, and that reply is one the
        receiver routes by id."""
        host_requests = {k for k, v in c.HOST_CLASSES.items() if v is c.Class.HOST_REQUEST}
        plugin_requests = {k for k, v in c.PLUGIN_CLASSES.items() if v is c.Class.PLUGIN_REQUEST}
        self.assertEqual(set(c.REQUEST_REPLIES), host_requests)
        self.assertEqual(set(c.PLUGIN_REQUEST_REPLIES), plugin_requests)
        self.assertLessEqual(
            set(c.PLUGIN_REQUEST_REPLIES.values()), set(c.REPLY_KINDS) - {"host_error"}
        )
        self.assertEqual(c.PLUGIN_REQUEST_REPLIES["contribute_rois"], "rois_contributed")
        self.assertEqual(c.PLUGIN_REQUEST_REPLIES["compile_label_volume"], "label_volume_compiled")

    def test_every_host_request_has_a_lane_and_every_lane_serves_a_request(self):
        """§4 rule 3 gives every arm a class; §6 gives every HOST_REQUEST a
        worker lane.  The class check alone cannot see a request kind that
        falls off the end of the dispatch, so the lanes are a partition of the request kinds,
        asserted at the server's import and here against a fresh table."""
        from vizi_plugin_sdk import server as s

        requests = {k for k, cls in c.HOST_CLASSES.items() if cls is c.Class.HOST_REQUEST}
        self.assertEqual(requests, set(s._REQUEST_LANES))
        for pool, handler in s._REQUEST_LANES.values():
            self.assertTrue(hasattr(s._Servicer, handler), handler)
            self.assertTrue(pool.endswith("_pool"), pool)
        with self.assertRaises(RuntimeError):
            with mock.patch.dict(s._REQUEST_LANES, {"asset_result": ("_reader_pool", "_on_open")}):
                s._assert_request_lanes_exhaustive()

    def test_the_classes_are_the_documents(self):
        self.assertIs(c.classify("invoke_command"), c.Class.HOST_REQUEST)
        self.assertIs(c.classify("epoch_change"), c.Class.ARRANGEMENT)
        self.assertIs(c.classify("release_loan"), c.Class.HOST_ONE_WAY)
        self.assertIs(c.classify("panel_patch", inbound=False), c.Class.COALESCING)
        self.assertIs(c.classify("grant_ack", inbound=False), c.Class.ONE_SHOT)
        self.assertIs(c.classify("overlay_remove", inbound=False), c.Class.ONE_SHOT)
        # §5.7: the first host->plugin coalescing push, and the two plugin
        # pushes that answer it.
        self.assertIs(c.classify("chunks_wanted"), c.Class.HOST_COALESCING)
        self.assertIs(c.classify("overlay_chunk", inbound=False), c.Class.COALESCING)
        self.assertIs(c.classify("overlay_label_table", inbound=False), c.Class.COALESCING)

    def test_chunks_wanted_is_kept_whole_per_overlay_and_handed_to_the_hook(self):
        """§5.7: the host restates the WHOLE set; this end keeps the latest per
        overlay and tells the hook, in the host's order, `t` absent as None."""
        seen = []
        plane = c.ControlPlane(on_chunks_wanted=lambda oid, wants: seen.append((oid, wants)))
        first = pb2.HostMsg(
            chunks_wanted=pb2.ChunksWanted(
                overlay_id="labels",
                wants=[pb2.ChunkWant(level=0, t=4), pb2.ChunkWant(level=2, t=4)],
            )
        )
        routing = plane.receive(first)
        self.assertIs(routing.cls, c.Class.HOST_COALESCING)
        self.assertIs(routing.action, c.Action.HANDLED)
        self.assertEqual(plane.chunks_wanted, {"labels": [(0, 4), (2, 4)]})
        plane.receive(
            pb2.HostMsg(
                chunks_wanted=pb2.ChunksWanted(overlay_id="labels", wants=[pb2.ChunkWant(level=0)])
            )
        )
        self.assertEqual(plane.chunks_wanted, {"labels": [(0, None)]}, "replaced, not merged")
        plane.receive(pb2.HostMsg(chunks_wanted=pb2.ChunksWanted(overlay_id="labels")))
        self.assertEqual(plane.chunks_wanted, {"labels": []}, "empty is a state: nothing wanted")
        self.assertEqual(
            seen, [("labels", [(0, 4), (2, 4)]), ("labels", [(0, None)]), ("labels", [])]
        )

    def test_a_chunk_and_a_table_have_their_own_replacement_keys(self):
        chunk = pb2.PluginMsg(overlay_chunk=pb2.OverlayChunk(overlay_id="labels", level=2, t=4))
        static = pb2.PluginMsg(overlay_chunk=pb2.OverlayChunk(overlay_id="labels", level=2))
        table = pb2.PluginMsg(overlay_label_table=pb2.OverlayLabelTable(overlay_id="labels"))
        update = pb2.PluginMsg(overlay_update=pb2.OverlayUpdate(overlay_id="labels"))
        self.assertEqual(c.coalescing_key(chunk), ("chunk", "labels", 2, 4))
        self.assertEqual(c.coalescing_key(static), ("chunk", "labels", 2, None))
        self.assertEqual(c.coalescing_key(table), ("label_table", "labels"))
        self.assertNotEqual(c.coalescing_key(table), c.coalescing_key(update))

    def test_a_push_arriving_while_a_request_is_pending_is_not_a_stray(self):
        """The failure §4 names: an implementation that matches ids first drops
        an uncorrelated push as a stray."""
        plane = c.ControlPlane()
        routing = plane.receive(pb2.HostMsg(release_loan=pb2.ReleaseLoan(segment_id=1)))
        self.assertIs(routing.cls, c.Class.HOST_ONE_WAY)
        self.assertIs(routing.action, c.Action.HANDLED)

    def test_an_unclassified_kind_is_counted_and_never_reported(self):
        """§11: forward compatibility is not a violation -- reporting it would
        make every protocol addition look like a defect at the far end."""
        plane = c.ControlPlane(reporter=ViolationReporter())
        routing = plane.receive(pb2.HostMsg())
        self.assertIs(routing.action, c.Action.UNKNOWN)
        self.assertEqual(plane.unknown_kinds, {"": 1})
        self.assertEqual(plane.reporter.counts(), {})

    def test_an_unknown_kind_with_an_id_is_refused_by_name(self):
        """D4: silence is never an answer.  A kind this SDK predates that
        carries an id is a request whose sender is blocked."""
        plane = c.ControlPlane()
        plane.receive(pb2.HostMsg(id="r7"))
        (sent,) = plane.outbox.drain()
        self.assertEqual(sent.id, "r7")
        self.assertEqual(sent.error.kind, pb2.Error.UNSUPPORTED)

    def test_an_unknown_kind_is_never_a_reply(self):
        """§4 rule 8: an unknown kind under an id is offered to the reply path,
        whose pending request faults on it (the server's ``route_reply``); under
        an id nothing waits on, it is refused ``UNSUPPORTED``.  The id tables
        are apart, so a known reply under the same id is never a reuse."""
        offered: list = []

        def deliver(hm):
            offered.append(hm.WhichOneof("msg"))
            if hm.id == "q3" and hm.WhichOneof("msg") is None:
                raise c.SessionFault("an unknown kind under a pending id")
            return True

        plane = c.ControlPlane(deliver_reply=deliver)
        with self.assertRaises(c.SessionFault):
            plane.receive(pb2.HostMsg(id="q3"))
        self.assertEqual(plane.outbox.drain(), [], "a fault, not an UNSUPPORTED answer")
        plane = c.ControlPlane(deliver_reply=lambda hm: False)
        self.assertIs(plane.receive(pb2.HostMsg(id="q4")).action, c.Action.UNKNOWN)
        (sent,) = plane.outbox.drain()
        self.assertEqual((sent.id, sent.error.kind), ("q4", pb2.Error.UNSUPPORTED))
        routing = plane.receive(pb2.HostMsg(id="q4", view_state=pb2.ViewState()))
        self.assertIs(routing.action, c.Action.HANDLED, "the reply table is its own")

    def test_a_diagnostic_reaches_stderr_which_is_the_surface_the_host_keeps(self):
        """§12: a counter nobody can read is not a surface.  The list is read
        by nothing in production; the host drains stderr and keeps a tail it
        shows in Plugin Manager.  A routing's own notes go the same way."""
        import contextlib
        import io

        plane = c.ControlPlane()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            plane.diagnose("tool 'paint' press raised: RuntimeError('boom')")
            routing = plane.receive(pb2.HostMsg())
        self.assertTrue(routing.diagnostics)
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("press raised", lines[0])
        self.assertIn("unclassified message kind", lines[1])
        self.assertEqual(plane.diagnostics[-1], routing.diagnostics[0])


class IdsAreNeverReused(unittest.TestCase):
    """§4 rule 7: every message that deserves a reply carries an id used once.

    A byte-identical re-send is deduplicated; anything else under a used id is
    a protocol error that faults the session, naming the id; the last 65 536
    ids received are checked and older ones are not."""

    def setUp(self) -> None:
        self.delivered: list[str] = []
        self.waiting = {"q1"}
        self.plane = c.ControlPlane(deliver_reply=self._deliver)

    def _deliver(self, hm) -> bool:
        if hm.id in self.waiting:
            self.waiting.discard(hm.id)
            self.delivered.append(hm.id)
            return True
        return False

    def test_a_resent_request_is_not_dispatched_again(self):
        request = pb2.HostMsg(id="r1", read_plane=pb2.ReadPlane(z=3))
        self.assertIs(self.plane.receive(request).action, c.Action.HOST_REQUEST)
        again = self.plane.receive(pb2.HostMsg.FromString(request.SerializeToString()))
        self.assertIs(again.action, c.Action.RESENT)
        self.assertEqual(self.plane.outbox.drain(), [])
        self.assertEqual(self.plane.resent, 1)

    def test_a_resent_reply_is_delivered_once_and_releases_nothing(self):
        reply = pb2.HostMsg(id="q1", view_state=pb2.ViewState(t=2))
        self.assertIs(self.plane.receive(reply).action, c.Action.HANDLED)
        routing = self.plane.receive(reply)
        self.assertIs(routing.action, c.Action.RESENT)
        self.assertEqual(self.delivered, ["q1"])
        self.assertFalse(any("matched no waiter" in d for d in routing.diagnostics))
        self.assertEqual(self.plane.outbox.drain(), [])

    def test_a_resent_unknown_kind_is_refused_once(self):
        self.plane.receive(pb2.HostMsg(id="r9"))
        self.assertIs(self.plane.receive(pb2.HostMsg(id="r9")).action, c.Action.RESENT)
        self.assertEqual([m.id for m in self.plane.outbox.drain()], ["r9"])

    def test_a_reused_id_with_different_content_faults_naming_it(self):
        cases = {
            "another request": (
                pb2.HostMsg(id="r1", read_plane=pb2.ReadPlane(z=3)),
                pb2.HostMsg(id="r1", read_plane=pb2.ReadPlane(z=4)),
            ),
            "an unknown kind under a request's id": (
                pb2.HostMsg(id="r1", read_plane=pb2.ReadPlane(z=3)),
                pb2.HostMsg(id="r1"),
            ),
            "a second reply": (
                pb2.HostMsg(id="q1", view_state=pb2.ViewState(t=2)),
                pb2.HostMsg(id="q1", host_error=pb2.Error(kind=pb2.Error.INTERNAL)),
            ),
            "a GrantStateQuery": (
                pb2.HostMsg(id="r5", grant_state_query=pb2.GrantStateQuery(segment_id=1)),
                pb2.HostMsg(id="r5", grant_state_query=pb2.GrantStateQuery(segment_id=2)),
            ),
        }
        for label, (first, second) in cases.items():
            with self.subTest(label):
                self.setUp()
                self.plane.receive(first)
                self.plane.outbox.drain()
                with self.assertRaises(c.SessionFault) as raised:
                    self.plane.receive(second)
                self.assertIn(repr(first.id), str(raised.exception))
                self.assertEqual(
                    self.plane.outbox.drain(), [], "nothing is dispatched under a reused id"
                )
                self.assertLessEqual(len(self.delivered), 1)

    def test_a_request_and_a_reply_do_not_share_an_id_space(self):
        """A reply echoes the requester's id, so the host's `r<n>` requests and
        its replies to this plugin's `q<n>` are recorded apart."""
        self.waiting.add("r2")
        self.plane.receive(pb2.HostMsg(id="r2", read_plane=pb2.ReadPlane()))
        self.plane.receive(pb2.HostMsg(id="r2", view_state=pb2.ViewState()))
        self.assertEqual(self.delivered, ["r2"])

    def test_a_push_with_an_empty_id_is_not_recorded(self):
        push = pb2.HostMsg(release_loan=pb2.ReleaseLoan(segment_id=1))
        self.assertIsNot(self.plane.receive(push).action, c.Action.RESENT)
        self.assertIsNot(self.plane.receive(push).action, c.Action.RESENT)
        self.assertEqual(self.plane.ids.requests, {})

    def test_the_last_65536_ids_are_checked_and_older_ones_are_not(self):
        self.assertEqual(c.ID_WINDOW, 65_536)
        self.assertEqual(self.plane.ids.window, c.ID_WINDOW)
        ledger = c.IdLedger()
        table = ledger.requests
        for n in range(c.ID_WINDOW + 1):
            self.assertEqual(
                ledger.observe(table, pb2.HostMsg(id=f"r{n}", read_plane=pb2.ReadPlane())), "new"
            )
        self.assertEqual(len(table), c.ID_WINDOW)
        other = pb2.HostMsg(id="r0", read_plane=pb2.ReadPlane(z=1))
        self.assertEqual(ledger.observe(table, other), "new", "r0 is older than the window")
        self.assertEqual(
            ledger.observe(table, pb2.HostMsg(id="r2", read_plane=pb2.ReadPlane(z=1))), "reused"
        )
        self.assertEqual(
            ledger.observe(table, pb2.HostMsg(id="r3", read_plane=pb2.ReadPlane())), "resent"
        )


# --------------------------------------------------------------------------
# §5.4 + §7: the outbound queues
# --------------------------------------------------------------------------


class Queues(unittest.TestCase):
    """§5.4: one item per KEY, and coalescing may not share a queue."""

    def patch(self, panel: str, widget: str, **op):
        return pb2.PluginMsg(
            panel_patch=pb2.PanelPatch(panel_id=panel, ops=[pb2.PatchOp(widget_id=widget, **op)])
        )

    def test_two_rows_cells_are_two_keys(self):
        """A list's cells share their template widget's id, so the
        row is part of the key -- filling a list must not lose every row but
        the last."""
        box = c.Outbox()
        for row, text in (("a", "Alpha"), ("b", "Beta"), ("a", "Alpha 2")):
            patch = self.patch("p", "rname", set_value=pb2.Value(s=text))
            patch.panel_patch.ops[0].row_key = row
            box.send(patch)
        got = [(op.row_key, op.set_value.s) for m in box.drain() for op in m.panel_patch.ops]
        self.assertEqual(got, [("a", "Alpha 2"), ("b", "Beta")])

    def test_a_burst_on_one_widget_does_not_evict_another(self):
        """The exact failure drop-oldest produces: A is then wrong for the rest
        of the session, with nothing to correct it."""
        box = c.Outbox()
        box.send(self.patch("p", "A", set_value=pb2.Value(f=1.0)))
        for n in range(50):
            box.send(self.patch("p", "B", set_value=pb2.Value(f=float(n))))
        keys = box.keys()
        self.assertEqual(keys, (("panel", "p", "A", ""), ("panel", "p", "B", "")))
        drained = box.drain()
        self.assertEqual(len(drained), 2)
        self.assertEqual(drained[0].panel_patch.ops[0].widget_id, "A")
        self.assertEqual(drained[1].panel_patch.ops[0].set_value.f, 49.0)

    def test_set_options_then_set_value_survives_in_that_order(self):
        """§13, named: applying the value against the old option list loses it,
        because the combo clears on set_options and restores by text.  And
        since the widget has been sent a `set_options`, the second value does
        NOT supersede the first: both are held, as sent (§5.4)."""
        box = c.Outbox()
        box.send(self.patch("p", "combo", set_options=pb2.Options(options=["a", "b"])))
        box.send(self.patch("p", "combo", set_value=pb2.Value(f=1.0)))
        box.send(self.patch("p", "combo", set_value=pb2.Value(f=2.0)))
        ops = box.drain()[0].panel_patch.ops
        self.assertEqual(
            [op.WhichOneof("op") for op in ops], ["set_options", "set_value", "set_value"]
        )
        self.assertEqual([op.set_value.f for op in ops[1:]], [1.0, 2.0])
        self.assertEqual(box.superseded, 0)

    def test_a_property_replaces_in_place_and_a_new_one_appends(self):
        box = c.Outbox()
        box.send(self.patch("p", "w", set_enabled=True))
        box.send(self.patch("p", "w", set_visible=True))
        box.send(self.patch("p", "w", set_enabled=False))
        ops = box.drain()[0].panel_patch.ops
        self.assertEqual([op.WhichOneof("op") for op in ops], ["set_enabled", "set_visible"])
        self.assertFalse(ops[0].set_enabled)

    def test_an_overlay_replaces_wholly_and_a_shortfall_merges(self):
        box = c.Outbox()
        box.send(pb2.PluginMsg(overlay_update=pb2.OverlayUpdate(overlay_id="o", revision=1)))
        box.send(pb2.PluginMsg(overlay_update=pb2.OverlayUpdate(overlay_id="o", revision=2)))
        box.send(
            pb2.PluginMsg(
                segment_shortfall=pb2.SegmentShortfall(
                    segment_id=7, region=1, wanted_bytes=128, largest_free_bytes=64, dropped=1
                )
            )
        )
        box.send(
            pb2.PluginMsg(
                segment_shortfall=pb2.SegmentShortfall(
                    segment_id=7, region=1, wanted_bytes=64, largest_free_bytes=32, dropped=3
                )
            )
        )
        out = {msg.WhichOneof("msg"): msg for msg in box.drain()}
        self.assertEqual(out["overlay_update"].overlay_update.revision, 2)
        short = out["segment_shortfall"].segment_shortfall
        self.assertEqual(short.wanted_bytes, 128, "a re-grant clearing the biggest clears both")
        self.assertEqual(short.dropped, 4, "summed, because §12 needs the total")
        self.assertEqual(
            short.largest_free_bytes,
            64,
            "§5.4: the free run travels with the want it was measured beside, because "
            "the host reads the pair to tell exhaustion from fragmentation",
        )

    def test_a_larger_later_want_brings_its_own_free_run(self):
        box = c.Outbox()
        for wanted, free in ((64, 32), (128, 96)):
            box.send(
                pb2.PluginMsg(
                    segment_shortfall=pb2.SegmentShortfall(
                        segment_id=8, region=0, wanted_bytes=wanted, largest_free_bytes=free
                    )
                )
            )
        short = box.drain()[0].segment_shortfall
        self.assertEqual((short.wanted_bytes, short.largest_free_bytes), (128, 96))

    def test_a_superseded_message_hands_its_bytes_back(self):
        """§5.6: coalescing is a policy about the VALUE; it never disposes of
        the bytes that carried it."""
        freed: list = []
        box = c.Outbox(on_superseded=freed.append)
        first = pb2.PluginMsg(overlay_update=pb2.OverlayUpdate(overlay_id="o", revision=1))
        box.send(first)
        box.send(pb2.PluginMsg(overlay_update=pb2.OverlayUpdate(overlay_id="o", revision=2)))
        self.assertEqual(freed, [first])
        self.assertEqual(box.superseded, 1)

    def image(self, panel="p", widget="w", *, segment=1, offset=32, length=64):
        return pb2.PluginMsg(
            panel_patch=pb2.PanelPatch(
                panel_id=panel,
                ops=[
                    pb2.PatchOp(
                        widget_id=widget,
                        set_image=pb2.ImageRef(segment_id=segment, offset=offset, len=length),
                    )
                ],
            )
        )

    def overlay_from_segment(self, overlay="o", *, revision=1):
        return pb2.PluginMsg(
            overlay_update=pb2.OverlayUpdate(
                overlay_id=overlay,
                revision=revision,
                items=[
                    pb2.OverlayItem(
                        item_id="i",
                        line_set=pb2.OverlayLineSet(
                            positions_zyx=pb2.GeomBuffer(
                                segment=pb2.SegmentRef(segment_id=1, offset=32, len=48)
                            )
                        ),
                    )
                ],
            )
        )

    def test_a_push_naming_a_loan_is_not_merged_when_nothing_can_release_it(self):
        """§12: a replacement is the class working -- *unless the superseded
        message named a loan*, and then something is lost until it is returned.
        Here the bytes are the sender's own arena, so an end that cannot
        release at replacement time must not coalesce."""
        box = c.Outbox()
        box.send(self.image(offset=32))
        box.send(self.image(offset=96))
        drained = box.drain()
        self.assertEqual(
            [msg.panel_patch.ops[0].set_image.offset for msg in drained],
            [32, 96],
            "both went out, in order",
        )
        self.assertEqual(box.superseded, 0)

    def test_the_same_holds_for_an_overlay_served_from_a_segment(self):
        box = c.Outbox()
        box.send(self.overlay_from_segment(revision=1))
        box.send(self.overlay_from_segment(revision=2))
        self.assertEqual([m.overlay_update.revision for m in box.drain()], [1, 2])

    def test_an_inline_overlay_still_coalesces(self):
        """The carve-out is about bytes that must be given back, not about the
        message kind -- an inline item names none."""
        box = c.Outbox()
        box.send(pb2.PluginMsg(overlay_update=pb2.OverlayUpdate(overlay_id="o", revision=1)))
        box.send(pb2.PluginMsg(overlay_update=pb2.OverlayUpdate(overlay_id="o", revision=2)))
        self.assertEqual([m.overlay_update.revision for m in box.drain()], [2])

    def test_a_caller_that_can_release_may_still_merge(self):
        freed: list = []
        box = c.Outbox(on_superseded=freed.append)
        box.send(self.image(offset=32))
        box.send(self.image(offset=96))
        self.assertEqual(len(box.drain()), 1, "merged, because the arena is reclaimable")
        self.assertEqual(freed[0].panel_patch.ops[0].set_image.offset, 32)

    def test_an_ordered_message_does_not_overtake_what_was_pending_for_its_key(self):
        """Ordering within a queue is contractual, and for one widget it is the
        whole meaning: `set_enabled` then an image must arrive that way."""
        box = c.Outbox()
        box.send(self.patch("p", "w", set_enabled=True))
        box.send(self.image(panel="p", widget="w"))
        kinds = [[op.WhichOneof("op") for op in msg.panel_patch.ops] for msg in box.drain()]
        self.assertEqual(kinds, [["set_enabled"], ["set_image"]])
        self.assertEqual(box.pending_coalescing, 0)

    def test_an_image_does_not_overtake_its_panels_pending_rows(self):
        """SDK report (region_quant's hostsim, 2 runs in 7): `set_rows` then a
        cell image for a row it adds.  The image took the never-drop queue and
        froze only its own key, so the list's `set_rows` -- another key --
        stayed behind and the host refused the image for a row it did not have
        yet.  The panel's pending `set_rows` leaves ahead of it; other cells'
        state keeps coalescing (freezing every key of the panel
        would fill the never-drop queue and refuse the image)."""
        box = c.Outbox()
        box.send(self.patch("other", "w", set_enabled=True))
        box.send(self.patch("p", "regions", set_rows=pb2.RowKeys(keys=["a"])))
        name = self.patch("p", "name", set_value=pb2.Value(s="Alpha"))
        name.panel_patch.ops[0].row_key = "a"
        box.send(name)
        swatch = self.image(panel="p", widget="swatch")
        swatch.panel_patch.ops[0].row_key = "a"
        box.send(swatch)
        got = [
            (m.panel_patch.panel_id, op.widget_id, op.WhichOneof("op"))
            for m in box.drain()
            for op in m.panel_patch.ops
        ]
        self.assertEqual(
            got,
            [
                ("p", "regions", "set_rows"),
                ("p", "swatch", "set_image"),
                ("other", "w", "set_enabled"),
                ("p", "name", "set_value"),
            ],
        )

    def test_an_image_is_not_refused_for_a_panel_full_of_other_pending_cells(self):
        """The image froze every pending key of its panel into the
        never-drop queue, so past ~255 undrained cells it was refused."""
        box = c.Outbox()
        for i in range(300):
            cell = self.patch("p", "name", set_value=pb2.Value(s=str(i)))
            cell.panel_patch.ops[0].row_key = f"r{i}"
            box.send(cell)
        swatch = self.image(panel="p", widget="swatch")
        swatch.panel_patch.ops[0].row_key = "r1"
        box.send(swatch)  # admitted
        self.assertEqual(box.refused, 0)

    def test_an_unmergeable_push_is_refused_rather_than_fatal(self):
        """§5.4 gives a coalescing kind "count, report" under overload and
        reserves the session fault for one-shot -- and refusing leaves the
        bytes with the caller, which is the end that can still reclaim them."""
        box = c.Outbox(one_shot_capacity=2)
        box.send(pb2.PluginMsg(epoch_ack=pb2.EpochAck()))  # one slot spent
        box.send(self.patch("p", "w", set_enabled=True))  # one key pending
        with self.assertRaises(c.Refused):
            box.send(self.image(panel="p", widget="w"))  # needs two: the freeze and itself
        self.assertEqual(box.refused, 1)
        self.assertEqual(box.pending_one_shot, 1, "a refused send took nothing")
        self.assertEqual(box.pending_coalescing, 1, "and flushed nothing halfway")

    def test_the_never_drop_queue_is_served_first(self):
        """§7: ordinary work must not starve the messages that finish it."""
        box = c.Outbox()
        box.send(pb2.PluginMsg(overlay_update=pb2.OverlayUpdate(overlay_id="o")))
        box.send(pb2.PluginMsg(get_view_state=pb2.GetViewState()))
        box.send(pb2.PluginMsg(epoch_ack=pb2.EpochAck()))
        self.assertEqual(
            [msg.WhichOneof("msg") for msg in box.drain()],
            ["epoch_ack", "get_view_state", "overlay_update"],
        )

    def test_a_full_one_shot_queue_faults_rather_than_dropping(self):
        """§7: dropping an ack makes an arrangement permanently pending, and
        dropping a release leaks a reservation for the life of the session."""
        box = c.Outbox(one_shot_capacity=2)
        box.send(pb2.PluginMsg(epoch_ack=pb2.EpochAck()))
        box.send(pb2.PluginMsg(epoch_ack=pb2.EpochAck()))
        with self.assertRaises(c.SessionFault):
            box.send(pb2.PluginMsg(epoch_ack=pb2.EpochAck()))

    def test_coalescing_is_unbounded_by_design(self):
        """§5.4: the key space is structural, so eviction is unreachable and
        there is nothing to recover from."""
        box = c.Outbox(one_shot_capacity=2)
        for n in range(500):
            box.send(pb2.PluginMsg(overlay_update=pb2.OverlayUpdate(overlay_id=f"o{n}")))
        self.assertEqual(box.pending_coalescing, 500)


# --------------------------------------------------------------------------
# §5.5: arrangements
# --------------------------------------------------------------------------


class DuplicateReservations(unittest.TestCase):
    """§5.6, new at 1.0: **within one message, no two operations may name the
    same reservation.**  A receiver may split a message for queueing and each
    part disposes separately, so a reservation named twice is disposed twice
    while `uses` counted the message once -- and the first release frees bytes
    the second part still needs."""

    def setUp(self):
        self.outbox = c.Outbox()

    def geom(self, offset):
        return pb2.GeomBuffer(
            segment=pb2.SegmentRef(segment_id=9, offset=offset, len=24),
            shape=[1, 3],
            dtype=pb2.DTYPE_F64,
        )

    def item(self, item_id, offset):
        return pb2.OverlayItem(
            item_id=item_id, marker=pb2.OverlayMarker(positions_zyx=self.geom(offset))
        )

    def test_two_items_on_one_buffer_are_refused_whole_and_counted(self):
        """Reachable by an AUTHOR, not by the allocator: the arena is a bump
        cursor that cannot hand out an outstanding offset twice, but nothing
        stops one geometry buffer appearing in two items."""
        msg = pb2.PluginMsg(
            overlay_update=pb2.OverlayUpdate(
                overlay_id="cells", items=[self.item("a", 32), self.item("b", 32)], revision=1
            )
        )
        with self.assertRaises(c.Refused) as caught:
            self.outbox.send(msg)
        self.assertIn("more than once", str(caught.exception))
        self.assertEqual(self.outbox.refused, 1, "§12 can see it")
        self.assertEqual(self.outbox.drain(), [], "refused whole; nothing half-queued")

    def test_distinct_reservations_in_one_message_are_fine(self):
        msg = pb2.PluginMsg(
            overlay_update=pb2.OverlayUpdate(
                overlay_id="cells", items=[self.item("a", 32), self.item("b", 96)], revision=1
            )
        )
        self.outbox.send(msg)
        self.assertEqual(len(self.outbox.drain()), 1)

    def test_two_widgets_showing_one_image_are_refused(self):
        """A `PanelPatch` is the case §5.6 names, because the receiver
        coalesces it per widget -- so the split is not hypothetical."""
        ref = pb2.ImageRef(segment_id=4, offset=64, len=800)
        msg = pb2.PluginMsg(
            panel_patch=pb2.PanelPatch(
                panel_id="p",
                ops=[
                    pb2.PatchOp(widget_id="left", set_image=ref),
                    pb2.PatchOp(widget_id="right", set_image=ref),
                ],
            )
        )
        with self.assertRaises(c.Refused):
            self.outbox.send(msg)

    def test_the_same_bytes_in_two_messages_are_not_this_rule(self):
        """The rule is within ONE message: two messages naming one reservation
        is what `SegmentRef.uses` is for, and the release is owed at zero."""
        for _ in range(2):
            self.outbox.send(
                pb2.PluginMsg(
                    overlay_update=pb2.OverlayUpdate(
                        overlay_id="cells", items=[self.item("a", 32)], revision=1
                    )
                )
            )
        self.assertEqual(self.outbox.refused, 0)


class Arrangements(unittest.TestCase):
    """Install locally, THEN acknowledge — and acknowledge a repeat again."""

    def setUp(self):
        self.transport = _Transport()
        self.reporter = ViolationReporter()
        self.plane = c.ControlPlane(transport=self.transport, reporter=self.reporter)

    def epoch_change(self, view_id=1, epoch=5, gen=9, revision=0):
        return pb2.HostMsg(
            epoch_change=pb2.EpochChange(
                datasets=[pb2.DatasetState(gen=gen, time_mapping_revision=revision)],
                panes=[pb2.PaneEpoch(view_id=view_id, epoch=epoch, gen=gen)],
            )
        )

    def drained(self):
        return [msg.WhichOneof("msg") for msg in self.plane.outbox.drain()]

    def test_the_transport_is_told_before_the_ack_goes_out(self):
        """§5.5 rule 3: acknowledge first and the PRESS under the new epoch is
        ruled invalid by the fast path and dropped."""
        self.plane.receive(self.epoch_change())
        self.assertEqual(self.transport.calls, [("epoch", 1, 5)])
        acks = self.plane.outbox.drain()
        self.assertEqual(len(acks), 1)
        pane = acks[0].epoch_ack.panes[0]
        self.assertEqual((pane.view_id, pane.epoch), (1, 5))

    def test_a_repeat_is_acknowledged_again(self):
        """§5.5 rule 4: the host re-sends on a lost ack, so suppressing the
        repeat would leave a correctly installed arrangement escalated."""
        self.plane.receive(self.epoch_change())
        self.plane.receive(self.epoch_change())
        self.assertEqual(self.drained(), ["epoch_ack", "epoch_ack"])

    def test_the_same_identity_with_different_state_is_reported(self):
        self.plane.receive(self.epoch_change(gen=9))
        routing = self.plane.receive(
            pb2.HostMsg(
                epoch_change=pb2.EpochChange(
                    datasets=[pb2.DatasetState(gen=11)],
                    panes=[pb2.PaneEpoch(view_id=1, epoch=5, gen=11)],
                )
            )
        )
        self.assertEqual(
            [w for w, _ in routing.violations],
            ["arrangement_content_mismatch"],
            "its own site since 1.0: `interaction_mismatch` is an E-record replay",
        )
        self.assertEqual(
            self.drained(),
            ["epoch_ack", "contract_violation"],
            "the second was not acknowledged, and the report reached the wire",
        )

    def test_the_same_identity_with_a_different_geometry_is_reported(self):
        """vizi's finding: the identity tuple came
        off `PaneEpoch` alone, so a host re-sending one epoch with two
        geometries -- a crop change, a voxel-size override, a channel rename,
        none of which advance `gen` -- installed both silently.  The state is
        the WHOLE `DatasetState`, and moving only the geometry is the case the
        test above does not reach because it moves `gen` as well."""

        def change(z0):
            return pb2.HostMsg(
                epoch_change=pb2.EpochChange(
                    datasets=[
                        pb2.DatasetState(
                            gen=9,
                            geometry=pb2.Geometry(
                                crop=pb2.EdgeBox(z0=z0, z1=10.5, y0=-0.5, y1=20.5, x0=-0.5, x1=30.5)
                            ),
                        )
                    ],
                    panes=[pb2.PaneEpoch(view_id=1, epoch=5, gen=9)],
                )
            )

        self.plane.receive(change(z0=-0.5))
        self.assertEqual(self.drained(), ["epoch_ack"])
        # A byte-identical re-send is still a re-send (§5.5 rule 4).
        self.plane.receive(change(z0=-0.5))
        self.assertEqual(self.drained(), ["epoch_ack"])
        routing = self.plane.receive(change(z0=1.5))
        self.assertEqual([w for w, _ in routing.violations], ["arrangement_content_mismatch"])
        self.assertEqual(self.drained(), ["contract_violation"], "reported, not acknowledged")
        self.assertEqual(self.plane.datasets[9].geometry.crop.z0, -0.5, "the first stays installed")

    def test_an_epoch_naming_an_unarranged_mapping_is_not_acknowledged(self):
        """§8.10, stated outright: MUST NOT acknowledge, MUST report
        `missing_arrangement` -- the host's remedy is to send the mapping,
        wait, and re-send the epoch."""
        routing = self.plane.receive(self.epoch_change(revision=3))
        self.assertEqual([w for w, _ in routing.violations], ["missing_arrangement"])
        self.assertEqual(self.drained(), ["contract_violation"], "reported, not acknowledged")
        # And the remedy works: arrange the mapping, then re-send.
        self.plane.receive(
            pb2.HostMsg(time_mapping=pb2.TimeMapping(gen=9, revision=3, t=[0.0, 1.0]))
        )
        self.plane.receive(self.epoch_change(revision=3))
        self.assertEqual(self.drained(), ["time_mapping_ack", "epoch_ack"])

    def test_the_acknowledged_pane_state_is_readable_without_a_round_trip(self):
        """A tool handler needs the geometry the epoch stands
        for -- spacing, levels, the crop, which channel >= 15 is visible, the
        time mapping -- and had only a polled `view_state()`.  It is the
        arrangement this plugin already acknowledged, keyed by the pane."""
        self.plane.receive(
            pb2.HostMsg(time_mapping=pb2.TimeMapping(gen=9, revision=1, t=[0.0, 0.5, 1.0]))
        )
        geometry = pb2.Geometry(
            gen=9,
            channels=[
                pb2.ChannelInfo(
                    index=0, name="red", source=pb2.ChannelNameSource.CHANNEL_NAME_SOURCE_FILE
                ),
                pb2.ChannelInfo(
                    index=15,
                    name="hi",
                    source=pb2.ChannelNameSource.CHANNEL_NAME_SOURCE_PLACEHOLDER,
                ),
            ],
            extent=pb2.Extent(z0=0, z_n=10, y0=0, y_n=20, x0=0, x_n=30),
            crop=pb2.EdgeBox(z0=-0.5, z1=9.5, y0=1.5, y1=17.5, x0=-0.5, x1=29.5),
            spacing_z=2.0,
            spacing_y=0.5,
            spacing_x=0.5,
            spacing_unit="um",
            levels=[
                pb2.LevelScale(z=1, y=1, x=1),
                pb2.LevelScale(z=2, y=2, x=2),
                # A host whose third level is not 4x, and not the same per axis.
                pb2.LevelScale(z=2, y=3, x=5),
            ],
        )
        self.plane.receive(
            pb2.HostMsg(
                epoch_change=pb2.EpochChange(
                    datasets=[pb2.DatasetState(gen=9, geometry=geometry, time_mapping_revision=1)],
                    panes=[pb2.PaneEpoch(view_id=1, epoch=5, gen=9, channels_high=[15])],
                )
            )
        )
        self.assertEqual(self.drained(), ["time_mapping_ack", "epoch_ack"])
        state = self.plane.pane_state(1, 5)
        self.assertEqual((state.view_id, state.epoch, state.gen), (1, 5, 9))
        self.assertEqual(state.spacing_zyx, (2.0, 0.5, 0.5))
        self.assertTrue(state.any_axis_known)
        self.assertEqual(state.extent, ((0, 10), (0, 20), (0, 30)))
        self.assertEqual(state.crop.at_level(1).as_tuple(), (-0.5, 4.5, 0.5, 8.5, -0.5, 14.5))
        self.assertEqual(state.level_scale(1), (2.0, 2.0, 2.0))
        self.assertEqual(state.crop_at(1).as_tuple(), state.crop.at_level(1).as_tuple())
        # The pane's conversion applies the factor the host
        # DECLARED, per axis; `EdgeBox.at_level` assumes vizi's 2^L and is not it.
        self.assertEqual(state.level_scale(2), (2.0, 3.0, 5.0))
        c = state.crop
        self.assertEqual(
            state.crop_at(2).as_tuple(),
            tuple(
                (e + 0.5) / f - 0.5 for e, f in zip(c.as_tuple(), (2.0, 2.0, 3.0, 3.0, 5.0, 5.0))
            ),
        )
        self.assertNotEqual(state.crop_at(2).as_tuple(), state.crop.at_level(2).as_tuple())
        with self.assertRaises(IndexError):
            state.crop_at(3)
        self.assertEqual(state.visible_channels(0x8001), frozenset({0, 15}))
        self.assertEqual(state.visible_channels(0x0001), frozenset({0}))
        self.assertEqual(state.channel_names(), {0: "red", 15: "hi"})
        # And where each name came from; a placeholder is not declared.
        red, hi = state.channels()
        self.assertEqual((red.name, red.source, red.declared), ("red", "FILE", True))
        self.assertEqual((hi.name, hi.source, hi.declared), ("hi", "PLACEHOLDER", False))
        self.assertEqual(state.time_table, (0.0, 0.5, 1.0))
        # An epoch this plugin has moved past, or a pane it holds nothing for.
        self.assertIsNone(self.plane.pane_state(1, 4))
        self.assertIsNone(self.plane.pane_state(2))
        self.assertIs(self.plane.pane_state(1).gen, 9)
        # A newer generation prunes the older one's state; a mapping arranged
        # for a gen no pane names yet is newer than every live one and stays.
        self.plane.receive(pb2.HostMsg(time_mapping=pb2.TimeMapping(gen=11, revision=1, t=[0.0])))
        self.plane.receive(self.epoch_change(view_id=1, epoch=6, gen=10))
        self.assertNotIn(9, self.plane.datasets)
        self.assertNotIn(9, self.plane.time_tables)
        self.assertIn(11, self.plane.time_tables)
        self.assertIsNone(self.plane.pane_state(1).time_table, "gen 10 arranged no mapping")

    def test_epoch_zero_is_refused_by_identity(self):
        routing = self.plane.receive(self.epoch_change(epoch=0))
        self.assertEqual([w for w, _ in routing.violations], ["zero_id"])
        self.assertEqual(self.drained(), ["contract_violation"], "reported, not acknowledged")

    def test_an_epoch_that_would_rewind_is_not_acknowledged(self):
        self.plane.receive(self.epoch_change(epoch=7))
        routing = self.plane.receive(self.epoch_change(epoch=6))
        self.assertEqual([w for w, _ in routing.violations], ["arrangement_regressed"])
        self.assertEqual(self.drained(), ["epoch_ack", "contract_violation"])

    def test_some_panes_may_be_acknowledged_and_others_not(self):
        """§8.10: each pane resumes on its own."""
        msg = pb2.HostMsg(
            epoch_change=pb2.EpochChange(
                datasets=[
                    pb2.DatasetState(gen=1),
                    pb2.DatasetState(gen=2, time_mapping_revision=4),
                ],
                panes=[
                    pb2.PaneEpoch(view_id=0, epoch=2, gen=1),
                    pb2.PaneEpoch(view_id=1, epoch=2, gen=2),
                ],
            )
        )
        routing = self.plane.receive(msg)
        acks = self.plane.outbox.drain()
        self.assertEqual([p.view_id for p in acks[0].epoch_ack.panes], [0])
        self.assertEqual([w for w, _ in routing.violations], ["missing_arrangement"])

    def test_a_tool_handle_installs_before_its_ack_and_updates_in_place(self):
        """§8.10: a binding for a known handle updates in place and does not
        advance the mark -- the message a conflict's recourse needs."""
        self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=3, tool_id="paint", revision=1))
        )
        self.assertEqual(self.transport.calls, [("activate", 3)])
        self.assertEqual(self.drained(), ["tool_binding_ack"])
        self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=3, tool_id="paint", revision=1))
        )
        self.assertEqual(self.drained(), ["tool_binding_ack"])
        self.assertEqual(self.transport.high_water, 3, "a re-statement does not advance it")

    def test_the_ack_echoes_the_revision_that_was_installed(self):
        """§5.5 rule 4: one identity carries a SEQUENCE of contents, so a handle
        alone cannot say which of them an ack confirms."""
        self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=3, tool_id="paint", revision=4))
        )
        ack = self.plane.outbox.drain()[0].tool_binding_ack
        self.assertEqual((ack.tool, ack.revision), (3, 4))

    def test_a_re_send_carries_the_same_revision_and_is_acknowledged_again(self):
        """A retry is idempotent because its revision is unchanged; the host
        re-sends on a lost ack, so suppressing the repeat would leave a
        correctly installed arrangement escalated."""
        binding = pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=3, tool_id="paint", revision=1))
        self.plane.receive(binding)
        self.plane.receive(binding)
        acks = self.plane.outbox.drain()
        self.assertEqual([a.tool_binding_ack.revision for a in acks], [1, 1])

    def test_new_content_under_a_new_revision_is_installed_in_place(self):
        """§8.10's conflict recourse needs this: a second plugin starting must
        be able to take a binding away from one already running, so the ban on
        changed content under one identity had to go."""
        self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=3, tool_id="paint", revision=1))
        )
        self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=3, tool_id="erase", revision=2))
        )
        self.assertEqual(self.drained(), ["tool_binding_ack", "tool_binding_ack"])
        self.assertEqual(self.plane.tools[3].tool_id, "erase")
        self.assertEqual(self.transport.high_water, 3, "in place; the mark does not advance")

    def test_one_revision_naming_two_contents_is_its_own_violation(self):
        """Not `interaction_mismatch` -- that is an `E`-record replay whose
        bytes changed.  §12 aggregates per `where`, so a shared code merges two
        unrelated defects into one counter and one retained exemplar."""
        self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=3, tool_id="paint", revision=1))
        )
        self.plane.outbox.drain()
        routing = self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=3, tool_id="erase", revision=1))
        )
        self.assertEqual([w for w, _ in routing.violations], ["arrangement_content_mismatch"])
        self.assertEqual(self.drained(), ["contract_violation"], "reported, not acknowledged")
        self.assertEqual(self.plane.tools[3].tool_id, "paint", "the installed one stands")

    def test_a_content_change_is_found_whatever_field_carries_it(self):
        """Compared whole, not against an enumerated list of content fields: a
        list that has to grow with the IDL is one that will not."""
        self.plane.receive(
            pb2.HostMsg(
                tool_binding=pb2.ToolBinding(
                    tool=3, tool_id="paint", revision=1, cancel_on_camera_move=True
                )
            )
        )
        self.plane.outbox.drain()
        routing = self.plane.receive(
            pb2.HostMsg(
                tool_binding=pb2.ToolBinding(
                    tool=3, tool_id="paint", revision=1, cancel_on_camera_move=False
                )
            )
        )
        self.assertEqual([w for w, _ in routing.violations], ["arrangement_content_mismatch"])
        self.assertIn("cancel_on_camera_move", routing.violations[0][1])

    def test_a_revision_going_backwards_is_refused(self):
        """Monotonic per handle: a statement older than the one in force can be
        neither a retry of it nor a successor to it."""
        self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=3, tool_id="paint", revision=5))
        )
        self.plane.outbox.drain()
        routing = self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=3, tool_id="paint", revision=4))
        )
        self.assertEqual([w for w, _ in routing.violations], ["arrangement_regressed"])

    def test_a_backwards_revision_is_not_the_same_site_as_changed_content(self):
        """§11 at `1.0`: two defects a developer wants to tell apart, and §12
        aggregates per ``where`` -- one counter for both is one counter nobody
        can act on.  The same revision carrying different content is
        `arrangement_content_mismatch`; the identity moving the wrong way is
        `arrangement_regressed`."""
        self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=4, tool_id="paint", revision=5))
        )
        self.plane.outbox.drain()
        changed = self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=4, tool_id="erase", revision=5))
        )
        self.assertEqual([w for w, _ in changed.violations], ["arrangement_content_mismatch"])

    def test_a_first_binding_that_does_not_begin_at_one_is_installed_and_reported(self):
        """§5.5 rule 4: each IDENTITY'S sequence begins at 1, so a sender
        allocating from a per-session counter is non-conforming -- **and a
        receiver installs it and reports rather than refusing.**

        Nothing downstream depends on the number: a receiver's only uses of a
        revision are echoing it in the acknowledgement and checking the identity
        has not regressed, so 12 echoes as 12 and opens the gate on exactly the
        content installed.  Refusing would cost a session over arithmetic.  Two
        SDKs shipped the split and this is the answer to it."""
        routing = self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=9, tool_id="paint", revision=12))
        )
        self.assertEqual([w for w, _ in routing.violations], ["arrangement_first_revision"])
        self.assertIn(9, self.plane.tools, "installed")
        self.assertEqual(
            self.drained(),
            ["tool_binding_ack", "contract_violation"],
            "acknowledged with the revision it was given, and the defect reported",
        )

    def test_the_acknowledgement_echoes_the_revision_it_was_given(self):
        """The receiver echoes what it INSTALLED, which is the sender's number
        -- that is what makes 12 match the sender's ledger."""
        self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=11, tool_id="paint", revision=12))
        )
        ack = [m for m in self.plane.outbox.drain() if m.WhichOneof("msg") == "tool_binding_ack"]
        self.assertEqual(ack[0].tool_binding_ack.revision, 12)

    def test_a_first_time_mapping_that_does_not_begin_at_one_is_reported_too(self):
        """§5.5 rule 4 binds the ARRANGEMENT CLASS.  §8.11 gives `TimeMapping`
        a revision per `gen` and §8.12 lists it as an arrangement, so `gen` is
        an identity like a tool handle and its sequence begins at 1.  This SDK
        applied the rule to one tenant of the class; the Go SDK applied it to
        two, which is how the gap was found."""
        routing = self.plane.receive(
            pb2.HostMsg(time_mapping=pb2.TimeMapping(gen=4, revision=7, t=[0.0, 1.0]))
        )
        self.assertEqual([w for w, _ in routing.violations], ["arrangement_first_revision"])
        self.assertEqual(self.plane.mappings[4], 7, "installed")
        self.assertEqual(self.drained(), ["time_mapping_ack", "contract_violation"])

    def test_a_later_revision_for_a_known_gen_says_nothing(self):
        self.plane.receive(pb2.HostMsg(time_mapping=pb2.TimeMapping(gen=5, revision=1, t=[0.0])))
        self.plane.outbox.drain()
        routing = self.plane.receive(
            pb2.HostMsg(time_mapping=pb2.TimeMapping(gen=5, revision=9, t=[0.0]))
        )
        self.assertEqual(routing.violations, ())

    def test_a_time_mapping_that_regresses_or_changes_under_one_revision_is_refused(self):
        """§5.5 rule 4, as the other arrangement receivers apply it:
        older than installed is `arrangement_regressed`; the same
        revision with a different table is `arrangement_content_mismatch`.
        Neither is installed or acknowledged; a true restatement is re-acked."""
        tm = lambda rev, t: pb2.HostMsg(  # noqa: E731
            time_mapping=pb2.TimeMapping(gen=6, revision=rev, t=t)
        )
        self.plane.receive(tm(2, [0.0, 1.0]))
        self.plane.outbox.drain()
        routing = self.plane.receive(tm(1, [0.0]))
        self.assertEqual([w for w, _ in routing.violations], ["arrangement_regressed"])
        routing = self.plane.receive(tm(2, [0.0, 2.0]))
        self.assertEqual([w for w, _ in routing.violations], ["arrangement_content_mismatch"])
        self.assertEqual(self.plane.time_tables[6], (0.0, 1.0), "the installed table stands")
        self.assertNotIn("time_mapping_ack", self.drained())
        routing = self.plane.receive(tm(2, [0.0, 1.0]))
        self.assertEqual(routing.violations, ())
        self.assertEqual(self.drained(), ["time_mapping_ack"])

    def test_a_first_binding_at_one_says_nothing(self):
        routing = self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=10, tool_id="paint", revision=1))
        )
        self.assertEqual(routing.violations, ())

    def test_revision_zero_names_no_statement(self):
        """§5.5 rule 4: revisions begin at 1, and a 0 confirms nothing while
        looking exactly like confirmation -- which is the failure the field was
        added to prevent."""
        routing = self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=3, tool_id="paint"))
        )
        self.assertEqual([w for w, _ in routing.violations], ["zero_id"])
        self.assertEqual(self.drained(), ["contract_violation"], "not acknowledged")
        self.assertNotIn(3, self.plane.tools)

    def test_a_gap_is_accepted_and_going_backwards_is_not(self):
        """§13: handles arriving out of order are rejected, and a gap is
        accepted -- a handle may be allocated and never registered."""
        self.plane.receive(pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=5, revision=1)))
        self.assertEqual(self.drained(), ["tool_binding_ack"])
        routing = self.plane.receive(pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=4, revision=1)))
        self.assertEqual([w for w, _ in routing.violations], ["unknown_tool"])
        self.assertEqual(self.drained(), ["contract_violation"], "reported, not acknowledged")

    def test_retiring_a_handle_never_activated_is_reported(self):
        routing = self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=9, retired=True, revision=1))
        )
        self.assertEqual([w for w, _ in routing.violations], ["unknown_tool"])

    def test_a_retirement_is_acknowledged_and_drops_the_tool(self):
        self.plane.receive(pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=2, revision=1)))
        self.plane.outbox.drain()
        self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=2, retired=True, revision=2))
        )
        self.assertEqual(self.drained(), ["tool_binding_ack"])
        self.assertEqual(self.plane.tools, {})

    def test_a_binding_whose_mode_this_build_does_not_know_is_refused_alone(self):
        """§8.10: a later minor's mode is refused -- neither activated nor
        acknowledged, the mode diagnosed locally -- and is not a violation.
        A retirement activates nothing, so its mode refuses nothing."""
        for handle, revision, mode in ((3, 1, 3), (4, 1, 1000)):
            with self.subTest(mode=mode):
                routing = self.plane.receive(
                    pb2.HostMsg(
                        tool_binding=pb2.ToolBinding(
                            tool=handle, tool_id="paint", revision=revision, mode=mode
                        )
                    )
                )
                self.assertEqual(routing.violations, ())
                self.assertIn(f"mode {mode} is not a ToolMode", routing.diagnostics[0])
                self.assertIn(f"mode {mode} is not a ToolMode", self.plane.diagnostics[-1])
                self.assertEqual(self.drained(), [], "not acknowledged")
                self.assertNotIn(handle, self.plane.tools)
                self.assertNotIn(("activate", handle), self.transport.calls)
        self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=5, tool_id="paint", revision=1))
        )
        self.assertEqual(self.drained(), ["tool_binding_ack"], "the session continues")
        routing = self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=5, retired=True, revision=2, mode=7))
        )
        self.assertEqual((routing.violations, self.drained()), ((), ["tool_binding_ack"]))
        self.assertEqual(self.plane.tools, {})

    def test_a_restatement_after_a_refused_first_binding_is_not_a_first_revision(self):
        """A refused first statement counts as received: revision 1 named a mode this build does not
        know and was refused; the host's re-statement at revision 2 is the identity's SECOND
        statement, as §5.5 rule 4 requires it to be, and is not reported as an identity starting in
        the wrong place. A refused statement still counts as received."""
        self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=6, tool_id="paint", revision=1, mode=99))
        )
        self.assertEqual(self.drained(), [], "refused, not acknowledged")
        routing = self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=6, tool_id="paint", revision=2))
        )
        self.assertEqual(routing.violations, ())
        self.assertIn(6, self.plane.tools)
        self.assertEqual(self.drained(), ["tool_binding_ack"])

    def test_a_refused_bindings_diagnostic_is_recorded_once(self):
        """The routing carries the note and `receive` diagnoses it; the
        binding handler diagnosed it as well, so it was recorded twice."""
        said = len(self.plane.diagnostics)
        routing = self.plane.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=3, tool_id="paint", revision=1, mode=99))
        )
        self.assertEqual(len(self.plane.diagnostics), said + 1)
        self.assertEqual(routing.diagnostics, (self.plane.diagnostics[-1],))
        self.assertIn("mode 99 is not a ToolMode", self.plane.diagnostics[-1])

    def test_a_restatement_differing_only_by_an_unknown_field_is_the_same_content(self):
        """§5.5 rule 4: an arrangement's content is the fields the receiver
        knows, so a field a later minor adds takes no part -- at any depth."""
        binding = pb2.ToolBinding(
            tool=3, tool_id="paint", revision=1, mode=pb2.ToolMode.TOOL_MODE_GESTURE
        )
        binding.triggers.add(key="P")
        self.plane.receive(pb2.HostMsg(tool_binding=binding))
        restated = _with_unknown(binding)
        restated.triggers[0].CopyFrom(_with_unknown(restated.triggers[0]))
        routing = self.plane.receive(pb2.HostMsg(tool_binding=restated))
        self.assertEqual(routing.violations, ())
        self.assertEqual(self.drained(), ["tool_binding_ack", "tool_binding_ack"])
        changed = pb2.ToolBinding()
        changed.CopyFrom(restated)
        changed.triggers[0].key = "Q"
        routing = self.plane.receive(pb2.HostMsg(tool_binding=changed))
        self.assertEqual([w for w, _ in routing.violations], ["arrangement_content_mismatch"])
        self.assertIn("triggers", routing.violations[0][1])

    def test_an_epoch_re_sent_with_only_an_unknown_field_is_acknowledged_again(self):
        """§5.5 rule 4 on the pane identity: the dataset's known fields are its
        content, so a field a later minor adds to it changes nothing."""
        first = self.epoch_change()
        first.epoch_change.datasets[0].geometry.CopyFrom(
            pb2.Geometry(crop=pb2.EdgeBox(z0=-0.5, z1=10.5))
        )
        self.plane.receive(first)
        again = pb2.HostMsg()
        again.CopyFrom(first)
        dataset = again.epoch_change.datasets[0]
        dataset.geometry.CopyFrom(_with_unknown(dataset.geometry))
        dataset.CopyFrom(_with_unknown(dataset))
        routing = self.plane.receive(again)
        self.assertEqual(routing.violations, ())
        self.assertEqual(self.drained(), ["epoch_ack", "epoch_ack"])

    def test_a_time_mapping_over_its_bound_is_refused_rather_than_applied(self):
        """§9: the bound is 1 048 576 -- 8 MiB of doubles, the only field here a
        legitimate large dataset can push."""
        huge = pb2.HostMsg(
            time_mapping=pb2.TimeMapping(gen=1, revision=1, t=[0.0] * (c.MAX_TIME_MAPPING + 1))
        )
        routing = self.plane.receive(huge)
        self.assertTrue(routing.diagnostics)
        self.assertEqual(
            self.drained(),
            ["contract_violation"],
            "§13 at `1.0`: decode, refuse to ACT, and report -- site 21 exists "
            "because both SDKs already refused and kept the reason to themselves",
        )
        self.assertEqual(self.plane.mappings, {})
        self.assertEqual(self.plane.refused_bounds, 1)

    def test_a_pyramid_deeper_than_the_bound_is_refused(self):
        """§9: 64 levels, which is logarithmic in extent and cannot plausibly
        be exceeded -- so one that is says the message is wrong."""
        geometry = pb2.Geometry(gen=1, levels=[pb2.LevelScale() for _ in range(c.MAX_LEVELS + 1)])
        routing = self.plane.receive(
            pb2.HostMsg(
                epoch_change=pb2.EpochChange(
                    datasets=[pb2.DatasetState(gen=1, geometry=geometry)],
                    panes=[pb2.PaneEpoch(view_id=0, epoch=1, gen=1)],
                )
            )
        )
        self.assertTrue(routing.diagnostics)
        self.assertEqual(
            self.drained(),
            ["contract_violation"],
            "nothing installed, nothing acknowledged -- and the host told why",
        )

    def test_an_over_long_roi_list_wakes_its_waiter_instead_of_delivering(self):
        """§13: refuse to APPLY it.  §5.1: and do not make the waiter pay for
        the peer's mistake — a lost reply is unrecoverable for the session, so
        the caller would sit out its whole deadline for an answer that already
        exists.  This SDK let it time out until the Go SDK's reading found it."""
        delivered: list = []
        plane = c.ControlPlane(deliver_reply=lambda hm: bool(delivered.append(hm)) or True)
        rois = pb2.RoiList(rois=[pb2.RoiRecord() for _ in range(3)])
        plane.receive(pb2.HostMsg(id="q", rois=rois))
        self.assertEqual(len(delivered), 1, "an ordinary list is delivered")
        big = pb2.HostMsg(id="q2")
        big.rois.rois.extend(pb2.RoiRecord() for _ in range(c.MAX_REPEATED + 1))
        plane.receive(big)
        self.assertEqual(len(delivered), 2, "the waiter was woken")
        self.assertEqual(delivered[1].WhichOneof("msg"), "host_error")
        self.assertEqual(delivered[1].host_error.kind, pb2.Error.PROTOCOL)
        self.assertEqual(delivered[1].rois.rois, [], "and not with the list it refused")
        self.assertEqual(plane.refused_bounds, 1)

    def test_an_over_long_request_is_answered_rather_than_left_to_time_out(self):
        """§11a: **answer every host request.**  An `Error` is a legal answer to
        any request (§8), so refusing to act is the whole of §13's ask and
        refusing to speak is a second failure on top of the peer's first."""
        big = pb2.HostMsg(id="r")
        big.export_chunk.handle = "e0"
        big.export_chunk.channels.extend(range(c.MAX_REPEATED + 1))
        routing = self.plane.receive(big)
        self.assertEqual(routing.action, c.Action.HANDLED, "and never dispatched")
        out = self.plane.outbox.drain()
        kinds = [m.WhichOneof("msg") for m in out]
        self.assertEqual(sorted(kinds), ["contract_violation", "error"])
        answer = next(m for m in out if m.WhichOneof("msg") == "error")
        self.assertEqual((answer.id, answer.error.kind), ("r", pb2.Error.PROTOCOL))
        self.assertEqual(self.plane.refused_bounds, 1)

    def test_an_inline_field_over_eight_mib_is_refused_on_receive_too(self):
        """§9: a `string` is an inline byte field for this bound, and the
        receiver walks bytes AND string fields -- `MetadataResult.text` is the
        case.  Refused rather than applied, answered where there is a reply,
        and no site is claimed for it (none names an inline overflow)."""
        big = pb2.HostMsg(id="r")
        big.export_chunk.handle = "e0"
        big.export_chunk.inline = b"x" * (c.MAX_INLINE_BYTES + 1)
        routing = self.plane.receive(big)
        self.assertEqual(routing.action, c.Action.HANDLED, "and never dispatched")
        out = self.plane.outbox.drain()
        kinds = [m.WhichOneof("msg") for m in out]
        self.assertEqual(kinds, ["error"], "answered, and no violation site claimed")
        self.assertEqual(out[0].error.kind, pb2.Error.PROTOCOL)
        self.assertIn("bytes inline, over", out[0].error.message)
        self.assertEqual(self.plane.refused_bounds, 1)
        found = c.walk_bounds(big)
        self.assertEqual(found[0], "inline")

    def test_the_bound_covers_every_repeated_field_and_not_a_named_three(self):
        """§9: 65 536 is *"a backstop against a peer that ignores its own
        manifest"*, so it is the general rule and the three fields with their
        own numbers are the exceptions.  This SDK enumerated three fields and
        left every other one unbounded until the Go SDK's reflective walk
        showed what that missed."""
        cases = {
            "vizi.plugin.v1.EpochChange.panes": pb2.HostMsg(
                epoch_change=pb2.EpochChange(
                    panes=[pb2.PaneEpoch(view_id=1, epoch=1, gen=1)] * (c.MAX_REPEATED + 1)
                )
            ),
            "vizi.plugin.v1.Geometry.channels": pb2.HostMsg(
                epoch_change=pb2.EpochChange(
                    datasets=[
                        pb2.DatasetState(
                            gen=1,
                            geometry=pb2.Geometry(
                                channels=[pb2.ChannelInfo()] * (c.MAX_REPEATED + 1)
                            ),
                        )
                    ]
                )
            ),
            "vizi.plugin.v1.InstallSegment.regions": pb2.HostMsg(
                install_segment=pb2.InstallSegment(
                    segment_id=1, purpose="V", regions=[pb2.Region()] * (c.MAX_REPEATED + 1)
                )
            ),
        }
        for name, msg in cases.items():
            with self.subTest(name):
                plane = c.ControlPlane(reporter=ViolationReporter())
                routing = plane.receive(msg)
                self.assertTrue(routing.diagnostics)
                self.assertIn(name, routing.diagnostics[0])
                self.assertEqual(plane.refused_bounds, 1)

    def test_the_pose_arrays_take_the_time_axis_bound(self):
        """§9: the three dense pose arrays are the time axis
        again, via the span an ROI is posed over, so they take `TimeMapping.t`'s
        1 048 576 for `TimeMapping.t`'s reason.  At 65 536 the general backstop
        fired at 10 922 frames of `pose_stretch`, which a calcium-imaging
        timelapse reaches -- on a reply the HOST sends."""
        plane = c.ControlPlane(reporter=ViolationReporter())
        posed = pb2.HostMsg(id="q")
        roi = posed.rois.rois.add()
        roi.roi.pose_stretch.extend([0.0] * (c.MAX_REPEATED + 1))
        plane.receive(posed)
        self.assertEqual(plane.refused_bounds, 0, "6*span is not a defect")
        over = pb2.HostMsg(id="q2")
        roi = over.rois.rois.add()
        roi.roi.pose_stretch.extend([0.0] * (c.MAX_TIME_MAPPING + 1))
        routing = plane.receive(over)
        self.assertEqual(plane.refused_bounds, 1, "and 1 048 576 is still a bound")
        self.assertIn("pose_stretch", routing.diagnostics[0])

    def test_a_pose_extension_takes_the_bound_of_the_pose_it_extends(self):
        """``RoiPoseExtension``'s arrays had no row, so a
        tracker could contribute ~174 k frames of pose but extend one by only
        10 922 frames of stretch per message; the host answered PROTOCOL."""
        extend = pb2.PluginMsg(id="q")
        ext = extend.extend_roi_poses.extensions.add()
        ext.pose_stretch.extend([0.0] * (c.MAX_REPEATED + 1))
        self.assertIsNone(c.walk_bounds(extend), "6*span of an extension is not a defect")
        ext.pose_stretch.extend([0.0] * (c.MAX_TIME_MAPPING - c.MAX_REPEATED))
        found = c.walk_bounds(extend)
        self.assertIsNotNone(found, "and 1 048 576 is still a bound")
        self.assertIn("pose_stretch", found[1])

    def test_every_bounded_field_exists_in_the_stubs(self):
        """The proto package is the protocol major and
        moves with it; `_FIELD_BOUNDS` is keyed on full names, and a
        key that misses falls back to the general bound silently, which would
        bound `TimeMapping.t` at 65 536.  This is the check that says so at
        once."""
        self.assertEqual(c.field_bounds_resolve(), [])
        with mock.patch.dict(c._FIELD_BOUNDS, {"vizi.plugin.v0.TimeMapping.t": 5}):
            self.assertEqual(c.field_bounds_resolve(), ["vizi.plugin.v0.TimeMapping.t"])
        with mock.patch.dict(c._FIELD_BOUNDS, {"vizi.plugin.v1.TimeMapping.nope": 5}):
            self.assertEqual(c.field_bounds_resolve(), ["vizi.plugin.v1.TimeMapping.nope"])

    def test_a_field_with_its_own_number_keeps_it(self):
        """§9's three exceptions scale with something other than the manifest,
        so the general backstop must not tighten them."""
        plane = c.ControlPlane(reporter=ViolationReporter())
        ok = pb2.HostMsg(
            time_mapping=pb2.TimeMapping(gen=1, revision=1, t=[0.0] * (c.MAX_REPEATED + 1))
        )
        self.assertEqual(plane.receive(ok).diagnostics, (), "1 048 576 is its bound, not 65 536")
        deep = pb2.HostMsg(
            epoch_change=pb2.EpochChange(
                datasets=[
                    pb2.DatasetState(gen=1, geometry=pb2.Geometry(levels=[pb2.LevelScale()] * 65))
                ]
            )
        )
        self.assertTrue(plane.receive(deep).diagnostics, "64 is its bound, not 65 536")


# --------------------------------------------------------------------------
# §8.12: the grant path
# --------------------------------------------------------------------------


def staged_region(index: int = 0, offset: int = 64, extent: int = 1024):
    return pb2.Region(
        index=index,
        offset=offset,
        extent=extent,
        discipline=pb2.RegionDiscipline.REGION_DISCIPLINE_STAGED,
        producer=pb2.RegionProducer.REGION_PRODUCER_HOST,
    )


class StageArms(unittest.TestCase):
    """``processing_stages.md`` §4: an arm is an arrangement identified by
    ``(pipeline, position)``, acknowledged only when this end can use it."""

    RING = 40

    def setUp(self):
        self.store = m.SegmentStore(m.PURPOSES)
        self.armed: list = []
        self.plane = c.ControlPlane(
            memory=self.store, reporter=ViolationReporter(), on_stage_arm=self.armed.append
        )
        self.grant(self.RING)

    def grant(self, segment_id, purpose="S", region=None):
        self.plane.capability(segment_id, 4096, PERM_RW, fd=make_object(4096, segment_id))
        self.plane.receive(
            pb2.HostMsg(
                install_segment=pb2.InstallSegment(
                    segment_id=segment_id,
                    purpose=purpose,
                    regions=[region if region is not None else staged_region()],
                )
            )
        )
        self.plane.outbox.drain()

    def arm(self, *, stage="stabilize", revision=1, pipeline=7, position=1, segment=None, **kw):
        return pb2.HostMsg(
            stage_arm=pb2.StageArm(
                stage_id=stage,
                revision=revision,
                segment_id=self.RING if segment is None else segment,
                pipeline=pipeline,
                position=position,
                input=pb2.StageShape(channels=[0, 1], z=4, y=8, x=8, dtype=pb2.DType.DTYPE_U16),
                **{"output": pb2.StageShape(z=4, y=8, x=8, dtype=pb2.DType.DTYPE_U16), **kw},
            )
        )

    def acks(self):
        return [
            msg.stage_arm_ack
            for msg in self.plane.outbox.drain()
            if msg.WhichOneof("msg") == "stage_arm_ack"
        ]

    def wheres(self, routing):
        return [w for w, _ in routing.violations]

    def test_an_arm_is_installed_and_acknowledged_with_its_identity(self):
        routing = self.plane.receive(self.arm(position=3, new_channels={"flow": 2}))
        self.assertEqual(routing.violations, ())
        (ack,) = self.acks()
        self.assertEqual(
            (ack.stage_id, ack.revision, ack.pipeline, ack.position), ("stabilize", 1, 7, 3)
        )
        self.assertEqual(self.plane.stage_arms[(7, 3)].new_channels["flow"], 2)
        self.assertEqual(len(self.armed), 1, "the runner is told")

    def test_one_stage_at_two_positions_is_two_arms(self):
        """§4: vizi assigns positions, and an arm at a new position adds one."""
        self.plane.receive(self.arm(position=1))
        self.plane.receive(self.arm(position=3))
        self.assertEqual(sorted(self.plane.stage_arms), [(7, 1), (7, 3)])
        self.assertEqual(len(self.acks()), 2)

    def test_a_repeat_is_acknowledged_again_and_a_changed_one_is_a_mismatch(self):
        self.plane.receive(self.arm())
        self.plane.receive(self.arm())
        self.assertEqual(len(self.acks()), 2)
        routing = self.plane.receive(self.arm(position=1, new_channels={"x": 9}))
        self.assertEqual(self.wheres(routing), ["arrangement_content_mismatch"])
        self.assertEqual(self.acks(), [])

    def test_a_later_revision_replaces_and_an_earlier_one_regresses(self):
        self.plane.receive(self.arm(revision=2))
        self.plane.receive(self.arm(revision=3, segment=self.RING))
        self.assertEqual(self.plane.stage_arms[(7, 1)].revision, 3)
        routing = self.plane.receive(self.arm(revision=2))
        self.assertEqual(self.wheres(routing), ["arrangement_regressed"])

    def test_a_first_revision_other_than_1_is_installed_and_reported(self):
        routing = self.plane.receive(self.arm(revision=4))
        self.assertEqual(self.wheres(routing), ["arrangement_first_revision"])
        self.assertEqual(len(self.acks()), 1)

    def test_revision_0_names_no_statement(self):
        routing = self.plane.receive(self.arm(revision=0))
        self.assertEqual(self.wheres(routing), ["zero_id"])
        self.assertEqual(self.acks(), [])

    def test_an_unusable_arm_is_refused_and_not_acknowledged(self):
        """§4: never held for a grant that may arrive later -- a conforming
        host arms only after the ring's grant is installed."""
        self.grant(41, purpose="V", region=loaned_region())
        self.plane.declared_stages = frozenset({"stabilize"})
        cases = {
            "no such segment": self.arm(segment=99),
            "not a ring": self.arm(segment=41),
            "undeclared": self.arm(stage="denoise"),
            "pipeline 0": self.arm(pipeline=0),
            "pipeline too big": self.arm(pipeline=0x10000),
            "position 0": self.arm(position=0),
            "position too big": self.arm(position=32767),
            "channel too big": self.arm(new_channels={"flow": 0x10000}),
        }
        for label, msg in cases.items():
            with self.subTest(label):
                routing = self.plane.receive(msg)
                self.assertEqual(self.wheres(routing), ["stage_arm_refused"])
                self.assertEqual(self.acks(), [])
        self.assertEqual(self.plane.stage_arms, {})
        self.assertEqual(self.armed, [])

    def test_an_arm_that_cannot_hold_the_stage_to_anything_is_refused(self):
        """An arm with no input or output shape made every
        slot fail §7 (dtype 0 matches no header); one naming a channel twice
        is ambiguous about which plane is which (§5.2).  Refused at arming,
        site 32, not acknowledged."""
        no_output = self.arm()
        no_output.stage_arm.ClearField("output")
        no_input = self.arm()
        no_input.stage_arm.ClearField("input")
        twice = self.arm()
        twice.stage_arm.input.channels[:] = [0, 0]
        clash = self.arm(new_channels={"flow": 1})
        for label, msg in (
            ("no output", no_output),
            ("no input", no_input),
            ("input twice", twice),
            ("new channel on an input", clash),
        ):
            with self.subTest(label):
                routing = self.plane.receive(msg)
                self.assertEqual(self.wheres(routing), ["stage_arm_refused"])
                self.assertEqual(self.acks(), [])

    def test_an_arm_naming_a_dtype_this_build_does_not_know_is_refused_alone(self):
        """§4: a later minor's dtype, on the input or the output, refuses the
        arm -- not acknowledged, diagnosed locally naming the dtype -- and is
        never ``stage_arm_refused``."""
        unknown_input = self.arm()
        unknown_input.stage_arm.input.dtype = 5
        unknown_output = self.arm()
        unknown_output.stage_arm.output.dtype = 1000
        for label, msg, named in (
            ("input", unknown_input, "input dtype 5"),
            ("output", unknown_output, "output dtype 1000"),
        ):
            with self.subTest(label):
                routing = self.plane.receive(msg)
                self.assertEqual(routing.violations, ())
                self.assertIn(f"{named} is not a DType", routing.diagnostics[0])
                self.assertIn(f"{named} is not a DType", self.plane.diagnostics[-1])
                self.assertEqual(self.plane.outbox.drain(), [], "not acknowledged, nothing sent")
        self.assertEqual((self.plane.stage_arms, self.armed), ({}, []))
        self.plane.receive(self.arm())
        self.assertEqual(len(self.acks()), 1, "the session continues")

    def test_a_rearm_after_a_refused_first_arm_is_not_a_first_revision(self):
        """A refused first statement counts as received: revision 1 carried a later minor's `Value`
        kind in `params` -- refused, not acknowledged, not reported (§4). The host re-arms the
        position at revision 2 with a kind this build knows, as §4 allows and §5.5 rule 4 numbers
        it: acknowledged, and not reported as a first statement, since the identity's first was
        received."""
        later = self.arm()
        later.stage_arm.params["sigma"].MergeFromString(b"\xf8\xf0\x04\x01")
        routing = self.plane.receive(later)
        self.assertEqual(routing.violations, ())
        self.assertIn("this build does not know", routing.diagnostics[0])
        self.assertEqual(self.acks(), [], "refused, not acknowledged")
        routing = self.plane.receive(self.arm(revision=2))
        self.assertEqual(routing.violations, ())
        self.assertEqual([a.revision for a in self.acks()], [2])

    def test_a_rearm_after_an_unusable_first_arm_is_not_a_first_revision(self):
        """The same for an arm refused as unusable: the host was told
        (`stage_arm_refused`), and its next statement is revision 2."""
        routing = self.plane.receive(self.arm(segment=999))
        self.assertEqual(self.wheres(routing), ["stage_arm_refused"])
        routing = self.plane.receive(self.arm(revision=2))
        self.assertEqual(routing.violations, ())
        self.assertEqual(len(self.acks()), 1)

    def test_an_arm_restated_with_only_an_unknown_field_is_acknowledged_again(self):
        """§5.5 rule 4: content is the fields the receiver knows."""
        first = self.arm()
        self.plane.receive(first)
        again = pb2.HostMsg(stage_arm=_with_unknown(first.stage_arm))
        again.stage_arm.input.CopyFrom(_with_unknown(again.stage_arm.input))
        routing = self.plane.receive(again)
        self.assertEqual(routing.violations, ())
        self.assertEqual(len(self.acks()), 2)

    def test_the_largest_position_is_armable(self):
        self.plane.receive(self.arm(position=c.STAGE_MAX_POSITION))
        self.assertEqual(len(self.acks()), 1)

    def test_a_retiring_ring_is_not_armed_and_an_arm_outlives_its_ring(self):
        """Disarming is retiring the ring (§4); the arm's identity stays, so
        the re-arm against the superseding ring is its next revision."""
        self.plane.receive(self.arm())
        self.plane.receive(pb2.HostMsg(retire_segment=pb2.RetireSegment(segment_id=self.RING)))
        self.assertIn((7, 1), self.plane.stage_arms)
        routing = self.plane.receive(self.arm(revision=2))
        self.assertEqual(self.wheres(routing), ["stage_arm_refused"])
        self.grant(42)
        routing = self.plane.receive(self.arm(revision=2, segment=42))
        self.assertEqual(routing.violations, ())
        self.assertEqual(self.plane.stage_arms[(7, 1)].segment_id, 42)


class Grants(unittest.TestCase):
    """Two halves on two channels, either order, one acknowledgement."""

    def setUp(self):
        self.store = m.SegmentStore(PURPOSES)
        self.reporter = ViolationReporter()
        self.plane = c.ControlPlane(memory=self.store, reporter=self.reporter)

    def install(self, segment_id=1, supersedes=0, purpose="V"):
        return pb2.HostMsg(
            install_segment=pb2.InstallSegment(
                segment_id=segment_id,
                purpose=purpose,
                supersedes=supersedes,
                regions=[loaned_region()],
            )
        )

    def drain(self):
        return self.plane.outbox.drain()

    def test_a_stage_ring_crosses_the_wire_as_STAGED_slots(self):
        """``processing_stages.md`` §4: purpose ``S``, and no subject."""
        store = m.SegmentStore(m.PURPOSES)
        plane = c.ControlPlane(memory=store)
        plane.capability(12, 4096, PERM_RW, fd=make_object(4096, 12))
        plane.receive(
            pb2.HostMsg(
                install_segment=pb2.InstallSegment(
                    segment_id=12,
                    purpose="S",
                    regions=[
                        pb2.Region(
                            index=0,
                            offset=64,
                            extent=1024,
                            discipline=pb2.RegionDiscipline.REGION_DISCIPLINE_STAGED,
                            producer=pb2.RegionProducer.REGION_PRODUCER_HOST,
                        )
                    ],
                )
            )
        )
        acks = plane.outbox.drain()
        self.assertTrue(acks[0].grant_ack.HasField("installed"), acks)
        self.assertIs(store.segment(12).regions[0].discipline, m.Discipline.STAGED)

    def test_the_second_half_emits_the_acknowledgement_in_either_order(self):
        for order in ("install first", "capability first"):
            with self.subTest(order):
                store = m.SegmentStore(PURPOSES)
                plane = c.ControlPlane(memory=store)
                segment_id = 10 if order == "install first" else 11
                fd = make_object(4096, segment_id)
                if order == "install first":
                    plane.receive(self.install(segment_id))
                    self.assertEqual(plane.outbox.drain(), [], "no ack on one half")
                    plane.capability(segment_id, 4096, PERM_RW, fd=fd)
                else:
                    plane.capability(segment_id, 4096, PERM_RW, fd=fd)
                    self.assertEqual(plane.outbox.drain(), [])
                    plane.receive(self.install(segment_id))
                acks = plane.outbox.drain()
                self.assertEqual(len(acks), 1)
                self.assertEqual(acks[0].grant_ack.segment_id, segment_id)
                self.assertEqual(
                    acks[0].grant_ack.installed.identity_proof,
                    b"\xa5" * 16,
                    "read OUT OF the mapping, never echoed from the record",
                )

    def test_a_superseding_grant_announces_the_old_segments_release(self):
        """§9: `supersedes` behaves as a `RetireSegment` in every respect,
        the `SegmentReleased` included -- a store that retires the old segment
        silently leaves a host re-granting a chunked overlay's arena waiting
        on a release that never comes."""
        for order in ("install first", "capability first"):
            with self.subTest(order):
                store = m.SegmentStore(PURPOSES)
                plane = c.ControlPlane(memory=store)
                old, new = (21, 22) if order == "install first" else (23, 24)
                plane.receive(self.install(old))
                plane.capability(old, 4096, PERM_RW, fd=make_object(4096, old))
                self.assertEqual([k.WhichOneof("msg") for k in plane.outbox.drain()], ["grant_ack"])
                fd = make_object(4096, new)
                if order == "install first":
                    plane.receive(self.install(new, supersedes=old))
                    self.assertEqual(plane.outbox.drain(), [], "nothing retires on one half")
                    plane.capability(new, 4096, PERM_RW, fd=fd)
                else:
                    plane.capability(new, 4096, PERM_RW, fd=fd)
                    self.assertEqual(plane.outbox.drain(), [])
                    plane.receive(self.install(new, supersedes=old))
                sent = plane.outbox.drain()
                self.assertEqual(
                    [
                        (k.WhichOneof("msg"), getattr(k, k.WhichOneof("msg")).segment_id)
                        for k in sent
                    ],
                    [("grant_ack", new), ("segment_released", old)],
                )
                self.assertEqual([seg.segment_id for seg in store.installed()], [new])
                # §9.4: a RetireSegment for it afterwards is answered again.
                plane.receive(pb2.HostMsg(retire_segment=pb2.RetireSegment(segment_id=old)))
                self.assertEqual(plane.outbox.drain()[0].segment_released.segment_id, old)

    def test_a_rejected_M_is_acknowledged_as_a_malformed_grant(self):
        """`M` §7: the transport reports the reason and closes the descriptor;
        this end owes the acknowledgement, and owes it against the segment id
        so the host's query is answered `failed` rather than `not present`."""
        self.plane.reject_capability(18, "reserved perms bit set", record=b"\x12" * 112)
        ack = self.drain()[0].grant_ack
        self.assertEqual(ack.segment_id, 18)
        self.assertEqual(ack.failed.failure_class, pb2.GrantFailureClass.GRANT_FAILURE_MALFORMED)
        self.plane.receive(
            pb2.HostMsg(id="q", grant_state_query=pb2.GrantStateQuery(segment_id=18))
        )
        self.assertEqual(self.drain()[0].grant_state.state, pb2.GrantStateKind.GRANT_STATE_FAILED)

    def test_a_failure_is_reported_without_waiting_for_the_missing_half(self):
        routing = self.plane.receive(self.install(12, purpose="nope"))
        self.assertIs(routing.action, c.Action.HANDLED)
        acks = self.drain()
        self.assertEqual(
            acks[0].grant_ack.failed.failure_class,
            pb2.GrantFailureClass.GRANT_FAILURE_UNSUPPORTED,
        )

    def test_a_query_is_answered_by_a_push_echoing_its_id(self):
        """§4 rule 4: the one push the protocol correlates."""
        fd = make_object(4096, 13)
        self.plane.capability(13, 4096, PERM_RW, fd=fd)
        self.drain()
        self.plane.receive(
            pb2.HostMsg(id="r7", grant_state_query=pb2.GrantStateQuery(segment_id=13))
        )
        answer = self.drain()[0]
        self.assertEqual(answer.id, "r7")
        self.assertEqual(answer.grant_state.state, pb2.GrantStateKind.GRANT_STATE_PENDING)
        self.assertTrue(answer.grant_state.have_capability)
        self.assertFalse(answer.grant_state.have_install)

    def test_a_query_crossing_a_retirement_says_released(self):
        """§9a step 8: answering `not_present` would provoke a re-send that
        revives a grant the plugin has released."""
        fd = make_object(4096, 14)
        self.plane.capability(14, 4096, PERM_RW, fd=fd)
        self.plane.receive(self.install(14))
        self.drain()
        self.plane.receive(pb2.HostMsg(retire_segment=pb2.RetireSegment(segment_id=14)))
        self.assertEqual(self.drain()[0].segment_released.segment_id, 14)
        self.plane.receive(
            pb2.HostMsg(id="q", grant_state_query=pb2.GrantStateQuery(segment_id=14))
        )
        self.assertEqual(self.drain()[0].grant_state.state, pb2.GrantStateKind.GRANT_STATE_RELEASED)

    def test_a_half_arriving_after_release_is_answered_with_segment_released(self):
        fd = make_object(4096, 15)
        self.plane.capability(15, 4096, PERM_RW, fd=fd)
        self.plane.receive(self.install(15))
        self.drain()
        self.plane.receive(pb2.HostMsg(retire_segment=pb2.RetireSegment(segment_id=15)))
        self.drain()
        self.plane.receive(self.install(15))
        self.assertEqual(self.drain()[0].segment_released.segment_id, 15)

    def test_a_release_loan_for_a_plugin_arena_is_applied(self):
        fd = make_object(4096, 16)
        self.plane.capability(16, 4096, PERM_RW, fd=fd)
        self.plane.receive(
            pb2.HostMsg(
                install_segment=pb2.InstallSegment(
                    segment_id=16, purpose="V", regions=[loaned_region(host=False)]
                )
            )
        )
        self.drain()
        arena = self.store.segment(16).loaned(0)
        offset, length = arena.reserve(b"overlay items")
        self.plane.receive(
            pb2.HostMsg(
                release_loan=pb2.ReleaseLoan(segment_id=16, region=0, offset=offset, len=length)
            )
        )
        self.assertEqual(arena.outstanding, 0)

    def test_an_applied_release_loan_tells_the_hook_which_segment_has_room(self):
        """§5.7: a deferred chunk publish waits on exactly this."""
        fd = make_object(4096, 18)
        freed = []
        plane = c.ControlPlane(
            memory=self.store, reporter=self.reporter, on_release_loan=freed.append
        )
        plane.capability(18, 4096, PERM_RW, fd=fd)
        plane.receive(
            pb2.HostMsg(
                install_segment=pb2.InstallSegment(
                    segment_id=18, purpose="V", regions=[loaned_region(host=False)]
                )
            )
        )
        plane.outbox.drain()
        arena = self.store.segment(18).loaned(0)
        offset, length = arena.reserve(b"a chunk")
        plane.receive(
            pb2.HostMsg(
                release_loan=pb2.ReleaseLoan(segment_id=18, region=0, offset=offset, len=length)
            )
        )
        self.assertEqual(freed, [18])
        plane.receive(
            pb2.HostMsg(
                release_loan=pb2.ReleaseLoan(segment_id=18, region=0, offset=offset - 32, len=8)
            )
        )
        self.assertEqual(freed, [18], "a refused release frees nothing and says so to nobody")

    def test_a_mistargeted_release_is_reported_and_not_applied(self):
        fd = make_object(4096, 17)
        self.plane.capability(17, 4096, PERM_RW, fd=fd)
        self.plane.receive(
            pb2.HostMsg(
                install_segment=pb2.InstallSegment(
                    segment_id=17, purpose="V", regions=[loaned_region(host=False)]
                )
            )
        )
        self.drain()
        arena = self.store.segment(17).loaned(0)
        offset, length = arena.reserve(b"x" * 8)
        routing = self.plane.receive(
            pb2.HostMsg(
                release_loan=pb2.ReleaseLoan(
                    segment_id=17, region=0, offset=offset - 32, len=length
                )
            )
        )
        self.assertEqual([w for w, _ in routing.violations], ["release_region_mismatch"])
        self.assertEqual(arena.outstanding, 1)
        self.assertEqual(self.reporter.counts(), {"release_region_mismatch": 1})


class RideAlong(unittest.TestCase):
    """§5.5's exception and §8.12: apply the install, THEN read the descriptor."""

    def setUp(self):
        self.store = m.SegmentStore(PURPOSES)
        self.delivered: list = []
        self.plane = c.ControlPlane(
            memory=self.store, deliver_reply=lambda hm: bool(self.delivered.append(hm)) or True
        )

    def data_volume(self, segment_id=20, *, install=True, offset=32, length=8):
        msg = pb2.HostMsg(
            id="q1",
            data_volume=pb2.DataVolume(
                segment=pb2.SegmentRef(segment_id=segment_id, offset=offset, len=length)
            ),
        )
        if install:
            msg.data_volume.install.CopyFrom(
                pb2.InstallSegment(segment_id=segment_id, purpose="V", regions=[loaned_region()])
            )
        return msg

    def test_the_first_transfer_installs_and_is_usable_with_no_extra_round_trip(self):
        """§13: the first `DataVolume` into a new segment installs and is
        usable, with no wait for an acknowledgement of the message carrying it."""
        fd = make_object(4096, 20)
        self.plane.capability(20, 4096, PERM_RW, fd=fd)
        self.plane.outbox.drain()
        routing = self.plane.receive(self.data_volume(20))
        self.assertFalse(routing.deferred)
        self.assertEqual(len(self.delivered), 1)
        acks = self.plane.outbox.drain()
        self.assertEqual(acks[0].grant_ack.segment_id, 20, "and the ack still follows")

    def test_a_ride_along_before_the_capability_holds_the_descriptor_unread(self):
        """§8.12: applying an installation is not the same as being able to
        read.  The receiver records the table, holds the descriptor, and
        completes both when `M` lands -- it does not fail the message."""
        routing = self.plane.receive(self.data_volume(21))
        self.assertTrue(routing.deferred)
        self.assertEqual(self.delivered, [])
        self.assertEqual(list(self.plane.deferred), [21])
        fd = make_object(4096, 21)
        self.plane.capability(21, 4096, PERM_RW, fd=fd)
        self.assertEqual(len(self.delivered), 1, "delivered when the capability landed")
        self.assertEqual(self.plane.deferred, {})


class ExportChunkRideAlong(unittest.TestCase):
    """§8.12: `ExportChunk` is the REQUEST that carries an installation, so
    "apply, then interpret" has to hold on the request path too."""

    def setUp(self):
        self.store = m.SegmentStore(PURPOSES)
        self.dispatched: list = []
        self.plane = c.ControlPlane(memory=self.store, on_deferred_request=self.dispatched.append)

    def chunk(self, segment_id=50, *, install=True, inline=b"", request_id="r1"):
        chunk = pb2.ExportChunk(handle="x0", t=0)
        if inline:
            chunk.inline = inline
        else:
            chunk.segment.CopyFrom(pb2.SegmentRef(segment_id=segment_id, offset=32, len=8))
        if install:
            chunk.install.CopyFrom(
                pb2.InstallSegment(segment_id=segment_id, purpose="V", regions=[loaned_region()])
            )
        return pb2.HostMsg(id=request_id, export_chunk=chunk)

    def test_the_installation_is_applied_before_the_handler_runs(self):
        fd = make_object(4096, 50)
        self.plane.capability(50, 4096, PERM_RW, fd=fd)
        self.plane.outbox.drain()
        routing = self.plane.receive(self.chunk(50))
        self.assertIs(routing.action, c.Action.HOST_REQUEST, "and now it may be handled")
        self.assertIsNotNone(self.store.segment(50), "installed before the handler")
        self.assertEqual(self.plane.outbox.drain()[0].grant_ack.segment_id, 50)

    def test_a_request_waits_for_its_capability_rather_than_being_handled(self):
        routing = self.plane.receive(self.chunk(51))
        self.assertTrue(routing.deferred)
        self.assertIsNot(routing.action, c.Action.HOST_REQUEST, "not dispatched yet")
        self.assertEqual(self.dispatched, [])
        fd = make_object(4096, 51)
        self.plane.capability(51, 4096, PERM_RW, fd=fd)
        self.assertEqual(len(self.dispatched), 1, "dispatched when the mapping landed")

    def test_an_install_beside_inline_names_no_segment_to_install_into(self):
        """§8.12: a `ContractViolation` rather than a grant -- and the inline
        payload is still good, so the chunk is still handled."""
        routing = self.plane.receive(self.chunk(52, inline=b"pixels"))
        self.assertEqual([w for w, _ in routing.violations], ["install_without_segment"])
        self.assertIs(routing.action, c.Action.HOST_REQUEST)
        self.assertIs(self.store.state(52).state, m.SegmentState.NOT_PRESENT)

    def test_a_ride_along_for_another_segment_is_not_applied(self):
        msg = self.chunk(53)
        msg.export_chunk.install.segment_id = 54
        routing = self.plane.receive(msg)
        self.assertTrue(routing.diagnostics)
        self.assertIs(self.store.state(54).state, m.SegmentState.NOT_PRESENT)

    def test_finishing_the_request_disposes_of_its_descriptor(self):
        """§5.6: the handler is done with the bytes whatever it did with them."""
        fd = make_object(4096, 55)
        self.plane.capability(55, 4096, PERM_RW, fd=fd)
        self.plane.receive(self.chunk(55))
        self.plane.outbox.drain()
        msg = self.chunk(55, install=False, request_id="r2")
        self.plane.receive(msg)
        self.assertTrue(self.plane.finish_request(msg))
        release = self.plane.outbox.drain()[0].release_deposit
        self.assertEqual((release.segment_id, release.region, release.offset), (55, 0, 32))


class RequestIdentity(unittest.TestCase):
    """§4 rule 1: a request carries a non-empty id; a reply echoes it."""

    def test_a_request_with_no_id_is_not_dispatched(self):
        """Answering one is impossible: the reply would carry an empty id and
        read as an unsolicited push, so the host's call never completes."""
        plane = c.ControlPlane(reporter=ViolationReporter())
        routing = plane.receive(pb2.HostMsg(read_volume=pb2.ReadVolume()))
        self.assertIsNot(routing.action, c.Action.HOST_REQUEST)
        self.assertEqual([w for w, _ in routing.violations], ["zero_id"])

    def test_an_unsolicited_push_may_not_carry_one(self):
        """§5.2: a push that named its subject in `id` lands in the receiver's
        reply matcher, which is classify-by-kind failing where it costs most."""
        box = c.Outbox()
        with self.assertRaises(ValueError):
            box.send(pb2.PluginMsg(id="r7", epoch_ack=pb2.EpochAck()))
        box.send(pb2.PluginMsg(id="r7", grant_state=pb2.GrantState(segment_id=1)))
        self.assertEqual(box.drain()[0].id, "r7", "the one push the protocol correlates")


class TheUnlockIsTheHandoff(unittest.TestCase):
    """§8.12's receiver rule: a descriptor into a
    segment not yet installed is held unread, WHICHEVER half is missing, and
    read when the installation completes.  The hold does not key on the
    ride-along: a bare chunk after a standalone install is held too, since
    vizi's export path sends exactly that shape and must not pass only
    because a local-socket `M` beats a gRPC chunk."""

    def setUp(self):
        self.store = m.SegmentStore(PURPOSES)
        self.dispatched: list = []
        self.delivered: list = []
        self.plane = c.ControlPlane(
            memory=self.store,
            on_deferred_request=self.dispatched.append,
            deliver_reply=lambda hm: bool(self.delivered.append(hm)) or True,
        )

    def install(self, segment_id):
        # `V` rather than `X` only because this file's store recognizes `V`
        # alone (`PURPOSES`); the rule is the same for every purpose.
        return pb2.HostMsg(
            install_segment=pb2.InstallSegment(
                segment_id=segment_id, purpose="V", regions=[loaned_region()]
            )
        )

    def chunk(self, segment_id, request_id="r1", offset=32):
        """A BARE chunk: no ride-along, as the `1.0` host sends them."""
        return pb2.HostMsg(
            id=request_id,
            export_chunk=pb2.ExportChunk(
                handle="x0",
                t=0,
                segment=pb2.SegmentRef(segment_id=segment_id, offset=offset, len=8),
            ),
        )

    def capability(self, segment_id):
        self.plane.capability(segment_id, 4096, PERM_RW, fd=make_object(4096, segment_id))

    def test_install_then_chunk_with_the_capability_late(self):
        """The host's export shape: install standalone, chunk at once, no
        acknowledgement waited for; `M` lands after the chunk."""
        self.plane.receive(self.install(60))
        routing = self.plane.receive(self.chunk(60))
        self.assertTrue(routing.deferred, "held, not failed")
        self.assertIs(routing.action, c.Action.HANDLED)
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.plane.outbox.drain(), [], "no ack yet, no reply, no error")
        self.capability(60)
        self.assertEqual([hm.id for hm in self.dispatched], ["r1"], "read once installed")
        self.assertEqual(self.plane.outbox.drain()[0].grant_ack.segment_id, 60)
        self.assertEqual(self.plane.deferred, {})

    def test_capability_then_chunk_with_the_install_late(self):
        """The other half missing: the mapping is here and its meaning is not."""
        self.capability(61)
        routing = self.plane.receive(self.chunk(61))
        self.assertTrue(routing.deferred)
        self.assertEqual(self.dispatched, [])
        self.plane.receive(self.install(61))
        self.assertEqual([hm.id for hm in self.dispatched], ["r1"])

    def test_a_segment_never_heard_of_is_the_same_hold(self):
        """Neither half yet: indistinguishable from "install not yet arrived",
        so it is held, and the grant's deadline at the host is the backstop."""
        routing = self.plane.receive(self.chunk(62))
        self.assertTrue(routing.deferred)
        self.assertEqual(self.plane.dropped_descriptors, 0, "not dropped, not stale")
        self.assertEqual(list(self.plane.deferred), [62])
        self.plane.receive(self.install(62))
        self.assertEqual(self.dispatched, [], "one half is not enough")
        self.capability(62)
        self.assertEqual([hm.id for hm in self.dispatched], ["r1"])

    def test_held_descriptors_are_dispatched_in_arrival_order(self):
        self.plane.receive(self.install(63))
        for i in range(3):
            self.plane.receive(self.chunk(63, request_id=f"r{i}", offset=32 + 64 * i))
        self.plane.receive(
            pb2.HostMsg(
                id="q",
                data_volume=pb2.DataVolume(
                    segment=pb2.SegmentRef(segment_id=63, offset=32 + 64 * 3, len=8)
                ),
            )
        )
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.delivered, [])
        self.capability(63)
        self.assertEqual([hm.id for hm in self.dispatched], ["r0", "r1", "r2"])
        self.assertEqual([hm.id for hm in self.delivered], ["q"], "a held reply, delivered too")

    def test_the_state_read_and_the_enqueue_are_atomic_with_the_release(self):
        """The state must not be
        read OUTSIDE the lock the release pops under, so the doorbell thread
        could complete the installation and pop the still-empty list between
        this thread's read of *pending* and its append -- a request deferred
        forever on a segment installed and acknowledged.  Force exactly that
        interleaving: the hold thread reads *pending*, then waits until the
        memory layer says *installed* before it enqueues.  The release must
        find the request."""
        import threading
        import time
        from unittest import mock

        self.plane.receive(self.install(66))  # one half; the segment is pending
        real_state_of = self.plane._state_of
        read = threading.Event()

        def state_of_then_wait(segment_id):
            state = real_state_of(segment_id)
            if segment_id == 66 and not read.is_set():
                read.set()
                # The other thread completes the installation now.  The
                # memory layer's lock is its own, so this poll sees it move
                # even while this thread holds the control plane's lock.
                deadline = time.monotonic() + 5.0
                while self.store.state(66).state is not m.SegmentState.INSTALLED:
                    self.assertLess(time.monotonic(), deadline, "the capability never landed")
                    time.sleep(0.001)
            return state

        def complete_the_grant():
            read.wait(5.0)
            self.capability(66)  # memory: installed; then _release_deferred

        other = threading.Thread(target=complete_the_grant)
        other.start()
        with mock.patch.object(self.plane, "_state_of", side_effect=state_of_then_wait):
            routing = self.plane.receive(self.chunk(66))
        other.join(5.0)
        self.assertFalse(other.is_alive(), "the release deadlocked against the hold")
        self.assertTrue(routing.deferred, "read pending, so held")
        self.assertEqual([hm.id for hm in self.dispatched], ["r1"], "and the release found it")
        self.assertEqual(self.plane.deferred, {}, "nothing stranded")

    def test_a_held_request_whose_grant_fails_is_answered_not_forgotten(self):
        """§8.12: dropped with the grant, counted -- and §11a: every host
        request is answered, so the chunk gets an `Error` rather than a
        deadline that was suspended for a half the host itself owed."""
        self.plane.receive(self.chunk(64))
        self.plane.receive(self.chunk(64, request_id="r2", offset=96))
        self.assertEqual(list(self.plane.deferred), [64])
        # The install refuses on its purpose: the grant fails and the hold
        # goes with it.
        self.plane.receive(
            pb2.HostMsg(
                install_segment=pb2.InstallSegment(
                    segment_id=64, purpose="nope", regions=[loaned_region()]
                )
            )
        )
        self.assertEqual(self.plane.deferred, {})
        self.assertEqual(self.plane.dropped_descriptors, 2)
        self.assertEqual(self.dispatched, [])
        out = self.plane.outbox.drain()
        self.assertEqual(
            [(msg.WhichOneof("msg"), msg.id) for msg in out],
            [("grant_ack", ""), ("error", "r1"), ("error", "r2")],
        )
        self.assertEqual(out[1].error.kind, pb2.Error.NOT_FOUND)
        self.assertIn("segment 64", out[1].error.message)

    def test_a_descriptor_into_a_failed_segment_is_answered_at_once(self):
        """Once the grant has failed there is nothing to wait for."""
        self.plane.receive(
            pb2.HostMsg(
                install_segment=pb2.InstallSegment(
                    segment_id=65, purpose="nope", regions=[loaned_region()]
                )
            )
        )
        self.plane.outbox.drain()
        routing = self.plane.receive(self.chunk(65))
        self.assertFalse(routing.deferred)
        self.assertIs(routing.action, c.Action.HANDLED)
        self.assertEqual(self.plane.dropped_descriptors, 1)
        self.assertEqual(self.dispatched, [])
        out = self.plane.outbox.drain()
        self.assertEqual((out[0].WhichOneof("msg"), out[0].id), ("error", "r1"))


class DeferredDescriptors(unittest.TestCase):
    """A hold assumes the grant completes; when it does not, it is dropped."""

    def test_a_failed_grant_drops_what_was_held_for_it(self):
        store = m.SegmentStore(PURPOSES)
        delivered: list = []
        plane = c.ControlPlane(
            memory=store, deliver_reply=lambda hm: bool(delivered.append(hm)) or True
        )
        msg = pb2.HostMsg(
            id="q",
            data_volume=pb2.DataVolume(segment=pb2.SegmentRef(segment_id=30, offset=32, len=8)),
        )
        msg.data_volume.install.CopyFrom(
            pb2.InstallSegment(segment_id=30, purpose="nope", regions=[loaned_region()])
        )
        routing = plane.receive(msg)
        # The install failed on its purpose, so nothing is held for a capability
        # that has nothing to install into.
        self.assertEqual(plane.deferred, {})
        self.assertEqual(delivered, [])
        self.assertEqual(plane.dropped_descriptors, 1)
        self.assertTrue(routing.diagnostics)
        acks = plane.outbox.drain()
        self.assertEqual(
            acks[0].grant_ack.failed.failure_class,
            pb2.GrantFailureClass.GRANT_FAILURE_UNSUPPORTED,
        )


class AbandonedReplies(unittest.TestCase):
    """§4: a reply that names a resource has an owner from the moment it
    arrives, whether or not anyone was waiting for it."""

    def setUp(self):
        self.store = m.SegmentStore(PURPOSES)
        self.plane = c.ControlPlane(memory=self.store, deliver_reply=lambda hm: False)
        # Two host rings, so "which region" is a question with a wrong answer.
        fd = make_object(4096, 40)
        self.plane.capability(40, 4096, PERM_RW, fd=fd)
        self.plane.receive(
            pb2.HostMsg(
                install_segment=pb2.InstallSegment(
                    segment_id=40,
                    purpose="V",
                    regions=[loaned_region(0, 32, 512), loaned_region(1, 544, 512)],
                )
            )
        )
        self.plane.outbox.drain()

    def reply(self, offset, length, *, uses=0, segment=40, request_id="q9"):
        return pb2.HostMsg(
            id=request_id,
            data_volume=pb2.DataVolume(
                segment=pb2.SegmentRef(segment_id=segment, offset=offset, len=length, uses=uses)
            ),
        )

    def test_an_abandoned_deposit_is_released_and_the_payload_delivered_to_nobody(self):
        routing = self.plane.receive(self.reply(600, 300))
        self.assertTrue(routing.diagnostics, "an unmatched id is never silently discarded")
        release = self.plane.outbox.drain()[0].release_deposit
        self.assertEqual((release.segment_id, release.offset, release.len), (40, 600, 300))
        self.assertEqual(release.region, 1, "the region is resolved, never assumed to be 0")

    def test_a_release_names_the_region_the_offset_is_actually_in(self):
        """§8.12: `region` and `offset` are redundant so a mistargeting is
        detected -- and a sender that always said 0 would be the mistargeting."""
        self.plane.receive(self.reply(64, 8))
        self.assertEqual(self.plane.outbox.drain()[0].release_deposit.region, 0)

    def test_a_reservation_with_two_declared_uses_is_released_once(self):
        """§5.6: releasing at the first disposition hands the sender back bytes
        the second message still names."""
        self.plane.receive(self.reply(600, 300, uses=2))
        self.assertEqual(self.plane.outbox.drain(), [], "one use is still outstanding")
        self.plane.receive(self.reply(600, 300, uses=2, request_id="q10"))
        self.assertEqual(len(self.plane.outbox.drain()), 1, "released at zero")

    def test_a_range_in_no_region_is_counted_rather_than_mis_released(self):
        self.plane.receive(self.reply(3000, 8))
        self.assertEqual(self.plane.outbox.drain(), [])
        self.assertEqual(self.plane.unreleasable, 1)

    def test_a_reply_that_names_nothing_is_only_diagnosed(self):
        self.plane.receive(pb2.HostMsg(id="q9", view_state=pb2.ViewState()))
        self.assertEqual(self.plane.outbox.drain(), [])

    def test_a_reply_that_faults_the_session_still_releases_what_it_names(self):
        """§4 rule 8 faults the session on a reply of the wrong kind, and rule
        6 still holds: the deposit is released before the fault goes on."""

        def wrong_kind(hm):
            raise c.SessionFault(
                f"reply data_volume under id {hm.id!r} of pending request get_rois"
            )

        self.plane._deliver_reply = wrong_kind
        with self.assertRaises(c.SessionFault):
            self.plane.receive(self.reply(600, 300))
        release = self.plane.outbox.drain()[0].release_deposit
        self.assertEqual((release.segment_id, release.offset, release.len), (40, 600, 300))


class DoorbellFaults(unittest.TestCase):
    """§8.13: composed, not forwarded — and exactly one per terminal event."""

    def setUp(self):
        self.plane = c.ControlPlane()

    def fault(self, **over):
        from vizi_plugin_sdk.doorbell import Fault

        fields = dict(
            failure_class="bad_crc",
            direction="host_to_plugin",
            transport_class="seqpacket",
            detail="crc 0x1 != 0x2",
            expected_crc=1,
            observed_crc=2,
        )
        fields.update(over)
        return Fault(**fields)

    def test_the_transport_half_and_the_session_half_are_both_carried(self):
        self.plane.doorbell_fault(
            self.fault(), plugin_id="demo", sdk_version="1.0.0", peer_protocol_minor=12
        )
        msg = self.plane.outbox.drain()[0].doorbell_fault
        self.assertEqual(msg.failure_class, pb2.DoorbellFailureClass.DOORBELL_FAILURE_BAD_CRC)
        self.assertEqual((msg.expected_crc, msg.observed_crc), (1, 2))
        self.assertEqual((msg.plugin_id, msg.sdk_version), ("demo", "1.0.0"))
        self.assertEqual(msg.peer_protocol_minor, 12)

    def test_the_windows_stream_is_named_tcp_on_the_wire(self):
        """The IDL spells the classes `seqpacket | dgram | tcp`; the transport's
        own name for the Windows socket, "stream", is not one of them."""
        self.plane.doorbell_fault(self.fault(transport_class="stream"), plugin_id="demo")
        self.assertEqual(self.plane.outbox.drain()[0].doorbell_fault.transport_class, "tcp")

    def test_a_field_neither_half_can_establish_is_absent(self):
        """§8.13: absent, not zero-filled -- `plugin_principles.md` 11 is why,
        and "healthy at detection" is a snapshot with a known blind spot."""
        self.plane.doorbell_fault(self.fault(), plugin_id="demo")
        msg = self.plane.outbox.drain()[0].doorbell_fault
        for absent in (
            "os_error",
            "ordinal",
            "manifest_source",
            "process_healthy_at_detection",
            "stream_healthy_at_detection",
        ):
            with self.subTest(absent):
                self.assertFalse(msg.HasField(absent))

    def test_exactly_one_is_sent_per_terminal_event(self):
        """A transport may surface one event through two views -- a terminal
        mailbox and the ordered stream -- and reporting both gives Plugin
        Manager two diagnostics for one fault."""
        self.assertIsNotNone(self.plane.doorbell_fault(self.fault()))
        self.assertIsNone(self.plane.doorbell_fault(self.fault()))
        self.assertEqual(len(self.plane.outbox.drain()), 1)

    def test_an_unrecognised_class_cannot_be_sent_and_is_kept_locally(self):
        """The IDL: enum zero is never a value, it is *a protocol error where
        it appears* -- so reporting an unknown class as UNSPECIFIED would be
        committing a violation in order to report one.  Nothing goes out, the
        reason survives as a diagnostic, and the one-per-event latch is
        unspent, so a later mappable fault still reports."""
        self.assertIsNone(self.plane.doorbell_fault(self.fault(failure_class="from_future")))
        self.assertEqual(self.plane.outbox.drain(), [])
        self.assertTrue(any("from_future" in note for note in self.plane.diagnostics))
        self.assertIsNotNone(self.plane.doorbell_fault(self.fault()))

    def test_ready_is_the_one_push_whose_id_is_not_empty(self):
        """§4 rule 4 keeps an unsolicited push's id empty; §3.1 makes `Ready`
        the exception in the other direction -- its id is the identity string,
        non-empty by rule.  The outbox must not refuse it."""
        self.plane.outbox.send(pb2.PluginMsg(id="Ready; I am x 1.0", ready=pb2.Ready()))
        self.assertEqual(self.plane.outbox.drain()[0].id, "Ready; I am x 1.0")
        with self.assertRaises(ValueError):
            self.plane.outbox.send(pb2.PluginMsg(id="named", panel_patch=pb2.PanelPatch()))

    def test_a_second_doorbell_shutdown_is_counted_not_acted_on(self):
        """The window opened at the first; the host does not restart its own
        on a repeat either."""
        first = pb2.HostMsg(
            doorbell_shutdown=pb2.DoorbellShutdown(
                reason=pb2.DOORBELL_SHUTDOWN_FRAMING_FAULT, deadline_ms=2000
            )
        )
        second = pb2.HostMsg(
            doorbell_shutdown=pb2.DoorbellShutdown(
                reason=pb2.DOORBELL_SHUTDOWN_HOST_SHUTDOWN, deadline_ms=500
            )
        )
        self.assertIs(self.plane.receive(first).action, c.Action.DOORBELL_SHUTDOWN)
        self.assertIs(self.plane.receive(second).action, c.Action.HANDLED)
        self.assertEqual(self.plane.doorbell_shutdown.deadline_ms, 2000, "the first is kept")
        self.assertTrue(any("second DoorbellShutdown" in d for d in self.plane.diagnostics))

    def test_a_final_report_is_one_shot_on_this_channel(self):
        """§8.13: sent while this channel is still usable, which is the entire
        point of reporting a doorbell failure on the other one."""
        self.plane.doorbell_final_report("listener stopped after 3 records")
        msg = self.plane.outbox.drain()[0]
        self.assertEqual(msg.WhichOneof("msg"), "doorbell_final_report")
        self.assertIs(c.PLUGIN_CLASSES["doorbell_final_report"], c.Class.ONE_SHOT)


class Actions(unittest.TestCase):
    """What `receive` leaves for its caller."""

    def test_the_caller_keeps_the_handshake_the_hook_and_the_handlers(self):
        cases = {
            "hello": (pb2.HostMsg(hello=pb2.Hello()), c.Action.HELLO),
            "shutdown": (pb2.HostMsg(shutdown=pb2.Shutdown()), c.Action.SHUTDOWN),
            "panel_opened": (
                pb2.HostMsg(panel_opened=pb2.PanelOpened(panel_id="p")),
                c.Action.PANEL_OPENED,
            ),
            "read_volume": (
                pb2.HostMsg(id="r1", read_volume=pb2.ReadVolume()),
                c.Action.HOST_REQUEST,
            ),
            # §8.13: one channel's end is the caller's to act on -- it stops
            # the doorbell -- where `shutdown` is the session's.
            "doorbell_shutdown": (
                pb2.HostMsg(doorbell_shutdown=pb2.DoorbellShutdown(deadline_ms=2000)),
                c.Action.DOORBELL_SHUTDOWN,
            ),
        }
        for name, (msg, want) in cases.items():
            with self.subTest(name):
                # A plane each: once `shutdown` has been received, a request
                # is refused rather than dispatched (§3.3), which is its own
                # test rather than this one.
                self.assertIs(c.ControlPlane().receive(msg).action, want)


# --------------------------------------------------------------------------
# §5.4: the order-sensitive widget, checked by the state it produces
# --------------------------------------------------------------------------


class _Dropdown:
    """§5.4's account of the shipping widget builder's combo, and only that.

    ``set_options`` replaces the list and restores the prior selection BY TEXT,
    falling back to the first entry as a rebuilt combo does; ``set_value`` is
    bounds-checked -- or text-matched -- against whatever list is there now,
    and a miss is dropped.  The host's own widget code is not in this tree, so
    this is not it; it is the two behaviors the document says the merge rule
    exists for, which is enough to replay merged and unmerged sequences through
    and compare -- *"the test for a merge is the state it produces, never the
    order it emits"*.
    """

    def __init__(self, options=(), selected=None) -> None:
        self.options = list(options)
        self.selected = selected

    def run(self, ops):
        for op in ops:
            which = op.WhichOneof("op")
            if which == "set_options":
                self.options = list(op.set_options.options)
                if self.selected not in self.options:
                    self.selected = self.options[0] if self.options else None
            elif which == "set_value":
                value = op.set_value
                if value.WhichOneof("kind") == "s":
                    if value.s in self.options:
                        self.selected = value.s
                else:
                    index = int(value.f if value.WhichOneof("kind") == "f" else value.i)
                    if 0 <= index < len(self.options):
                        self.selected = self.options[index]
        return self.selected


def _options(*names):
    return pb2.PatchOp(widget_id="combo", set_options=pb2.Options(options=list(names)))


def _value(v):
    value = pb2.Value(s=v) if isinstance(v, str) else pb2.Value(i=v)
    return pb2.PatchOp(widget_id="combo", set_value=value)


def _replace_in_place(ops):
    """§5.4's first failed rule: last-write-wins per property, at the
    property's existing position."""
    merged: dict = {}
    for op in ops:
        merged[op.WhichOneof("op")] = op
    return list(merged.values())


def _canonical_order(ops):
    """§5.4's second failed rule: one per property, `set_options` first."""
    merged = {op.WhichOneof("op"): op for op in ops}
    return [merged[k] for k in ("set_options", "set_value") if k in merged]


#: (starting list, starting selection, the ops as sent) -- the three sequences
#: §5.4 records, each of which one of the failed rules gets wrong.
_SEQUENCES = (
    (["a"], "a", [_value("a"), _options("a", "b"), _value("b")]),
    (["a", "b"], "a", [_options("a", "b"), _value(1), _options("b", "a")]),
    (["a", "b"], "b", [_options("a"), _options("a", "b")]),
)


class OrderSensitiveWidgets(unittest.TestCase):
    """§5.4: *"a widget that has been sent a `set_options` is order-sensitive,
    and its pending ops are held whole: nothing is superseded, nothing is
    reordered."*"""

    def send(self, box, ops):
        for op in ops:
            box.send(pb2.PluginMsg(panel_patch=pb2.PanelPatch(panel_id="p", ops=[op])))

    def test_the_documents_three_sequences_end_where_the_unmerged_ones_do(self):
        for options, selected, ops in _SEQUENCES:
            with self.subTest([op.WhichOneof("op") for op in ops]):
                box = c.Outbox()
                self.send(box, ops)
                drained = box.drain()
                self.assertEqual(len(drained), 1, "one key, one message")
                merged = list(drained[0].panel_patch.ops)
                self.assertEqual(
                    _Dropdown(options, selected).run(merged),
                    _Dropdown(options, selected).run(ops),
                )
                self.assertEqual(merged, ops, "nothing superseded, nothing reordered")
                self.assertEqual(box.superseded, 0)

    def test_the_check_has_teeth(self):
        """Each rule §5.4 records as failed does fail it: the model reproduces
        the document's own counter-examples, so a regression to either would
        be caught by the test above rather than by a host."""
        options, selected, ops = _SEQUENCES[0]
        self.assertEqual(_Dropdown(options, selected).run(ops), "b")
        self.assertEqual(_Dropdown(options, selected).run(_replace_in_place(ops)), "a")
        options, selected, ops = _SEQUENCES[1]
        self.assertEqual(_Dropdown(options, selected).run(ops), "b")
        self.assertEqual(_Dropdown(options, selected).run(_canonical_order(ops)), "a")
        options, selected, ops = _SEQUENCES[2]
        self.assertEqual(_Dropdown(options, selected).run(ops), "a")
        self.assertEqual(_Dropdown(options, selected).run(_canonical_order(ops)), "b")

    def test_a_row_that_comes_back_and_a_selection_after_its_rows_keep_order(self):
        """`set_rows` [a, b] -> [a] -> [a, b] merged to
        one [a, b], and a list's newer `set_value` stayed ahead of its rows."""

        def rows(*keys):
            return pb2.PatchOp(widget_id="list", set_rows=pb2.RowKeys(keys=list(keys)))

        def select(key):
            return pb2.PatchOp(widget_id="list", set_value=pb2.Value(s=key))

        def shown(box):
            return [
                (
                    ("rows", tuple(op.set_rows.keys))
                    if op.WhichOneof("op") == "set_rows"
                    else ("select", op.set_value.s)
                )
                for msg in box.drain()
                for op in msg.panel_patch.ops
            ]

        box = c.Outbox()
        self.send(box, [rows("a", "b"), rows("a"), rows("a", "b")])
        self.assertEqual(shown(box), [("rows", ("a",)), ("rows", ("a", "b"))])
        box = c.Outbox()
        self.send(box, [select("x"), rows("y"), select("y")])
        self.assertEqual(shown(box), [("rows", ("y",)), ("select", "y")])

    def test_order_sensitivity_ends_at_the_drain_that_empties_it(self):
        """§5.4 at `1.0`: it lasts as long as a `set_options` is PENDING, and
        the difference is a drain.  Afterwards the receiver has applied the
        options, so two `set_value`s may merge -- the condition a merge has to
        satisfy is that no `set_options` sits BETWEEN them, and after a drain
        none does.  This SDK read it as sticky and was conservative; the host
        has always been per-batch."""
        box = c.Outbox()
        self.send(box, [_options("a", "b")])
        self.assertEqual(box.order_sensitive, frozenset({("panel", "p", "combo", "")}))
        box.drain()
        self.assertEqual(box.order_sensitive, frozenset(), "nothing is pending")
        self.send(box, [_value(1), _value(0)])
        ops = list(box.drain()[0].panel_patch.ops)
        self.assertEqual([op.set_value.i for op in ops], [0], "merged, and the last wins")
        self.assertEqual(box.superseded, 1)

    def test_a_set_options_in_the_same_batch_still_holds_everything_whole(self):
        """The relaxation is about batches, not about the rule: an options list
        the receiver has NOT applied yet is still a step the widget's state
        goes through."""
        box = c.Outbox()
        self.send(box, [_value(1), _options("a", "b"), _value(0)])
        ops = list(box.drain()[0].panel_patch.ops)
        self.assertEqual(
            [op.WhichOneof("op") for op in ops],
            ["set_value", "set_options", "set_value"],
        )
        self.assertEqual(box.superseded, 0)

    def test_what_was_pending_keeps_its_order_when_the_widget_turns_out_to_be_one(self):
        """Merges made before the first `set_options` were legitimate -- the
        widget was not known to be a dropdown -- and what survives them keeps
        the order it was sent in.  Everything after is held as sent."""
        box = c.Outbox()
        enabled = lambda on: pb2.PatchOp(widget_id="combo", set_enabled=on)  # noqa: E731
        self.send(box, [enabled(False), _value(1), enabled(True), _options("a", "b"), _value(0)])
        ops = list(box.drain()[0].panel_patch.ops)
        self.assertEqual(
            [op.WhichOneof("op") for op in ops],
            ["set_enabled", "set_value", "set_options", "set_value"],
        )
        self.assertTrue(ops[0].set_enabled, "the one pre-options merge, last write wins")
        self.assertEqual(box.superseded, 1)

    def test_every_other_widget_still_collapses_to_one_op_per_property(self):
        """A checkbox takes what it is given, so two values are one pending
        op -- which is what coalescing is for."""
        box = c.Outbox()
        tick = lambda on: pb2.PatchOp(widget_id="box", set_value=pb2.Value(b=on))  # noqa: E731
        self.send(box, [tick(True), tick(False), tick(True)])
        ops = list(box.drain()[0].panel_patch.ops)
        self.assertEqual(len(ops), 1)
        self.assertEqual(box.superseded, 2)
        self.assertEqual(box.order_sensitive, frozenset())

    def test_the_ceiling_refuses_the_push_rather_than_faulting_the_session(self):
        """Nothing can be superseded on such a widget, so the only bound is a
        ceiling — and reaching it is **the sender's** rule.

        §9's *session fault* and §5.4's *refused to its caller* are not two
        answers to one event; they bind opposite ends: `PanelPatch` is
        plugin→host, so this SDK is always the sender and the host always the
        receiver.  A conforming SDK makes §9's row unreachable.  This one
        applied the host's rule to itself and faulted its own session over its
        own caller's overflow."""
        original = c.HELD_OPS_CEILING
        self.addCleanup(setattr, c, "HELD_OPS_CEILING", original)
        c.HELD_OPS_CEILING = 3
        box = c.Outbox()
        self.send(box, [_options("a", "b"), _value(1), _value(0)])
        with self.assertRaises(c.Refused):
            self.send(box, [_value(1)])
        self.assertNotIsInstance(box, c.SessionFault, "and the session is not the thing that ended")
        # The batch that was already admitted is untouched and still goes.
        ops = list(box.drain()[0].panel_patch.ops)
        self.assertEqual(len(ops), 3)

    def test_nothing_is_handed_back_because_nothing_is_superseded(self):
        freed: list = []
        box = c.Outbox(on_superseded=freed.append)
        image = lambda n: pb2.PatchOp(  # noqa: E731
            widget_id="combo", set_image=pb2.ImageRef(segment_id=1, offset=0, len=n)
        )
        self.send(box, [_options("a"), image(8), image(16)])
        self.assertEqual(freed, [], "both images are still pending; neither is owed back")
        self.assertEqual(len(box.drain()[0].panel_patch.ops), 3)


# --------------------------------------------------------------------------
# §3.3: the window, at this end
# --------------------------------------------------------------------------


class Teardown(unittest.TestCase):
    """§3.3: *"new work is refused at both ends"*."""

    def test_a_request_inside_the_window_is_answered_with_error_and_not_run(self):
        plane = c.ControlPlane()
        opened = plane.receive(pb2.HostMsg(shutdown=pb2.Shutdown()))
        self.assertIs(opened.action, c.Action.SHUTDOWN)
        routing = plane.receive(pb2.HostMsg(id="r1", read_volume=pb2.ReadVolume()))
        self.assertIs(routing.action, c.Action.HANDLED, "not dispatched")
        self.assertTrue(routing.diagnostics)
        sent = plane.outbox.drain()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].id, "r1")
        self.assertEqual(sent[0].WhichOneof("msg"), "error")
        self.assertEqual(sent[0].error.kind, pb2.Error.PROTOCOL)
        self.assertEqual(plane.refused_in_teardown, 1)

    def test_a_second_shutdown_does_not_reopen_the_window(self):
        plane = c.ControlPlane()
        plane.receive(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=100)))
        routing = plane.receive(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=9000)))
        self.assertIs(routing.action, c.Action.HANDLED)
        self.assertTrue(routing.diagnostics)

    def test_a_refused_request_still_releases_what_it_named(self):
        """§5.6: an `Error` is a disposition like any other, so the bytes an
        `ExportChunk` carried in are handed back rather than pinned."""
        store = m.SegmentStore(PURPOSES)
        plane = c.ControlPlane(memory=store)
        plane.capability(40, 4096, PERM_RW, fd=make_object(4096, 40))
        plane.receive(
            pb2.HostMsg(
                install_segment=pb2.InstallSegment(
                    segment_id=40, purpose="V", regions=[loaned_region(0, 32, 512)]
                )
            )
        )
        plane.outbox.drain()
        plane.receive(pb2.HostMsg(shutdown=pb2.Shutdown()))
        plane.receive(
            pb2.HostMsg(
                id="x1",
                export_chunk=pb2.ExportChunk(
                    segment=pb2.SegmentRef(segment_id=40, offset=64, len=100)
                ),
            )
        )
        kinds = [msg.WhichOneof("msg") for msg in plane.outbox.drain()]
        self.assertEqual(kinds, ["error", "release_deposit"])

    def test_before_the_window_a_request_is_dispatched(self):
        plane = c.ControlPlane()
        routing = plane.receive(pb2.HostMsg(id="r1", read_volume=pb2.ReadVolume()))
        self.assertIs(routing.action, c.Action.HOST_REQUEST)
        self.assertEqual(plane.refused_in_teardown, 0)


if __name__ == "__main__":
    unittest.main()


class RetirementWakeup(unittest.TestCase):
    """A retiring segment whose last claim drops between the claim
    check and joining the retiring set was never settled."""

    def test_a_claim_that_drops_in_the_window_still_releases(self):
        from unittest import mock

        memory = mock.MagicMock()
        calls = []

        def begin_retire(segment_id):
            calls.append(segment_id)
            return len(calls) > 1  # held at the first look, free by the next

        memory.begin_retire.side_effect = begin_retire
        memory.release.return_value = True
        plane = c.ControlPlane(memory=memory)
        plane._retire(7)
        sent = [msg.WhichOneof("msg") for msg in plane.outbox.drain()]
        self.assertEqual(sent, ["segment_released"])
        self.assertEqual(plane._retiring, set())
