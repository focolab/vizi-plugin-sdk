"""Behavioral tests for the session dispatcher.

``control_plane_protocol.md`` §6 is the rule under test: **neither end may run
work on the dispatch path**, so routing, arrangements and acknowledgements
never wait on an application handler.
``control_plane_implementation_details.md`` §3.1a names the seven handlers
that run off the loop -- the three reads and the four export arms -- and
observes that §9's acknowledgement deadlines are derivable only because they
do.

The handlers themselves are stubbed here.  What is under test is the *routing*:
which lane a message runs on, what the loop is free to do meanwhile, and what
happens at teardown.
"""

from __future__ import annotations

import os
import queue
import struct
import tempfile
import threading
import time
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

import numpy as np
from vizi_plugin_sdk import control as c
from vizi_plugin_sdk import doorbell as db
from vizi_plugin_sdk import memory as m
from vizi_plugin_sdk import server as s
from vizi_plugin_sdk import violations as v
from vizi_plugin_sdk._proto import vizi_plugin_pb2 as pb2

_END = object()

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


def loaned(index: int, offset: int, extent: int, *, producer) -> m.Region:
    return m.Region(index, offset, extent, m.Discipline.LOANED, producer)


def pulsed(index: int, offset: int, *, buffers: int = 3, slot: int = 512) -> m.Region:
    """A plugin-published live lane.  No `id`: §8's table says a receiver
    IGNORES it, so nothing here may be found by one."""
    return m.Region(
        index,
        offset,
        buffers * (16 + slot),
        m.Discipline.PULSED,
        m.Producer.PLUGIN,
        buffers,
        slot,
    )


def _hello(minor: int = s.PROTOCOL_MINOR, major: int = s.PROTOCOL_MAJOR):
    """The host's first message, which the plugin reads before its `Ready`."""
    return pb2.HostMsg(
        id="Hello from vizi 1.0.0",
        hello=pb2.Hello(protocol_major=major, protocol_minor=minor),
    )


class _Feed:
    """A request iterator the test drives one message at a time."""

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()

    def __iter__(self) -> "_Feed":
        return self

    def __next__(self):
        item = self._q.get()
        if item is _END:
            raise StopIteration
        return item

    def send(self, msg) -> None:
        self._q.put(msg)

    def close(self) -> None:
        self._q.put(_END)


_HELLO = pb2.HostMsg(
    id="Hello from vizi test",
    hello=pb2.Hello(protocol_major=s.PROTOCOL_MAJOR, protocol_minor=s.PROTOCOL_MINOR),
)


class _Harness(unittest.TestCase):
    def setUp(self) -> None:
        # No purpose set: §3 makes a receiver's recognized set the one it
        # implements, and this SDK implements all five.
        self.servicer = s._Servicer(None, "test")
        self.addCleanup(self.servicer.close)
        self._drained: list = []
        self.feed = _Feed()
        self.addCleanup(self.feed.close)
        self.addCleanup(s._SHUTDOWN_HOOK.clear)

    def run_loop(self) -> threading.Thread:
        thread = threading.Thread(
            target=self.servicer._dispatch_loop, args=(self.feed,), daemon=True
        )
        thread.start()
        return thread

    def emitted(self, timeout: float = 2.0):
        """The next message the session would send, drained from the outbox."""
        deadline = time.monotonic() + timeout
        while True:
            if self._drained:
                return self._drained.pop(0)
            self._drained.extend(self.servicer.outbox.drain())
            if self._drained:
                continue
            if time.monotonic() > deadline:
                raise AssertionError("nothing was sent")
            time.sleep(0.005)

    def closed(self, timeout: float = 2.0) -> bool:
        """§3.3: the stream closing is what "done" means."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.servicer._closing:
                return True
            time.sleep(0.005)
        return False


class OffTheDispatchPath(_Harness):
    """§6: the loop routes; handlers run somewhere else."""

    def test_a_slow_read_does_not_stall_the_loop(self):
        """The case §3.1a names: an export chunk gets 120 s, and nothing
        behind it on the loop may wait that long -- a GrantAck, an EpochAck, a
        DoorbellShutdown."""
        started, release = threading.Event(), threading.Event()

        def slow(hm):
            started.set()
            release.wait(5)
            return pb2.PluginMsg(id=hm.id, error=pb2.Error(message="read"))

        routed = threading.Event()
        self.servicer._on_read_volume = slow
        self.servicer._host_client.route_reply = lambda hm: routed.set()
        self.run_loop()

        self.feed.send(pb2.HostMsg(id="1", read_volume=pb2.ReadVolume()))
        self.assertTrue(started.wait(2), "the handler ran")
        self.feed.send(pb2.HostMsg(view_state=pb2.ViewState()))
        self.assertTrue(routed.wait(2), "the loop kept routing while the handler ran")
        release.set()
        self.assertEqual(self.emitted().id, "1")

    def test_a_slow_export_does_not_delay_a_read(self):
        """One worker per class, not one for both: across classes they are
        independent, which is the property the split buys."""
        release = threading.Event()

        def slow_export(hm):
            release.wait(5)
            return pb2.PluginMsg(id=hm.id, error=pb2.Error(message="export"))

        self.servicer._on_export_chunk = slow_export
        self.servicer._on_read_plane = lambda hm: pb2.PluginMsg(id=hm.id)
        self.run_loop()

        self.feed.send(pb2.HostMsg(id="chunk", export_chunk=pb2.ExportChunk()))
        self.feed.send(pb2.HostMsg(id="plane", read_plane=pb2.ReadPlane()))
        self.assertEqual(self.emitted().id, "plane", "the read overtook the blocked export")
        release.set()
        self.assertEqual(self.emitted().id, "chunk")

    def test_within_a_class_the_stream_order_is_kept(self):
        """`@exporter` promises chunks arrive in order, and a pool of two
        workers would break that while satisfying §6."""
        seen: list[str] = []
        first = threading.Event()

        def record(hm):
            if hm.id == "a":
                first.wait(5)
            seen.append(hm.id)
            return pb2.PluginMsg(id=hm.id)

        self.servicer._on_export_chunk = record
        self.run_loop()
        for name in ("a", "b", "c"):
            self.feed.send(pb2.HostMsg(id=name, export_chunk=pb2.ExportChunk()))
        first.set()
        for name in ("a", "b", "c"):
            self.assertEqual(self.emitted().id, name)
        self.assertEqual(seen, ["a", "b", "c"])

    def test_a_handler_that_raises_still_answers(self):
        """A request with no reply is a peer waiting on a missing-ack deadline,
        which is a worse failure than the one that caused it."""

        def boom(hm):
            raise RuntimeError("segment full")

        self.servicer._on_open = boom
        self.run_loop()
        self.feed.send(pb2.HostMsg(id="7", open_reader=pb2.OpenReader(uri="x")))
        reply = self.emitted()
        self.assertEqual(reply.id, "7")
        self.assertIn("segment full", reply.error.message)

    def test_a_handler_answering_the_wrong_arm_is_caught(self):
        """§8 pairs each request with the reply that answers it; another arm is
        a message the host's call cannot decode."""
        self.servicer._on_read_volume = lambda hm: pb2.PluginMsg(
            id=hm.id, closed=pb2.Closed(handle="r0")
        )
        self.run_loop()
        self.feed.send(pb2.HostMsg(id="1", read_volume=pb2.ReadVolume()))
        reply = self.emitted()
        self.assertEqual(reply.WhichOneof("msg"), "error")
        self.assertIn("expected segment", reply.error.message)

    def test_commands_still_have_their_own_worker(self):
        """§3.1a's point is that `invoke_command` was the ONLY arm off the
        loop, not that it was wrong."""
        self.assertIsNot(self.servicer._invoke_pool, self.servicer._reader_pool)
        self.assertIsNot(self.servicer._reader_pool, self.servicer._export_pool)


class ReplyRouting(_Harness):
    """§3.1: dispatch by kind, and let nothing fall through silently."""

    def test_an_asset_reply_reaches_the_request_that_is_blocked_on_it(self):
        """Regression: `asset_result` was missing from the routed set, so every
        `request_asset` waited out its 30 s timeout and raised `HostError` with
        the answer already in hand and discarded."""
        self.run_loop()
        answer: list = []

        def ask():
            try:
                answer.append(self.servicer._host_client.request_asset("slot-a", timeout=5))
            except Exception as exc:  # noqa: BLE001 — the failure IS the result
                answer.append(exc)

        threading.Thread(target=ask, daemon=True).start()
        request = self.emitted()
        self.assertEqual(request.WhichOneof("msg"), "request_asset")
        self.feed.send(
            pb2.HostMsg(
                id=request.id,
                asset_result=pb2.AssetResult(slot="slot-a", path="/tmp/imported.tif"),
            )
        )
        for _ in range(200):
            if answer:
                break
            time.sleep(0.01)
        # `request_asset` answers a `Path`; compared as one, since Windows
        # spells this path with backslashes and the host's string is the
        # same file either way.
        self.assertEqual(answer, [Path("/tmp/imported.tif")])

    def test_a_pending_asset_request_names_itself_for_a_status_probe(self):
        """The status probe is for a long wait, so the wait has to
        be nameable by a thread other than the one waiting."""
        self.run_loop()
        host = self.servicer._host_client
        pending = host.request_asset_async("slot-a")
        request = self.emitted()
        self.assertEqual(pending.request_id, request.id)
        self.assertFalse(pending.done())
        with self.assertRaises(TimeoutError):
            pending.result(timeout=0.05)

        statuses: list = []
        threading.Thread(target=lambda: statuses.append(pending.status()), daemon=True).start()
        probe = self.emitted()
        self.assertEqual(probe.request_status_query.subject_id, pending.request_id)
        self.feed.send(
            pb2.HostMsg(
                id=probe.id,
                request_status=pb2.RequestStatus(
                    subject_id=pending.request_id,
                    disposition=pb2.RequestStatus.PENDING,
                    awaiting_user=True,
                ),
            )
        )
        for _ in range(200):
            if statuses:
                break
            time.sleep(0.01)
        self.assertTrue(statuses[0].pending and statuses[0].awaiting_user)

        self.feed.send(
            pb2.HostMsg(
                id=request.id,
                asset_result=pb2.AssetResult(slot="slot-a", path="/tmp/picked.tif"),
            )
        )
        self.assertEqual(pending.result(timeout=5), Path("/tmp/picked.tif"))
        self.assertTrue(pending.done())
        self.assertEqual(pending.result(), Path("/tmp/picked.tif"), "the result is kept")
        self.assertNotIn(pending.request_id, host._waiters)

    def test_done_neither_waits_behind_result_nor_leaks_the_waiter(self):
        """``done()`` took the lock a waiting
        ``result()`` held for its whole wait, and a reply ``done()`` took first
        left its waiter entry behind for good."""
        self.run_loop()
        host = self.servicer._host_client
        pending = host.request_asset_async("slot-a")
        request = self.emitted()
        waiting = threading.Thread(target=lambda: pending.result(timeout=2), daemon=True)
        waiting.start()
        time.sleep(0.05)
        started = time.monotonic()
        self.assertFalse(pending.done())
        self.assertLess(time.monotonic() - started, 0.5, "done() must not wait behind result()")
        self.feed.send(
            pb2.HostMsg(
                id=request.id,
                asset_result=pb2.AssetResult(slot="slot-a", path="/tmp/picked.tif"),
            )
        )
        for _ in range(200):
            if pending.done():
                break
            time.sleep(0.01)
        self.assertTrue(pending.done())
        self.assertNotIn(pending.request_id, host._waiters, "done() first still forgets")
        self.assertEqual(pending.result(), Path("/tmp/picked.tif"))
        waiting.join(2)

    def test_an_abandoned_request_drops_its_late_reply(self):
        self.run_loop()
        host = self.servicer._host_client
        pending = host.request_asset_async("slot-a")
        request = self.emitted()
        pending.abandon()
        late = pb2.HostMsg(id=request.id, asset_result=pb2.AssetResult(slot="slot-a", path="x"))
        self.assertFalse(host.route_reply(late))

    def test_a_host_refusal_is_raised_from_the_result(self):
        self.run_loop()
        pending = self.servicer._host_client.request_asset_async("slot-a")
        request = self.emitted()
        self.feed.send(
            pb2.HostMsg(
                id=request.id,
                host_error=pb2.Error(kind=pb2.Error.UNSUPPORTED, message="no"),
            )
        )
        with self.assertRaisesRegex(s.HostError, "no"):
            pending.result(timeout=5)

    def test_a_kind_nothing_handles_is_counted_rather_than_dropped(self):
        """A silent fall-through is how a new arm arrives, does nothing, and is
        diagnosed as a host bug.  Every arm in today's IDL is classified, so
        the only unclassifiable message is one carrying no arm at all -- a
        newer peer's addition reaches this SDK exactly that way."""
        self.run_loop()
        self.feed.send(pb2.HostMsg(id="x"))
        self.feed.send(pb2.HostMsg(id="y"))
        for _ in range(200):
            if self.servicer.unhandled_kinds.get("") == 2:
                break
            time.sleep(0.01)
        self.assertEqual(self.servicer.unhandled_kinds, {"": 2})

    def test_metadata_and_close_are_answered_rather_than_dropped(self):
        """§8.2 and §11a: answer every host request.  Both arms were silently
        dropped, so the host waited out its timeout and marked the session
        suspect -- which §5.1 makes unrecoverable."""
        self.run_loop()
        self.feed.send(pb2.HostMsg(id="m", metadata=pb2.Metadata(handle="r0")))
        reply = self.emitted()
        # No reader is open in this fixture, so the ANSWER is the refusal the
        # contract names (`reader_writer_contract.md` §1.8) -- and an answer.
        self.assertEqual((reply.id, reply.WhichOneof("msg")), ("m", "error"))
        self.assertEqual(reply.error.kind, pb2.Error.NOT_FOUND)
        self.assertIn("r0", reply.error.message)
        self.feed.send(pb2.HostMsg(id="c", close_reader=pb2.CloseReader(handle="r0")))
        reply = self.emitted()
        self.assertEqual((reply.id, reply.WhichOneof("msg")), ("c", "closed"))

    def test_an_inbound_contract_violation_is_kept_and_does_not_end_the_session(self):
        """§8.12: symmetric since 1.0, and report-and-continue at both ends --
        it is not a DoorbellFault."""
        self.run_loop()
        self.feed.send(
            pb2.HostMsg(
                contract_violation=pb2.ContractViolation(
                    where=pb2.ViolationSite.VIOLATION_SITE_RELEASE_REGION_MISMATCH,
                    detail="offset outside region 2",
                    count=3,
                )
            )
        )
        self.servicer._on_read_plane = lambda hm: pb2.PluginMsg(id=hm.id)
        self.feed.send(pb2.HostMsg(id="after", read_plane=pb2.ReadPlane()))
        self.assertEqual(self.emitted().id, "after", "the session kept going")
        self.assertEqual(len(self.servicer.violations_received), 1)
        self.assertEqual(self.servicer.violations_received[0].count, 3)
        self.assertEqual(
            self.servicer.violations_received_counts,
            {pb2.VIOLATION_SITE_RELEASE_REGION_MISMATCH: 3},
        )
        # §12: aggregate rather than flood.  The retained exemplars are bounded;
        # the totals are not, and are what a developer reads.
        for _ in range(s._control.MAX_VIOLATIONS_RETAINED + 10):
            self.feed.send(
                pb2.HostMsg(
                    contract_violation=pb2.ContractViolation(
                        where=pb2.VIOLATION_SITE_ZERO_ID, detail="again", count=2
                    )
                )
            )
        self.feed.send(pb2.HostMsg(id="still", read_plane=pb2.ReadPlane()))
        self.assertEqual(self.emitted().id, "still")
        self.assertEqual(len(self.servicer.violations_received), s._control.MAX_VIOLATIONS_RETAINED)
        self.assertEqual(
            self.servicer.violations_received_counts[pb2.VIOLATION_SITE_ZERO_ID],
            2 * (s._control.MAX_VIOLATIONS_RETAINED + 10),
        )


class Shutdown(_Harness):
    """§3.3: why the session is ending, and what the window is for."""

    def test_the_reason_and_the_window_reach_the_hook(self):
        got: list[s.ShutdownNotice] = []
        s.on_shutdown(got.append)
        self.run_loop()
        self.feed.send(
            pb2.HostMsg(
                shutdown=pb2.Shutdown(
                    reason=pb2.ShutdownReason.SHUTDOWN_REASON_RESTART,
                    deadline_ms=2000,
                    detail="reloading",
                )
            )
        )
        self.assertTrue(self.closed(), "closing the stream is what done means")
        self.assertEqual(len(got), 1)
        notice = got[0]
        self.assertEqual(notice.reason, "restart")
        self.assertEqual(notice.deadline_ms, 2000)
        self.assertEqual(notice.detail, "reloading")
        self.assertEqual(notice.grace_seconds, 2.0)
        self.assertIs(notice.starting_again, True, "the one reason to persist")

    def test_the_open_reader_is_closed_and_open_exports_aborted_at_shutdown(self):
        """`reader_writer_contract.md` §1.8: a file handle that matters is
        released on `CloseReader` when it comes, "and on `OpenReader` of the
        next reader and at shutdown when it does not" -- the servicer did the
        first two and not the third.  Process exit
        releases a descriptor; a lock file or a sidecar written on close
        does not get its chance.  An export the host will never finish is
        told so the same way, once, after the hook and before the report;
        `close()` afterwards finds nothing left to release."""
        events: list[str] = []

        class _Locking:
            def open(self, uri):
                return s.ReaderInfo(shape_tczyx=(1, 1, 1, 1, 1), dtype="u8")

            def close(self):
                events.append("reader closed")
                raise OSError("sidecar write failed")

        @s.exporter("x")
        class _Partial:
            def begin(self, desc):
                pass

            def abort(self):
                events.append("export aborted")

        self.addCleanup(s._EXPORTERS.pop, "x", None)
        s.on_shutdown(lambda notice: events.append("hook"))
        servicer = s._Servicer({"r": _Locking}, "test")
        self.addCleanup(servicer.close)
        servicer._on_open(
            pb2.HostMsg(id="o", open_reader=pb2.OpenReader(uri="f.crate", reader_id="r"))
        )
        servicer._on_open_export(
            pb2.HostMsg(
                id="e",
                open_export=pb2.OpenExport(
                    exporter_id="x", shape_tczyx=[1] * 5, dtype=pb2.DTYPE_U8
                ),
            )
        )
        threading.Thread(target=servicer._dispatch_loop, args=(self.feed,), daemon=True).start()
        self.feed.send(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=2000)))
        deadline = time.monotonic() + 2.0
        while not servicer._closing and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(servicer._closing)
        self.assertEqual(events, ["hook", "reader closed", "export aborted"])
        self.assertIsNone(servicer._impl)
        self.assertEqual(servicer._exports, {})
        # A close that raises is in the sweep, not swallowed and not fatal.
        sent = [m.WhichOneof("msg") for m in servicer.outbox.drain()]
        self.assertEqual(sent[-1], "shutdown_report")
        self.assertTrue(servicer.session_ended.is_set(), "and serve() returns after it")
        self.assertTrue(
            any("sidecar write failed" in d for d in servicer._control.diagnostics), events
        )
        servicer.close()
        self.assertEqual(events, ["hook", "reader closed", "export aborted"], "released once")

    def test_serve_returns_when_the_session_ends_however_it_ended(self):
        """§3.3: a plugin process serves one session and exits with it.  The
        reference waited on `server.wait_for_termination()`, which nothing
        ever called, so a plugin whose host crashed or was killed outlived it
        indefinitely -- on unix the plugin is in a session of its own by
        design, and a dead host reaches nothing -- holding its mapped
        segments.  Every ending passes through `_close_stream`; `serve()`
        waits on that and stops the server, letting the stream flush the
        `ShutdownReport` first."""
        import threading

        stopped: list[float] = []

        class _Server:
            def stop(self, grace):
                stopped.append(grace)
                done = threading.Event()
                done.set()
                return done

        # The host is gone: the dispatch loop's iterator ends with no Shutdown.
        servicer = s._Servicer(None, "test")
        self.addCleanup(servicer.close)
        waiter = threading.Thread(
            target=s._serve_until_session_ends, args=(_Server(), servicer), daemon=True
        )
        waiter.start()
        waiter.join(0.2)
        self.assertTrue(waiter.is_alive(), "nothing has ended the session yet")
        servicer._dispatch_loop(iter(()))
        waiter.join(2.0)
        self.assertFalse(waiter.is_alive(), "serve() did not return after the stream ended")
        self.assertEqual(stopped, [s.SESSION_STREAM_FLUSH_S])
        # The doorbell reading the host's EOF is another ending, before any
        # stream: the same event, so serve() returns then too.
        # It waits for the host's `Shutdown` first (§8.13), and a backstop
        # ends it if none comes.
        other = s._Servicer(None, "test")
        self.addCleanup(other.close)
        other._doorbell_backstop_s = 0.05
        self.assertFalse(other.session_ended.is_set())
        other._on_doorbell_teardown(s._doorbell.Teardown("peer_gone", "recv: EOF"))
        self.assertFalse(other.session_ended.is_set(), "the host's Shutdown is owed first")
        self.assertTrue(other.session_ended.wait(2.0))

    def test_a_stream_that_ends_without_shutdown_still_releases_the_reader(self):
        """The host gone or a fault skips `_finish_shutdown`; `close()` is
        the backstop, and it comes before the memory layer the reader may
        still deposit into."""
        events: list[str] = []

        class _Locking:
            def open(self, uri):
                return s.ReaderInfo(shape_tczyx=(1, 1, 1, 1, 1), dtype="u8")

            def close(self):
                events.append("reader closed")

        servicer = s._Servicer({"r": _Locking}, "test")
        servicer._on_open(
            pb2.HostMsg(id="o", open_reader=pb2.OpenReader(uri="f.crate", reader_id="r"))
        )
        servicer.close()
        self.assertEqual(events, ["reader closed"])
        servicer.close()
        self.assertEqual(events, ["reader closed"], "idempotent")

    def test_an_empty_shutdown_stays_wire_valid(self):
        """Every field is optional; a host that sends none reads as
        UNSPECIFIED with no deadline rather than as an error."""
        got: list[s.ShutdownNotice] = []
        s.on_shutdown(got.append)
        self.run_loop()
        self.feed.send(pb2.HostMsg(shutdown=pb2.Shutdown()))
        self.assertTrue(self.closed())
        self.assertEqual(got[0].reason, "unspecified")
        self.assertIsNone(got[0].deadline_ms, "named none, which is not the same as zero")
        self.assertEqual(got[0].grace_seconds, s.DEFAULT_SHUTDOWN_GRACE_S)
        self.assertIsNone(got[0].starting_again)
        # §9: adopting the 5 s is half the rule -- *"a missing one is a host
        # defect the plugin REPORTS and does not stall on"* -- and substituting
        # silently agrees with a wrong peer and tells nobody.  Site 25, and it
        # is checkable at all only because `deadline_ms` became `optional`:
        # absent and 0 were identical bytes before that.
        self.assertTrue(
            any(
                m.WhichOneof("msg") == "contract_violation"
                and m.contract_violation.where == pb2.VIOLATION_SITE_MISSING_DEADLINE
                for m in self._drained + self.servicer.outbox.drain()
            )
        )

    def test_a_deadline_of_zero_is_no_grace_and_not_a_missing_one(self):
        """The field became `optional` so absent and 0 stop being the same
        bytes, and a receiver that reads a present 0 as "unset" hands the host
        five seconds it deliberately did not offer.  Only ABSENCE takes §9's
        default, and only absence is the violation."""
        absent, present = pb2.Shutdown(), pb2.Shutdown(deadline_ms=0)
        self.assertEqual(absent.SerializeToString(), b"")
        self.assertNotEqual(present.SerializeToString(), b"")
        got: list[s.ShutdownNotice] = []
        s.on_shutdown(got.append)
        self.run_loop()
        self.feed.send(pb2.HostMsg(shutdown=present))
        self.assertTrue(self.closed())
        self.assertEqual(got[0].deadline_ms, 0)
        self.assertEqual(got[0].grace_seconds, 0.0, "no grace, because none was offered")
        self.assertFalse(
            any(
                m.WhichOneof("msg") == "contract_violation"
                and m.contract_violation.where == pb2.VIOLATION_SITE_MISSING_DEADLINE
                for m in self._drained + self.servicer.outbox.drain()
            ),
            "a host that named zero named one",
        )

    def test_a_hook_that_raises_does_not_stall_teardown(self):
        def boom(notice):
            raise RuntimeError("hook")

        s.on_shutdown(boom)
        self.run_loop()
        self.feed.send(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=100)))
        self.assertTrue(self.closed())

    def test_work_already_running_keeps_its_reply(self):
        """§3.3's window is for flushing what exists: a reply emitted after the
        stream closes is lost, so the close waits for what is in flight."""
        release = threading.Event()

        def slow(hm):
            release.wait(5)
            return pb2.PluginMsg(id=hm.id, error=pb2.Error(message="late"))

        self.servicer._on_read_volume = slow
        self.run_loop()
        self.feed.send(pb2.HostMsg(id="late", read_volume=pb2.ReadVolume()))
        self.feed.send(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=4000)))
        release.set()
        self.assertEqual(self.emitted().id, "late")
        self.assertEqual(
            self.emitted().WhichOneof("msg"),
            "shutdown_report",
            "the report is the LAST message: it rides ahead of the close and nothing else",
        )
        self.assertTrue(self.closed())

    def test_the_notice_is_recorded_for_a_plugin_with_no_hook(self):
        self.run_loop()
        self.feed.send(
            pb2.HostMsg(shutdown=pb2.Shutdown(reason=pb2.ShutdownReason.SHUTDOWN_REASON_HOST_EXIT))
        )
        self.assertTrue(self.closed())
        self.assertEqual(self.servicer.shutdown_notice.reason, "host_exit")
        self.assertIs(self.servicer.shutdown_notice.starting_again, False)


class TheStream(_Harness):
    """The generator that feeds gRPC, and what §5.4 buys on the way out."""

    def test_ready_goes_first_and_the_stream_ends_on_shutdown(self):
        """§8.1: `Ready` is the plugin's first message; §3.3: closing the
        stream is what "done" means."""
        stream = self.servicer.Session(self.feed, None)
        self.feed.send(_hello())
        first = next(stream)
        self.assertEqual(first.WhichOneof("msg"), "ready")
        self.feed.send(_HELLO)
        self.feed.send(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=100)))
        # §3.3: close alone cannot say WHY, so the outcome rides ahead of it.
        self.assertEqual(next(stream).WhichOneof("msg"), "shutdown_report")
        with self.assertRaises(StopIteration):
            next(stream)

    def test_a_slow_consumer_coalesces_instead_of_growing(self):
        """§5.4's whole value on this side: it costs nothing when the stream
        keeps up, and bounds the backlog when it does not.  A drop-oldest FIFO
        would instead lose widget A's only update to widget B's burst."""
        stream = self.servicer.Session(self.feed, None)
        self.feed.send(_hello())
        self.assertEqual(next(stream).WhichOneof("msg"), "ready")
        panel = self.servicer._panel("p")
        panel.set_value("A", 1.0)
        for n in range(200):
            panel.set_value("B", float(n))
        sent = [next(stream) for _ in range(2)]
        kinds = [msg.panel_patch.ops[0].widget_id for msg in sent]
        self.assertEqual(sorted(kinds), ["A", "B"], "202 sends, two messages")
        by_widget = {msg.panel_patch.ops[0].widget_id: msg for msg in sent}
        self.assertEqual(by_widget["A"].panel_patch.ops[0].set_value.f, 1.0, "A survived")
        self.assertEqual(by_widget["B"].panel_patch.ops[0].set_value.f, 199.0, "B is current")

    def test_an_acknowledgement_is_never_coalesced_behind_a_push(self):
        """§7: ordinary work must not starve the messages that finish it."""
        stream = self.servicer.Session(self.feed, None)
        self.feed.send(_hello())
        next(stream)
        self.servicer._panel("p").set_value("A", 1.0)
        self.servicer._emit(pb2.PluginMsg(epoch_ack=pb2.EpochAck()))
        self.assertEqual(next(stream).WhichOneof("msg"), "epoch_ack")
        self.assertEqual(next(stream).WhichOneof("msg"), "panel_patch")


def _view_state(**over):
    fields = dict(
        t=0,
        z=0,
        c=0,
        level=0,
        shape_tczyx=(1, 1, 64, 64, 64),
        dtype=None,
        has_crop=True,
        crop_min_zyx=(25, 25, 25),
        crop_max_zyx=(1520, 1520, 1520),
        mip=False,
        levels=(),
        dataset_uri="",
        dataset_name="",
        open_generation=1,
    )
    fields.update(over)
    return s.HostViewState(**fields)


class ViewStateCrop(unittest.TestCase):
    """§8.11: the index form cannot cross a pyramid level; the edge form can."""

    def view_state(self, **over):
        return _view_state(**over)

    def test_the_crop_converts_to_edges_and_then_to_a_level(self):
        box = self.view_state().crop_box()
        self.assertEqual((box.z0, box.z1), (24.5, 1519.5))
        centres = box.at_level(1).centres()[0]
        self.assertEqual((centres.start, centres.stop - 1), (12, 759))

    def test_no_crop_is_no_box(self):
        self.assertIsNone(self.view_state(has_crop=False).crop_box())
        self.assertIsNone(self.view_state(crop_min_zyx=None).crop_box())


class PanelProgress(unittest.TestCase):
    """The patch a `progress` widget takes, and why it is not
    `set_value`."""

    def _ops(self, call):
        sent = []
        handle = s.PanelHandle("p", sent.append, None)
        call(handle)
        self.assertEqual(len(sent), 1)
        return list(sent[0].panel_patch.ops)

    def test_a_fraction_rides_set_progress(self):
        (op,) = self._ops(lambda h: h.set_progress("bar", 0.25))
        self.assertEqual(op.WhichOneof("op"), "set_progress")
        self.assertEqual(op.widget_id, "bar")
        self.assertAlmostEqual(op.set_progress.value, 0.25)
        self.assertFalse(op.set_progress.indeterminate)

    def test_indeterminate_is_its_own_field_not_a_sentinel_value(self):
        (op,) = self._ops(lambda h: h.set_progress("bar", indeterminate=True))
        self.assertTrue(op.set_progress.indeterminate)
        self.assertEqual(op.set_progress.value, 0.0, "not a magic number")


class ViewStateSpacing(unittest.TestCase):
    """§8.7 sends a plugin with no tool to `ViewState`, which now carries
    the pitch.  `any_axis_known` is the same test a pane's is -- the empty unit
    is the one thing that separates a measurement from the display fallback."""

    def test_a_pitch_from_the_file_is_known(self):
        vs = _view_state(spacing_zyx=(2.0, 0.5, 0.5), spacing_unit="um", spacing_provenance="FILE")
        self.assertTrue(vs.any_axis_known)
        self.assertEqual(vs.spacing_zyx, (2.0, 0.5, 0.5))

    def test_the_display_fallback_is_not_known_however_plausible_the_numbers(self):
        vs = _view_state(
            spacing_zyx=(1.0, 1.0, 1.0),
            spacing_unit="",
            spacing_provenance="DISPLAY_FALLBACK",
        )
        self.assertFalse(vs.any_axis_known)

    def test_a_snapshot_with_no_dataset_open_carries_no_pitch(self):
        self.assertIsNone(_view_state().spacing_zyx)
        self.assertFalse(_view_state().any_axis_known)

    def test_the_provenance_arrives_as_its_name_not_its_number(self):
        hm = pb2.HostMsg(
            id="q1",
            view_state=pb2.ViewState(
                spacing_zyx=[2.0, 0.5, 0.5],
                spacing_unit="um",
                spacing_provenance=pb2.SpacingProvenance.SPACING_PROVENANCE_OVERRIDE,
            ),
        )
        host = s.HostClient.__new__(s.HostClient)
        host._request = lambda *a, **k: hm
        host._seq = iter([1])
        self.assertEqual(host.view_state().spacing_provenance, "OVERRIDE")

    def test_an_inclusion_read_carries_its_clip_box(self):
        """With a ``box``, the host clips to it and builds only that
        part.  Absent, the request names no box, which is the whole dataset."""
        sent = []

        def answer(msg, timeout):
            sent.append(msg.read_roi_inclusion)
            return pb2.HostMsg(
                id=msg.id, roi_inclusion=pb2.RoiInclusion(region_id="r", t=2, present=False)
            )

        host = s.HostClient.__new__(s.HostClient)
        host._request = answer
        host._seq = iter(range(5))
        self.assertIsNone(host.read_roi_inclusion("r", 2, roi=((0, 1, 2), (3, 4, 5))))
        self.assertIsNone(host.read_roi_inclusion("r", 2))
        self.assertEqual(
            [(list(q.roi_min_zyx), list(q.roi_max_zyx)) for q in sent],
            [([0, 1, 2], [3, 4, 5]), ([], [])],
        )

    def test_a_provenance_this_sdk_does_not_know_is_its_number(self):
        """A newer host's enum value made ``Name()`` raise out
        of ``view_state()``.  It arrives as its number instead, which matches
        no name and so is never read as a measurement."""
        hm = pb2.HostMsg(
            id="q1",
            view_state=pb2.ViewState(spacing_zyx=[2.0, 0.5, 0.5], spacing_provenance=97),
        )
        host = s.HostClient.__new__(s.HostClient)
        host._request = lambda *a, **k: hm
        host._seq = iter([1])
        self.assertEqual(host.view_state().spacing_provenance, "97")
        self.assertEqual(s._enum_name(pb2.DoorbellShutdownReason, 97), "97")


class WireLimits(unittest.TestCase):
    """§9: 32 MiB decoded (sized against the ROI pose), 8 MiB inline
    — and grpc-python's default is 4 MiB."""

    def test_the_session_raises_the_transport_ceiling_to_the_protocol_bound(self):
        """Left at the default, a legal 8 MiB `ExportChunk.inline` is refused
        by the transport before the rule that permits it applies.  What `serve`
        hands `grpc.server` is captured -- not its source text read, which a
        refactor passes while the call itself changes."""
        from unittest import mock

        import grpc

        class _Built(Exception):
            pass

        seen = {}

        def server(executor, interceptors=(), options=()):
            seen.update(dict(options))
            executor.shutdown(wait=False)
            raise _Built

        class _Doorbell:
            transport_class = "seqpacket"

            def start(self):
                pass

            def stop(self):
                pass

        with (
            mock.patch.object(grpc, "server", server),
            mock.patch.object(s._doorbell, "open_doorbell", lambda *a, **k: _Doorbell()),
            mock.patch.object(s._Servicer, "attach_doorbell", lambda self, t: None),
            self.assertRaises(_Built),
        ):
            s.serve(plugin_id="org.test.limits")
        self.assertEqual(seen["grpc.max_receive_message_length"], 32 << 20)
        self.assertEqual(seen["grpc.max_send_message_length"], 32 << 20)
        self.assertEqual(s._control.MAX_MESSAGE_BYTES, 32 << 20)
        self.assertEqual(s._control.MAX_INLINE_BYTES, 8 << 20)


class Handshake(_Harness):
    """§3.1: both ends check, exactly, on major AND minor."""

    HOST_ID = "Hello from vizi 1.0.0"

    def hello(self, major=s.PROTOCOL_MAJOR, minor=s.PROTOCOL_MINOR, token="", identity=HOST_ID):
        return pb2.HostMsg(
            id=identity,
            hello=pb2.Hello(protocol_major=major, protocol_minor=minor, session_token=token),
        )

    def test_a_matching_hello_continues_the_session(self):
        self.servicer._on_read_plane = lambda hm: pb2.PluginMsg(id=hm.id)
        self.run_loop()
        self.feed.send(self.hello())
        self.feed.send(pb2.HostMsg(id="after", read_plane=pb2.ReadPlane()))
        self.assertEqual(self.emitted().id, "after")
        self.assertIsNone(self.servicer.handshake_error)
        # A plugin can read which minor the host speaks.
        self.assertEqual(self.servicer._control.host_protocol, (s.PROTOCOL_MAJOR, s.PROTOCOL_MINOR))

    def quiet(self):
        """§12 sends the diagnostic to stderr, which the host retains; a test
        run does not need it."""
        import contextlib
        import io

        ctx = contextlib.redirect_stderr(io.StringIO())
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def test_a_version_mismatch_fails_the_session_naming_both(self):
        """The defect §2 of the implementation notes lists: the SDK validated
        the token and read `protocol_major` nowhere, so a plugin that must
        reject a `0.x` host had no check to reject it with."""
        self.quiet()
        self.run_loop()
        self.feed.send(self.hello(major=s.PROTOCOL_MAJOR + 1))
        self.assertTrue(self.closed())
        self.assertIn(f"{s.PROTOCOL_MAJOR + 1}.{s.PROTOCOL_MINOR}", self.servicer.handshake_error)
        self.assertIn(f"{s.PROTOCOL_MAJOR}.{s.PROTOCOL_MINOR}", self.servicer.handshake_error)

    def test_a_newer_minor_is_not_a_mismatch(self):
        """§3.2 from 1.0 on: a minor bump is backward-compatible, so a `1.1`
        host serves a `1.0` plugin.  The policy this replaced matched on both
        numbers, which made every version a breaking version."""
        self.servicer._on_read_plane = lambda hm: pb2.PluginMsg(id=hm.id)
        self.run_loop()
        self.feed.send(self.hello(minor=s.PROTOCOL_MINOR + 7))
        self.feed.send(pb2.HostMsg(id="after", read_plane=pb2.ReadPlane()))
        self.assertEqual(self.emitted().id, "after")
        self.assertIsNone(self.servicer.handshake_error)

    def test_the_doorbell_sees_the_token_the_stream_proved(self):
        """§2.2: the token is checked before `Ready`, so the `Hello` this loop
        reads belongs to a stream that already proved it -- and the doorbell's
        own compare (`doorbell_protocol.md` §3.2 step 8) is handed the proven
        token rather than whatever a metadata-authenticated `Hello` repeated."""
        self.servicer._expected_token = "abc"
        doorbell = _Doorbell()
        self.servicer.attach_doorbell(doorbell)
        self.servicer._on_read_plane = lambda hm: pb2.PluginMsg(id=hm.id)
        self.run_loop()
        self.feed.send(self.hello(token=""))
        self.feed.send(pb2.HostMsg(id="after", read_plane=pb2.ReadPlane()))
        self.assertEqual(self.emitted().id, "after")
        self.assertIsNone(self.servicer.handshake_error)
        self.assertEqual(doorbell.handshakes, [(s.PROTOCOL_MAJOR, s.PROTOCOL_MINOR, "abc")])

    def test_ready_carries_this_builds_identity_string(self):
        """§3.1: `Ready.id` is `Ready; I am <version string>`, the
        one push whose id is non-empty by rule -- and the host fails the
        session on any other form, so it is what stands between the `1.0`
        host and `serve()`."""
        stream = self.servicer.Session(self.feed, None)
        self.feed.send(self.hello())
        first = next(stream)
        self.assertEqual(first.WhichOneof("msg"), "ready")
        self.assertTrue(first.id.startswith(s.READY_PREFIX), first.id)
        self.assertIsNone(s.check_identity(first.id, s.READY_PREFIX))
        self.assertIn("test", first.id, "the plugin id is the natural content")
        self.assertIn("vizi-plugin-sdk", first.id)
        self.assertEqual(first.id, self.servicer._host_client.plugin_identity)
        self.feed.send(self.hello())
        self.feed.send(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=100)))
        self.assertEqual(next(stream).WhichOneof("msg"), "shutdown_report")

    def test_the_hosts_identity_is_exposed_once_the_handshake_settles(self):
        self.servicer._on_read_plane = lambda hm: pb2.PluginMsg(id=hm.id)
        self.assertIsNone(self.servicer._host_client.host_identity)
        self.run_loop()
        self.feed.send(self.hello())
        self.feed.send(pb2.HostMsg(id="after", read_plane=pb2.ReadPlane()))
        self.assertEqual(self.emitted().id, "after")
        self.assertEqual(self.servicer._host_client.host_identity, self.HOST_ID)

    def test_a_malformed_host_identity_fails_the_session_naming_both(self):
        """§3.1: the receiver checks the FORM and nothing else -- prefix,
        non-empty tail, 2048 bytes, no control character -- and a first
        message that fails it fails the session, naming the receiver's own
        string and what arrived, truncated and escaped."""
        cases = {
            "empty": "",
            "wrong prefix": "hello from vizi 1.0.0",
            "old correlation id": "hello",
            "no version string": "Hello from ",
            "control character": "Hello from vizi\n1.0.0",
            "oversized": "Hello from " + "v" * 2048,
        }
        for label, identity in cases.items():
            with self.subTest(label):
                self.setUp()
                self.quiet()
                self.run_loop()
                self.feed.send(self.hello(identity=identity))
                self.assertTrue(self.closed(), "the session did not fail")
                problem = self.servicer.handshake_error
                self.assertIn("Hello.id", problem)
                self.assertIn(s.READY_PREFIX, problem, "names this end's own string")
                self.assertNotIn("\n", problem, "a control character cannot forge a log line")
                self.assertLess(len(problem), 600, "an oversized id is not echoed whole")

    def test_a_first_message_that_is_not_hello_fails_the_session(self):
        """§3.1: `Hello` is the host's first message, and §11's unknown-kind
        rule applies only after the handshake -- so a first message of any
        other kind, one this SDK predates included, fails the session and is
        not answered."""
        cases = {
            "a request": pb2.HostMsg(id="r1", read_plane=pb2.ReadPlane()),
            "a push": pb2.HostMsg(release_loan=pb2.ReleaseLoan(segment_id=1)),
            "an unknown kind": pb2.HostMsg(id="r1"),
        }
        for label, first in cases.items():
            with self.subTest(label):
                self.setUp()
                self.quiet()
                stream = self.servicer.Session(self.feed, None)
                # `Ready` waits for `Hello` (§3.1, its minor is negotiated
                # from it), so a first message that is not one ends the
                # session with nothing sent at all.
                sent: list = []
                reader = threading.Thread(target=lambda: sent.extend(stream), daemon=True)
                reader.start()
                self.feed.send(first)
                self.assertTrue(self.closed(), "the session did not fail")
                self.assertIn("not Hello", self.servicer.handshake_error)
                reader.join(5)
                self.assertEqual(sent, [], "a first message that fails is not answered")

    def test_after_the_handshake_an_unknown_kind_is_refused_not_fatal(self):
        stream = self.servicer.Session(self.feed, None)
        self.feed.send(self.hello())
        self.assertEqual(next(stream).WhichOneof("msg"), "ready")
        self.feed.send(pb2.HostMsg(id="r1"))
        reply = next(stream)
        self.assertEqual((reply.id, reply.error.kind), ("r1", pb2.Error.UNSUPPORTED))
        self.assertIsNone(self.servicer.handshake_error)
        self.assertFalse(self.servicer._closing)

    def test_an_id_reused_with_different_content_faults_the_session_naming_it(self):
        """§4 rule 7: a byte-identical re-send is dropped, and anything else
        under a used id ends the session with the id in its diagnostic."""
        self.quiet()
        ran: list[str] = []
        self.servicer._on_read_plane = lambda hm: ran.append(hm.id) or pb2.PluginMsg(id=hm.id)
        stream = self.servicer.Session(self.feed, None)
        self.feed.send(self.hello())
        next(stream)
        request = pb2.HostMsg(id="r1", read_plane=pb2.ReadPlane(z=1))
        self.feed.send(request)
        self.assertEqual(next(stream).id, "r1")
        self.feed.send(request)
        self.feed.send(pb2.HostMsg(id="r1", read_plane=pb2.ReadPlane(z=2)))
        self.assertTrue(self.closed())
        self.assertEqual(ran, ["r1"], "the re-send was dispatched again")
        self.assertIn("'r1'", self.servicer.session_fault)
        self.assertIsNone(self.servicer.handshake_error)

    def test_a_reply_of_another_kind_under_a_pending_id_faults_the_session(self):
        """§4 rule 8: a pending request's id belongs to its reply alone.  A
        known reply of a kind the request does not take ends the session,
        naming the id, the request and the reply; the waiter never receives
        it, and is woken with host_error{PROTOCOL} rather than left to its
        timeout."""
        self.quiet()
        stream = self.servicer.Session(self.feed, None)
        self.feed.send(self.hello())
        next(stream)
        pending = self.servicer._host_client.request_asset_async("slot-a")
        request = next(stream)
        self.assertEqual(request.WhichOneof("msg"), "request_asset")
        self.feed.send(pb2.HostMsg(id=request.id, view_state=pb2.ViewState()))
        self.assertTrue(self.closed())
        for name in (repr(request.id), "request_asset", "view_state", "asset_result"):
            self.assertIn(name, self.servicer.session_fault)
        with self.assertRaises(s.HostError) as raised:
            pending.result(timeout=2)
        self.assertEqual(raised.exception.kind, pb2.Error.PROTOCOL)

    def test_an_unknown_kind_under_a_pending_id_faults_the_session(self):
        """§4 rule 8: an unknown kind is never a reply, and
        under the id of a pending request it faults the session on both sides
        -- the waiter is woken with PROTOCOL, not left to its timeout."""
        self.quiet()
        stream = self.servicer.Session(self.feed, None)
        self.feed.send(self.hello())
        next(stream)
        pending = self.servicer._host_client.request_asset_async("slot-a")
        request = next(stream)
        self.feed.send(pb2.HostMsg(id=request.id))
        self.assertTrue(self.closed())
        self.assertIn(repr(request.id), self.servicer.session_fault)
        self.assertIn("request_asset", self.servicer.session_fault)
        with self.assertRaises(s.HostError) as raised:
            pending.result(timeout=2)
        self.assertEqual(raised.exception.kind, pb2.Error.PROTOCOL)

    def test_a_reply_of_the_kind_its_request_takes_or_an_error_is_delivered(self):
        """§4 rule 8 leaves the right kind, and `Error`, alone."""
        stream = self.servicer.Session(self.feed, None)
        self.feed.send(self.hello())
        next(stream)
        host = self.servicer._host_client
        refused = host.request_asset_async("slot-a")
        request = next(stream)
        self.feed.send(
            pb2.HostMsg(
                id=request.id, host_error=pb2.Error(kind=pb2.Error.NOT_FOUND, message="no slot")
            )
        )
        with self.assertRaises(s.HostError):
            refused.result(timeout=2)
        picked = host.request_asset_async("slot-a")
        request = next(stream)
        self.feed.send(
            pb2.HostMsg(
                id=request.id, asset_result=pb2.AssetResult(slot="slot-a", path="/tmp/a.tif")
            )
        )
        self.assertEqual(picked.result(timeout=2), Path("/tmp/a.tif"))
        self.assertIsNone(self.servicer.session_fault)

    def test_the_version_string_is_never_matched_or_parsed(self):
        """Free-form after the prefix: nothing here compares it to anything."""
        self.servicer._on_read_plane = lambda hm: pb2.PluginMsg(id=hm.id)
        self.run_loop()
        self.feed.send(self.hello(identity="Hello from ünïcödé host 🚀 build deadbeef; whatever"))
        self.feed.send(pb2.HostMsg(id="after", read_plane=pb2.ReadPlane()))
        self.assertEqual(self.emitted().id, "after")
        self.assertIsNone(self.servicer.handshake_error)


class IdentityString(unittest.TestCase):
    """§3.1: the plugin's `Ready; I am <version string>`, composed once at
    start-up from what the log needs -- the manifest id and version -- and
    checked by the host's own rule so a plugin that could not compose it fails
    HERE with the reason, not at the handshake with the host's."""

    def in_plugin_dir(self, manifest: "dict | None"):
        """The host launches a plugin in its own directory (`docs/manifest.md`),
        which is where `vizi-plugin.json` is read from."""
        import json

        directory = tempfile.mkdtemp(prefix="identity-")
        if manifest is not None:
            with open(os.path.join(directory, s.MANIFEST_FILE), "w", encoding="utf-8") as f:
                json.dump(manifest, f)
        previous = os.getcwd()
        os.chdir(directory)
        self.addCleanup(os.chdir, previous)

    def test_the_manifest_supplies_the_version_by_default(self):
        self.in_plugin_dir({"id": "org.lab.probe", "version": "2.1.0"})
        identity = s.compose_identity("org.lab.probe")
        self.assertTrue(identity.startswith(s.READY_PREFIX + "org.lab.probe 2.1.0; "), identity)
        self.assertIn("vizi-plugin-sdk ", identity, "the SDK carries the conversation")
        self.assertNotIn("protocol", identity, "an implementation detail the Ready fields state")
        self.assertIsNone(s.check_identity(identity, s.READY_PREFIX))

    def test_serve_takes_the_plugin_id_from_the_manifest_by_default(self):
        """A bare `serve()` announced `I am plugin`, a valid
        string naming nothing.  The host launches in the plugin's directory,
        so the manifest is there to read; the explicit argument stays for a
        manifest that lives elsewhere."""
        self.in_plugin_dir({"id": "org.lab.probe", "version": "2.1.0"})
        self.assertEqual(s.default_plugin_id(), "org.lab.probe")
        self.in_plugin_dir(None)
        self.assertEqual(s.default_plugin_id(), "plugin")
        self.in_plugin_dir({"id": 7})
        self.assertEqual(s.default_plugin_id(), "plugin")

    def test_an_explicit_version_wins_and_a_foreign_manifest_is_ignored(self):
        self.in_plugin_dir({"id": "org.lab.other", "version": "9.9"})
        self.assertIn("org.lab.probe 3.0;", s.compose_identity("org.lab.probe", "3.0"))
        self.assertIn("org.lab.probe;", s.compose_identity("org.lab.probe"))

    def test_no_manifest_and_a_broken_one_still_compose(self):
        self.in_plugin_dir(None)
        self.assertIn("org.lab.probe;", s.compose_identity("org.lab.probe"))
        self.in_plugin_dir({"id": 7})
        self.assertIn("org.lab.probe;", s.compose_identity("org.lab.probe"))
        with open(s.MANIFEST_FILE, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIn("org.lab.probe;", s.compose_identity("org.lab.probe"))

    def test_the_sdk_version_is_the_loaded_sources_not_the_environments(self):
        """With an unrelated distribution's
        metadata answering for `vizi-plugin-sdk`, the identity claimed
        `99.88.77`.  The version named must be the one carried by the file
        that was loaded -- `__version__` -- because vizi's in-tree pull, a
        `PYTHONPATH` checkout and an older install beside a namesake all load
        different code and would all answer the same to `importlib.metadata`."""
        import importlib.metadata
        from unittest import mock

        import vizi_plugin_sdk

        self.in_plugin_dir(None)
        with mock.patch.object(importlib.metadata, "version", return_value="99.88.77"):
            identity = s.compose_identity("org.lab.probe")
        self.assertIn(f"vizi-plugin-sdk {vizi_plugin_sdk.__version__}", identity)
        self.assertNotIn("99.88.77", identity)
        # The copy that spoke is named when it was imported under another
        # name, as vizi's pull of this package is.
        with mock.patch.object(s, "__name__", "vizi.plugins.sdk.server"):
            identity = s.compose_identity("org.lab.probe")
        self.assertIn(
            f"vizi-plugin-sdk {vizi_plugin_sdk.__version__} as vizi.plugins.sdk", identity
        )
        self.assertIsNone(s.check_identity(identity, s.READY_PREFIX))

    def test_a_build_tag_is_the_authors_to_add(self):
        """Only the build step knows what it built; `build=` is its slot."""
        self.in_plugin_dir({"id": "org.lab.probe", "version": "1.0"})
        identity = s.compose_identity("org.lab.probe", build="ci 4471 deadbeef")
        self.assertIn("org.lab.probe 1.0; build ci 4471 deadbeef; vizi-plugin-sdk", identity)
        self.assertIsNone(s.check_identity(identity, s.READY_PREFIX))

    def test_a_string_the_host_would_refuse_fails_at_start_up(self):
        self.in_plugin_dir(None)
        with self.assertRaises(ValueError) as caught:
            s.compose_identity("org.lab.probe", "1.0\nrelease")
        self.assertIn("control character", str(caught.exception))
        with self.assertRaises(ValueError):
            s.compose_identity("org.lab.probe", "x" * 2048)
        with self.assertRaises(ValueError):
            s._Servicer(None, "org.lab.probe", version="1.0\x7f")


class ShutdownWorker(_Harness):
    """§11a: off the dispatch path, and on a worker of its own."""

    def test_the_hook_runs_on_its_own_worker(self):
        """Not the command pool: a command already running would otherwise
        hold the window the hook was given."""
        where: list[str] = []
        s.on_shutdown(lambda notice: where.append(threading.current_thread().name))
        self.run_loop()
        self.feed.send(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=1000)))
        self.assertTrue(self.closed())
        self.assertEqual(len(where), 1)
        self.assertTrue(where[0].startswith("vizi-sdk-shutdown"), where[0])

    def test_a_hook_that_overruns_is_cut_off_rather_than_waited_for(self):
        """§3.3: the window is an upper bound, not a budget -- the host tears
        down at its own deadline either way."""
        started, release = threading.Event(), threading.Event()

        def slow(notice):
            started.set()
            release.wait(5)

        s.on_shutdown(slow)
        self.addCleanup(release.set)
        self.run_loop()
        self.feed.send(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=100)))
        self.assertTrue(started.wait(2))
        self.assertTrue(self.closed())
        # The hook is STILL RUNNING at the close, which is what "cut off" means
        # -- a wall-clock threshold would only say the same thing less reliably.
        self.assertFalse(release.is_set())


class ShutdownReports(_Harness):
    """§3.3: the plugin says the outcome in a message before it closes, because
    hook completed, hook raised, no hook and hook still running are one event
    at the socket."""

    def report(self):
        msg = self.emitted()
        self.assertEqual(msg.WhichOneof("msg"), "shutdown_report")
        return msg.shutdown_report

    def test_no_hook(self):
        self.run_loop()
        self.feed.send(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=200)))
        report = self.report()
        self.assertEqual(report.outcome, pb2.ShutdownReport.NO_HOOK)
        self.assertTrue(self.closed())

    def test_completed(self):
        s.on_shutdown(lambda notice: None)
        self.run_loop()
        self.feed.send(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=200)))
        report = self.report()
        self.assertEqual(report.outcome, pb2.ShutdownReport.COMPLETED)
        self.assertEqual(report.detail, "")
        self.assertTrue(self.closed())

    def test_raised_carries_the_exception(self):
        def boom(notice):
            raise RuntimeError("disk full")

        s.on_shutdown(boom)
        self.run_loop()
        self.feed.send(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=200)))
        report = self.report()
        self.assertEqual(report.outcome, pb2.ShutdownReport.RAISED)
        self.assertEqual(report.detail, "RuntimeError: disk full")
        self.assertTrue(self.closed(), "teardown continues either way")

    def test_overran(self):
        release = threading.Event()
        s.on_shutdown(lambda notice: release.wait(5))
        self.addCleanup(release.set)
        self.run_loop()
        self.feed.send(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=100)))
        report = self.report()
        self.assertEqual(report.outcome, pb2.ShutdownReport.OVERRAN)
        self.assertTrue(self.closed())
        self.assertFalse(release.is_set(), "cut off, not waited for")

    def test_the_report_is_a_one_shot_push_with_no_id(self):
        self.run_loop()
        self.feed.send(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=200)))
        self.assertEqual(self.emitted().id, "")
        self.assertEqual(self.servicer.outbox.sent, {"one_shot": 1})


class TeardownRefusals(_Harness):
    """§3.3: *"new work is refused at both ends"* -- and the loop keeps
    reading so that it can be."""

    def test_a_request_arriving_inside_the_window_is_refused_not_run(self):
        started, release = threading.Event(), threading.Event()

        def hook(notice):
            started.set()
            release.wait(5)

        ran = threading.Event()

        def handler(hm):
            ran.set()
            return pb2.PluginMsg(id=hm.id, segment=pb2.Segment())

        s.on_shutdown(hook)
        self.addCleanup(release.set)
        self.servicer._on_read_volume = handler
        self.run_loop()
        self.feed.send(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=2000)))
        self.assertTrue(started.wait(2), "the window is open")
        self.feed.send(pb2.HostMsg(id="r2", read_volume=pb2.ReadVolume()))
        reply = self.emitted()
        self.assertEqual(reply.id, "r2")
        self.assertEqual(reply.WhichOneof("msg"), "error")
        self.assertFalse(ran.is_set(), "answered with an Error rather than run")
        self.assertEqual(self.servicer._control.refused_in_teardown, 1)
        release.set()
        self.assertEqual(self.emitted().WhichOneof("msg"), "shutdown_report")
        self.assertTrue(self.closed())

    def test_a_command_in_flight_runs_to_the_window_and_keeps_its_reply(self):
        """§3.3's "work already running" row, for the command lane: the
        shutdown barrier waits for it as for every other lane."""
        started, release = threading.Event(), threading.Event()

        def slow(ctx):
            started.set()
            release.wait(5)
            return "done"

        s._COMMANDS["slow"] = slow
        self.addCleanup(s._COMMANDS.pop, "slow", None)
        self.addCleanup(release.set)
        self.run_loop()
        self.feed.send(pb2.HostMsg(id="c1", invoke_command=pb2.InvokeCommand(command_id="slow")))
        self.assertTrue(started.wait(2))
        self.feed.send(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=4000)))
        release.set()
        reply = self.emitted()
        self.assertEqual(reply.id, "c1")
        self.assertEqual(reply.command_done.message, "done")
        self.assertEqual(self.emitted().WhichOneof("msg"), "shutdown_report")
        self.assertTrue(self.closed())


class PendingAtSessionEnd(_Harness):
    """§3.3: when `Shutdown` arrives, or the session ends any other way, every
    request the plugin still has pending fails at once with `SessionEnded`
    (a `HostError`), before the shutdown hook runs; a request made afterwards
    fails without being sent."""

    TIMEOUT_S = 30.0

    def _ask_in_background(self, lock: "threading.Lock | None" = None):
        """A handler waiting on `view_state`, holding ``lock`` while it waits."""
        outcome: list = []
        done = threading.Event()

        def ask():
            started = time.monotonic()
            if lock is not None:
                lock.acquire()
            try:
                outcome.append(self.servicer._host_client.view_state(timeout=self.TIMEOUT_S))
            except Exception as exc:  # noqa: BLE001 -- the failure IS the result
                outcome.append(exc)
            finally:
                outcome.append(time.monotonic() - started)
                if lock is not None:
                    lock.release()
                done.set()

        threading.Thread(target=ask, daemon=True).start()
        self.assertEqual(self.emitted().WhichOneof("msg"), "get_view_state")
        return outcome, done

    def assert_ended(self, outcome, done, why: str) -> None:
        self.assertTrue(done.wait(5), "the waiter was not released")
        exc, waited = outcome
        self.assertIsInstance(exc, s.SessionEnded)
        self.assertEqual(exc.kind, pb2.Error.KIND_UNSPECIFIED)
        self.assertEqual(str(exc), f"the session ended: {why}")
        self.assertLess(waited, 5.0, "well under the request's own timeout")

    def test_session_ended_is_a_host_error_of_kind_zero(self):
        exc = s.SessionEnded("the session ended: why")
        self.assertIsInstance(exc, s.HostError)
        self.assertEqual(exc.kind, pb2.Error.KIND_UNSPECIFIED)
        self.assertEqual(str(exc), "the session ended: why")
        import vizi_plugin_sdk

        self.assertIs(vizi_plugin_sdk.SessionEnded, s.SessionEnded)
        self.assertIn("SessionEnded", vizi_plugin_sdk.__all__)

    def test_a_request_pending_at_shutdown_fails_before_the_hook_and_the_hook_completes(self):
        """A handler holds a lock across an unanswered request, and the hook
        takes that lock: it completes, because the request failed first."""
        lock = threading.Lock()
        seen: dict = {}

        def hook(notice):
            seen["failed_first"] = probe.done()
            with lock:  # the waiting handler's lock: free once its request failed
                seen["took_lock"] = True

        s.on_shutdown(hook)
        self.run_loop()
        outcome, done = self._ask_in_background(lock)
        probe = self.servicer._host_client.request_asset_async("slot-a")
        self.assertEqual(self.emitted().WhichOneof("msg"), "request_asset")
        sent = time.monotonic()
        self.feed.send(
            pb2.HostMsg(
                shutdown=pb2.Shutdown(
                    reason=pb2.ShutdownReason.SHUTDOWN_REASON_HOST_EXIT, deadline_ms=3000
                )
            )
        )
        report = self.emitted(timeout=5)
        self.assertEqual(report.WhichOneof("msg"), "shutdown_report")
        self.assertEqual(
            report.shutdown_report.outcome,
            pb2.ShutdownReport.COMPLETED,
            report.shutdown_report.detail,
        )
        self.assertLess(time.monotonic() - sent, 1.0, "the hook waited on the pending request")
        self.assertTrue(seen.get("failed_first"), "the request failed before the hook ran")
        self.assertTrue(seen.get("took_lock"))
        self.assertTrue(self.closed())
        reason = self.servicer.shutdown_notice.reason
        why = f"shutdown began ({reason}): the host answers no request in its window"
        self.assert_ended(outcome, done, why)
        with self.assertRaises(s.SessionEnded) as caught:
            probe.result(timeout=0)
        self.assertEqual(str(caught.exception), f"the session ended: {why}")

    def test_a_request_in_flight_when_the_host_stream_ends_fails_at_once(self):
        self.run_loop()
        outcome, done = self._ask_in_background()
        self.feed.close()
        self.assert_ended(outcome, done, "the host's stream ended")

    def test_a_request_in_flight_at_a_session_fault_fails_at_once(self):
        # §4 rule 7: an id reused with different content faults the session.
        self.servicer._on_read_plane = lambda hm: pb2.PluginMsg(id=hm.id)
        self.run_loop()
        outcome, done = self._ask_in_background()
        with mock.patch("sys.stderr"):
            self.feed.send(pb2.HostMsg(id="r1", read_plane=pb2.ReadPlane(z=1)))
            self.assertEqual(self.emitted().id, "r1")
            self.feed.send(pb2.HostMsg(id="r1", read_plane=pb2.ReadPlane(z=2)))
            self.assertTrue(self.closed())
            self.assert_ended(outcome, done, f"a session fault ({self.servicer.session_fault})")

    def test_a_request_made_after_the_end_fails_at_once_and_is_never_sent(self):
        self.run_loop()
        self.feed.close()
        self.assertTrue(self.closed())
        host = self.servicer._host_client
        started = time.monotonic()
        with self.assertRaises(s.SessionEnded) as caught:
            host.view_state(timeout=self.TIMEOUT_S)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(caught.exception.kind, pb2.Error.KIND_UNSPECIFIED)
        self.assertEqual(str(caught.exception), "the session has ended: the host's stream ended")
        pending = host.request_asset_async("slot-a")
        self.assertTrue(pending.done(), "the async form has already failed")
        with self.assertRaises(s.SessionEnded):
            pending.result(timeout=0)
        self.assertEqual(self.servicer.outbox.drain(), [], "nothing was sent")
        self.assertEqual(host._waiters, {})


class HandlersEndQuietlyWithTheSession(_Harness):
    """A handler that ends because a `SessionEnded` propagated out of it did
    not throw: the session's end is the host's doing (§3.3).  Nothing reaches
    stderr or the violation reporter; any other exception is reported as
    before."""

    def setUp(self):
        super().setUp()
        saved = dict(s._SUBSCRIBED)
        self.addCleanup(lambda: (s._SUBSCRIBED.clear(), s._SUBSCRIBED.update(saved)))

    def _push_handler_blocked_at_shutdown(self, then_raise=None) -> str:
        """A regions handler waits on `get_rois`, Shutdown arrives; returns
        what reached stderr."""
        import io

        ended = threading.Event()

        def handler(ctx, change):
            try:
                ctx.host.get_rois(timeout=30, region_ids=[c.region_id for c in change.changes])
            except s.SessionEnded:
                if then_raise is not None:
                    raise then_raise from None
                raise
            finally:
                ended.set()

        s.on_regions_changed(handler)
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            self.run_loop()
            self.feed.send(
                pb2.HostMsg(
                    regions_changed=pb2.RegionsChanged(
                        open_generation=1,
                        reset=True,
                        changes=[pb2.RegionChange(region_id="r1", revision=1)],
                    )
                )
            )
            self.assertEqual(self.emitted().WhichOneof("msg"), "get_rois")
            self.feed.send(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=2000)))
            self.assertTrue(ended.wait(2), "the handler was not released")
            self.assertEqual(self.emitted().WhichOneof("msg"), "shutdown_report")
            self.assertTrue(self.closed())
            time.sleep(0.05)  # the worker's report, if it made one, is written by now
        return err.getvalue()

    def test_a_push_handler_blocked_at_shutdown_ends_quietly(self):
        logged = self._push_handler_blocked_at_shutdown()
        self.assertEqual(logged, "", "nothing on stderr")
        self.assertEqual(self.servicer._control.diagnostics, [])
        self.assertFalse(
            [
                m
                for m in self.servicer.outbox.drain()
                if m.WhichOneof("msg") == "contract_violation"
            ],
            "nothing for the violation reporter",
        )

    def test_any_other_exception_from_a_push_handler_is_still_reported(self):
        with self.assertLogs(s.__name__, "ERROR") as logs:
            self._push_handler_blocked_at_shutdown(then_raise=RuntimeError("its own bug"))
        self.assertIn("subscribed-push handler failed", logs.output[0])
        self.assertIn("its own bug", logs.output[0])

    def test_a_follow_driver_ending_with_the_session_is_not_reported(self):
        import io

        def driver(ctx, run):
            ctx.host.view_state(timeout=30)

        saved = dict(s._FOLLOWS)
        self.addCleanup(lambda: (s._FOLLOWS.clear(), s._FOLLOWS.update(saved)))
        s._FOLLOWS["p"] = driver
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            self.run_loop()
            self.servicer._on_follow("follow_started", pb2.FollowStarted(follow_id=1, panel_id="p"))
            self.assertEqual(self.emitted().WhichOneof("msg"), "get_view_state")
            self.feed.close()
            self.assertTrue(self.closed())
            for _ in range(200):
                if not self.servicer._follows:
                    break
                time.sleep(0.01)
        self.assertEqual(self.servicer._follows, {}, "the driver ended")
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(self.servicer._control.diagnostics, [])

    def _chunk_source_blocked_at_shutdown(self, then_raise=None) -> "tuple[str, list]":
        """A `@chunk_source` waits on `view_state`, Shutdown arrives; returns
        what reached stderr and every `_diagnose` call on its overlay."""
        import io

        ended = threading.Event()

        def source(ctx, want):
            try:
                ctx.host.view_state(timeout=30)
            except s.SessionEnded:
                if then_raise is not None:
                    raise then_raise from None
                raise
            finally:
                ended.set()

        self.addCleanup(s._CHUNK_SOURCES.clear)
        s._CHUNK_SOURCES["labels"] = source
        overlay = self.servicer._overlay("labels")
        diagnosed: list = []
        reported = overlay._diagnose
        overlay._diagnose = lambda text: (diagnosed.append(text), reported(text))
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            self.run_loop()
            self.feed.send(
                pb2.HostMsg(
                    chunks_wanted=pb2.ChunksWanted(
                        overlay_id="labels", wants=[pb2.ChunkWant(level=0, t=1)]
                    )
                )
            )
            self.assertEqual(self.emitted().WhichOneof("msg"), "get_view_state")
            self.feed.send(pb2.HostMsg(shutdown=pb2.Shutdown(deadline_ms=2000)))
            self.assertTrue(ended.wait(2), "the source was not released")
            self.assertEqual(self.emitted().WhichOneof("msg"), "shutdown_report")
            self.assertTrue(self.closed())
            for _ in range(200):  # the chunk server has handled the exception
                if overlay._in_flight is None:
                    break
                time.sleep(0.01)
            time.sleep(0.05)
        return err.getvalue(), diagnosed

    def test_a_chunk_source_blocked_at_shutdown_ends_quietly(self):
        logged, diagnosed = self._chunk_source_blocked_at_shutdown()
        self.assertEqual(logged, "", "nothing on stderr")
        self.assertEqual(diagnosed, [], "no _diagnose call")
        self.assertEqual(self.servicer._control.diagnostics, [])

    def test_any_other_exception_from_a_chunk_source_is_still_diagnosed(self):
        logged, diagnosed = self._chunk_source_blocked_at_shutdown(
            then_raise=RuntimeError("its own bug")
        )
        self.assertEqual(len(diagnosed), 1)
        self.assertIn("chunk source for", diagnosed[0])
        self.assertIn("its own bug", diagnosed[0])
        self.assertIn("its own bug", logged)
        self.assertTrue(any("its own bug" in d for d in self.servicer._control.diagnostics))


class LongRunningWork(_Harness):
    """§8.16: a request reports its progress and can be canceled, and
    background tasks are reported alongside.  Canceling is cooperative and
    arrives on the loop, which is free while the work runs on its lane."""

    def next_of(self, kind: str):
        while True:
            msg = self.emitted()
            if msg.WhichOneof("msg") == kind:
                return msg

    def command(self, name, fn):
        s._COMMANDS[name] = fn
        self.addCleanup(s._COMMANDS.pop, name, None)

    def invoke(self, rid: str, name: str) -> None:
        self.feed.send(pb2.HostMsg(id=rid, invoke_command=pb2.InvokeCommand(command_id=name)))

    def cancel(self, **subject) -> None:
        self.feed.send(pb2.HostMsg(cancel_work=pb2.CancelWork(**subject)))

    def test_a_command_reports_its_progress_under_its_request_id(self):
        step, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.addCleanup(step.set)

        def slow(ctx):
            ctx.progress(0.25, "a quarter")
            step.wait(5)
            ctx.progress(None, "x" * 400)
            release.wait(5)
            return "done"

        self.command("slow", slow)
        self.run_loop()
        self.invoke("c1", "slow")
        first = self.next_of("work_progress").work_progress
        self.assertEqual(first.request_id, "c1")
        self.assertEqual((first.progress.value, first.text), (0.25, "a quarter"))
        step.set()
        second = self.next_of("work_progress").work_progress
        self.assertTrue(second.progress.indeterminate)
        self.assertEqual(len(second.text.encode()), s._work.MAX_WORK_TEXT_BYTES, "clipped")
        release.set()
        self.assertEqual(self.next_of("command_done").command_done.message, "done")

    def test_a_cancel_reaches_a_command_while_its_lane_is_busy(self):
        started = threading.Event()

        def stubborn(ctx):
            started.set()
            deadline = time.monotonic() + 5
            while not ctx.cancelled and time.monotonic() < deadline:
                time.sleep(0.005)
            raise s.Cancelled("stopped at step 3")

        self.command("stubborn", stubborn)
        self.run_loop()
        self.invoke("c1", "stubborn")
        self.assertTrue(started.wait(2))
        self.cancel(request_id="c1")
        reply = self.next_of("error")
        self.assertEqual(reply.id, "c1")
        self.assertEqual(reply.error.kind, pb2.Error.CANCELLED)
        self.assertEqual(reply.error.message, "stopped at step 3")

    def test_a_request_cancelled_before_it_starts_is_never_run(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        ran = threading.Event()

        def first(ctx):
            started.set()
            release.wait(5)
            return "first"

        def second(ctx):
            ran.set()
            return "second"

        self.command("first", first)
        self.command("second", second)
        self.run_loop()
        self.invoke("c1", "first")
        self.assertTrue(started.wait(2))
        self.invoke("c2", "second")
        self.cancel(request_id="c2")
        time.sleep(0.05)
        release.set()
        self.assertEqual(self.next_of("command_done").id, "c1")
        reply = self.next_of("error")
        self.assertEqual((reply.id, reply.error.kind), ("c2", pb2.Error.CANCELLED))
        self.assertFalse(ran.is_set())

    def test_a_reader_read_reports_and_notices_through_the_module_functions(self):
        started = threading.Event()

        def read(hm):
            s.report_progress(0.5, text="decompressing")
            started.set()
            deadline = time.monotonic() + 5
            while not s.cancelled() and time.monotonic() < deadline:
                time.sleep(0.005)
            raise s.Cancelled()

        self.servicer._on_read_volume = read
        self.run_loop()
        self.feed.send(pb2.HostMsg(id="r1", read_volume=pb2.ReadVolume()))
        progress = self.next_of("work_progress").work_progress
        self.assertEqual((progress.request_id, progress.text), ("r1", "decompressing"))
        self.assertTrue(started.wait(2))
        self.cancel(request_id="r1")
        reply = self.next_of("error")
        self.assertEqual((reply.id, reply.error.kind), ("r1", pb2.Error.CANCELLED))
        s.report_progress(0.9)  # outside a handler: nothing, and no error
        self.assertFalse(s.cancelled())

    def test_a_background_task_is_begun_reported_cancelled_and_ended(self):
        self.run_loop()
        task = self.servicer._plugin_context().start_task("Index the file")
        begun = self.next_of("work_progress").work_progress
        self.assertEqual((begun.task_id, begun.title), (task.id, "Index the file"))
        self.assertTrue(begun.cancellable and begun.progress.indeterminate)
        task.progress(0.5, "half")
        self.assertEqual(self.next_of("work_progress").work_progress.progress.value, 0.5)
        self.cancel(task_id=task.id)
        deadline = time.monotonic() + 2
        while not task.cancelled and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(task.cancelled)
        task.done("stopped early")
        ended = self.next_of("work_progress").work_progress
        self.assertTrue(ended.done)
        self.assertEqual(ended.text, "stopped early")
        self.assertEqual(self.servicer._work.open_tasks(), ())

    def test_a_task_used_as_a_context_manager_ends_with_what_escaped(self):
        ctx = self.servicer._plugin_context()
        with self.assertRaises(ValueError):
            with ctx.start_task("Fit") as task:
                raise ValueError("no convergence")
        msgs = [m.work_progress for m in self.servicer.outbox.drain()]
        self.assertEqual(msgs[-1].task_id, task.id)
        self.assertTrue(msgs[-1].done)
        self.assertEqual(msgs[-1].text, "failed: no convergence")

    def test_open_tasks_are_bounded(self):
        ctx = self.servicer._plugin_context()
        for i in range(s._work.MAX_OPEN_TASKS):
            ctx.start_task(f"t{i}")
            self.servicer.outbox.drain()
        with self.assertRaisesRegex(RuntimeError, "already open"):
            ctx.start_task("one too many")

    def test_cancelling_what_is_not_open_is_a_no_op(self):
        self.assertFalse(self.servicer._work.cancel(pb2.CancelWork(request_id="gone")))
        self.assertFalse(self.servicer._work.cancel(pb2.CancelWork(task_id="t99")))
        self.assertFalse(self.servicer._work.cancel(pb2.CancelWork()))

    def test_progress_coalesces_per_piece_of_work(self):
        key = s._control.coalescing_key
        self.assertEqual(
            key(pb2.PluginMsg(work_progress=pb2.WorkProgress(request_id="r3"))),
            ("work", False, "r3"),
        )
        self.assertEqual(
            key(pb2.PluginMsg(work_progress=pb2.WorkProgress(task_id="r3"))),
            ("work", True, "r3"),
        )


class CommandOutcomes(_Harness):
    """§8.3: an `InvokeCommand` for a command the manifest does not declare is
    `Error{NOT_FOUND}`; a declared one with no handler is
    `CommandDone{ok=false}` with its id and a message; a canceled one is
    `Error{CANCELLED}`.  Only the manifest tells the first two apart, so a
    plugin served without one keeps the `CommandDone` for both."""

    def serve_declaring(self, *command_ids: str) -> None:
        self.servicer = s._Servicer(None, "test", commands=frozenset(command_ids))
        self.addCleanup(self.servicer.close)

    def command(self, name, fn) -> None:
        s._COMMANDS[name] = fn
        self.addCleanup(s._COMMANDS.pop, name, None)

    def invoke(self, rid: str, name: str):
        self.feed.send(pb2.HostMsg(id=rid, invoke_command=pb2.InvokeCommand(command_id=name)))
        return self.emitted()

    def test_an_undeclared_command_is_not_found(self):
        ran = threading.Event()
        self.serve_declaring("declared")
        # A handler alone does not make a command this plugin's: the manifest
        # is what the host read, and it does not offer this one.
        self.command("stray", lambda ctx: ran.set())
        self.run_loop()
        reply = self.invoke("c1", "stray")
        self.assertEqual(reply.id, "c1")
        self.assertEqual(reply.WhichOneof("msg"), "error")
        self.assertEqual(reply.error.kind, pb2.Error.NOT_FOUND)
        self.assertEqual(reply.error.message, 'command "stray" is not one this plugin declares')
        self.assertFalse(ran.is_set(), "refused rather than run")

    def test_a_declared_command_without_a_handler_is_a_failed_command_done(self):
        self.serve_declaring("declared")
        self.run_loop()
        reply = self.invoke("c1", "declared")
        self.assertEqual(reply.id, "c1")
        self.assertEqual(reply.WhichOneof("msg"), "command_done")
        self.assertFalse(reply.command_done.ok)
        self.assertEqual(reply.command_done.command_id, "declared")
        self.assertEqual(reply.command_done.message, "no command declared")

    def test_a_declared_command_with_a_handler_runs(self):
        self.serve_declaring("declared")
        self.command("declared", lambda ctx: "ran")
        self.run_loop()
        reply = self.invoke("c1", "declared")
        self.assertTrue(reply.command_done.ok)
        self.assertEqual(reply.command_done.command_id, "declared")
        self.assertEqual(reply.command_done.message, "ran")

    def test_a_cancelled_declared_command_is_cancelled(self):
        self.serve_declaring("declared")

        def stopped(ctx):
            raise s.Cancelled("stopped at step 3")

        self.command("declared", stopped)
        self.run_loop()
        reply = self.invoke("c1", "declared")
        self.assertEqual(reply.id, "c1")
        self.assertEqual(reply.WhichOneof("msg"), "error")
        self.assertEqual(reply.error.kind, pb2.Error.CANCELLED)
        self.assertEqual(reply.error.message, "stopped at step 3")

    def test_without_a_manifest_an_unhandled_command_is_a_failed_command_done(self):
        self.run_loop()
        reply = self.invoke("c1", "anything")
        self.assertEqual(reply.WhichOneof("msg"), "command_done")
        self.assertFalse(reply.command_done.ok)
        self.assertEqual(reply.command_done.command_id, "anything")
        self.assertEqual(reply.command_done.message, "no command anything")


class DeclaredCommands(unittest.TestCase):
    """Where `serve()` learns the declared command ids: the declaration in
    code, else `vizi-plugin.json` in the working directory when it is this
    plugin's; nothing otherwise."""

    def in_plugin_dir(self, manifest: "dict | str | None") -> None:
        import json

        directory = tempfile.mkdtemp(prefix="commands-")
        if manifest is not None:
            text = manifest if isinstance(manifest, str) else json.dumps(manifest)
            Path(directory, s.MANIFEST_FILE).write_text(text, encoding="utf-8")
        previous = os.getcwd()
        os.chdir(directory)
        self.addCleanup(os.chdir, previous)

    def test_the_manifest_in_the_working_directory_declares_them(self):
        self.in_plugin_dir(
            {
                "id": "org.lab.probe",
                "contributions": {"commands": [{"id": "run"}, {"id": "reset"}]},
            }
        )
        self.assertEqual(s._declared_commands("org.lab.probe"), frozenset({"run", "reset"}))

    def test_a_manifest_that_declares_no_commands_declares_none(self):
        self.in_plugin_dir({"id": "org.lab.probe"})
        self.assertEqual(s._declared_commands("org.lab.probe"), frozenset())

    def test_no_foreign_or_broken_manifest_is_unknown(self):
        self.in_plugin_dir(None)
        self.assertIsNone(s._declared_commands("org.lab.probe"))
        self.in_plugin_dir({"id": "org.lab.other", "contributions": {"commands": [{"id": "x"}]}})
        self.assertIsNone(s._declared_commands("org.lab.probe"))
        self.in_plugin_dir("{not json")
        self.assertIsNone(s._declared_commands("org.lab.probe"))

    def test_a_declaration_in_code_declares_them(self):
        from vizi_plugin_sdk import declare

        self.in_plugin_dir(None)
        with declare._fresh():
            declare.plugin(id="org.lab.probe", name="Probe", version="1")
            s.command("run")(lambda ctx: None)
            self.addCleanup(s._COMMANDS.pop, "run", None)
            self.assertEqual(s._declared_commands("org.lab.probe"), frozenset({"run"}))


class ShutdownVocabulary(unittest.TestCase):
    """The reasons are the IDL's, and a plugin branches on `restart`."""

    def test_every_idl_reason_has_a_spelling(self):
        idl = {
            v.number: v.name.removeprefix("SHUTDOWN_REASON_").lower()
            for v in pb2.ShutdownReason.DESCRIPTOR.values
        }
        self.assertEqual(s._SHUTDOWN_REASONS, idl)

    def test_only_restart_promises_the_work_comes_back(self):
        """§3.3: the distinction earns its place only because `restart` exists
        -- persisting a cache is worth doing exactly once."""
        again = {
            reason: s.ShutdownNotice(reason=reason).starting_again
            for reason in s._SHUTDOWN_REASONS.values()
        }
        self.assertEqual([r for r, v in again.items() if v is True], ["restart"])
        self.assertEqual(
            sorted(r for r, v in again.items() if v is False), ["host_exit", "user_disabled"]
        )


class SessionToken(unittest.TestCase):
    """§2.2: on the token transport a stream proves the token -- in call
    metadata, or in a first `Hello` -- before it is accepted, and a stream that
    cannot is refused ``UNAUTHENTICATED`` without claiming or ending the session.

    Run over a real gRPC server on loopback TCP, which is the Windows endpoint;
    only the environment and the port file are Windows-specific, and neither is
    under test here.  The server is built the way :func:`serve` builds it on
    that transport, with :func:`session_token_interceptor` registered.
    """

    TOKEN = "0123456789abcdef0123456789abcdef"
    HOST_ID = "Hello from vizi 0.9.5"

    def setUp(self) -> None:
        from concurrent import futures

        import grpc
        from vizi_plugin_sdk._proto import vizi_plugin_pb2_grpc as pb2_grpc

        self.grpc = grpc
        self.servicer = s._Servicer(None, "test")
        self.servicer._expected_token = self.TOKEN
        self.addCleanup(self.servicer.close)
        self.addCleanup(s._SHUTDOWN_HOOK.clear)
        self.server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=4),
            interceptors=[s.session_token_interceptor(self.TOKEN)],
        )
        pb2_grpc.add_PluginServicer_to_server(self.servicer, self.server)
        port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()
        self.addCleanup(self.server.stop, 0)
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        self.addCleanup(channel.close)
        self.stub = pb2_grpc.PluginStub(channel)

    def hello(self, token):
        return pb2.HostMsg(
            id=self.HOST_ID,
            hello=pb2.Hello(
                protocol_major=s.PROTOCOL_MAJOR,
                protocol_minor=s.PROTOCOL_MINOR,
                session_token=token,
            ),
        )

    def open(self, *first, metadata=None):
        """One `Session` call: the first message it answers, or the status
        code it was refused with."""
        feed = _Feed()
        self.addCleanup(feed.close)
        for msg in first:
            feed.send(msg)
        call = self.stub.Session(feed, metadata=metadata, timeout=10)
        self.addCleanup(call.cancel)
        try:
            return next(call)
        except self.grpc.RpcError as exc:
            return exc.code()

    def assert_unclaimed(self):
        self.assertFalse(self.servicer._claimed, "a refused stream claimed the session")
        self.assertFalse(self.servicer.session_ended.is_set(), "a refused stream ended the session")
        self.assertFalse(self.servicer._closing)

    def test_no_token_is_refused_and_the_session_stays_unclaimed(self):
        unauthenticated = self.grpc.StatusCode.UNAUTHENTICATED
        self.assertEqual(self.open(self.hello("")), unauthenticated)
        self.assertEqual(
            self.open(pb2.HostMsg(id="r", read_plane=pb2.ReadPlane())),
            unauthenticated,
            "a first message that is not a `Hello` proves nothing",
        )
        self.assert_unclaimed()
        ready = self.open(self.hello(self.TOKEN))
        self.assertEqual(ready.WhichOneof("msg"), "ready", "the plugin kept accepting streams")

    def test_a_wrong_metadata_token_is_refused_before_the_stream_is_accepted(self):
        refused = self.open(
            self.hello(self.TOKEN), metadata=((s.SESSION_TOKEN_METADATA_KEY, "f" * 32),)
        )
        self.assertEqual(refused, self.grpc.StatusCode.UNAUTHENTICATED)
        self.assert_unclaimed()
        ready = self.open(self.hello(""), metadata=((s.SESSION_TOKEN_METADATA_KEY, self.TOKEN),))
        self.assertEqual(ready.WhichOneof("msg"), "ready")

    def test_the_right_metadata_token_needs_none_in_hello(self):
        """The stream proved itself before the handler ran, so its `Hello`
        carries no token; `Ready` still follows `Hello`, whose minor it
        negotiates (§3.1)."""
        ready = self.open(self.hello(""), metadata=((s.SESSION_TOKEN_METADATA_KEY, self.TOKEN),))
        self.assertEqual(ready.WhichOneof("msg"), "ready")
        self.assertTrue(self.servicer._claimed)

    def test_a_1_0_hosts_hello_only_form_still_works(self):
        ready = self.open(self.hello(self.TOKEN))
        self.assertEqual(ready.WhichOneof("msg"), "ready")
        self.assertIsNone(self.servicer.handshake_error)

    def test_headers_a_wrapper_already_sent_are_not_sent_again(self):
        """A plugin that takes the call and sends its headers before handing
        over -- still loading, say -- marks ``_headers_sent``, and the
        Hello-only gate does not send them a second time, which gRPC refuses
        ("initial metadata no longer allowed") and which would fail the host's
        handshake on the Windows transport, where the token is checked."""
        from concurrent import futures

        from vizi_plugin_sdk._proto import vizi_plugin_pb2_grpc as pb2_grpc

        class _TakenEarly(s._Servicer):
            def Session(self, request_iterator, context):
                context.send_initial_metadata(())
                self._headers_sent = True
                yield from super().Session(request_iterator, context)

        servicer = _TakenEarly(None, "test")
        servicer._expected_token = self.TOKEN
        self.addCleanup(servicer.close)
        server = self.grpc.server(
            futures.ThreadPoolExecutor(max_workers=4),
            interceptors=[s.session_token_interceptor(self.TOKEN)],
        )
        pb2_grpc.add_PluginServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        self.addCleanup(server.stop, 0)
        channel = self.grpc.insecure_channel(f"127.0.0.1:{port}")
        self.addCleanup(channel.close)
        feed = _Feed()
        self.addCleanup(feed.close)
        feed.send(self.hello(self.TOKEN))
        call = pb2_grpc.PluginStub(channel).Session(feed, timeout=10)
        self.addCleanup(call.cancel)
        try:
            first = next(call)
        except self.grpc.RpcError as exc:
            self.fail(f"the stream failed: {exc.code()} {exc.details()}")
        self.assertEqual(first.WhichOneof("msg"), "ready")
        self.assertIsNone(servicer.handshake_error)

    def test_a_second_stream_does_not_take_the_session(self):
        metadata = ((s.SESSION_TOKEN_METADATA_KEY, self.TOKEN),)
        self.assertEqual(self.open(self.hello(""), metadata=metadata).WhichOneof("msg"), "ready")
        self.assertEqual(
            self.open(self.hello(""), metadata=metadata), self.grpc.StatusCode.ALREADY_EXISTS
        )
        self.assertFalse(self.servicer.session_ended.is_set())

    def test_a_silent_stream_is_dropped_without_touching_the_session(self):
        """A stranger that opens the call and sends nothing holds a server
        worker only for `UNAUTHENTICATED_HELLO_WAIT_S`."""
        with mock.patch.object(s, "UNAUTHENTICATED_HELLO_WAIT_S", 0.2):
            refused = self.open()
        self.assertIsInstance(refused, self.grpc.StatusCode)
        self.assert_unclaimed()
        ready = self.open(self.hello(self.TOKEN))
        self.assertEqual(ready.WhichOneof("msg"), "ready")


class SessionTokenGate(unittest.TestCase):
    """The two halves of §2.2's gate, without a server: the interceptor and
    the compare it and `Session` share."""

    TOKEN = "0123456789abcdef0123456789abcdef"

    def intercept(self, metadata):
        called = []
        details = types.SimpleNamespace(invocation_metadata=metadata, method="/vizi.Plugin/Session")
        handler = s.session_token_interceptor(self.TOKEN).intercept_service(
            lambda d: called.append(d) or "continued", details
        )
        return handler, called

    def test_a_wrong_metadata_token_never_reaches_the_handler(self):
        import grpc

        for label, metadata in {
            "wrong": ((s.SESSION_TOKEN_METADATA_KEY, "f" * 32),),
            "empty": ((s.SESSION_TOKEN_METADATA_KEY, ""),),
            "twice": (
                (s.SESSION_TOKEN_METADATA_KEY, self.TOKEN),
                (s.SESSION_TOKEN_METADATA_KEY, "f" * 32),
            ),
        }.items():
            with self.subTest(label):
                handler, called = self.intercept(metadata)
                self.assertEqual(called, [])
                context = mock.Mock()
                handler.stream_stream(iter(()), context)
                context.abort.assert_called_once()
                self.assertEqual(context.abort.call_args[0][0], grpc.StatusCode.UNAUTHENTICATED)

    def test_a_right_or_absent_key_passes_through(self):
        for metadata in (((s.SESSION_TOKEN_METADATA_KEY, self.TOKEN),), (("other", "x"),), None):
            handler, called = self.intercept(metadata)
            self.assertEqual(handler, "continued")
            self.assertEqual(len(called), 1)

    def test_the_compare_is_constant_time_and_takes_any_presented_string(self):
        """`hmac.compare_digest` refuses a non-ASCII ``str`` outright, and a
        stranger chooses what it sends."""
        with mock.patch.object(s.hmac, "compare_digest", wraps=s.hmac.compare_digest) as cmp:
            self.assertTrue(s._token_matches(self.TOKEN, self.TOKEN))
            self.assertFalse(s._token_matches("é" * 32, self.TOKEN))
            self.assertFalse(s._token_matches("", self.TOKEN))
        self.assertEqual(cmp.call_count, 3)


# --------------------------------------------------------------------------
# The doorbell, as the supervisor sees it (`doorbell_protocol.md` §11a)
# --------------------------------------------------------------------------


class _Doorbell:
    """Stands in for `DoorbellTransport` where only the supervisor is under test.

    It answers the five lifecycle inputs and records the `D`s sent, which is
    the whole of what this end calls into the transport for.  The transport's
    own rules have `tests/test_doorbell.py`; putting a real socket here would
    test the transport again and the wiring not at all.
    """

    def __init__(self) -> None:
        self.notifications: list[tuple] = []
        self.handshakes: list[tuple] = []
        self.tools: set[int] = set()
        self.high_water = 0
        self.epochs: dict[int, int] = {}
        self.stops = 0
        self.counters: dict[str, int] = {}

    def handshake_complete(self, major, minor, session_token=None):
        self.handshakes.append((major, minor, session_token))

    def stop(self):
        self.stops += 1

    def send_notification(self, segment_id, regions=(), epoch=0, view_id=0, *, mask=None):
        # Encode for REAL and record what the mask says, not what the caller
        # passed: a stand-in that returned SENT for any argument hid a mask of
        # zero for a whole round, and the test
        # asserting `(11, 0, 0, 0)` was asserting the defect.
        if isinstance(regions, int):
            raise TypeError("regions is an iterable of region indices")
        payload = db.encode_notification_payload(
            segment_id, regions if mask is None else mask, epoch, view_id
        )
        bits = int.from_bytes(payload[db._D_MASK_OFFSET : db._D_MASK_END], "little")
        if not bits:
            return db.SendResult.SUPPRESSED
        self.notifications.append((segment_id, bits, epoch, view_id))
        return db.SendResult.SENT

    def tool_activated(self, handle):
        if handle == 0:
            return False
        if handle > self.high_water:
            self.high_water = handle
            self.tools.add(handle)
            return True
        return handle in self.tools

    def tool_retired(self, handle):
        self.tools.discard(handle)
        return True

    def epoch_acknowledged(self, view_id, epoch):
        if epoch == 0:
            return False
        self.epochs[view_id] = epoch
        return True


_EVENT_FIELDS = dict(
    gen=1,
    t=0,
    interaction=1,
    seq=0,
    epoch=1,
    target_id=0,
    target_part=0,
    tool=1,
    channels=1,
    phase=db.PHASE_PRESS,
    view_id=0,
    level=0,
    mods=0,
    click_count=1,
    target_kind=0,
    payload_kind=0,
    position_flags=0,
    action=0,
    position=None,
)


def _press_record(*, record_id: int, interaction: int, tool: int, gen: int, epoch: int) -> bytes:
    """One well-formed `E` PRESS on the wire, for the tests that need a REAL
    transport to open a gesture.  `tests/test_doorbell.py` owns the encoder's
    rules; this is the least of it."""
    head = db._E_HEAD.pack(
        gen,
        0,
        interaction,
        0,
        epoch,
        0,
        0,
        tool,
        1,
        db.PHASE_PRESS,
        0,
        0,
        0,
        1,
        0,
        db.KIND_NONE,
        0,
    )
    payload = head + b"\0\0" + bytes(44) + bytes(12)
    body = struct.pack("<BBBBI", db.OP_INPUT, db.RECORD_VERSION, 0, 0, record_id) + payload
    return body + struct.pack("<I", zlib.crc32(body))


def frame(record, *, disposition=db.Disposition.DECODED, raw=b"\x00" * 112, **kw):
    return s._doorbell.Frame(raw, 1, disposition, None, record, **kw)


class _Fast(_Harness):
    """A servicer with a doorbell attached and one segment it can be granted."""

    def setUp(self) -> None:
        super().setUp()
        self.doorbell = _Doorbell()
        self.servicer.attach_doorbell(self.doorbell)

    def drained(self):
        return [(msg.WhichOneof("msg"), msg) for msg in self.servicer.outbox.drain()]

    def fill_the_never_drop_queue(self, segment_id: int) -> None:
        """Leave the one-shot queue at §9's capacity, so the next admission is
        a session fault.  Whatever the grant already queued counts toward it."""
        for i in range(s._control.ONE_SHOT_CAPACITY * 2):
            try:
                self.servicer.outbox.send(
                    pb2.PluginMsg(
                        release_deposit=pb2.ReleaseDeposit(
                            segment_id=segment_id, region=0, offset=i, len=1
                        )
                    )
                )
            except s._control.SessionFault:
                return
        raise AssertionError("the one-shot queue never filled")

    def kinds(self):
        return [kind for kind, _ in self.drained()]

    def grant(self, segment_id, regions, *, size=1 << 16, perms=PERM_RW, purpose="V", subject=""):
        """Both halves, doorbell first, and the ack that follows the second."""
        fd = make_object(size, segment_id)
        self.servicer.on_doorbell(
            frame(
                db.MemoryGrant(segment_id, size, perms, 0, None),
                descriptor=db.Descriptor(fd),
                raw=bytes([segment_id & 0xFF]) * 112,
            )
        )
        self.servicer._control.receive(
            pb2.HostMsg(
                install_segment=pb2.InstallSegment(
                    segment_id=segment_id,
                    purpose=purpose,
                    subject=subject,
                    regions=[_wire_region(r) for r in regions],
                )
            )
        )
        segment = self.servicer._memory.segment(segment_id)
        self.assertIsNotNone(segment, "the grant did not complete")
        return segment


def _wire_region(region: m.Region) -> pb2.Region:
    out = pb2.Region(
        index=region.index,
        offset=region.offset,
        extent=region.extent,
        discipline={
            m.Discipline.CONSTANT: pb2.RegionDiscipline.REGION_DISCIPLINE_CONSTANT,
            m.Discipline.OWNED: pb2.RegionDiscipline.REGION_DISCIPLINE_OWNED,
            m.Discipline.PULSED: pb2.RegionDiscipline.REGION_DISCIPLINE_PULSED,
            m.Discipline.LOANED: pb2.RegionDiscipline.REGION_DISCIPLINE_LOANED,
        }[region.discipline],
        producer={
            m.Producer.HOST: pb2.RegionProducer.REGION_PRODUCER_HOST,
            m.Producer.PLUGIN: pb2.RegionProducer.REGION_PRODUCER_PLUGIN,
        }[region.producer],
        buffers=region.buffers,
        slot_bytes=region.slot_bytes,
        id=region.id,
    )
    # §2: `epoch_offset` is OPTIONAL on the wire, and absence is the whole
    # meaning -- the region is not pane-associated.  Setting it unconditionally
    # would make every region declare a header at 0.
    if region.epoch_offset is not None:
        out.epoch_offset = region.epoch_offset
    return out


class TheGrantPath(_Fast):
    """§9a: two halves on two channels, and the supervisor owns the answer."""

    def test_a_capability_claims_its_descriptor_and_the_second_half_acks(self):
        segment = self.grant(1, [loaned(0, 32, 4096, producer=m.Producer.HOST)])
        acks = [msg for kind, msg in self.drained() if kind == "grant_ack"]
        self.assertEqual(len(acks), 1, "one ack, on the second half")
        self.assertEqual(acks[0].grant_ack.segment_id, 1)
        # §8.12: the proof is read back OUT OF the mapping, never echoed.
        self.assertEqual(acks[0].grant_ack.installed.identity_proof, segment.identity_proof)

    def test_a_record_level_rejection_becomes_a_grant_failure_not_a_violation(self):
        """`M` §7: everything but `zero_id` rides the acknowledgement, and
        recording it against the id is what stops `GrantStateQuery` sending the
        host back around the same rejected grant."""
        self.servicer.on_doorbell(
            frame(
                db.MemoryGrant(4, 16, 0x01, 0, None),
                disposition=db.Disposition.SEMANTIC_REJECTION,
                violation="size_below_preamble",
                grant_failure=True,
            )
        )
        kind, msg = self.drained()[0]
        self.assertEqual(kind, "grant_ack")
        self.assertEqual(msg.grant_ack.WhichOneof("outcome"), "failed")
        self.assertIs(self.servicer._memory.state(4).state, m.SegmentState.FAILED)

    def test_zero_id_is_a_contract_violation_carrying_the_record(self):
        """`M` §7 routes it to `ContractViolation` rather than to an ack --
        there is no grant for the failure to be about -- and §2 exempts it from
        aggregation, because a count cannot name WHICH grant to close out."""
        self.servicer.on_doorbell(
            frame(
                db.MemoryGrant(0, 4096, 0x01, 0, None),
                disposition=db.Disposition.SEMANTIC_REJECTION,
                violation="zero_id",
                raw=b"\x4d" * 112,
            )
        )
        kind, msg = self.drained()[0]
        self.assertEqual(kind, "contract_violation")
        self.assertEqual(msg.contract_violation.where, pb2.VIOLATION_SITE_ZERO_ID)
        self.assertEqual(msg.contract_violation.record, b"\x4d" * 112)

    def test_two_zero_ids_are_two_reports_rather_than_a_count(self):
        for _ in range(2):
            self.servicer.on_doorbell(
                frame(
                    db.MemoryGrant(0, 4096, 0x01, 0, None),
                    disposition=db.Disposition.SEMANTIC_REJECTION,
                    violation="zero_id",
                )
            )
        self.assertEqual(self.kinds(), ["contract_violation", "contract_violation"])


def ctx_arrangement(servicer, handle):
    ctx = s.ToolContext(_overlays=None, _host=None, _control=servicer._control)
    return ctx.arrangement(types.SimpleNamespace(tool=handle))


class InputRouting(_Fast):
    """`E` §1: a record names a tool HANDLE, and the manifest id is the
    `ToolBinding` arrangement's."""

    def setUp(self) -> None:
        super().setUp()
        self.seen: list[tuple] = []
        test = self

        @s.tool("paint")
        class _Paint:
            def press(self, ctx, ev):
                test.seen.append(("press", ev))

            def drag(self, ctx, ev):
                test.seen.append(("drag", ev))

            def cancel(self, ctx, ev):
                test.seen.append(("cancel", ev))

            def fire(self, ctx, ev):
                test.seen.append(("fire", ev))

        self.addCleanup(s._TOOLS.pop, "paint", None)

    def bind(self, handle=1, tool_id="paint", retired=False, revision=1, actions=()):
        self.servicer._control.receive(
            pb2.HostMsg(
                tool_binding=pb2.ToolBinding(
                    tool=handle,
                    tool_id=tool_id,
                    retired=retired,
                    revision=revision,
                    actions=[
                        pb2.ToolAction(action=i, action_id=a, key=k)
                        for i, (a, k) in enumerate(actions, start=1)
                    ],
                )
            )
        )
        self.servicer.outbox.drain()

    def event(self, **kw):
        fields = dict(
            gen=1,
            t=0,
            interaction=1,
            seq=0,
            epoch=1,
            target_id=0,
            target_part=0,
            tool=1,
            channels=1,
            phase=db.PHASE_PRESS,
            view_id=0,
            level=0,
            mods=0,
            click_count=1,
            target_kind=0,
            payload_kind=0,
            position_flags=0,
            action=0,
            position=None,
        )
        fields.update(kw)
        return db.InputEvent(**fields)

    def test_an_event_reaches_the_tool_its_binding_names(self):
        self.bind()
        self.servicer.on_doorbell(frame(self.event()))
        self.assertEqual([name for name, _ in self.seen], ["press"])

    def test_the_cancel_a_retirement_queued_reaches_its_tool_after_the_binding_has_gone(self):
        """`E` §2: deactivation ends an open gesture.  The transport queues the
        retirement's `Cancellation` for the listener, and the control plane
        drops the binding in the same breath -- so it was dropped unheard
        whenever the listener drained after the retirement."""
        seen: list = []

        @s.tool("probe")
        class _Probe:
            def cancel(self, ctx, ev):
                seen.append(ev.reason)

        self.addCleanup(s._TOOLS.pop, "probe", None)
        self.bind(handle=2, tool_id="probe")
        self.bind(handle=2, tool_id="probe", retired=True, revision=2)
        self.assertEqual(self.servicer._control.tools, {})
        self.servicer.on_doorbell(
            db.Cancellation(interaction=1, tool=2, gen=1, view_id=0, reason="tool_retired")
        )
        self.assertEqual(seen, ["tool_retired"])
        # A handle that never named a tool still reaches nothing.
        self.servicer.on_doorbell(
            db.Cancellation(interaction=2, tool=9, gen=1, view_id=0, reason="tool_retired")
        )
        self.assertEqual(seen, ["tool_retired"])

    def test_only_the_newest_retired_tools_are_remembered(self):
        control = self.servicer._control
        for handle in range(1, c.RETIRED_TOOLS_KEPT + 3):
            self.bind(handle=handle, tool_id="probe")
            self.bind(handle=handle, tool_id="probe", retired=True, revision=2)
        self.assertEqual(len(control.retired_tool_ids), c.RETIRED_TOOLS_KEPT)
        self.assertNotIn(1, control.retired_tool_ids)
        self.assertEqual(next(reversed(control.retired_tool_ids)), c.RETIRED_TOOLS_KEPT + 2)

    def test_a_handler_reads_the_pane_state_its_event_was_captured_under(self):
        """The geometry an epoch stands for reaches the
        handler through `ctx.pane(ev)` -- the acknowledged arrangement, no
        round trip -- and an epoch the plugin moved past answers None."""
        seen: list = []

        @s.tool("probe")
        class _Probe:
            def press(self, ctx, ev):
                seen.append(ctx.pane(ev))

            def cancel(self, ctx, ev):
                seen.append(ctx.pane(ev))

        self.addCleanup(s._TOOLS.pop, "probe", None)
        self.bind(handle=2, tool_id="probe")
        geometry = pb2.Geometry(
            gen=1, spacing_z=3.0, spacing_y=1.0, spacing_x=1.0, spacing_unit="um"
        )
        self.servicer._control.receive(
            pb2.HostMsg(
                epoch_change=pb2.EpochChange(
                    datasets=[pb2.DatasetState(gen=1, geometry=geometry)],
                    panes=[pb2.PaneEpoch(view_id=0, epoch=1, gen=1, channels_high=[16])],
                )
            )
        )
        self.servicer.outbox.drain()
        self.servicer.on_doorbell(frame(self.event(tool=2, epoch=1, channels=0x8001)))
        self.servicer.on_doorbell(
            db.Cancellation(interaction=1, tool=2, gen=1, view_id=0, reason="camera_moved")
        )
        self.assertEqual(len(seen), 2)
        state = seen[0]
        self.assertEqual(state.spacing_zyx, (3.0, 1.0, 1.0))
        self.assertEqual(state.visible_channels(0x8001), frozenset({0, 16}))
        self.assertEqual(seen[1].epoch, 1, "a cancellation names the pane; its state is current")
        # The transport refuses a stale epoch before a handler sees it; asked
        # directly, the context answers None for one the plugin moved past.
        ctx = s.ToolContext(
            _overlays=self.servicer._overlay, _host=None, _control=self.servicer._control
        )
        self.assertIsNone(ctx.pane(self.event(epoch=7)))
        self.assertIsNone(ctx.pane(3))
        self.assertEqual(ctx.pane(0).gen, 1)

    def test_a_tool_less_context_reads_the_pane_it_subscribed_to(self):
        """A panel, reader or exporter that subscribes to
        ``"epochs"`` had its EpochChange acked and stored with no accessor.
        ``PluginContext.pane(view_id)`` answers what ``ToolContext.pane``
        answers; ``panes()`` names the views it can."""
        ctx = self.servicer._plugin_context()
        self.assertEqual(ctx.panes(), ())
        self.assertIsNone(ctx.pane(0))
        geometry = pb2.Geometry(
            gen=1, spacing_z=3.0, spacing_y=1.0, spacing_x=1.0, spacing_unit="um"
        )
        self.servicer._control.receive(
            pb2.HostMsg(
                epoch_change=pb2.EpochChange(
                    datasets=[pb2.DatasetState(gen=1, geometry=geometry)],
                    panes=[
                        pb2.PaneEpoch(view_id=2, epoch=1, gen=1),
                        pb2.PaneEpoch(view_id=0, epoch=1, gen=1),
                    ],
                )
            )
        )
        self.servicer.outbox.drain()
        self.assertEqual(ctx.panes(), (0, 2))
        self.assertEqual(ctx.pane(2).spacing_zyx, (3.0, 1.0, 1.0))
        self.assertIsNone(ctx.pane(5))

    def test_the_tool_hears_its_activation_come_and_go(self):
        """`ToolBinding{retired}` is how the host withdraws an
        activation, and the SDK handled it -- canceling the gesture -- while
        telling the author nothing, so a panel could not show whether its tool
        was active.  ``activated`` / ``deactivated`` are the hooks, optional
        like every other handler, called after the SDK's own handling; a
        re-statement of a live binding is not a second activation."""
        seen: list[str] = []

        @s.tool("lamp")
        class _Lamp:
            def activated(self, ctx):
                seen.append("on")

            def deactivated(self, ctx):
                seen.append("off")
                raise RuntimeError("a hook that raises is diagnosed, not fatal")

        self.addCleanup(s._TOOLS.pop, "lamp", None)
        self.bind(handle=3, tool_id="lamp")
        self.bind(handle=3, tool_id="lamp", revision=2)
        self.assertEqual(seen, ["on"], "re-stated, not re-activated")
        self.bind(handle=3, tool_id="lamp", retired=True, revision=3)
        self.assertEqual(seen, ["on", "off"])
        self.assertNotIn(3, self.servicer._control.tools)
        self.assertTrue(any("deactivated raised" in d for d in self.servicer._control.diagnostics))
        # A retirement of a handle never activated reaches no hook.
        self.bind(handle=8, tool_id="lamp", retired=True)
        self.assertEqual(seen, ["on", "off"])

    def test_an_activation_hook_can_read_its_own_arrangement(self):
        """The moment to say *P toggles, U undoes* is the
        activation, and the hook ran with no event to hand `arrangement()`
        and nothing else naming the binding -- so a tool could say it only
        on the first record, or through a private attribute.  Two doors, both
        open: ``activated(self, ctx, binding)`` is handed the binding by
        arity (a one-parameter hook is unchanged), and ``ctx.arrangement``
        takes the tool's manifest id.  ``deactivated`` gets the binding just
        withdrawn, which the id form can no longer find."""
        seen: list = []

        @s.tool("probe")
        class _Probe:
            def activated(self, ctx, binding):
                seen.append(("on", binding, ctx.arrangement("probe"), ctx.arrangement("other")))

            def deactivated(self, ctx, binding):
                seen.append(("off", binding, ctx.arrangement("probe")))

        self.addCleanup(s._TOOLS.pop, "probe", None)
        self.bind(handle=4, tool_id="probe", actions=[("probe.undo", "U"), ("probe.clear", "")])
        (on,) = seen
        self.assertEqual(on[0], "on")
        self.assertEqual(on[1].tool, 4)
        self.assertEqual(
            [(a.action_id, a.key) for a in on[1].actions],
            [("probe.undo", "U"), ("probe.clear", "")],
        )
        self.assertIs(on[2], on[1], "the id form finds the binding the hook was handed")
        self.assertIsNone(on[3], "an id with no arrangement is None, not an error")
        # Two handles for one id: the newest wins.
        self.bind(handle=6, tool_id="probe")
        ctx = s.ToolContext(
            _overlays=self.servicer._overlay, _host=None, _control=self.servicer._control
        )
        self.assertEqual(ctx.arrangement("probe").tool, 6)
        self.bind(handle=6, tool_id="probe", retired=True, revision=2)
        self.bind(handle=4, tool_id="probe", retired=True, revision=2)
        off = seen[-1]
        self.assertEqual((off[0], off[1].tool), ("off", 4))
        self.assertIsNone(off[2], "retired: the id form no longer finds it; the hook still has it")

    def test_a_record_whose_context_does_not_reconcile_is_delivered_unusable_and_reported(self):
        """`E` §3's third terminal row: a `level` the epoch does not declare,
        a non-MISS voxel outside the clip box, a `t` past the time table.
        The transport cannot see arranged state and may not reject a
        terminal, so the SUPERVISOR delivers the record with `usable` cleared
        and reports `unusable_context` -- the site the document named from
        the start and the enum did not carry."""
        seen: list = []

        @s.tool("probe")
        class _Probe:
            def press(self, ctx, ev):
                seen.append(ev)

            def release(self, ctx, ev):
                seen.append(ev)

        self.addCleanup(s._TOOLS.pop, "probe", None)
        self.bind(handle=2, tool_id="probe")
        geometry = pb2.Geometry(
            gen=1,
            extent=pb2.Extent(z0=0, z_n=10, y0=0, y_n=20, x0=0, x_n=30),
            crop=pb2.EdgeBox(z0=-0.5, z1=9.5, y0=-0.5, y1=19.5, x0=-0.5, x1=29.5),
            levels=[pb2.LevelScale(z=1, y=1, x=1), pb2.LevelScale(z=2, y=2, x=2)],
        )
        self.servicer._control.receive(
            pb2.HostMsg(time_mapping=pb2.TimeMapping(gen=1, revision=1, t=[0.0, 1.0, 2.0]))
        )
        self.servicer._control.receive(
            pb2.HostMsg(
                epoch_change=pb2.EpochChange(
                    datasets=[pb2.DatasetState(gen=1, geometry=geometry, time_mapping_revision=1)],
                    panes=[pb2.PaneEpoch(view_id=0, epoch=1, gen=1)],
                )
            )
        )
        self.servicer.outbox.drain()

        def send(**kw):
            self.servicer.on_doorbell(frame(self.event(tool=2, epoch=1, **kw)))
            return seen[-1]

        inside = db.Voxel(z=9.0, y=19.0, x=29.0)
        outside = db.Voxel(z=9.0, y=19.0, x=30.0)
        # In every respect reconcilable: usable, nothing reported.
        ev = send(payload_kind=db.KIND_VOXEL, position=inside, seq=0)
        self.assertTrue(ev.usable)
        self.assertEqual(self.kinds(), [])
        # A level-1 index of 14 covers level-0 x 28..29: inside at that level.
        ev = send(level=1, payload_kind=db.KIND_VOXEL, position=db.Voxel(4.0, 9.0, 14.0), seq=1)
        self.assertTrue(ev.usable)
        # The clip box at level 1 ends at 14.5; 15 is outside.
        ev = send(level=1, payload_kind=db.KIND_VOXEL, position=db.Voxel(4.0, 9.0, 15.0), seq=2)
        self.assertFalse(ev.usable)
        # A level the epoch does not declare.
        ev = send(level=3, payload_kind=db.KIND_VOXEL, position=inside, seq=3)
        self.assertFalse(ev.usable)
        # A t past the arranged table.
        ev = send(t=3, payload_kind=db.KIND_VOXEL, position=inside, seq=4)
        self.assertFalse(ev.usable)
        # Outside but MISS: the host said so; reconcilable.
        ev = send(payload_kind=db.KIND_VOXEL, position=outside, position_flags=db.FLAG_MISS, seq=5)
        self.assertTrue(ev.usable)
        # A ray's eye lies outside the box by construction; exempt.
        ray = db.Ray(camera=db.Voxel(100.0, 10.0, 15.0), far=inside, entry_frac=0.9, roll=0.0)
        ev = send(payload_kind=db.KIND_RAY, position=ray, seq=6)
        self.assertTrue(ev.usable)
        # ...but its pick is a voxel and is checked.
        ev = send(
            payload_kind=db.KIND_RAY_AND_PICKED,
            position=db.RayAndPicked(ray=ray, picked=outside),
            seq=7,
        )
        self.assertFalse(ev.usable)
        # Every unusable record was still delivered -- the interaction closes
        # -- and each was reported under the site the IDL now carries.
        self.assertEqual(len(seen), 8)
        reports = [m for k, m in self.drained() if k == "contract_violation"]
        self.assertTrue(reports)
        self.assertEqual(
            {r.contract_violation.where for r in reports}, {pb2.VIOLATION_SITE_UNUSABLE_CONTEXT}
        )
        # §7's rate bound: the first goes at once and the rest fold into the
        # reporter's count until the interval elapses.
        self.assertEqual(self.servicer._control.reporter.counts().get("unusable_context"), 4)

    def test_an_event_for_an_unbound_handle_reaches_nothing(self):
        """§5.5's gate rule from this side: the arrangement precedes the fast
        path, so a handle with no binding has no manifest id to dispatch on --
        and the transport has already reported it as `unknown_tool`."""
        self.servicer.on_doorbell(frame(self.event(tool=9)))
        self.assertEqual(self.seen, [])

    def test_fire_is_its_own_handler_and_never_a_press(self):
        """`E` §1: FIRE opens and closes at once, so a tool that implements
        only press/drag/release sees nothing from it."""
        self.bind()
        self.servicer.on_doorbell(frame(self.event(phase=db.PHASE_FIRE)))
        self.assertEqual([name for name, _ in self.seen], ["fire"])

    def test_an_action_fire_names_its_action_and_reaches_the_action_handler(self):
        """§8.10: a key fired INTO the engaged tool.  `ctx.action(ev)` resolves
        the record's index through the installed arrangement; the class's
        `action` takes it when there is one, `fire` otherwise -- and the open
        gesture is not disturbed either way."""
        self.bind(actions=(("add_node", "N"), ("close", "")))
        resolved = []
        test = self

        class _Polygon:
            def press(self, ctx, ev):
                test.seen.append(("press", ev))

            def drag(self, ctx, ev):
                test.seen.append(("drag", ev))

            def fire(self, ctx, ev):
                test.seen.append(("fire", ev))
                resolved.append(ctx.action(ev))

        s._TOOLS["paint"] = _Polygon
        self.servicer._tool_instances.clear()
        self.servicer.on_doorbell(frame(self.event(phase=db.PHASE_PRESS)))
        self.servicer.on_doorbell(frame(self.event(interaction=2, phase=db.PHASE_FIRE, action=2)))
        self.servicer.on_doorbell(frame(self.event(seq=1, phase=db.PHASE_DRAG)))
        self.assertEqual([name for name, _ in self.seen], ["press", "fire", "drag"])
        self.assertEqual(resolved, ["close"])

        class _WithAction(_Polygon):
            def action(self, ctx, ev):
                test.seen.append(("action", ev))
                resolved.append((ctx.action(ev), ev.action))

        s._TOOLS["paint"] = _WithAction
        self.servicer._tool_instances.clear()
        self.servicer.on_doorbell(frame(self.event(interaction=3, phase=db.PHASE_FIRE, action=1)))
        self.servicer.on_doorbell(frame(self.event(interaction=4, phase=db.PHASE_FIRE, action=9)))
        self.assertEqual([name for name, _ in self.seen][3:], ["action", "action"])
        self.assertEqual(resolved[1:], [("add_node", 1), (None, 9)])
        keys = {a.action_id: a.key for a in ctx_arrangement(self.servicer, 1).actions}
        self.assertEqual(keys, {"add_node": "N", "close": ""})

    def test_a_cancellation_tells_the_tool_the_gesture_is_over(self):
        """`E` §6: an interaction can end with neither its own terminal nor a
        successor announcing it, and a tool holding gesture state has to be
        told or it holds it forever."""
        self.bind()
        self.servicer.on_doorbell(
            db.Cancellation(interaction=1, tool=1, gen=1, view_id=0, reason="camera_moved")
        )
        self.assertEqual([name for name, _ in self.seen], ["cancel"])

    def test_a_handler_that_raises_does_not_stop_the_listener(self):
        """`doorbell_protocol.md` §8: framing faults tear down, everything else
        reports and continues -- and the next grant is queued behind this."""
        self.bind()

        @s.tool("boom")
        class _Boom:
            def press(self, ctx, ev):
                raise RuntimeError("plugin bug")

        self.addCleanup(s._TOOLS.pop, "boom", None)
        self.bind(handle=2, tool_id="boom")
        self.servicer.on_doorbell(frame(self.event(tool=2)))
        self.servicer.on_doorbell(frame(self.event(tool=1)))
        self.assertEqual([name for name, _ in self.seen], ["press"])
        self.assertTrue(
            any("boom" in note for note in self.servicer._control.diagnostics),
            self.servicer._control.diagnostics,
        )

    def test_activation_goes_through_the_transport_before_the_ack(self):
        """§5.5 rule 3, and the transport is what refuses a handle out of
        order -- a refusal is not acknowledged."""
        self.servicer._control.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=5, tool_id="paint", revision=1))
        )
        self.assertIn(5, self.doorbell.tools)
        self.assertEqual(self.kinds(), ["tool_binding_ack"])


class Notifications(_Fast):
    """`doorbell_D_notification.md`: a hint can only bring a check forward."""

    def test_a_hint_naming_an_owned_region_is_reported_and_ignored(self):
        """§3: the read IS the handoff for Owned, so re-checking is unsafe --
        the discipline check is the memory layer's, because the transport
        cannot see a discipline."""
        self.grant(
            2,
            [
                m.Region(0, 32, 64, m.Discipline.OWNED, m.Producer.HOST),
                loaned(1, 96, 512, producer=m.Producer.HOST),
            ],
        )
        self.servicer.outbox.drain()
        self.servicer.on_doorbell(
            frame(db.Notification(segment_id=2, mask=0b1, epoch=0, view_id=0))
        )
        kind, msg = self.drained()[0]
        self.assertEqual(kind, "contract_violation")
        self.assertEqual(msg.contract_violation.where, pb2.VIOLATION_SITE_OWNED_REGION_HINT)

    def test_one_record_naming_two_owned_regions_is_one_occurrence(self):
        """§2's reporting unit is the RECORD: a `D` naming three Owned regions
        is one occurrence and not three."""
        self.grant(
            3,
            [
                m.Region(0, 32, 64, m.Discipline.OWNED, m.Producer.HOST),
                m.Region(1, 96, 64, m.Discipline.OWNED, m.Producer.HOST),
            ],
        )
        self.servicer.outbox.drain()
        self.servicer.on_doorbell(
            frame(db.Notification(segment_id=3, mask=0b11, epoch=0, view_id=0))
        )
        self.assertEqual(self.kinds(), ["contract_violation"])

    def test_a_hint_for_a_consumed_lane_wakes_the_schedule(self):
        """§5: `D` accelerates the unconditional re-check and never replaces
        it, so what a hint does here is exactly one wake."""
        self.grant(
            4,
            [
                m.Region(0, 32, 3 * (16 + 64), m.Discipline.PULSED, m.Producer.HOST, 3, 64),
            ],
        )
        self.servicer.outbox.drain()
        woken = threading.Event()
        self.servicer.reads.wake = woken.set
        self.servicer.on_doorbell(
            frame(db.Notification(segment_id=4, mask=0b1, epoch=0, view_id=0))
        )
        self.assertTrue(woken.is_set())

    def test_an_unknown_segment_is_counted_and_says_nothing(self):
        self.servicer.on_doorbell(
            frame(db.Notification(segment_id=99, mask=0b1, epoch=0, view_id=0))
        )
        self.assertEqual(self.kinds(), [])


class DoorbellTerminals(_Fast):
    """§8.13: report on the control plane WHILE IT IS STILL USABLE."""

    def fault(self):
        return db.Fault(
            failure_class="bad_length",
            direction="inbound",
            transport_class="seqpacket",
            detail="a 72-byte datagram",
            observed_len=72,
        )

    def test_one_doorbell_fault_goes_out_and_the_host_ends_the_session(self):
        """§8.13: the plugin reports and keeps its stream open; the host's
        `Shutdown` follows the window.  Closing here raced the report."""
        self.servicer._doorbell_backstop_s = 0.05
        self.servicer.on_doorbell_fault(self.fault())
        kind, msg = self.drained()[0]
        self.assertEqual(kind, "doorbell_fault")
        self.assertEqual(msg.doorbell_fault.detail, "a 72-byte datagram")
        self.assertEqual(msg.doorbell_fault.observed_len, 72)
        self.assertEqual(
            msg.doorbell_fault.sdk_version, s._sdk_version(), "the same provenance as Ready.id"
        )
        self.assertFalse(self.servicer._closing, "the stream stays open for the answer")
        self.assertTrue(self.servicer.session_ended.wait(2.0), "a backstop if none comes")

    def test_the_two_views_of_one_fault_report_once(self):
        """The transport offers a terminal mailbox AND the ordered outcome
        stream, and reporting both would give Plugin Manager two diagnostics
        for one event."""
        fault = self.fault()
        self.servicer.on_doorbell_fault(fault)  # the fault sink
        self.servicer.on_doorbell(fault)  # the same object, as an outcome
        self.assertEqual(self.kinds(), ["doorbell_fault"])

    def test_a_peer_close_ends_the_session_and_a_local_stop_does_not(self):
        self.servicer._doorbell_backstop_s = 0.05
        self.servicer.on_doorbell(s._doorbell.Teardown("stopped"))
        self.assertIsNone(self.servicer._doorbell_backstop)
        self.servicer.on_doorbell(s._doorbell.Teardown("peer_closed"))
        self.assertTrue(self.servicer.session_ended.wait(2.0))

    def notice(self, **kw):
        fields = dict(
            reason=pb2.DOORBELL_SHUTDOWN_FRAMING_FAULT,
            failure_class=pb2.DOORBELL_FAILURE_BAD_CRC,
            deadline_ms=2000,
            detail="record 7 failed its CRC",
        )
        fields.update(kw)
        return pb2.HostMsg(doorbell_shutdown=pb2.DoorbellShutdown(**fields))

    def test_a_doorbell_shutdown_stops_the_doorbell_and_keeps_the_session(self):
        """§8.13: on our bad frame the host
        sends `DoorbellShutdown{framing_fault, class}` with 2000 ms, stops
        emitting at once, and releases its socket only when the window closes
        -- on our `DoorbellFinalReport` or at expiry.  So the notice is what
        stops this end's listener (on macOS nothing else can: a datagram
        peer's close is not observable), the report goes out inside the
        window, and the SESSION continues: a panel needs no doorbell."""
        self.doorbell.counters = {"send_sent": 3, "send_refused": 1}
        self.assertTrue(self.servicer._route_one(self.notice()))
        self.assertEqual(self.doorbell.stops, 1, "the listener was stopped from the notice")
        kind, msg = self.drained()[0]
        self.assertEqual(kind, "doorbell_final_report")
        self.assertIn("DOORBELL_SHUTDOWN_FRAMING_FAULT", msg.doorbell_final_report.detail)
        self.assertIn("DOORBELL_FAILURE_BAD_CRC", msg.doorbell_final_report.detail)
        self.assertIn("send_refused=1", msg.doorbell_final_report.detail)
        self.assertFalse(self.servicer._closing, "one channel's end is not the session's")
        self.assertIs(
            self.servicer._control.doorbell_shutdown.reason, pb2.DOORBELL_SHUTDOWN_FRAMING_FAULT
        )

    def test_a_second_doorbell_shutdown_changes_nothing(self):
        """The window opened at the first, the host does not restart its own
        (a repeat fault does not restart the window), and
        the final report is one-shot on this channel."""
        self.servicer._route_one(self.notice())
        self.servicer._route_one(self.notice(reason=pb2.DOORBELL_SHUTDOWN_HOST_SHUTDOWN))
        self.assertEqual(self.doorbell.stops, 1)
        self.assertEqual(self.kinds(), ["doorbell_final_report"])
        self.assertTrue(
            any("second DoorbellShutdown" in d for d in self.servicer._control.diagnostics)
        )

    def test_a_doorbell_shutdown_cancels_an_open_gesture(self):
        """Through a REAL transport: `stop()` cancels every open interaction
        (`E` §6, `transport_ended`), so a tool mid-drag hears `cancel` rather
        than holding gesture state for a doorbell that will never release it."""
        import socket as _socket

        seen: list[str] = []

        @s.tool("paint")
        class _Paint:
            def press(self, ctx, ev):
                seen.append("press")

            def cancel(self, ctx, ev):
                seen.append(f"cancel:{ev.reason}")

        self.addCleanup(s._TOOLS.pop, "paint", None)
        transport_class = db.platform_transport_class()
        if transport_class == "stream":
            ours, theirs = _socket.socketpair()  # Windows: a loopback stream pair
        else:
            sock_type = db._SOCK_TYPE_FOR_CLASS[transport_class]
            ours, theirs = _socket.socketpair(_socket.AF_UNIX, sock_type)
        self.addCleanup(theirs.close)
        transport = db.DoorbellTransport(
            ours, transport_class, self.servicer.on_doorbell, handup_deadline=0.5
        )
        self.servicer.attach_doorbell(transport)
        transport.start()
        self.addCleanup(transport.stop)
        transport.handshake_complete(s.PROTOCOL_MAJOR, s.PROTOCOL_MINOR)
        self.servicer._control.receive(
            pb2.HostMsg(tool_binding=pb2.ToolBinding(tool=1, tool_id="paint", revision=1))
        )
        self.servicer._control.receive(
            pb2.HostMsg(
                epoch_change=pb2.EpochChange(
                    datasets=[pb2.DatasetState(gen=1)],
                    panes=[pb2.PaneEpoch(view_id=0, epoch=1, gen=1)],
                )
            )
        )
        self.servicer.outbox.drain()
        theirs.sendall(_press_record(record_id=1, interaction=1, tool=1, gen=1, epoch=1))
        deadline = time.monotonic() + 2.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(seen, ["press"])
        self.servicer._route_one(self.notice())
        self.assertTrue(transport.join(2.0), "the listener did not stop on the notice")
        self.assertEqual(seen, ["press", "cancel:transport_ended"])
        self.assertEqual(self.kinds(), ["doorbell_final_report"])
        self.assertFalse(self.servicer._closing)


class TheHandshakeGatesTheDoorbell(_Fast):
    """§3.1a: no record may be sent before the handshake settles the contract
    -- the version is what selects the framing."""

    def test_hello_completes_the_transport_handshake(self):
        self.assertIsNone(
            self.servicer._check_hello(
                pb2.HostMsg(
                    id="Hello from vizi 1.0.0",
                    hello=pb2.Hello(
                        protocol_major=s.PROTOCOL_MAJOR, protocol_minor=s.PROTOCOL_MINOR
                    ),
                )
            )
        )
        self.assertEqual(self.doorbell.handshakes, [(s.PROTOCOL_MAJOR, s.PROTOCOL_MINOR, None)])

    def test_a_mismatched_version_never_reaches_the_transport(self):
        problem = self.servicer._check_hello(
            pb2.HostMsg(
                id="Hello from vizi 1.0.0",
                hello=pb2.Hello(
                    protocol_major=s.PROTOCOL_MAJOR + 1, protocol_minor=s.PROTOCOL_MINOR
                ),
            )
        )
        self.assertIsNotNone(problem)
        self.assertEqual(self.doorbell.handshakes, [])


# --------------------------------------------------------------------------
# The plugin-produced arenas (`control_plane_protocol.md` §5.6)
# --------------------------------------------------------------------------


class PanelImages(_Fast):
    """§5.6: a descriptor a plugin sends is a LOAN -- one cursor per arena, and
    a full arena fails the push rather than blocking or reusing."""

    def test_an_image_is_reserved_from_the_arena_named_for_the_panel(self):
        segment = self.grant(
            5,
            [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_PANEL,
            subject="plots",
        )
        self.servicer.outbox.drain()
        self.servicer._panel("plots").push_image("figure", b"PNG-ish")
        kind, msg = self.drained()[0]
        self.assertEqual(kind, "panel_patch")
        ref = msg.panel_patch.ops[0].set_image
        self.assertEqual((ref.segment_id, ref.len), (5, 7))
        self.assertEqual(segment.loaned(0).outstanding, 1, "and it stays lent")

    def test_the_bytes_are_not_reused_until_release_loan_names_them(self):
        segment = self.grant(
            6,
            [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_PANEL,
            subject="plots",
        )
        panel = self.servicer._panel("plots")
        panel.push_image("figure", b"first")
        panel.push_image("figure", b"second")
        self.servicer.outbox.drain()
        offsets = {off for off, _ in segment.loaned(0)._outstanding}
        self.assertEqual(len(offsets), 2, "a second push may not overwrite the first")
        self.assertEqual(segment.loaned(0).outstanding, 2)

    def test_a_release_loan_from_the_host_frees_the_reservation(self):
        segment = self.grant(
            7,
            [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_PANEL,
            subject="plots",
        )
        self.servicer._panel("plots").push_image("figure", b"pixels")
        patch = [msg for kind, msg in self.drained() if kind == "panel_patch"][0]
        ref = patch.panel_patch.ops[0].set_image
        self.servicer._control.receive(
            pb2.HostMsg(
                release_loan=pb2.ReleaseLoan(segment_id=7, region=0, offset=ref.offset, len=ref.len)
            )
        )
        self.assertEqual(segment.loaned(0).outstanding, 0)

    def test_a_full_arena_fails_the_push_and_says_by_how_much(self):
        """§5.6: never block, never reuse -- and §5.4's `SegmentShortfall` is
        what turns "a plugin is short" into "short by this much"."""
        self.grant(
            8,
            [loaned(0, 32, 128, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_PANEL,
            subject="plots",
        )
        self.servicer.outbox.drain()
        panel = self.servicer._panel("plots")
        with self.assertRaises(s.NoRegion):
            panel.push_image("figure", b"x" * 4096)
        kinds = self.kinds()
        self.assertIn("segment_shortfall", kinds)

    def test_a_push_that_is_not_admitted_takes_its_bytes_back(self):
        """§5.6's refusals are all reachable, and a reservation whose message
        never left owns bytes nothing will ever release -- the host was not
        told about them, so no `ReleaseLoan` is coming.  The reserve and the
        admission have to be one step or the other."""
        segment = self.grant(
            25,
            [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_PANEL,
            subject="plots",
        )
        arena = segment.loaned(0)
        free = arena.largest_free
        # §5.4: a coalescing push naming a loan may not be merged, so it needs
        # a never-drop slot -- and a refusal there is `Refused`, not a fault.
        self.fill_the_never_drop_queue(25)
        with self.assertRaises(s._control.Refused):
            self.servicer._panel("plots").push_image("figure", b"x" * 500)
        self.assertEqual(arena.outstanding, 0, "the bytes came back")
        self.assertEqual(arena.largest_free, free, "and the cursor rewound")

    def test_one_overlay_id_is_one_handle_whichever_thread_asks_first(self):
        """A tool handler on the listener thread and a command on the worker
        can reach `_overlay` together.  Two handles for one id are two
        revision counters, and the host's monotonic arbitration then drops one
        of them for good -- so the second asker waits for the first's handle.
        Reproduced by parking the first construction inside `__init__`."""
        original = s.OverlayHandle.__init__
        inside, release = threading.Event(), threading.Event()
        constructed = []

        def slow_init(handle, *args, **kwargs):
            constructed.append(handle)
            inside.set()
            self.assertTrue(release.wait(5.0))
            original(handle, *args, **kwargs)

        from unittest import mock

        first: list = []
        with mock.patch.object(s.OverlayHandle, "__init__", slow_init):
            t = threading.Thread(target=lambda: first.append(self.servicer._overlay("cells")))
            t.start()
            self.assertTrue(inside.wait(5.0))
            second: list = []
            t2 = threading.Thread(target=lambda: second.append(self.servicer._overlay("cells")))
            t2.start()
            t2.join(0.2)
            self.assertTrue(t2.is_alive(), "the second asker did not wait for the first")
            release.set()
            t.join(5.0)
            t2.join(5.0)
        self.assertEqual(len(constructed), 1)
        self.assertIs(first[0], second[0])
        self.assertIs(self.servicer._overlay("cells"), first[0])

    def test_a_refused_overlay_push_takes_every_buffer_back(self):
        """Geometry is reserved per item while the caller is still building the
        message, so a refusal has several reservations to reclaim, not one."""
        segment = self.grant(
            26,
            [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_OVERLAY,
            subject="cells",
        )
        arena = segment.loaned(0)
        overlay = self.servicer._overlay("cells")
        items = [
            overlay.marker_item("a", [[1.0, 2.0, 3.0]]),
            overlay.marker_item("b", [[4.0, 5.0, 6.0]]),
        ]
        self.assertEqual(arena.outstanding, 2, "built, not yet pushed")
        self.fill_the_never_drop_queue(26)
        with self.assertRaises(s._control.Refused):
            overlay.set_items(items)
        self.assertEqual(arena.outstanding, 0, "both buffers came back")

    def test_one_buffer_in_two_items_is_refused_before_it_is_queued(self):
        """§5.6 at 1.0: within one message no two operations may name the same
        reservation.  Unreachable through the allocator -- a bump cursor cannot
        hand out an outstanding offset twice -- and entirely reachable by an
        author reusing an item."""
        segment = self.grant(
            27,
            [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_OVERLAY,
            subject="cells",
        )
        self.servicer.outbox.drain()
        overlay = self.servicer._overlay("cells")
        item = overlay.marker_item("m", [[1.0, 2.0, 3.0]])
        with self.assertRaises(s._control.Refused):
            overlay.set_items([item, item])
        self.assertEqual(self.kinds(), [], "refused whole; nothing queued")
        self.assertEqual(segment.loaned(0).outstanding, 0, "and the bytes came back")

    def test_an_image_pushed_before_the_panel_opens_is_held_and_sent_when_it_does(self):
        """The host grants a panel's segment when the person opens it, so an
        image pushed earlier has nowhere to go yet.  It is held -- the latest
        per widget and row, not an error -- and sent as soon as the segment is
        installed (`PanelOpened` may come first, and a relaunched
        session may never hear it), so it is ahead of the plugin's own
        `@panel_opened` handler, whose newer state then wins."""
        panel = self.servicer._panel("plots")
        panel.push_image("figure", b"old")
        panel.push_image("figure", b"newest")
        panel.push_image("cell", b"row-a", row="a")
        self.assertEqual([k for k, _ in self.drained()], [], "nothing to send before it opens")
        segment = self.grant(
            9,
            [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_PANEL,
            subject="plots",
        )
        self.servicer._invoke_pool.submit(lambda: None).result(timeout=5)  # the install's flush
        ops = [
            op
            for kind, msg in self.drained()
            if kind == "panel_patch"
            for op in msg.panel_patch.ops
        ]
        self.assertEqual(
            [(op.widget_id, op.row_key, op.set_image.len) for op in ops],
            [("figure", "", 6), ("cell", "a", 5)],
        )
        self.assertEqual(segment.loaned(0).outstanding, 2)
        self.servicer.on_panel_opened("plots")
        self.assertEqual(self.drained(), [], "sent once, not on every open")

    def test_a_direct_push_retires_the_held_image_it_supersedes(self):
        """An image sent directly left an older held one behind,
        which a later flush sent over it."""
        panel = self.servicer._panel("plots")
        panel.push_image("figure", b"old")
        self.grant(
            9,
            [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_PANEL,
            subject="plots",
        )
        self.servicer._invoke_pool.submit(lambda: None).result(timeout=5)
        self.drained()
        panel._held_images[("figure", "")] = b"stale"  # as the race left it
        panel.push_image("figure", b"newest")
        panel._flush_held_images()
        lens = [
            op.set_image.len
            for k, msg in self.drained()
            if k == "panel_patch"
            for op in msg.panel_patch.ops
        ]
        self.assertEqual(lens, [6], "only the newest, sent once")

    def test_one_held_image_that_cannot_go_does_not_take_the_rest(self):
        """The first failure in a flush dropped every image held
        behind it; and what a closed panel holds is bounded."""
        panel = self.servicer._panel("plots")
        panel.push_image("big", b"x" * 5000)  # more than the arena below
        panel.push_image("small", b"fits")
        self.grant(
            9,
            [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_PANEL,
            subject="plots",
        )
        self.servicer._invoke_pool.submit(lambda: None).result(timeout=5)
        ops = [op for k, msg in self.drained() if k == "panel_patch" for op in msg.panel_patch.ops]
        self.assertEqual([op.widget_id for op in ops], ["small"])
        other = self.servicer._panel("closed")
        with self.assertRaises(s.NoRegion):
            other.push_image("huge", b"x" * (s.HELD_IMAGE_BYTES + 1))

    def test_two_threads_asking_for_a_panel_get_one_handle(self):
        """`_panel` made handles without a lock."""
        got = []
        barrier = threading.Barrier(8)

        def ask():
            barrier.wait()
            got.append(self.servicer._panel("race"))

        threads = [threading.Thread(target=ask) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len({id(h) for h in got}), 1)

    def test_set_rows_past_the_lists_max_rows_is_refused_here(self):
        """The host refuses an over-long list, but the cell ops
        for its rows could first overrun the session's coalescing capacity,
        which faults the session."""
        from vizi_plugin_sdk import declare
        from vizi_plugin_sdk import widgets as w

        with declare._fresh():
            declare.panel(
                "plots", title="P", root=w.list_rows("regions", [w.label("n")], max_rows=2)
            )
            panel = self.servicer._panel("plots")
            panel.set_rows("regions", ["a", "b"])
            with self.assertRaisesRegex(ValueError, "at most 2 rows"):
                panel.set_rows("regions", ["a", "b", "c"])

    def test_a_held_image_for_a_row_the_list_removed_is_dropped(self):
        from vizi_plugin_sdk import declare
        from vizi_plugin_sdk import widgets as w

        with declare._fresh():
            declare.panel("plots", title="P", root=w.list_rows("regions", [w.image("cell")]))
            panel = self.servicer._panel("plots")
            panel.set_rows("regions", ["a", "b"])
            panel.push_image("cell", b"a", row="a")
            panel.push_image("cell", b"b", row="b")
            panel.set_rows("regions", ["a"])
            self.assertEqual(sorted(panel._held_images), [("cell", "a")])


class Overlays(_Fast):
    """The live registers are a PULSED region and a `D`; the bulk push
    is a plugin-produced LOANED arena."""

    def regions(self):
        return [
            pulsed(0, 32),
            loaned(1, 32 + 3 * (16 + 512), 8192, producer=m.Producer.PLUGIN),
        ]

    def test_bulk_geometry_is_reserved_and_the_update_names_it(self):
        segment = self.grant(9, self.regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        self.servicer.outbox.drain()
        overlay = self.servicer._overlay("cells")
        item = overlay.marker_item("m", [[1.0, 2.0, 3.0]])
        overlay.set_items([item])
        kind, msg = self.drained()[0]
        self.assertEqual(kind, "overlay_update")
        ref = msg.overlay_update.items[0].marker.positions_zyx.segment
        self.assertEqual(ref.segment_id, 9)
        self.assertEqual(ref.len, 24)
        self.assertEqual(segment.loaned(1).outstanding, 1)

    def test_two_pushes_do_not_share_bytes(self):
        """A handle that rewinds its cursor after every push is only safe
        while the host copies out during apply.  §5.6 says the bytes are
        the host's until `ReleaseLoan` names them."""
        segment = self.grant(10, self.regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        overlay = self.servicer._overlay("cells")
        overlay.set_items([overlay.marker_item("m", [[1.0, 2.0, 3.0]])])
        overlay.set_items([overlay.marker_item("m", [[4.0, 5.0, 6.0]])])
        self.servicer.outbox.drain()
        self.assertEqual(segment.loaned(1).outstanding, 2)

    def test_a_builder_declares_how_many_messages_will_name_its_bytes(self):
        """§5.6: only the sender knows how many messages will name one
        reservation, and `SegmentRef.uses` is where it says so.  A push and a
        `CompileLabelVolume` of one item are two (§8.8 repeats the descriptor
        by design), and the builders had no way to declare it -- so the host
        released after the first and the second named freed bytes, on every
        path, with no error anywhere."""
        import numpy as np

        self.grant(13, self.regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        overlay = self.servicer._overlay("cells")
        labels = np.array([[[0, 1], [1, 0]]], dtype=np.uint8)
        lut = np.zeros((256, 4), dtype=np.uint8)
        item = overlay.label_volume_item("m", labels, lut, uses=2)
        self.assertEqual(item.label_volume.labels.segment.uses, 2)
        self.assertEqual(item.label_volume.colormap.segment.uses, 2, "the LUT is a loan too")
        for built in (
            overlay.marker_item("a", [[1.0, 2.0, 3.0]], uses=3),
            overlay.line_item("b", [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], uses=3),
        ):
            with self.subTest(item=built.item_id):
                geometry = getattr(built, built.WhichOneof("geometry"))
                self.assertEqual(geometry.positions_zyx.segment.uses, 3)

    def test_a_label_volume_the_builder_cannot_finish_takes_its_reservations_back(self):
        """The builder reserved the labels, then the LUT; a full arena failed
        the second and the first stayed outstanding -- no item was returned,
        no descriptor reached the host, and so no `ReleaseLoan` ever would.
        An 8192-byte arena filled by one 7 KiB volume was then short by that
        much for the rest of the session.  A refusal AFTER both reservations
        (a bad `label_names` key) stranded both."""
        import numpy as np

        segment = self.grant(16, self.regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        arena = segment.loaned(1)
        overlay = self.servicer._overlay("cells")
        lut = np.zeros((256, 4), dtype=np.uint8)
        big = np.ones((7, 33, 32), dtype=np.uint8)  # 7392 of 8192: the 1024-byte LUT cannot follow
        with self.assertRaises(s.NoRegion):
            overlay.label_volume_item("m", big, lut)
        self.assertEqual(arena.outstanding, 0, "the labels reservation is taken back")
        self.assertEqual(arena.largest_free, 8192)
        with self.assertRaises(ValueError):
            overlay.label_volume_item(
                "m", np.ones((1, 2, 2), np.uint8), lut, label_names={"x": "y"}
            )
        self.assertEqual(arena.outstanding, 0, "nothing was reserved for an item refused")
        # And the arena is whole: a volume the LUT CAN follow builds in it.
        fits = np.ones((6, 32, 32), dtype=np.uint8)  # 6144 + 1024
        item = overlay.label_volume_item("m", fits, lut)
        self.assertEqual(item.label_volume.labels.segment.len, fits.nbytes)
        self.assertEqual(arena.outstanding, 2)

    def test_one_use_stays_off_the_wire(self):
        """0 and 1 both mean one (§5.6), so the default declares nothing and a
        descriptor that sets nothing is unchanged."""
        self.grant(14, self.regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        overlay = self.servicer._overlay("cells")
        item = overlay.marker_item("m", [[1.0, 2.0, 3.0]])
        self.assertEqual(item.marker.positions_zyx.segment.uses, 0)

    def test_uses_is_a_positive_count_of_a_reservation(self):
        self.grant(15, self.regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        overlay = self.servicer._overlay("cells")
        for bad in (0, -1, 1.5, True, "2"):
            with self.subTest(uses=bad), self.assertRaises(ValueError):
                overlay.marker_item("m", [[1.0, 2.0, 3.0]], uses=bad)
        with self.assertRaises(ValueError, msg="inline geometry has no reservation"):
            overlay.marker_item("m", [[1.0, 2.0, 3.0]], inline=True, uses=2)
        # `uses=1` with inline is the default's meaning and is allowed.
        overlay.marker_item("m", [[1.0, 2.0, 3.0]], inline=True, uses=1)

    def test_a_message_past_the_declared_uses_is_refused_before_it_leaves(self):
        """§5.6, the sender's own accounting: an undeclared push-then-compile released the bytes
        under the message still in flight, silently, on every timing.  The
        excess message is refused here as `UsesExceeded`, in EITHER order --
        the check that only fires in one order would hide the bug in the
        other -- and nothing is queued."""
        self.grant(19, self.regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        self.servicer.outbox.drain()
        overlay = self.servicer._overlay("cells")
        host = self.servicer._host_client
        # Push then compile, the shipped shape, with the default count.
        item = overlay.marker_item("m", [[1.0, 2.0, 3.0]])
        overlay.set_items([item])
        with self.assertRaises(m.UsesExceeded) as caught:
            host.compile_label_volume("cells", item)
        self.assertIn("uses=2", str(caught.exception))
        self.assertEqual([k for k, _ in self.drained()], ["overlay_update"], "nothing queued")
        # Compile then push: the same refusal, on the push.
        item = overlay.marker_item("n", [[1.0, 2.0, 3.0]])
        self.servicer._host_client._waiters.clear()
        with self.assertRaises((m.UsesExceeded, s.HostError)):
            # The compile is sent (no host answers, so it times out); the
            # reservation is now named once.
            host.compile_label_volume("cells", item, timeout=0.05)
        with self.assertRaises(m.UsesExceeded):
            overlay.set_items([item])
        # Declared for two, both go; a third is the excess.
        item = overlay.marker_item("o", [[1.0, 2.0, 3.0]], uses=2)
        overlay.set_items([item])
        with self.assertRaises(s.HostError):
            host.compile_label_volume("cells", item, timeout=0.05)
        with self.assertRaises(m.UsesExceeded):
            overlay.set_items([item])

    def test_a_later_message_not_admitted_leaves_an_earlier_ones_reservation_alone(self):
        """A failed send reclaimed the reservation
        unconditionally, even one an earlier, successfully-sent message still
        named -- freeing bytes under a message in flight, and handing the host
        a release it would then refuse.  A reservation nobody has sent comes
        back; one somebody has stays outstanding and is counted."""
        segment = self.grant(20, self.regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        self.servicer.outbox.drain()
        arena = segment.loaned(1)
        overlay = self.servicer._overlay("cells")
        item = overlay.marker_item("m", [[1.0, 2.0, 3.0]], uses=2)
        overlay.set_items([item])
        self.assertEqual(arena.outstanding, 1)
        notes: list[str] = []

        def refuse(msg):
            raise s._control.Refused("no room")

        compile_msg = pb2.PluginMsg(
            id="q", compile_label_volume=pb2.CompileLabelVolume(overlay_id="cells", item=item)
        )
        with self.assertRaises(s._control.Refused):
            s._emit_or_reclaim(refuse, compile_msg, self.servicer._memory, notes.append)
        self.assertEqual(arena.outstanding, 1, "the push's reservation is the host's to release")
        self.assertEqual(arena.stranded, 1)
        self.assertTrue(notes and "stays outstanding" in notes[0])
        # No further message will name it, so it holds no claim --
        # a retirement is not pinned by it for the rest of the session.
        self.assertFalse(segment._has_held_claims())
        # And a reservation no message carried still comes back whole.
        fresh = overlay.marker_item("n", [[1.0, 2.0, 3.0]])
        with self.assertRaises(s._control.Refused):
            s._emit_or_reclaim(
                refuse,
                pb2.PluginMsg(overlay_update=pb2.OverlayUpdate(overlay_id="cells", items=[fresh])),
                self.servicer._memory,
            )
        self.assertEqual(arena.outstanding, 1, "the never-sent reservation came back")

    def test_a_live_update_publishes_and_rings_the_doorbell(self):
        """The lane is region 0, and the `D` must name it: a mask with bit 0
        set.  Passing the index where a mask is read rings nothing for
        region 0."""
        segment = self.grant(11, self.regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        self.servicer.outbox.drain()
        overlay = self.servicer._overlay("cells")
        overlay.set_items_live([overlay.marker_item("m", [[1.0, 2.0, 3.0]], inline=True)])
        self.assertEqual(self.kinds(), [], "a live update sends no gRPC")
        self.assertEqual(self.doorbell.notifications, [(11, 1 << 0, 0, 0)])
        # And the value is really there: a reader of the same lane consumes it.
        self.assertIsNotNone(segment.pulsed_reader(0).read())

    def test_a_live_lane_that_never_makes_progress_says_so(self):
        """One coalesced publish is ordinary, a long
        unbroken run is not -- the host may be holding its claims -- and it
        was indistinguishable from coalescing.  A heads-up, not a fault."""
        self.grant(12, self.regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        self.servicer.outbox.drain()
        notes: list[str] = []
        overlay = self.servicer._overlay("cells")
        overlay._diagnose = notes.append
        segment, region, _writer = overlay._live()
        stuck = type("W", (), {"publish": lambda self, payload, pane=None: False})()
        overlay._live = lambda: (segment, region, stuck)
        item = overlay.marker_item("m", [[1.0, 2.0, 3.0]], inline=True)
        for _ in range(s._LIVE_STALL_REPORT * 2):
            overlay.set_items_live([item])
        self.assertEqual(len(notes), 2, notes)
        self.assertIn("no free buffer", notes[0])
        self.assertEqual(self.kinds(), [], "a diagnostic, not a message")

    def test_a_live_update_carries_the_pane_it_answers(self):
        """The host applies a value with a
        pair only while the pair names the pane's current epoch, so a tool that
        stamps the `E`'s `(view_id, epoch)` onto what it draws has its geometry
        dropped rather than drawn under a crop the user moved off.  The pair
        rides the slot's last eight bytes (§3.2) and the `D` alike."""
        lane = m.Region(
            0,
            32,
            3 * (16 + 512),
            m.Discipline.PULSED,
            m.Producer.PLUGIN,
            3,
            512,
            epoch_offset=512 - m.PANE_HEADER_BYTES,
        )
        regions = [lane, loaned(1, 32 + 3 * (16 + 512), 8192, producer=m.Producer.PLUGIN)]
        segment = self.grant(12, regions, purpose=m.PURPOSE_OVERLAY, subject="cells")
        self.servicer.outbox.drain()
        overlay = self.servicer._overlay("cells")
        item = overlay.marker_item("m", [[1.0, 2.0, 3.0]], inline=True)
        ev = db.InputEvent(**{**_EVENT_FIELDS, "view_id": 1, "epoch": 9})
        reader = segment.pulsed_reader(0)
        for pane, want in ((ev, (1, 9)), (m.PaneRef(0, 4), (0, 4)), ((1, 11), (1, 11))):
            with self.subTest(pane=pane):
                overlay.set_items_live([item], pane=pane)
                self.assertIsNotNone(reader.read())
                self.assertEqual((reader.last_pane.view_id, reader.last_pane.epoch), want)
                self.assertEqual(self.doorbell.notifications[-1], (12, 1 << 0, want[1], want[0]))
        # No pair clears the header rather than leaving the last one (§3.2).
        overlay.set_items_live([item])
        self.assertIsNotNone(reader.read())
        self.assertIsNone(reader.last_pane)
        self.assertEqual(self.doorbell.notifications[-1], (12, 1 << 0, 0, 0))
        with self.assertRaises(ValueError):
            overlay.set_items_live([item], pane=(1, 0))  # epoch 0 is no epoch

    def test_a_pane_on_a_lane_without_a_header_is_a_caller_error(self):
        segment = self.grant(13, self.regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        self.servicer.outbox.drain()
        overlay = self.servicer._overlay("cells")
        item = overlay.marker_item("m", [[1.0, 2.0, 3.0]], inline=True)
        with self.assertRaises(ValueError):
            overlay.set_items_live([item], pane=(0, 1))
        self.assertIsNone(segment.pulsed_reader(0).read(), "nothing was published")

    def test_a_live_update_refuses_segment_backed_geometry(self):
        """§3.3: a Pulsed value is claimed and consumed whole, so each
        committed one has to be complete and self-contained -- a descriptor
        inside one names a loan the sender cannot keep alive until the read."""
        self.grant(12, self.regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        overlay = self.servicer._overlay("cells")
        with self.assertRaises(ValueError) as caught:
            overlay.set_items_live([overlay.marker_item("m", [[1.0, 2.0, 3.0]])])
        self.assertIn("inline geometry only", str(caught.exception))

    def test_an_overlay_with_no_granted_lane_says_so(self):
        with self.assertRaises(s.NoRegion):
            self.servicer._overlay("nothing").set_items_live([])

    def test_two_overlays_are_two_segments_each_with_its_own_lane(self):
        """§3.1 at `1.0`: a plugin with two overlays is granted two `O`
        segments, and `subject` is what says which is which.  Before it, the
        `O` row read *lanes* plural with no key, and this SDK served one and
        refused two."""
        cells = self.grant(30, self.regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        edges = self.grant(31, self.regions(), purpose=m.PURPOSE_OVERLAY, subject="edges")
        self.servicer.outbox.drain()
        for name, segment in (("cells", cells), ("edges", edges)):
            overlay = self.servicer._overlay(name)
            overlay.set_items_live([overlay.marker_item(name, [[1.0, 2.0, 3.0]], inline=True)])
        self.assertEqual(
            [n for n, _, _, _ in self.doorbell.notifications],
            [30, 31],
            "each published into its own segment's lane",
        )
        for name, segment in (("cells", cells), ("edges", edges)):
            payload = segment.pulsed_reader(0).read()
            self.assertIsNotNone(payload)
            update = pb2.OverlayUpdate()
            update.ParseFromString(payload)
            self.assertEqual(update.overlay_id, name)
            self.assertEqual([i.item_id for i in update.items], [name])
            # §5.5 rule 4: **each IDENTITY'S sequence begins at 1**, not merely
            # "1 is the lowest legal value".  A sender allocating from a
            # per-session counter -- so that the second overlay's first
            # statement is revision 2 -- is non-conforming, because a receiver
            # has no other way to tell a first statement from one whose
            # predecessors it missed.
            self.assertEqual(update.revision, 1, "each overlay's first statement")

    def test_the_live_payload_is_a_bare_overlay_update(self):
        """§3.3: a serialized `OverlayUpdate` and nothing else -- no envelope,
        no framing, no length prefix.  The slot's published length IS the
        message's length, which is the whole reason the length lives in the
        state header rather than in the payload."""
        segment = self.grant(32, self.regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        self.servicer.outbox.drain()
        overlay = self.servicer._overlay("cells")
        overlay.set_items_live([overlay.marker_item("m", [[1.0, 2.0, 3.0]], inline=True)])
        payload = segment.pulsed_reader(0).read()
        update = pb2.OverlayUpdate()
        update.ParseFromString(payload)
        self.assertEqual(update.SerializeToString(), payload, "no bytes but the message")

    def test_the_live_revision_is_the_overlays_and_not_the_slots(self):
        """§3.3: two monotonic counters meet here and count different things --
        §6.3's revision orders publications of a buffer, this one orders
        versions of an overlay, and a late-arriving gRPC `OverlayUpdate` is
        compared against this one.  Conflating them drops valid updates
        whenever the two run at different rates, which they do."""
        segment = self.grant(33, self.regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        self.servicer.outbox.drain()
        overlay = self.servicer._overlay("cells")
        overlay.set_items([overlay.marker_item("bulk", [[1.0, 2.0, 3.0]])])
        self.servicer.outbox.drain()
        overlay.set_items_live([overlay.marker_item("m", [[1.0, 2.0, 3.0]], inline=True)])
        update = pb2.OverlayUpdate()
        update.ParseFromString(segment.pulsed_reader(0).read())
        self.assertEqual(update.revision, 2, "the overlay's second version, not the lane's first")

    def test_a_payload_over_the_slots_capacity_fails_the_call(self):
        """§3.3: the sender fails its own call rather than truncating or
        splitting -- §6 has no continuation, and a partial `OverlayUpdate` is
        not a smaller one."""
        self.grant(
            34,
            [pulsed(0, 32, slot=64), loaned(1, 32 + 3 * 80, 8192, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_OVERLAY,
            subject="cells",
        )
        self.servicer.outbox.drain()
        overlay = self.servicer._overlay("cells")
        with self.assertRaises(ValueError) as caught:
            overlay.set_items_live([overlay.marker_item("m", [[1.0, 2.0, 3.0]] * 8, inline=True)])
        self.assertIn("capacity", str(caught.exception))

    def test_the_capacity_is_eight_less_when_the_lane_carries_a_pane_header(self):
        """§3.2: the header is the slot's last eight bytes, so a
        pane-associated lane has eight fewer payload bytes -- and the SAME
        payload into the SAME slot size is refused with a header and published
        without one, which is the only way to see the eight."""
        slot, points = 136, 4
        for segment_id, name, epoch_offset in ((35, "paned", slot - 8), (36, "bare", None)):
            lane = m.Region(
                0,
                32,
                3 * (16 + slot),
                m.Discipline.PULSED,
                m.Producer.PLUGIN,
                3,
                slot,
                epoch_offset=epoch_offset,
            )
            self.grant(
                segment_id,
                [lane, loaned(1, 32 + 3 * (16 + slot), 8192, producer=m.Producer.PLUGIN)],
                purpose=m.PURPOSE_OVERLAY,
                subject=name,
            )
        self.servicer.outbox.drain()

        def item_for(name):
            overlay = self.servicer._overlay(name)
            return overlay, overlay.marker_item(
                "m", [[float(n), 2.0, 3.0] for n in range(points)], inline=True
            )

        paned, paned_item = item_for("paned")
        bare, bare_item = item_for("bare")
        size = len(
            pb2.OverlayUpdate(overlay_id="bare", items=[bare_item], revision=1).SerializeToString()
        )
        self.assertTrue(slot - 8 < size <= slot, f"the fixture needs {slot - 8} < {size} <= {slot}")
        with self.assertRaises(ValueError) as caught:
            paned.set_items_live([paned_item])
        self.assertIn(f"{slot - 8}B payload capacity", str(caught.exception))
        bare.set_items_live([bare_item])
        self.assertIsNotNone(self.servicer._memory.segment(36).pulsed_reader(0).read())


class LiveLaneContent(unittest.TestCase):
    """`shared_memory_protocol.md` §3.3: what a live overlay lane CARRIES.

    §3.2 settles where the bytes are; this settles what they mean, and without
    it two peers can agree on every offset and disagree about the message.
    The check is the RECEIVER's, and the sender runs the same one -- two
    implementations of one section is how the ends come to disagree.
    """

    def item(self, *, inline):
        buf = (
            pb2.GeomBuffer(inline=b"\x00" * 24, shape=[1, 3], dtype=pb2.DTYPE_F64)
            if inline
            else pb2.GeomBuffer(
                segment=pb2.SegmentRef(segment_id=4, offset=32, len=24),
                shape=[1, 3],
                dtype=pb2.DTYPE_F64,
            )
        )
        return pb2.OverlayItem(item_id="m", marker=pb2.OverlayMarker(positions_zyx=buf))

    def update(self, overlay_id="cells", *, inline=True):
        return pb2.OverlayUpdate(
            overlay_id=overlay_id, items=[self.item(inline=inline)], revision=3
        )

    def test_a_conforming_value_decodes_with_nothing_to_report(self):
        update, broken = s.decode_live_overlay(self.update().SerializeToString(), "cells")
        self.assertEqual(broken, [])
        self.assertEqual(update.revision, 3)

    def test_segment_backed_geometry_is_live_geometry_not_inline(self):
        _, broken = s.decode_live_overlay(self.update(inline=False).SerializeToString(), "cells")
        self.assertEqual([w for w, _ in broken], ["live_geometry_not_inline"])

    def test_a_buffer_with_no_source_is_not_inline_either(self):
        """§3.3 says every buffer MUST BE inline, which is not the same as
        "must not name a segment" -- and an empty `GeomBuffer` is the shape an
        author reaches by forgetting to fill one."""
        update = pb2.OverlayUpdate(
            overlay_id="cells",
            revision=1,
            items=[
                pb2.OverlayItem(
                    item_id="m",
                    marker=pb2.OverlayMarker(positions_zyx=pb2.GeomBuffer(shape=[1, 3])),
                )
            ],
        )
        _, broken = s.decode_live_overlay(update.SerializeToString(), "cells")
        self.assertEqual([w for w, _ in broken], ["live_geometry_not_inline"])
        self.assertIn("no source", broken[0][1])

    def test_an_overlay_id_disagreeing_with_the_subject_is_lane_subject_mismatch(self):
        """§3.3: the redundancy IS the check.  On a disagreement neither name
        is believed and the update is not applied -- applying either is how a
        mis-published lane becomes a mis-drawn one."""
        _, broken = s.decode_live_overlay(self.update("edges").SerializeToString(), "cells")
        self.assertEqual([w for w, _ in broken], ["lane_subject_mismatch"])

    def test_both_sites_are_wire_values_since_1_0(self):
        """Both were local diagnostics for want of a name, which is a rule the
        peer can break and never be told about."""
        for where in ("live_geometry_not_inline", "lane_subject_mismatch"):
            with self.subTest(where):
                self.assertTrue(v.is_wire_site(where))
        self.assertEqual(v.site_number("live_geometry_not_inline"), 22)
        self.assertEqual(v.site_number("lane_subject_mismatch"), 23)

    def test_the_payload_is_the_message_and_nothing_else(self):
        """No envelope, no framing, no length prefix: the slot's published
        length IS the message's length."""
        wire = self.update().SerializeToString()
        update, _ = s.decode_live_overlay(wire, "cells")
        self.assertEqual(update.SerializeToString(), wire)


class ReaderRouting(_Fast):
    """§8.2 at `1.0`: the HOST names which reader contribution is opened."""

    class _Fake:
        uri = None

        def open(self, uri):
            type(self).uri = uri
            return s.ReaderInfo(shape_tczyx=(1, 1, 1, 1, 1), dtype="u8")

    def reader(self, name):
        return type(f"_R_{name}", (self._Fake,), {"name": name})

    def open(self, servicer, reader_id=""):
        return servicer._on_open(
            pb2.HostMsg(id="q", open_reader=pb2.OpenReader(uri="f.tif", reader_id=reader_id))
        )

    def test_the_named_contribution_is_the_one_opened(self):
        tif, zarr = self.reader("tif"), self.reader("zarr")
        servicer = s._Servicer({"tif": tif, "zarr": zarr}, "test")
        self.assertEqual(self.open(servicer, "zarr").WhichOneof("msg"), "reader_opened")
        self.assertIsInstance(servicer._impl, zarr)

    def test_a_reader_that_knows_its_calibration_says_so_and_one_that_does_not_says_nothing(
        self,
    ):
        """§8.2: `ReaderOpened` carries the voxel pitch and
        the time increment a reader KNOWS, in Geometry's shape; unknown is 0
        and "", never an invented 1.0."""

        class _Calibrated(self._Fake):
            def open(self, uri):
                return s.ReaderInfo(
                    shape_tczyx=(1, 1, 1, 1, 1),
                    dtype="u8",
                    spacing_zyx=(2.0, 0.3, None),
                    spacing_unit="um",
                    time_increment=0.5,
                    time_unit="s",
                )

        class _Unitless(self._Fake):
            def open(self, uri):
                return s.ReaderInfo(
                    shape_tczyx=(1, 1, 1, 1, 1),
                    dtype="u8",
                    spacing_zyx=(2.0, 0.3, 0.3),
                    time_increment=0.5,
                )

        opened = self.open(s._Servicer({"c": _Calibrated}, "test"), "c").reader_opened
        self.assertEqual(
            (opened.spacing_z, opened.spacing_y, opened.spacing_x, opened.spacing_unit),
            (2.0, 0.3, 0.0, "um"),
            "an axis the reader does not know stays 0",
        )
        self.assertEqual((opened.time_increment, opened.time_unit), (0.5, "s"))
        # A pitch without a unit is no measurement: the unit stays unknown, as
        # in the Go, Rust and Java SDKs, and nothing is assumed µm or s.
        unitless = self.open(s._Servicer({"u": _Unitless}, "test"), "u").reader_opened
        self.assertEqual((unitless.spacing_unit, unitless.time_unit), ("", ""))
        plain = self.open(s._Servicer({"p": self.reader("p")}, "test"), "p").reader_opened
        self.assertEqual(
            (plain.spacing_z, plain.spacing_unit, plain.time_increment, plain.time_unit),
            (0.0, "", 0.0, ""),
        )
        self.assertTrue(plain.little_endian, "the deposit is little-endian (§2)")

    def test_the_reader_lifecycle_answers_every_question_the_contract_names(self):
        """`reader_writer_contract.md` §1: a metadata document with its form
        (the arm had never produced one in any test); a level the reader does
        not serve is OUT_OF_BOUNDS, never level-0 data; a close is idempotent;
        and a read on a closed handle is NOT_FOUND naming the handle -- a
        Python attribute error from a `None` implementation was what the host
        log got."""
        import numpy as np

        class _Documented(self._Fake):
            def metadata(self):
                return ("ome-xml", "<OME/>")

            def read_volume(self, t, c):
                return np.zeros((1, 1, 1), dtype=np.uint8)

        servicer = s._Servicer({"d": _Documented}, "test")
        self.assertEqual(self.open(servicer, "d").WhichOneof("msg"), "reader_opened")
        doc = servicer._on_metadata(pb2.HostMsg(id="m", metadata=pb2.Metadata(handle="r0")))
        self.assertEqual((doc.metadata.form, doc.metadata.text), ("ome-xml", "<OME/>"))
        deep = servicer._on_read_volume(
            pb2.HostMsg(id="v", read_volume=pb2.ReadVolume(handle="r0", t=0, c=0, level=2))
        )
        self.assertEqual(deep.error.kind, pb2.Error.OUT_OF_BOUNDS)
        # §1.5: an index outside the declared shape is refused by name before
        # the reader class is asked (the fake declares (1, 1, 1, 1, 1)).
        far = servicer._on_read_plane(
            pb2.HostMsg(id="p", read_plane=pb2.ReadPlane(handle="r0", t=0, z=3, c=0))
        )
        self.assertEqual(far.error.kind, pb2.Error.OUT_OF_BOUNDS)
        self.assertIn("z=3", far.error.message)
        fine = servicer._on_read_volume(
            pb2.HostMsg(id="ok", read_volume=pb2.ReadVolume(handle="r0", t=0, c=0))
        )
        self.assertEqual(fine.WhichOneof("msg"), "error", "no G arena in this fixture")
        self.assertIn("'G'", fine.error.message)
        for _ in range(2):  # the second close is done, not an error
            closed = servicer._on_close_reader(
                pb2.HostMsg(id="c", close_reader=pb2.CloseReader(handle="r0"))
            )
            self.assertEqual(closed.WhichOneof("msg"), "closed")
        for kind, msg in (
            ("read_volume", pb2.ReadVolume(handle="r0", t=0, c=0)),
            ("read_plane", pb2.ReadPlane(handle="r0", t=0, z=0, c=0)),
            ("metadata", pb2.Metadata(handle="r0")),
        ):
            reply = getattr(servicer, f"_on_{kind}")(pb2.HostMsg(id="q", **{kind: msg}))
            self.assertEqual(reply.error.kind, pb2.Error.NOT_FOUND, kind)
            self.assertIn("r0", reply.error.message)

    def test_a_second_open_opens_the_next_file_before_closing_the_reader_it_replaces(self):
        """`reader_writer_contract.md` §1.2's handshake: on a second
        `OpenReader` the plugin opens the next file FIRST and closes the
        previous reader only once it has, so that `Error` means nothing
        changed.  Handles never repeat within the session.  A close that
        raises is diagnosed and the open still succeeds."""
        events: list[str] = []

        class _Closing(self._Fake):
            def open(self, uri):
                events.append(f"open {uri}")
                if uri == "missing":
                    raise FileNotFoundError(uri)
                return super().open(uri)

            def close(self):
                events.append("close")
                if "boom" in events[0]:
                    raise RuntimeError("cannot close")

        servicer = s._Servicer({"c": _Closing}, "test")
        first = servicer._on_open(
            pb2.HostMsg(id="1", open_reader=pb2.OpenReader(uri="a", reader_id="c"))
        )
        second = servicer._on_open(
            pb2.HostMsg(id="2", open_reader=pb2.OpenReader(uri="b", reader_id="c"))
        )
        self.assertEqual(first.WhichOneof("msg"), "reader_opened")
        self.assertEqual(second.WhichOneof("msg"), "reader_opened")
        self.assertEqual(events, ["open a", "open b", "close"], "closed AFTER the next open")
        self.assertEqual(
            (first.reader_opened.handle, second.reader_opened.handle), ("r0", "r1"), "never reused"
        )
        # A next file that does not open leaves the previous reader as it was.
        events[:] = []
        refused = servicer._on_open(
            pb2.HostMsg(id="3", open_reader=pb2.OpenReader(uri="missing", reader_id="c"))
        )
        self.assertEqual(refused.WhichOneof("msg"), "error")
        self.assertEqual(events, ["open missing"], "nothing closed")
        self.assertEqual(servicer._reader_handle, "r1", "b is still open under r1")
        self.assertIsNotNone(servicer._impl)
        # A close that raises does not fail the open it follows.
        events[:] = []
        servicer = s._Servicer({"c": _Closing}, "test")
        servicer._on_open(
            pb2.HostMsg(id="1", open_reader=pb2.OpenReader(uri="boom", reader_id="c"))
        )
        third = servicer._on_open(
            pb2.HostMsg(id="3", open_reader=pb2.OpenReader(uri="fine", reader_id="c"))
        )
        self.assertEqual(third.WhichOneof("msg"), "reader_opened")
        self.assertEqual(events, ["open boom", "open fine", "close"])
        self.assertTrue(any("replaced reader" in d for d in servicer._control.diagnostics))

    def test_an_invalid_ReaderInfo_is_refused_and_the_open_reader_stays(self):
        """The reply is built BEFORE the open reader is released and the
        new one installed, so a `dtype` the wire has no word for fails the
        open with the old reader intact -- the host, told "nothing changed"
        (§1.2 H2), keeps a reader this end still has.  The new one is closed."""
        events: list[str] = []

        class _Reader(self._Fake):
            def open(self, uri):
                events.append(f"open {uri}")
                return s.ReaderInfo(
                    shape_tczyx=(1, 1, 2, 2, 2), dtype="u32" if uri == "b" else "u8"
                )

            def close(self):
                events.append("close")

        servicer = s._Servicer({"c": _Reader}, "test")
        servicer._on_open(pb2.HostMsg(id="1", open_reader=pb2.OpenReader(uri="a", reader_id="c")))
        refused = servicer._on_open(
            pb2.HostMsg(id="2", open_reader=pb2.OpenReader(uri="b", reader_id="c"))
        )
        self.assertEqual(refused.WhichOneof("msg"), "error")
        self.assertEqual(refused.error.kind, pb2.Error.INVALID_ARGUMENT)
        self.assertIn("u32", refused.error.message)
        self.assertEqual(events, ["open a", "open b", "close"], "B closed; A untouched")
        self.assertEqual((servicer._reader_handle, servicer._reader_uri), ("r0", "a"))

    def test_a_reopen_of_the_same_file_closes_first_and_a_failed_one_leaves_no_reader(self):
        """The exception in §1.2's handshake: an exclusively locked file
        cannot be opened twice, so a reopen of the
        URI the open reader holds closes it first -- and a reopen that then
        fails leaves no reader, which the spec says."""
        events: list[str] = []
        fail = []

        class _Locked(self._Fake):
            def open(self, uri):
                events.append(f"open {uri}")
                if fail:
                    raise OSError("gone")
                return super().open(uri)

            def close(self):
                events.append("close")

        servicer = s._Servicer({"c": _Locked}, "test")
        servicer._on_open(pb2.HostMsg(id="1", open_reader=pb2.OpenReader(uri="a", reader_id="c")))
        again = servicer._on_open(
            pb2.HostMsg(id="2", open_reader=pb2.OpenReader(uri="a", reader_id="c"))
        )
        self.assertEqual(again.WhichOneof("msg"), "reader_opened")
        self.assertEqual(events, ["open a", "close", "open a"], "the same file: closed first")
        fail.append(True)
        gone = servicer._on_open(
            pb2.HostMsg(id="3", open_reader=pb2.OpenReader(uri="a", reader_id="c"))
        )
        self.assertEqual(gone.WhichOneof("msg"), "error")
        self.assertIsNone(servicer._impl, "no reader is open")
        none = servicer._on_read_volume(
            pb2.HostMsg(id="v", read_volume=pb2.ReadVolume(handle="r1", t=0, c=0))
        )
        self.assertEqual(none.WhichOneof("msg"), "error")
        self.assertEqual(none.error.kind, pb2.Error.NOT_FOUND)

    def test_a_request_naming_a_replaced_handle_is_not_found_and_diagnosed(self):
        """§1.2's handshake has the host send nothing for a reader once it has
        asked for the next; a request that arrives anyway names the wrong
        file and is refused by handle (§1.8) -- and diagnosed, because it is
        the host out of step.  `CloseReader` for a replaced handle is the
        idempotent `Closed` and leaves the open reader alone: closing
        "whatever is open" for it is how a host closed the dataset it had
        just opened."""
        servicer = s._Servicer({"c": self._Fake}, "test")
        servicer._on_open(pb2.HostMsg(id="1", open_reader=pb2.OpenReader(uri="a", reader_id="c")))
        servicer._on_open(pb2.HostMsg(id="2", open_reader=pb2.OpenReader(uri="b", reader_id="c")))
        for name, msg in (
            ("read_volume", pb2.HostMsg(id="v", read_volume=pb2.ReadVolume(handle="r0", t=0, c=0))),
            (
                "read_plane",
                pb2.HostMsg(id="p", read_plane=pb2.ReadPlane(handle="r0", t=0, z=0, c=0)),
            ),
            ("metadata", pb2.HostMsg(id="m", metadata=pb2.Metadata(handle="r0"))),
        ):
            with self.subTest(request=name):
                reply = getattr(servicer, f"_on_{name}")(msg)
                self.assertEqual(reply.WhichOneof("msg"), "error")
                self.assertEqual(reply.error.kind, pb2.Error.NOT_FOUND)
                self.assertIn("r0", reply.error.message)
        self.assertEqual(
            sum("replaced" in d for d in servicer._control.diagnostics), 3, "each one diagnosed"
        )
        closed = servicer._on_close_reader(
            pb2.HostMsg(id="c", close_reader=pb2.CloseReader(handle="r0"))
        )
        self.assertEqual(closed.WhichOneof("msg"), "closed")
        self.assertEqual(servicer._reader_handle, "r1", "b is still open")
        self.assertIsNotNone(servicer._impl)
        closed = servicer._on_close_reader(
            pb2.HostMsg(id="c", close_reader=pb2.CloseReader(handle="r1"))
        )
        self.assertEqual(closed.WhichOneof("msg"), "closed")
        self.assertIsNone(servicer._impl, "the named handle closed")

    def test_an_empty_id_is_that_reader_when_there_is_exactly_one(self):
        """A plugin declaring exactly one reader may treat an empty
        `reader_id` as that one."""
        only = self.reader("only")
        servicer = s._Servicer({"tif": only}, "test")
        self.assertEqual(self.open(servicer).WhichOneof("msg"), "reader_opened")

    def test_an_empty_id_with_several_declared_is_refused_rather_than_guessed(self):
        """The ambiguity is the host's to resolve, and a guess is how one SDK
        opens a file another rejects.  Re-deriving it from the URI is an
        agreement this protocol asks for nowhere else -- extension lists, magic
        bytes and directory shapes all disagree where formats overlap."""
        servicer = s._Servicer({"tif": self.reader("tif"), "zarr": self.reader("zarr")}, "test")
        reply = self.open(servicer)
        self.assertEqual(reply.WhichOneof("msg"), "error")
        self.assertEqual(reply.error.kind, pb2.Error.INVALID_ARGUMENT)
        self.assertIsNone(servicer._impl, "nothing was opened")

    def test_a_name_this_build_does_not_serve_is_not_found(self):
        """NOT_FOUND rather than a guess: the host believes it matched a
        contribution, and opening a different one is worse than failing."""
        servicer = s._Servicer({"tif": self.reader("tif")}, "test")
        reply = self.open(servicer, "zarr")
        self.assertEqual(reply.error.kind, pb2.Error.NOT_FOUND)

    def test_a_reader_says_how_it_decided_its_axes(self):
        """`ReaderOpened.axis_inference` was hardcoded ``"plugin"`` with no
        field on `ReaderInfo` to set it from."""

        class _Guessing(self._Fake):
            def open(self, uri):
                return s.ReaderInfo(
                    shape_tczyx=(1, 1, 1, 1, 1), dtype="u8", axis_inference="fallback"
                )

        servicer = s._Servicer({"g": _Guessing, "p": self.reader("p")}, "test")
        self.assertEqual(self.open(servicer, "g").reader_opened.axis_inference, "fallback")
        self.assertEqual(self.open(servicer, "p").reader_opened.axis_inference, "plugin")

    def test_a_session_with_no_reader_says_so(self):
        reply = self.open(s._Servicer(None, "test"), "tif")
        self.assertEqual(reply.error.kind, pb2.Error.NOT_FOUND)


class SeriesChoiceOnOpen(_Fast):
    """A file holding several images is answered with its list, and the
    instance that listed it opens the one the host names."""

    class _Multi:
        instances: list = []

        def __init__(self):
            self.opened = None
            self.closed = False
            type(self).instances.append(self)

        def open(self, uri, series=None):
            if series is None:
                return s.SeriesChoice(
                    [
                        s.SeriesEntry("a", "Scene A", (1, 1, 2, 3, 4), "u16"),
                        s.SeriesEntry("b", "Scene B"),
                    ]
                )
            if series not in ("a", "b"):
                raise s.Refusal(pb2.Error.NOT_FOUND, f"no series {series!r}")
            self.opened = series
            return s.ReaderInfo(shape_tczyx=(1, 1, 1, 1, 1), dtype="u8")

        def close(self):
            self.closed = True

    def setUp(self):
        super().setUp()
        self._Multi.instances = []

    def open(self, servicer, uri="f.h5", series=None):
        request = pb2.OpenReader(uri=uri, reader_id="m")
        if series is not None:
            request.series = series
        return servicer._on_open(pb2.HostMsg(id="q", open_reader=request))

    def test_the_list_changes_nothing_and_the_same_instance_opens_the_choice(self):
        servicer = s._Servicer({"m": self._Multi}, "test")
        listed = self.open(servicer)
        self.assertEqual(listed.WhichOneof("msg"), "series_list")
        self.assertEqual([e.id for e in listed.series_list.entries], ["a", "b"])
        first = listed.series_list.entries[0]
        self.assertEqual((first.label, list(first.shape_tczyx)), ("Scene A", [1, 1, 2, 3, 4]))
        self.assertEqual(first.dtype, pb2.DType.DTYPE_U16)
        self.assertEqual(listed.series_list.entries[1].dtype, pb2.DType.DTYPE_UNSPECIFIED)
        self.assertFalse(listed.series_list.truncated)
        self.assertIsNone(servicer._impl, "a list opens nothing")
        opened = self.open(servicer, series="b")
        self.assertEqual(opened.WhichOneof("msg"), "reader_opened")
        self.assertEqual(len(self._Multi.instances), 1, "the listing instance opened it")
        self.assertEqual(self._Multi.instances[0].opened, "b")
        self.assertIsNone(servicer._pending_reader)

    def test_a_different_open_retires_the_listing_instance(self):
        servicer = s._Servicer({"m": self._Multi}, "test")
        self.open(servicer, uri="one.h5")
        self.open(servicer, uri="two.h5", series="a")
        first, second = self._Multi.instances
        self.assertTrue(first.closed, "the listing of one.h5 is over")
        self.assertEqual(second.opened, "a")

    def test_an_unanswered_list_is_closed_when_its_hold_expires(self):
        servicer = s._Servicer({"m": self._Multi}, "test")
        self.open(servicer)
        servicer._expire_pending(servicer._pending_token)
        self.assertTrue(self._Multi.instances[0].closed)
        self.assertIsNone(servicer._pending_reader)
        self.open(servicer, series="a")
        self.assertEqual(self._Multi.instances[1].opened, "a", "a fresh instance opens it")

    def test_an_unknown_series_is_not_found(self):
        servicer = s._Servicer({"m": self._Multi}, "test")
        refused = self.open(servicer, series="gone")
        self.assertEqual(refused.error.kind, pb2.Error.NOT_FOUND)

    def test_a_list_naming_one_id_twice_is_refused_before_it_is_sent(self):
        """The host answers with the id alone, so two entries
        sharing one cannot be told apart -- the sender's defect, refused like
        any other malformed list."""

        class _Twice(self._Multi):
            def open(self, uri, series=None):
                return s.SeriesChoice([s.SeriesEntry("x", "one"), s.SeriesEntry("x", "two")])

        refused = self.open(s._Servicer({"m": _Twice}, "test"))
        self.assertEqual(refused.error.kind, pb2.Error.INVALID_ARGUMENT)
        self.assertIn("'x' is listed twice", refused.error.message)

    def test_view_state_tells_no_series_from_the_series_named_empty(self):
        """`dataset_series` is `optional`, as `OpenReader.series`
        is -- `""` may be a reader's id."""
        host = s.HostClient.__new__(s.HostClient)
        for wire, want in ((pb2.ViewState(), None), (pb2.ViewState(dataset_series=""), "")):
            host._request = lambda *a, hm=pb2.HostMsg(id="q", view_state=wire), **k: hm
            host._seq = iter([1])
            self.assertEqual(host.view_state().dataset_series, want)

    def test_a_named_series_answered_with_a_list_is_refused(self):
        class _Stubborn(self._Multi):
            def open(self, uri, series=None):
                return s.SeriesChoice([s.SeriesEntry("x"), s.SeriesEntry("y")])

        refused = self.open(s._Servicer({"m": _Stubborn}, "test"), series="x")
        self.assertEqual(refused.error.kind, pb2.Error.INVALID_ARGUMENT)
        self.assertTrue(_Stubborn.instances[-1].closed)

    def test_a_list_of_one_opens_it(self):
        class _Single(self._Multi):
            def open(self, uri, series=None):
                if series is None:
                    return s.SeriesChoice([s.SeriesEntry("only")])
                return super().open(uri, "a") if series == "only" else None

        self.assertEqual(
            self.open(s._Servicer({"m": _Single}, "test")).WhichOneof("msg"), "reader_opened"
        )

    def test_a_reader_written_before_series_is_never_asked_one(self):
        seen = []

        class _Old:
            def open(self, uri):
                seen.append(uri)
                return s.ReaderInfo(shape_tczyx=(1, 1, 1, 1, 1), dtype="u8")

        opened = self.open(s._Servicer({"m": _Old}, "test"), series="ignored")
        self.assertEqual(opened.WhichOneof("msg"), "reader_opened")
        self.assertEqual(seen, ["f.h5"])

    def test_sixty_thousand_series_fit_in_one_message_and_pass_the_bounds(self):
        entries = [
            s.SeriesEntry(f"/acquisition/volume_{i:05d}", f"volume {i}") for i in range(60_000)
        ]
        listed = s._series_list(s.SeriesChoice(entries))
        self.assertEqual(len(listed.entries), 60_000)
        self.assertFalse(listed.truncated)

        self.assertIsNone(c.walk_bounds(pb2.PluginMsg(id="q", series_list=listed)))

    def test_a_list_past_the_message_ceiling_is_sent_as_a_marked_prefix(self):
        entries = [s.SeriesEntry(str(i), "x" * 100) for i in range(100)]
        with mock.patch.object(s._control, "MAX_MESSAGE_BYTES", 2_000):
            listed = s._series_list(s.SeriesChoice(entries))
        self.assertTrue(listed.truncated)
        self.assertLess(len(listed.entries), 100)
        self.assertLessEqual(pb2.PluginMsg(id="q", series_list=listed).ByteSize(), 2_000)
        self.assertEqual(
            [e.id for e in listed.entries], [str(i) for i in range(len(listed.entries))]
        )


class RefusalsByKind(_Fast):
    """`reader_writer_contract.md` §1.5 mandates the kinds "because a
    third-party host may branch on them" -- and every exception a reader or
    exporter raised was forwarded with `kind` unset, so a Python plugin could
    not send any of them.  The SDK's own refusals
    (`_no_reader`, `_index_refusal`) always carried one; the boundary was the
    class, not the wire."""

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(s._EXPORTERS.pop, "x", None)

    def open_reader(self, cls):
        servicer = s._Servicer({"r": cls}, "test")
        reply = servicer._on_open(
            pb2.HostMsg(id="o", open_reader=pb2.OpenReader(uri="f.tif", reader_id="r"))
        )
        return servicer, reply

    def test_a_reader_chooses_the_kind_it_refuses_with(self):
        class _NotMine:
            def open(self, uri):
                raise s.Refusal(pb2.Error.UNSUPPORTED, "dtype code 9 is not defined")

        _, reply = self.open_reader(_NotMine)
        self.assertEqual(reply.error.kind, pb2.Error.UNSUPPORTED)
        self.assertEqual(reply.error.message, "open failed: dtype code 9 is not defined")

    def test_a_missing_file_is_not_found_and_anything_else_is_an_io_error(self):
        """§1.5's two rows for a reader that did not choose: "`uri` cannot be
        opened" and "the file failed under the reader"."""

        class _Missing:
            def open(self, uri):
                raise FileNotFoundError(uri)

        _, reply = self.open_reader(_Missing)
        self.assertEqual(reply.error.kind, pb2.Error.NOT_FOUND)

        class _Broken:
            def open(self, uri):
                return s.ReaderInfo(shape_tczyx=(1, 1, 1, 1, 1), dtype="u8")

            def read_volume(self, t, c):
                raise ValueError("truncated page")

            def metadata(self):
                raise RuntimeError("no header")

        servicer, _ = self.open_reader(_Broken)
        read = servicer._on_read_volume(
            pb2.HostMsg(id="v", read_volume=pb2.ReadVolume(handle="r0", t=0, c=0))
        )
        self.assertEqual(read.error.kind, pb2.Error.IO_ERROR)
        self.assertEqual(read.error.message, "read failed: truncated page")
        doc = servicer._on_metadata(pb2.HostMsg(id="m", metadata=pb2.Metadata(handle="r0")))
        self.assertEqual(doc.error.kind, pb2.Error.IO_ERROR)

    def test_a_host_error_an_exporter_let_through_keeps_the_hosts_kind(self):
        """An exporter that reads its source through `ctx.host` and does not
        catch the reply propagates what the host said, not `IO_ERROR`."""

        @s.exporter("x")
        class _Relaying:
            def begin(self, desc):
                raise s.HostError(pb2.Error.OUT_OF_BOUNDS, "t=9 is outside T=4")

        reply = self.servicer._on_open_export(
            pb2.HostMsg(
                id="e",
                open_export=pb2.OpenExport(
                    exporter_id="x", shape_tczyx=[1] * 5, dtype=pb2.DTYPE_U8
                ),
            )
        )
        self.assertEqual(reply.error.kind, pb2.Error.OUT_OF_BOUNDS)
        self.assertEqual(reply.error.message, "begin failed: t=9 is outside T=4")
        # A declaration the SDK cannot interpret -- no dtype, a short shape --
        # is refused by kind rather than escaping as a KeyError.
        for bad in (dict(shape_tczyx=[1] * 5), dict(shape_tczyx=[1, 1], dtype=pb2.DTYPE_U8)):
            reply = self.servicer._on_open_export(
                pb2.HostMsg(id="b", open_export=pb2.OpenExport(exporter_id="x", **bad))
            )
            self.assertEqual(reply.error.kind, pb2.Error.INVALID_ARGUMENT, bad)

    def test_an_exporter_refuses_a_chunk_by_kind_and_a_dead_handle_is_not_found(self):
        """§2.4: "after either, the handle is dead and a chunk naming it is
        `Error{NOT_FOUND}`" -- on the exporter path as on the reader path."""

        @s.exporter("x")
        class _Strict:
            def begin(self, desc):
                pass

            def write(self, t, c, arr):
                raise s.Refusal(pb2.Error.OUT_OF_BOUNDS, "chunk is (1, 2, 2), artifact (1, 1, 1)")

            def finish(self):
                raise OSError("disk full")

            def abort(self):
                pass

        opened = self.servicer._on_open_export(
            pb2.HostMsg(
                id="e",
                open_export=pb2.OpenExport(
                    exporter_id="x", shape_tczyx=[1, 1, 1, 1, 1], dtype=pb2.DTYPE_U8
                ),
            )
        )
        handle = opened.export_opened.handle
        written = self.servicer._on_export_chunk(
            pb2.HostMsg(id="c", export_chunk=pb2.ExportChunk(handle=handle, t=0, inline=bytes(1)))
        )
        self.assertEqual(written.error.kind, pb2.Error.OUT_OF_BOUNDS)
        self.assertTrue(written.error.message.startswith("write failed: chunk is"))
        finished = self.servicer._on_finish_export(
            pb2.HostMsg(id="f", finish_export=pb2.FinishExport(handle=handle))
        )
        self.assertEqual(finished.error.kind, pb2.Error.IO_ERROR)
        self.assertEqual(finished.error.message, "finish failed: disk full")
        # A failed finish consumed the handle: every later request naming it
        # is NOT_FOUND, and so is one after an abort.
        for kind, msg in (
            ("export_chunk", pb2.ExportChunk(handle=handle, t=0, inline=bytes(1))),
            ("finish_export", pb2.FinishExport(handle=handle)),
        ):
            reply = getattr(self.servicer, f"_on_{kind}")(pb2.HostMsg(id="q", **{kind: msg}))
            self.assertEqual(reply.error.kind, pb2.Error.NOT_FOUND, kind)
            self.assertIn(handle, reply.error.message)
        unknown = self.servicer._on_open_export(
            pb2.HostMsg(id="u", open_export=pb2.OpenExport(exporter_id="nope"))
        )
        self.assertEqual(unknown.error.kind, pb2.Error.NOT_FOUND)


def _plus_filter(**over):
    fields = dict(
        plugin_id="org.example.kin",
        plugin_version="1.2",
        stage_id="stabilize",
        params={"opts": pb2.Value(s="--run=3"), "sigma": pb2.Value(f=2.0)},
        channels=[0, 2],
    )
    fields.update(over)
    return pb2.AppliedFilter(**fields)


class WhereVoxelsCameFrom(_Fast):
    """Every array the host hands a plugin says which filter, if any,
    its voxels passed through -- so a plugin can refuse its own output."""

    def test_the_wire_form_becomes_plain_values_and_absence_is_none(self):
        applied = s.AppliedFilter.from_wire(pb2.DataVolume(filter=_plus_filter()))
        self.assertEqual(
            applied,
            s.AppliedFilter(
                "org.example.kin", "1.2", "stabilize", {"opts": "--run=3", "sigma": 2.0}, (0, 2)
            ),
        )
        self.assertIsNone(s.AppliedFilter.from_wire(pb2.DataVolume()))

    def test_a_read_carries_its_filter_and_a_derived_array_keeps_it(self):
        ref = pb2.SegmentRef(segment_id=1, offset=0, len=4, shape_zyx=[1, 2, 2], dtype=pb2.DTYPE_U8)
        for wire, expect in ((_plus_filter(), "org.example.kin"), (None, None)):
            volume = pb2.DataVolume(segment=ref, open_generation=7)
            if wire is not None:
                volume.filter.CopyFrom(wire)
            host = s.HostClient.__new__(s.HostClient)
            host._request = lambda *a, hm=pb2.HostMsg(id="q1", data_volume=volume), **k: hm
            host._copy_deposit = lambda hm, ref: bytes(4)
            host._seq = iter([1])
            arr = host.read_volume(0, 0)
            self.assertEqual(arr.open_generation, 7)
            self.assertEqual(getattr(arr.filter, "plugin_id", None), expect)
            self.assertEqual(getattr(arr[0].copy().filter, "plugin_id", None), expect)

    def test_view_state_carries_the_frame_interval_or_none(self):
        host = s.HostClient.__new__(s.HostClient)
        for wire, want in (
            (pb2.ViewState(time_increment=0.5, time_unit="s"), (0.5, "s")),
            (pb2.ViewState(), (None, "")),
        ):
            host._request = lambda *a, hm=pb2.HostMsg(id="q", view_state=wire), **k: hm
            host._seq = iter([1])
            vs = host.view_state()
            self.assertEqual((vs.time_increment, vs.time_unit), want)

    def test_view_state_names_every_channel_and_where_the_name_came_from(self):
        """A source this build does not know is its number, never a
        name that might read as declared."""
        wire = pb2.ViewState(
            channels=[
                pb2.ChannelInfo(index=0, name="GCaMP", source=1),
                pb2.ChannelInfo(index=1, name="Channel 2", source=2),
                pb2.ChannelInfo(index=2, name="Ch3", source=9),
            ]
        )
        host = s.HostClient.__new__(s.HostClient)
        host._request = lambda *a, **k: pb2.HostMsg(id="q", view_state=wire)
        host._seq = iter([1])
        self.assertEqual(
            host.view_state().channels,
            (
                s.ChannelName(0, "GCaMP", "FILE"),
                s.ChannelName(1, "Channel 2", "PLACEHOLDER"),
                s.ChannelName(2, "Ch3", "9"),
            ),
        )

    def test_a_reader_declares_channel_names_and_no_more_than_it_has(self):
        """`ReaderOpened.channel_names`, "" for a channel it does not
        name, and never more names than channels."""
        opened, _, _ = s._reader_opened(
            s.ReaderInfo(shape_tczyx=(1, 3, 1, 1, 1), dtype="u8", channel_names=("", "GFP"))
        )
        self.assertEqual(list(opened.channel_names), ["", "GFP"])
        opened, _, _ = s._reader_opened(s.ReaderInfo(shape_tczyx=(1, 3, 1, 1, 1), dtype="u8"))
        self.assertEqual(list(opened.channel_names), [])
        with self.assertRaisesRegex(ValueError, "channel_names names 2 channels of 1"):
            s._reader_opened(
                s.ReaderInfo(shape_tczyx=(1, 1, 1, 1, 1), dtype="u8", channel_names=("a", "b"))
            )

    def test_view_state_names_the_filter_the_views_show(self):
        hm = pb2.HostMsg(id="q1", view_state=pb2.ViewState(filter=_plus_filter()))
        host = s.HostClient.__new__(s.HostClient)
        host._request = lambda *a, **k: hm
        host._seq = iter([1])
        self.assertEqual(host.view_state().filter.stage_id, "stabilize")
        host._request = lambda *a, **k: pb2.HostMsg(id="q2", view_state=pb2.ViewState())
        host._seq = iter([2])
        self.assertIsNone(host.view_state().filter)

    def test_an_export_names_its_filter_and_each_volume_says_whether_it_is_its_output(self):
        seen = {}

        @s.exporter("x")
        class _Recording:
            def begin(self, desc):
                seen["desc"] = desc.filter

            def write(self, t, c, arr):
                seen[(t, c)] = arr.filter

            def finish(self):
                pass

            def abort(self):
                pass

        opened = self.servicer._on_open_export(
            pb2.HostMsg(
                id="e",
                open_export=pb2.OpenExport(
                    exporter_id="x",
                    shape_tczyx=[2, 2, 1, 1, 1],
                    dtype=pb2.DTYPE_U8,
                    filter=_plus_filter(),
                ),
            )
        )
        handle = opened.export_opened.handle
        self.assertEqual(seen["desc"].plugin_id, "org.example.kin")
        # t=0: channel 0 is the stage's output; channel 1 it does not rewrite.
        # t=1: the stage refused, so channel 0 is the file's own.
        for t, filtered in ((0, [0]), (1, [])):
            written = self.servicer._on_export_chunk(
                pb2.HostMsg(
                    id=f"c{t}",
                    export_chunk=pb2.ExportChunk(
                        handle=handle,
                        t=t,
                        channels=[0, 1],
                        inline=bytes(2),
                        filtered_channels=filtered,
                    ),
                )
            )
            self.assertTrue(written.HasField("chunk_written"), written)
        self.assertEqual(seen[(0, 0)], seen["desc"])
        self.assertIsNone(seen[(0, 1)])
        self.assertIsNone(seen[(1, 0)])


class SavePath(unittest.TestCase):
    """`request_save_path` returns the chosen path, or None for a cancel."""

    def _host(self, reply):
        host = s.HostClient.__new__(s.HostClient)
        pending = mock.Mock()
        pending.result.side_effect = lambda timeout: s._save_path_result(reply)
        host.request_save_path_async = lambda *a, **k: pending
        return host

    def test_a_path_or_none(self):
        chosen = pb2.HostMsg(id="q", save_path_result=pb2.SavePathResult(path="/tmp/t.csv"))
        self.assertEqual(
            self._host(chosen).request_save_path("t", extension="csv"), Path("/tmp/t.csv")
        )
        declined = pb2.HostMsg(id="q", save_path_result=pb2.SavePathResult(declined=True))
        self.assertIsNone(self._host(declined).request_save_path("t"))
        empty = pb2.HostMsg(id="q", save_path_result=pb2.SavePathResult())
        with self.assertRaises(s.HostError):
            self._host(empty).request_save_path("t")


class SubscribedPushes(_Fast):
    """`ViewChanged` / `RegionsChanged` reach the registered handlers on
    a worker of their own, newest first, and region pushes merge rather than
    drop while a handler is busy."""

    def setUp(self):
        super().setUp()
        saved = dict(s._SUBSCRIBED)
        self.addCleanup(lambda: (s._SUBSCRIBED.clear(), s._SUBSCRIBED.update(saved)))
        self.seen = queue.Queue()

    def test_a_view_push_reaches_its_handler_with_a_context(self):
        s.on_view_changed(lambda ctx, change: self.seen.put((ctx, change)))
        routing = self.servicer._control.receive(
            pb2.HostMsg(
                view_changed=pb2.ViewChanged(
                    open_generation=3, t=7, z=2, visible_channels=[0, 2], selected_region_id="r9"
                )
            )
        )
        self.assertEqual(routing.cls, s._control.Class.HOST_COALESCING)
        ctx, change = self.seen.get(timeout=5)
        self.assertIsInstance(ctx, s.PluginContext)
        self.assertEqual(change, s.ViewChange(3, 7, 2, (0, 2), "r9"))

    def test_a_view_push_carries_the_filter_the_views_show(self):
        """A subscriber hears a filter applied as it hears a scrub."""
        s.on_view_changed(lambda ctx, change: self.seen.put(change))
        applied = pb2.AppliedFilter(plugin_id="org.f", plugin_version="1", stage_id="blur")
        self.servicer._control.receive(
            pb2.HostMsg(view_changed=pb2.ViewChanged(open_generation=1, filter=applied))
        )
        change = self.seen.get(timeout=5)
        self.assertEqual((change.filter.plugin_id, change.filter.stage_id), ("org.f", "blur"))

    def test_region_pushes_merge_while_the_handler_is_busy(self):
        busy = threading.Event()
        release = threading.Event()

        def handler(ctx, change):
            self.seen.put(change)
            busy.set()
            release.wait(5)

        s.on_regions_changed(handler)

        def push(reset, *changes):
            self.servicer._control.receive(
                pb2.HostMsg(
                    regions_changed=pb2.RegionsChanged(
                        open_generation=1,
                        reset=reset,
                        changes=[
                            pb2.RegionChange(region_id=r, revision=v, origin="user", deleted=d)
                            for r, v, d in changes
                        ],
                    )
                )
            )

        push(True, ("a", 1, False))
        busy.wait(5)
        push(False, ("b", 2, False))
        push(False, ("a", 3, False), ("b", 0, True))
        release.set()
        first = self.seen.get(timeout=5)
        merged = self.seen.get(timeout=5)
        self.assertTrue(first.reset)
        self.assertFalse(merged.reset)
        self.assertEqual(
            {(c.region_id, c.revision, c.deleted) for c in merged.changes},
            {("a", 3, False), ("b", 0, True)},
        )


class ColormapsAndLists(unittest.TestCase):
    """`get_colormap` returns vizi's table; a list-valued arg (a
    multi-select channel picker) is a tuple of ints both ways."""

    def test_get_colormap(self):
        table = bytes(range(256)) * 4
        host = s.HostClient.__new__(s.HostClient)
        host._request = lambda *a, **k: pb2.HostMsg(
            id="q", colormap_table=pb2.ColormapTable(name="viridis", rgba=table)
        )
        host._seq = iter([1])
        got = host.get_colormap("viridis")
        self.assertEqual(got.shape, (256, 4))
        self.assertEqual(got.tobytes(), table)

    def test_an_int_list_round_trips(self):
        value = s._python_to_value([0, 2])
        self.assertEqual(value.WhichOneof("kind"), "ints")
        self.assertEqual(s._value_to_python(value), (0, 2))


class ListRows(unittest.TestCase):
    """List rows: `set_rows` sends the whole ordered key list,
    every op takes `row=` for a cell, and the declaration counts each row."""

    def _panel(self):
        sent = []
        return s.PanelHandle("p", sent.append, None), sent

    def test_set_rows_and_cell_ops(self):
        panel, sent = self._panel()
        panel.set_rows("cells", ["r2", "r1"])
        panel.set_value("on", True, row="r1")
        panel.set_value("cells", "r2")
        rows, cell, selection = (m.panel_patch.ops[0] for m in sent)
        self.assertEqual(list(rows.set_rows.keys), ["r2", "r1"])
        self.assertEqual((cell.widget_id, cell.row_key, cell.set_value.b), ("on", "r1", True))
        self.assertEqual((selection.widget_id, selection.row_key), ("cells", ""))
        for bad in (["a", "a"], [""], ["x" * 129]):
            with self.assertRaises(ValueError):
                panel.set_rows("cells", bad)

    def test_a_removed_rows_pending_cells_are_dropped(self):
        """A newer `set_rows` drains in the slot its predecessor
        held, ahead of cell ops queued since; a removed row's pending cell
        would then be refused.  They are dropped -- and only that list's:
        another list's row of the same key keeps its op."""
        from vizi_plugin_sdk import control as ctl
        from vizi_plugin_sdk import declare
        from vizi_plugin_sdk import widgets as w

        with declare._fresh():
            declare.panel(
                "p",
                title="P",
                root=w.column(
                    "c",
                    w.list_rows("regions", [w.label("rname", "")]),
                    w.list_rows("others", [w.label("oname", "")]),
                ),
            )
            box = ctl.Outbox()
            panel = s.PanelHandle("p", box.send, None, drop_rows=box.drop_rows)
            panel.set_rows("regions", ["a", "b"])
            panel.set_rows("others", ["b"])
            panel.set_value("rname", "A", row="a")
            panel.set_value("rname", "B", row="b")
            panel.set_value("oname", "other B", row="b")
            panel.set_rows("regions", ["a"])
            got = [(op.widget_id, op.row_key) for m in box.drain() for op in m.panel_patch.ops]
        self.assertEqual(got, [("regions", ""), ("others", ""), ("rname", "a"), ("oname", "b")])

    def test_the_declaration(self):
        from vizi_plugin_sdk import widgets as w

        raw = {
            "manifest_version": 1,
            "id": "org.example.x",
            "name": "X",
            "version": "1",
            "engines": {"vizi": "1.0"},
            "entry": {"command": ["python3"]},
            "contributions": {
                "commands": [{"id": "set", "title": "Set"}],
                "panels": [
                    {
                        "id": "p",
                        "title": "P",
                        "root": w.list_rows(
                            "cells",
                            [w.label("name"), w.checkbox("on", command="set")],
                            selectable=True,
                            command="set",
                            max_rows=10,
                        ),
                    }
                ],
            },
        }
        panel = s._manifest.parse_manifest(raw).panels[0]
        self.assertEqual((panel.root.kind, panel.root.max_rows), ("list", 10))
        self.assertEqual(panel.widget_count(), 1 + 2 * 10, "each cell counted per possible row")
        raw["contributions"]["panels"][0]["root"] = w.dropdown("d", ["a"], command="set") | {
            "selectable": True
        }
        with self.assertRaises(s._manifest.ManifestError):
            s._manifest.parse_manifest(raw)


class SetViewDeclarations(unittest.TestCase):
    """The manifest half: `view_targets` per panel and tool, `moves_view` only
    on interactive widgets and only where the panel declares targets."""

    def _manifest(self, root, tools=(), **panel):
        return {
            "manifest_version": 1,
            "id": "org.example.x",
            "name": "X",
            "version": "1",
            "engines": {"vizi": "1.0"},
            "entry": {"command": ["python3"]},
            "contributions": {
                "commands": [{"id": "set", "title": "Set"}],
                "panels": [{"id": "p", "title": "P", "root": root, **panel}],
                "tools": list(tools),
            },
        }

    def test_the_declarations_parse(self):
        from vizi_plugin_sdk import widgets as w

        pick = {"id": "pick", "mode": "fire", "view_targets": ["t", "look_at"]}
        m = s._manifest.parse_manifest(
            self._manifest(
                w.image("trace", command="set", moves_view=True), [pick], view_targets=["t"]
            )
        )
        self.assertEqual(m.panels[0].view_targets, ("t",))
        self.assertTrue(m.panels[0].root.moves_view)
        self.assertEqual(m.tools[0].view_targets, ("t", "look_at"))

    def test_the_rules(self):
        for root, panel in (
            ({"kind": "label", "id": "l", "moves_view": True}, {"view_targets": ["t"]}),
            ({"kind": "image", "id": "i", "moves_view": True}, {"view_targets": ["t"]}),
            ({"kind": "button", "id": "b", "command": "set", "moves_view": True}, {}),
            ({"kind": "label", "id": "l"}, {"view_targets": ["t", "t"]}),
            (
                {
                    "kind": "dropdown",
                    "id": "d",
                    "command": "set",
                    "options": ["a"],
                    "filter": "mine",
                },
                {},
            ),
        ):
            with self.subTest(root=root, panel=panel), self.assertRaises(s._manifest.ManifestError):
                s._manifest.parse_manifest(self._manifest(root, **panel))
        # The move-the-view row lives on a panel, so a view-moving tool needs one.
        pick = {"id": "pick", "mode": "fire", "view_targets": ["t"]}
        with self.assertRaisesRegex(s._manifest.ManifestError, "no panel does"):
            s._manifest.parse_manifest(self._manifest({"kind": "label", "id": "l"}, [pick]))


class DeferredReportsGoOutUnasked(_Fast):
    """A repeat the rate defers is sent at its deadline even when
    no further traffic arrives to prompt a pass."""

    def test_a_repeat_inside_the_interval_is_sent_on_a_quiet_session(self):
        from vizi_plugin_sdk.violations import ViolationReporter

        reporter = ViolationReporter(interval=0.1, rate=100.0)
        self.servicer._control.reporter = reporter
        self.servicer._reporter = reporter
        reporter.report("stage_arm_refused", "first")
        self.servicer._pump()
        reporter.report("stage_arm_refused", "second")
        self.servicer._pump()  # the last pass this session will get
        deadline = time.monotonic() + 5
        repeats = []
        while time.monotonic() < deadline and not repeats:
            repeats = [
                m.contract_violation
                for k, m in self.drained()
                if k == "contract_violation"
                and m.contract_violation.count == 1
                and m.contract_violation.detail == "second"
            ]
            time.sleep(0.02)
        self.assertTrue(repeats, "the deferred repeat never went out")


class FollowDriving(_Fast):
    """Follow (§8.15): the person starts it, the
    plugin's `@follow` drives it on a thread of its own with rising `seq`, and
    it ends on the host's word, the plugin's `end()`, or the handler returning."""

    def setUp(self):
        super().setUp()
        saved = dict(s._FOLLOWS)
        self.addCleanup(lambda: (s._FOLLOWS.clear(), s._FOLLOWS.update(saved)))
        self.log = []

    def start(self, follow_id=7, panel="p", targets=("t", "frame_region"), resumed=False):
        self.servicer._route_one(
            pb2.HostMsg(
                follow_started=pb2.FollowStarted(
                    follow_id=follow_id, panel_id=panel, targets=list(targets), resumed=resumed
                )
            )
        )

    def wait_for(self, kind):
        """Drain until ``kind`` goes out; everything drained is kept in ``log``."""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.log += self.drained()
            got = [getattr(m, kind) for k, m in self.log if k == kind]
            if got:
                return got
            time.sleep(0.01)
        self.fail(f"no {kind} after 5 s")

    def test_a_tour_streams_states_and_returning_ends_it(self):
        def tour(ctx, f):
            self.assertEqual((f.id, f.targets, f.resumed), (7, ("t", "frame_region"), True))
            f.move(t=3)
            f.move(frame_region="r1")

        s.follow("p")(tour)
        self.start(resumed=True)
        (ended,) = self.wait_for("end_follow")
        self.assertEqual(ended.follow_id, 7)
        states = [
            (m.follow_state.seq, m.follow_state.t) for k, m in self.log if k == "follow_state"
        ]
        self.assertEqual(states[-1][0], 2)  # the latest wins; an earlier one may have merged

    def test_the_hosts_end_stops_the_handler_and_sends_nothing_more(self):
        moved = threading.Event()
        result = queue.Queue()

        def tour(ctx, f):
            f.move(t=1)
            moved.set()
            result.put((f.wait(5.0), f.reason, f.detail, f.move(t=2)))

        s.follow("p")(tour)
        self.start()
        moved.wait(5)
        self.servicer._route_one(
            pb2.HostMsg(
                follow_ended=pb2.FollowEnded(
                    follow_id=7,
                    reason=pb2.FOLLOW_END_USER_TOOK_CONTROL,
                    detail="you took control",
                )
            )
        )
        self.assertEqual(
            result.get(timeout=5), (False, "USER_TOOK_CONTROL", "you took control", False)
        )
        time.sleep(0.05)
        kinds = [k for k, _ in self.drained()]
        self.assertEqual(kinds.count("follow_state"), 1)
        self.assertNotIn("end_follow", kinds)  # the host ended it; nothing to say

    def test_no_handler_ends_it_at_once(self):
        self.start(panel="nobody")
        (ended,) = self.wait_for("end_follow")
        self.assertEqual(ended.follow_id, 7)


class FollowState(unittest.TestCase):
    """`Follow.move` builds whole state with a rising `seq`, and refuses a
    target the Follow does not drive before anything is sent."""

    def test_states(self):
        sent = []
        f = s.Follow(4, "p", ("t", "look_at"), False, sent.append)
        self.assertTrue(f.move(t=2))
        self.assertTrue(f.move(look_at=(1, 2, 3), look_fit=10))
        with self.assertRaises(ValueError):
            f.move(z=1)
        with self.assertRaises(ValueError):
            f.move()
        a, b = (m.follow_state for m in sent)
        self.assertEqual((a.follow_id, a.seq, a.t), (4, 1, 2))
        self.assertFalse(a.HasField("z"))
        self.assertEqual(
            (b.seq, list(b.look_at.target_zyx), b.look_at.fit_extent), (2, [1, 2, 3], 10)
        )
        f.end()
        f.end()
        self.assertEqual([m.WhichOneof("msg") for m in sent[2:]], ["end_follow"])
        self.assertFalse(f.move(t=3))
        self.assertEqual(f.reason, "PLUGIN_ENDED")

    def test_the_offer(self):
        sent = []
        panel = s.PanelHandle("p", sent.append, None)
        panel.offer_follow("flagged cells")
        panel.withdraw_follow()
        with self.assertRaises(ValueError):
            panel.offer_follow("")
        self.assertEqual(
            [(m.offer_follow.panel_id, m.offer_follow.label) for m in sent],
            [("p", "flagged cells"), ("p", "")],
        )


class SetView(unittest.TestCase):
    """Set-view (§8.15): a request names exactly one
    cause -- a command's, or a FIRE's -- and its one answer comes back."""

    def _host(self, outcome, reason=0):
        host = s.HostClient.__new__(s.HostClient)
        sent = []

        def request(pm, timeout):
            sent.append(pm.view_request)
            return pb2.HostMsg(
                id=pm.id,
                view_request_result=pb2.ViewRequestResult(
                    cause=pm.view_request.cause, outcome=outcome, reason=reason
                ),
            )

        host._request = request
        host._seq = iter(range(10))
        return host, sent

    def test_a_command_moves_the_view_under_its_own_cause(self):
        host, sent = self._host(pb2.VIEW_OUTCOME_APPLIED)
        ctx = s.CommandContext(_panels=None, _host=host, args={}, cause=41)
        result = ctx.move_view(t=430, select_region="r7", look_at=(1, 2, 3), look_fit=20)
        self.assertTrue(result.applied)
        (req,) = sent
        self.assertEqual((req.cause, req.t, req.select_region), (41, 430, "r7"))
        self.assertEqual(list(req.look_at.target_zyx), [1.0, 2.0, 3.0])
        self.assertFalse(req.HasField("z") or req.HasField("frame_region"))

    def test_a_decline_and_a_refusal_say_what_happened(self):
        host, sent = self._host(pb2.VIEW_OUTCOME_REFUSED, pb2.VIEW_REFUSAL_UNSUPPORTED)
        ctx = s.CommandContext(_panels=None, _host=host, args={}, cause=5)
        result = ctx.decline_view()
        self.assertTrue(sent[0].decline)
        self.assertEqual((result.outcome, result.reason), ("REFUSED", "UNSUPPORTED"))

    def test_no_cause_no_move(self):
        host, sent = self._host(pb2.VIEW_OUTCOME_APPLIED)
        with self.assertRaises(ValueError):
            s.CommandContext(_panels=None, _host=host, args={}).move_view(t=1)
        with self.assertRaises(ValueError):
            host.request_view(cause=3, tool_cause=(1, 2), t=1)
        self.assertEqual(sent, [])

    def test_only_a_fire_is_a_tools_cause(self):
        host, sent = self._host(pb2.VIEW_OUTCOME_APPLIED)
        ctx = s.ToolContext(_overlays=None, _host=host)
        fire = mock.Mock(phase=db.PHASE_FIRE, tool=9, interaction=4)
        ctx.move_view(fire, frame_region="r1")
        self.assertEqual((sent[0].tool_cause.tool, sent[0].tool_cause.interaction), (9, 4))
        with self.assertRaises(ValueError):
            ctx.move_view(mock.Mock(phase=db.PHASE_DRAG, tool=9, interaction=4), t=1)


class ContradictoryChunks(_Fast):
    """A chunk that contradicts its export is a host error the
    plugin refuses, writing nothing from it."""

    def test_filtered_channels_need_a_filter_and_must_be_in_the_chunk(self):
        written = []

        @s.exporter("x")
        class _Recording:
            def begin(self, desc):
                pass

            def write(self, t, c, arr):
                written.append((t, c))

            def finish(self):
                pass

            def abort(self):
                pass

        def opened(**extra):
            reply = self.servicer._on_open_export(
                pb2.HostMsg(
                    id="e",
                    open_export=pb2.OpenExport(
                        exporter_id="x", shape_tczyx=[1, 2, 1, 1, 1], dtype=pb2.DTYPE_U8, **extra
                    ),
                )
            )
            return reply.export_opened.handle

        def chunk(handle, filtered):
            return self.servicer._on_export_chunk(
                pb2.HostMsg(
                    id="c",
                    export_chunk=pb2.ExportChunk(
                        handle=handle,
                        t=0,
                        channels=[0],
                        inline=bytes(1),
                        filtered_channels=filtered,
                    ),
                )
            )

        no_filter = chunk(opened(), [0])
        self.assertEqual(no_filter.error.kind, pb2.Error.INVALID_ARGUMENT)
        self.assertIn("names no filter", no_filter.error.message)
        stray = chunk(opened(filter=_plus_filter()), [1])
        self.assertEqual(stray.error.kind, pb2.Error.INVALID_ARGUMENT)
        self.assertIn("[1]", stray.error.message)
        self.assertEqual(written, [], "nothing from a refused chunk is written")
        self.assertTrue(chunk(opened(filter=_plus_filter()), [0]).HasField("chunk_written"))


class ClassContexts(_Fast):
    """A reader, an exporter and a tool reach
    their panel from the class, not from a handle captured off whichever
    command ran first -- a crate opened before the panel was ever shown
    reached it only at the panel's next open."""

    def patches(self):
        return [msg.panel_patch for kind, msg in self.drained() if kind == "panel_patch"]

    def test_a_reader_that_asks_for_a_context_is_constructed_with_one(self):
        class _Counting:
            def __init__(self, ctx):
                self.ctx = ctx

            def open(self, uri):
                self.ctx.panel("crate").set_value("head", uri)
                return s.ReaderInfo(shape_tczyx=(1, 1, 1, 1, 1), dtype="u8")

        class _Bare:
            def open(self, uri):
                return s.ReaderInfo(shape_tczyx=(1, 1, 1, 1, 1), dtype="u8")

        servicer = s._Servicer({"c": _Counting, "b": _Bare}, "test")
        servicer.attach_doorbell(self.doorbell)
        for reader_id in ("c", "b"):
            reply = servicer._on_open(
                pb2.HostMsg(id="o", open_reader=pb2.OpenReader(uri="a.crate", reader_id=reader_id))
            )
            self.assertEqual(reply.WhichOneof("msg"), "reader_opened", reader_id)
        self.assertIsInstance(servicer._impl, _Bare)
        patches = [
            msg.panel_patch for msg in servicer.outbox.drain() if msg.HasField("panel_patch")
        ]
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0].panel_id, "crate")
        self.assertEqual(patches[0].ops[0].set_value.s, "a.crate")

    def test_a_reader_and_an_exporter_get_the_plugins_folder_for_their_file(self):
        """A reader's context names its folder for the file being opened,
        an exporter's descriptor its folder for the SOURCE -- each ``None``
        when the host sent none."""
        seen = {}

        class _Reader:
            def __init__(self, ctx):
                seen["reader"] = ctx.file_state_dir

            def open(self, uri):
                return s.ReaderInfo(shape_tczyx=(1, 1, 1, 1, 1), dtype="u8")

        @s.exporter("x")
        class _Exporter:
            def begin(self, desc):
                seen["exporter"] = desc.file_state_dir

        self.addCleanup(s._EXPORTERS.pop, "x", None)
        servicer = s._Servicer({"r": _Reader}, "test")
        servicer.attach_doorbell(self.doorbell)
        for folder, expected in (
            ("/d/a.tif.vizi/plugins/p", Path("/d/a.tif.vizi/plugins/p")),
            ("", None),
        ):
            servicer._on_open(
                pb2.HostMsg(
                    id="o",
                    open_reader=pb2.OpenReader(uri="a.tif", reader_id="r", file_state_dir=folder),
                )
            )
            self.assertEqual(seen["reader"], expected)
            servicer._on_open_export(
                pb2.HostMsg(
                    id="e",
                    open_export=pb2.OpenExport(
                        exporter_id="x",
                        shape_tczyx=[1] * 5,
                        dtype=pb2.DTYPE_U8,
                        file_state_dir=folder,
                    ),
                )
            )
            self.assertEqual(seen["exporter"], expected)

    def test_an_exporter_reports_its_outcome_on_the_panel_from_finish(self):
        outcomes: list[str] = []

        @s.exporter("x")
        class _Reporting:
            def __init__(self, ctx: s.PluginContext):
                self.ctx = ctx
                outcomes.append(type(ctx.host).__name__)

            def begin(self, desc):
                pass

            def finish(self):
                self.ctx.panel("export").set_value("status", "done")

        self.addCleanup(s._EXPORTERS.pop, "x", None)
        opened = self.servicer._on_open_export(
            pb2.HostMsg(
                id="e",
                open_export=pb2.OpenExport(
                    exporter_id="x", shape_tczyx=[1] * 5, dtype=pb2.DTYPE_U8
                ),
            )
        )
        self.assertEqual(outcomes, ["HostClient"])
        self.servicer._on_finish_export(
            pb2.HostMsg(id="f", finish_export=pb2.FinishExport(handle=opened.export_opened.handle))
        )
        patches = self.patches()
        self.assertEqual(
            [(p.panel_id, p.ops[0].set_value.s) for p in patches], [("export", "done")]
        )

    def test_a_tool_says_it_is_active_on_its_panel(self):
        @s.tool("lasso")
        class _Lasso:
            def activated(self, ctx):
                ctx.panel("seg").set_value("status", "Lasso active")

            def deactivated(self, ctx):
                ctx.panel("seg").set_value("status", "")

        self.addCleanup(s._TOOLS.pop, "lasso", None)
        seen: list[str] = []
        for retired, revision in ((False, 1), (True, 2)):
            self.servicer._control.receive(
                pb2.HostMsg(
                    tool_binding=pb2.ToolBinding(
                        tool=3, tool_id="lasso", retired=retired, revision=revision
                    )
                )
            )
            # Drained per step: two patches to one widget coalesce (§3.2).
            seen += [p.ops[0].set_value.s for p in self.patches()]
        self.assertEqual(seen, ["Lasso active", ""])


class ArenaLookup(_Fast):
    """§3 since 1.0: **at most one plugin-produced LOANED region per segment**,
    so the arena and the segment coincide -- and at `1.0`, `subject` says which
    contribution the segment is that of, so the lookup is
    ``(purpose, subject)`` and still never a region key."""

    def test_the_arena_is_found_by_the_segment_and_never_by_a_region_id(self):
        """§8's table: a receiver IGNORES `id`, it is not required to be
        unique, and it is never load-bearing.  A rule needing it present and
        authoritative is a change to `id`'s specification wearing the clothes
        of a convention -- so an arena with a misleading `id` is still found,
        because the SEGMENT's `subject` is what names the contribution."""
        self.grant(
            20,
            [m.Region(0, 32, 4096, m.Discipline.LOANED, m.Producer.PLUGIN, id="not-a-panel")],
            purpose=m.PURPOSE_PANEL,
            subject="plots",
        )
        self.servicer.outbox.drain()
        self.servicer._panel("plots").push_image("figure", b"pixels")
        kind, msg = self.drained()[0]
        self.assertEqual(kind, "panel_patch")
        self.assertEqual(msg.panel_patch.ops[0].set_image.segment_id, 20)

    def test_two_panels_are_two_segments_and_each_reserves_from_its_own(self):
        """§3: `subject` is required on `P` for the reason it is required on
        `O` -- the segment IS the contribution's, and two are otherwise
        indistinguishable.  So two panels do not share an arena; they are
        granted one each."""
        left = self.grant(
            21,
            [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_PANEL,
            subject="left",
        )
        right = self.grant(
            28,
            [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_PANEL,
            subject="right",
        )
        self.servicer._panel("left").push_image("figure", b"aaa")
        self.servicer._panel("right").push_image("figure", b"bbb")
        self.servicer.outbox.drain()
        self.assertEqual(left.loaned(0).outstanding, 1)
        self.assertEqual(right.loaned(0).outstanding, 1)

    def test_a_panel_with_no_segment_of_its_own_is_not_served_by_anothers(self):
        """The failure a shared-by-purpose lookup would hide: a push landing in
        the arena of a panel that happens to hold one."""
        self.grant(
            29,
            [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_PANEL,
            subject="left",
        )
        self.servicer.outbox.drain()
        right = self.servicer._panel("right")
        right.push_image("figure", b"pixels")
        self.assertEqual(self.drained(), [], "held for its own panel, not sent into another's")
        self.assertEqual(list(right._held_images), [("figure", "")])

    def test_two_segments_of_one_subject_are_refused_rather_than_guessed(self):
        """A key naming two live segments is a host that granted a replacement
        without retiring the incumbent, which `supersedes` exists to avoid.
        Picking one would make the wrong choice silently."""
        self.grant(
            22,
            [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_PANEL,
            subject="plots",
        )
        self.grant(
            23,
            [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_PANEL,
            subject="plots",
        )
        self.servicer.outbox.drain()
        with self.assertRaises(s.NoRegion):
            self.servicer._panel("plots").push_image("figure", b"pixels")

    def test_a_segment_declaring_two_arenas_never_installs(self):
        """§3 makes the ambiguity impossible to GRANT, which removes the lookup
        instead of strengthening it -- and fails where a host bug is cheapest
        to find."""
        fd = make_object(1 << 16, 24)
        self.servicer.on_doorbell(
            frame(
                db.MemoryGrant(24, 1 << 16, PERM_RW, 0, None),
                descriptor=db.Descriptor(fd),
                raw=b"\x18" * 112,
            )
        )
        self.servicer._control.receive(
            pb2.HostMsg(
                install_segment=pb2.InstallSegment(
                    segment_id=24,
                    purpose=m.PURPOSE_PANEL,
                    subject="plots",
                    regions=[
                        _wire_region(loaned(0, 32, 512, producer=m.Producer.PLUGIN)),
                        _wire_region(loaned(1, 32 + 512, 512, producer=m.Producer.PLUGIN)),
                    ],
                )
            )
        )
        ack = [msg for kind, msg in self.drained() if kind == "grant_ack"][0]
        self.assertEqual(ack.grant_ack.WhichOneof("outcome"), "failed")
        self.assertEqual(ack.grant_ack.failed.failure_class, pb2.GRANT_FAILURE_MALFORMED)


class ExportChunks(_Fast):
    """§8.12: the ride-along is applied before the handler runs, and the
    request's completion is a disposition like any other."""

    def setUp(self) -> None:
        super().setUp()
        self.written: list = []
        #: A second descriptor per segment, kept open so the test can write
        #: host bytes: the plugin's mapping of a host-produced region is
        #: read-only, which is what such a grant looks like.
        self._fds: dict[int, int] = {}
        test = self

        @s.exporter("tiff")
        class _Exporter:
            def begin(self, desc):
                test.written.append(("begin", desc.shape_tczyx))
                test.descriptor = desc

            def write(self, t, c, arr):
                test.written.append(("write", t, c, bytes(arr.tobytes())))

            def finish(self):
                test.written.append(("finish",))

        self.addCleanup(s._EXPORTERS.pop, "tiff", None)

    def open_export(self, **calibration):
        reply = self.servicer._on_open_export(
            pb2.HostMsg(
                id="r0",
                open_export=pb2.OpenExport(
                    exporter_id="tiff",
                    locator="/tmp/out.tif",
                    shape_tczyx=[1, 1, 1, 2, 2],
                    dtype=pb2.DTYPE_U8,
                    **calibration,
                ),
            )
        )
        return reply.export_opened.handle

    def test_an_exporter_is_handed_what_the_host_knows_and_nothing_it_does_not(self):
        """§8.3: the calibration rides `OpenExport`; an
        unknown axis is None on the descriptor, never 1.0."""
        self.open_export(spacing_z=2.0, spacing_y=0.3, spacing_unit="um", time_increment=0.5)
        desc = self.descriptor
        self.assertEqual(desc.spacing_zyx, (2.0, 0.3, None))
        self.assertEqual(desc.spacing_unit, "um")
        self.assertEqual((desc.time_increment, desc.time_unit), (0.5, ""))
        self.open_export()
        self.assertIsNone(self.descriptor.spacing_zyx)
        self.assertIsNone(self.descriptor.time_increment)

    def test_an_exporter_is_told_what_the_artifact_came_from(self):
        """The source's document -- provenance, not the artifact's
        description -- the artifact's channels with whose names they are, and
        vizi's record, parsed.  A record that is not a JSON object is none."""
        self.open_export(
            source_metadata=pb2.MetadataDoc(form="ome-ngff", text="{}"),
            channels=[pb2.ChannelInfo(index=2, name="GCaMP", source=1)],
            provenance='{"manifest_version": 1}',
        )
        desc = self.descriptor
        self.assertEqual((desc.source_metadata_form, desc.source_metadata_text), ("ome-ngff", "{}"))
        self.assertEqual(desc.channels, (s.ChannelName(2, "GCaMP", "FILE"),))
        self.assertEqual(desc.provenance, {"manifest_version": 1})
        for raw in ("", "[1, 2]", "{not json"):
            self.open_export(provenance=raw)
            self.assertIsNone(self.descriptor.provenance)
        self.assertEqual(self.descriptor.source_metadata_form, "")

    def test_a_chunk_reads_its_slab_out_of_the_host_segment(self):
        segment = self.grant(
            13,
            [loaned(0, 32, 4096, producer=m.Producer.HOST)],
            perms=m.PERM_PLUGIN_READ,
        )
        self.servicer.outbox.drain()
        handle = self.open_export()
        payload = bytes([1, 2, 3, 4])
        # `os.pwrite` is unix-only; the position is the fd's own here.
        fd = self._fds[segment.segment_id]
        os.lseek(fd, 64, os.SEEK_SET)
        os.write(fd, payload)
        hm = pb2.HostMsg(
            id="c1",
            export_chunk=pb2.ExportChunk(
                handle=handle,
                t=0,
                channels=[0],
                segment=pb2.SegmentRef(
                    segment_id=13, offset=64, len=4, shape_zyx=[1, 2, 2], dtype=pb2.DTYPE_U8
                ),
            ),
        )
        reply = self.servicer._on_export_chunk(hm)
        self.assertEqual(reply.WhichOneof("msg"), "chunk_written")
        self.assertEqual(self.written[-1], ("write", 0, 0, payload))
        # §5.6: finishing the request disposes of its descriptor, and the
        # release names the region resolved from our own table.
        self.assertTrue(self.servicer._control.finish_request(hm))
        release = [msg for kind, msg in self.drained() if kind == "release_deposit"][0]
        self.assertEqual((release.release_deposit.region, release.release_deposit.offset), (0, 64))

    def test_two_uses_counts_for_one_reservation_are_reported(self):
        """§5.6: `uses` is a property of the RESERVATION, so every message
        naming one carries the same count.  The receiver applies the count it
        was FIRST given -- adopting the larger accepts that the sender does not
        know its own set, which is the one thing `uses` exists to assert -- and
        **reports** it, `uses_disagreement`, site 24.  Both SDKs applied the
        first count and kept the reason to themselves until the site existed."""
        self.grant(17, [loaned(0, 32, 4096, producer=m.Producer.HOST)], perms=m.PERM_PLUGIN_READ)
        self.servicer.outbox.drain()
        handle = self.open_export()

        def chunk(uses):
            return pb2.HostMsg(
                id=f"c{uses}",
                export_chunk=pb2.ExportChunk(
                    handle=handle,
                    t=0,
                    channels=[0],
                    segment=pb2.SegmentRef(
                        segment_id=17,
                        offset=64,
                        len=4,
                        shape_zyx=[1, 2, 2],
                        dtype=pb2.DTYPE_U8,
                        uses=uses,
                    ),
                ),
            )

        self.servicer._on_export_chunk(chunk(2))
        self.assertEqual(
            [k for k, _ in self.drained() if k == "contract_violation"],
            [],
            "one declaration is not a disagreement",
        )
        self.servicer._on_export_chunk(chunk(3))
        reports = [m for k, m in self.drained() if k == "contract_violation"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].contract_violation.where, pb2.VIOLATION_SITE_USES_DISAGREEMENT)
        self.assertIn("applying 2", reports[0].contract_violation.detail)

    def grant(self, segment_id, regions, *, size=1 << 16, perms=PERM_RW, purpose="V", subject=""):
        fd = make_object(size, segment_id)
        keep = os.dup(fd)
        self._fds[segment_id] = keep
        self.addCleanup(os.close, keep)
        self.servicer.on_doorbell(
            frame(
                db.MemoryGrant(segment_id, size, perms, 0, None),
                descriptor=db.Descriptor(fd),
                raw=bytes([segment_id & 0xFF]) * 112,
            )
        )
        self.servicer._control.receive(
            pb2.HostMsg(
                install_segment=pb2.InstallSegment(
                    segment_id=segment_id,
                    purpose=purpose,
                    subject=subject,
                    regions=[_wire_region(r) for r in regions],
                )
            )
        )
        segment = self.servicer._memory.segment(segment_id)
        self.assertIsNotNone(segment, "the grant did not complete")
        return segment


class ThePump(_Fast):
    """§11a names the dispatch loop as an acceptable mechanism for the periodic
    work a session owes.  Neither piece has a timer of its own."""

    def test_an_aggregated_repeat_reaches_the_wire_on_a_later_pass(self):
        """§2: the first occurrence of a `where` carries the record and goes
        out at once; the rest are counted and re-reported on the interval.  A
        repeat the rate defers stays pending and comes out later, which is what
        makes the bound a rate rather than a loss."""
        # The interval and the rate are SDK policy, not wire constants: §2
        # requires a bound and leaves its size to the implementation.  A test
        # that waited out the shipping 1 s interval and 10/s rate would be
        # testing `time.monotonic`.
        reporter = self.servicer._reporter
        # A clock the test advances, continued from the real one's last
        # reading: the three violations land at one instant (so two fold
        # behind the first), and the test then moves time past the interval,
        # which refills the rate's bucket too.  A real clock left the refill
        # to how fast the box ran the lines between -- under a microsecond on
        # Windows with 3.13's high-resolution `monotonic`, and the repeat
        # stayed pending.
        now = [reporter._last_refill]
        reporter._clock = lambda: now[0]
        self.grant(16, [m.Region(0, 32, 64, m.Discipline.OWNED, m.Producer.HOST)])
        self.servicer.outbox.drain()
        for i in range(3):
            self.servicer.on_doorbell(
                frame(
                    db.Notification(segment_id=16, mask=0b1, epoch=0, view_id=0),
                    raw=bytes([i]) * 112,
                )
            )
        first = self.drained()
        self.assertEqual([kind for kind, _ in first], ["contract_violation"])
        self.assertEqual(first[0][1].contract_violation.count, 1, "the first occurrence")
        self.assertEqual(first[0][1].contract_violation.record, bytes(112), "and it carries it")
        self.assertEqual(reporter.pending, 2, "the other two are counted, not lost")
        now[0] += reporter.interval + 1.0  # the next interval, arrived
        self.servicer._pump()
        repeat = self.drained()
        self.assertEqual([kind for kind, _ in repeat], ["contract_violation"])
        self.assertEqual(repeat[0][1].contract_violation.count, 2, "the two folded into it")
        self.assertEqual(repeat[0][1].contract_violation.record, b"", "a repeat carries none")

    def test_a_retirement_held_by_a_claim_settles_when_the_claim_does(self):
        """§9.1: `RetireSegment` permits FINISHING, so `SegmentReleased` cannot
        go out inside it -- it goes out when the held claim finishes, and the
        loop is where a supervisor asks."""
        segment = self.grant(15, [pulsed(0, 32)], purpose=m.PURPOSE_OVERLAY, subject="cells")
        self.servicer.outbox.drain()
        writer = segment.pulsed_writer(0)
        writer.publish(b"a value")
        reader = segment.pulsed_reader(0)
        # Claim without finishing: `_claims` is what §9.1 waits on.
        segment._claimed(1)
        self.servicer._control.receive(pb2.HostMsg(retire_segment=pb2.RetireSegment(segment_id=15)))
        self.assertEqual(self.kinds(), [], "not released while a claim is held")
        self.assertTrue(segment.retiring)
        self.servicer._pump()
        self.assertEqual(self.kinds(), [], "and still not")
        segment._claimed(-1)
        # The last claim ending settles the retirement itself: no
        # further message, and no `_pump` from the test, is needed -- and it
        # goes out exactly once, however the passes interleave.
        deadline = time.monotonic() + 5
        kinds = []
        while time.monotonic() < deadline and "segment_released" not in kinds:
            kinds += self.kinds()
            time.sleep(0.01)
        self.servicer._pump()
        kinds += self.kinds()
        self.assertEqual(kinds, ["segment_released"])
        self.assertIsNotNone(reader)


class RetirementWaitsForWorkInFlight(_Fast):
    """§9.1: a loaned reservation, from `reserve` through the
    message that names it, and a deposit read are FINISHING -- a retirement
    waits for them, and a normal retirement produces no error anywhere."""

    def overlay_regions(self):
        return [
            pulsed(0, 32),
            loaned(1, 32 + 3 * (16 + 512), 8192, producer=m.Producer.PLUGIN),
        ]

    def retire(self, segment_id):
        self.servicer._control.receive(
            pb2.HostMsg(retire_segment=pb2.RetireSegment(segment_id=segment_id))
        )

    def until_released(self, seen):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "segment_released" not in seen:
            seen += self.kinds()
            time.sleep(0.01)
        return seen

    def test_a_reservation_not_yet_sent_holds_the_release_until_its_message_goes(self):
        self.grant(20, self.overlay_regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        self.servicer.outbox.drain()
        overlay = self.servicer._overlay("cells")
        item = overlay.marker_item("m", [[1.0, 2.0, 3.0]])  # reserved, not yet named
        self.retire(20)
        self.servicer._pump()
        self.assertEqual(self.kinds(), [], "not released under a reservation in flight")
        overlay.set_items([item])
        self.assertEqual(
            self.until_released([]),
            ["overlay_update", "segment_released"],
            "the message naming it goes first, then the release -- and no error",
        )

    def test_a_write_parked_mid_copy_is_not_unmapped_under_it(self):
        """The retirement arrives while a copy is under way.  Handling it waits
        for the copy (a real one takes microseconds), the mapping is still
        there when the copy resumes, and the release follows the message."""
        segment = self.grant(21, self.overlay_regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        self.servicer.outbox.drain()
        overlay = self.servicer._overlay("cells")
        parked, go = threading.Event(), threading.Event()
        mapped_on_resume = []
        write_at = segment._write_at

        def slow_write(offset, payload):
            parked.set()
            go.wait(5)
            mapped_on_resume.append(not segment._mm.closed)
            write_at(offset, payload)

        segment._write_at = slow_write
        failures = []

        def push():
            try:
                overlay.set_items([overlay.marker_item("m", [[1.0, 2.0, 3.0]])])
            except Exception as exc:  # noqa: BLE001 -- the test is that none happens
                failures.append(exc)

        pusher = threading.Thread(target=push)
        pusher.start()
        self.assertTrue(parked.wait(5))
        threading.Timer(0.2, go.set).start()
        self.retire(21)
        pusher.join(5)
        self.assertEqual(failures, [])
        self.assertEqual(mapped_on_resume, [True], "not unmapped under the copy")
        self.assertEqual(self.until_released([]), ["overlay_update", "segment_released"])

    def test_a_panel_push_after_its_segment_retired_is_held_quietly(self):
        self.grant(
            22,
            [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_PANEL,
            subject="plots",
        )
        self.servicer.outbox.drain()
        self.retire(22)
        self.assertEqual(self.until_released([]), ["segment_released"])
        panel = self.servicer._panel("plots")
        panel.push_image("figure", b"after")  # nowhere to go: kept, not an error
        self.assertEqual(self.kinds(), [])
        self.grant(
            23,
            [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_PANEL,
            subject="plots",
        )
        self.servicer._invoke_pool.submit(lambda: None).result(timeout=5)  # the install's flush
        ops = [op for k, msg in self.drained() if k == "panel_patch" for op in msg.panel_patch.ops]
        self.assertEqual([op.set_image.segment_id for op in ops], [23], "into the successor")

    def test_a_release_loan_crossing_the_segments_release_is_not_a_fault(self):
        """The host returns a loan while this end's
        `SegmentReleased` is on its way.  Released is told apart from
        never-installed -- ids are never reused -- and only the second is
        `unknown_release` -- and a crossing must name a loan that was out at
        the release, once: a duplicate or a loan that never was is the host's
        bug, which counting silently would hide."""
        segment = self.grant(25, self.overlay_regions(), purpose=m.PURPOSE_OVERLAY, subject="cells")
        self.servicer.outbox.drain()
        overlay = self.servicer._overlay("cells")
        overlay.set_items([overlay.marker_item("m", [[1.0, 2.0, 3.0]])])
        self.servicer.outbox.drain()
        arena = segment.loaned_unchecked(1)
        ((inside, length),) = list(arena._outstanding)
        self.retire(25)
        self.assertEqual(self.until_released([]), ["segment_released"])
        crossing = pb2.ReleaseLoan(
            segment_id=25, region=1, offset=32 + 3 * (16 + 512) + inside, len=length
        )
        routing = self.servicer._control.receive(pb2.HostMsg(release_loan=crossing))
        self.assertEqual(routing.violations, ())
        self.assertEqual(self.kinds(), [], "counted, not reported")
        self.assertEqual(self.servicer._control.crossed_releases, 1)
        again = self.servicer._control.receive(pb2.HostMsg(release_loan=crossing))
        self.assertEqual([where for where, _ in again.violations], ["unknown_release"])
        self.assertEqual(self.servicer._control.crossed_releases, 1)
        never = pb2.ReleaseLoan(segment_id=99, region=1, offset=0, len=24)
        routing = self.servicer._control.receive(pb2.HostMsg(release_loan=never))
        self.assertEqual([where for where, _ in routing.violations], ["unknown_release"])

    def test_a_deposit_read_already_begun_finishes_on_a_retiring_segment(self):
        segment = self.grant(24, [loaned(0, 32, 4096, producer=m.Producer.HOST)])
        segment._write_at(32, b"deposit")
        segment._retiring = True
        with self.assertRaises(m.Retiring):
            segment.deposits(0).take(32, 7)  # starting one: still no
        segment._claimed(1)
        try:
            self.assertEqual(segment._deposits_unchecked(0).take(32, 7, held=True), b"deposit")
        finally:
            segment._claimed(-1)


class ReaderReplies(_Fast):
    """A reader's answer is a descriptor into the arena granted for it."""

    def test_the_reply_names_the_reader_arena(self):
        import numpy as np

        segment = self.grant(
            14, [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)], purpose=m.PURPOSE_READER
        )
        self.servicer.outbox.drain()
        reply = self.servicer._reply_with_array(
            "r1", np.arange(4, dtype=np.uint8).reshape(1, 2, 2), (1, 2, 2)
        )
        self.assertEqual(reply.WhichOneof("msg"), "segment")
        self.assertEqual((reply.segment.segment_id, reply.segment.len), (14, 4))
        self.assertEqual(segment.loaned(0).outstanding, 1)

    def test_a_reply_that_disagrees_with_ReaderOpened_is_refused_before_it_reserves(self):
        """The wrong shape reached the host as a silently wrong
        array, and a dtype the wire has no word for raised AFTER the
        reservation, which then stayed taken -- every later read "arena is
        full".  The same values in the other byte order are converted."""
        import numpy as np

        segment = self.grant(
            15, [loaned(0, 32, 4096, producer=m.Producer.PLUGIN)], purpose=m.PURPOSE_READER
        )
        self.servicer.outbox.drain()
        self.servicer._reader_dtype = np.dtype(np.uint16)
        for bad in (np.zeros((1, 2, 2), ">i4"), np.zeros((1, 3, 2), np.uint16)):
            reply = self.servicer._reply_with_array("r", bad, (1, 2, 2))
            self.assertEqual(reply.WhichOneof("msg"), "error")
        self.assertEqual(segment.loaned(0).outstanding, 0, "nothing was reserved")
        swapped = np.arange(4, dtype=">u2").reshape(1, 2, 2)
        reply = self.servicer._reply_with_array("r", swapped, (1, 2, 2))
        self.assertEqual(reply.WhichOneof("msg"), "segment")
        self.assertEqual(reply.segment.dtype, pb2.DTYPE_U16)

    def test_no_arena_is_an_error_reply_rather_than_a_crash(self):
        import numpy as np

        reply = self.servicer._reply_with_array(
            "r1", np.arange(4, dtype=np.uint8).reshape(1, 2, 2), (1, 2, 2)
        )
        self.assertEqual(reply.WhichOneof("msg"), "error")


class ChunkedOverlays(_Fast):
    """§5.7: a `@chunk_source` answers the host's wants, one volume at a time,
    and a publish the arena cannot hold waits for a release."""

    ARENA = 4096

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(s._CHUNK_SOURCES.clear)
        self.calls: list = []
        self.answers: dict = {}

        @s.chunk_source("labels")
        def source(ctx, want):
            self.calls.append(want)
            return self.answers.get(want.key, np.ones((2, 2, 2), np.uint8))

        self.segment = self.grant(
            40,
            [pulsed(0, 32), loaned(1, 32 + 3 * (16 + 512), self.ARENA, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_OVERLAY,
            subject="labels",
        )
        self.servicer.outbox.drain()

    def want(self, *pairs):
        wants = []
        for level, t in pairs:
            w = pb2.ChunkWant(level=level)
            if t is not None:
                w.t = t
            wants.append(w)
        self.servicer._control.receive(
            pb2.HostMsg(chunks_wanted=pb2.ChunksWanted(overlay_id="labels", wants=wants))
        )

    def chunks(self, n: int, timeout: float = 3.0) -> list:
        """The next ``n`` `OverlayChunk`s the session sends, in order."""
        out = []
        deadline = time.monotonic() + timeout
        while len(out) < n and time.monotonic() < deadline:
            for msg in self.servicer.outbox.drain():
                if msg.WhichOneof("msg") == "overlay_chunk":
                    out.append(msg.overlay_chunk)
            time.sleep(0.005)
        return out

    def release(self, chunk) -> None:
        ref = chunk.labels.segment
        self.servicer._control.receive(
            pb2.HostMsg(
                release_loan=pb2.ReleaseLoan(
                    segment_id=ref.segment_id, region=1, offset=ref.offset, len=ref.len
                )
            )
        )

    def test_the_source_is_asked_in_the_hosts_order_and_the_chunks_are_segment_backed(self):
        self.answers[(1, 4)] = s.Chunk(np.full((1, 1, 1), 7, np.uint16), position_zyx=(1, 2, 3))
        self.want((0, 4), (1, 4))
        first, second = self.chunks(2)
        self.assertEqual([w.key for w in self.calls], [(0, 4), (1, 4)])
        self.assertEqual((first.level, first.t), (0, 4))
        self.assertEqual(first.labels.WhichOneof("source"), "segment")
        self.assertEqual(first.labels.segment.segment_id, 40)
        self.assertEqual(list(first.labels.shape), [2, 2, 2])
        self.assertEqual(first.labels.dtype, pb2.DTYPE_U8)
        self.assertEqual(list(first.position_zyx), [0, 0, 0])
        self.assertEqual((second.level, second.t), (1, 4))
        self.assertEqual(list(second.position_zyx), [1, 2, 3])
        self.assertEqual(second.labels.dtype, pb2.DTYPE_U16)
        self.assertEqual(self.segment.loaned(1).outstanding, 2)
        # Nothing is served twice while the want stands.
        self.want((0, 4), (1, 4))
        time.sleep(0.1)
        self.assertEqual(len(self.calls), 2)

    def test_a_publish_the_arena_cannot_hold_waits_for_the_release_and_is_never_dropped(self):
        big = np.ones((16, 16, 12), np.uint8)  # 3072 of a 4096-byte arena
        self.answers[(0, 1)] = big
        self.answers[(0, 2)] = big
        self.want((0, 1), (0, 2))
        (first,) = self.chunks(1)
        self.assertEqual((first.level, first.t), (0, 1))
        self.assertEqual(self.chunks(1, timeout=0.4), [], "the second waits; the arena is full")
        self.assertEqual(len(self.calls), 2, "computed once, held, not recomputed")
        self.assertEqual(self.segment.loaned(1).refused, 0, "a deferred publish is no shortfall")
        self.release(first)
        (second,) = self.chunks(1)
        self.assertEqual((second.level, second.t), (0, 2))
        self.assertEqual(len(self.calls), 2)

    def test_two_arenas_for_one_overlay_are_diagnosed_not_retried_forever(self):
        """A host that granted a replacement beside the incumbent left
        two arenas answering one overlay; the chunk server read that as "wait
        for room" and retried every 0.25 s for the rest of the session."""
        self.grant(
            41,
            [pulsed(0, 32), loaned(1, 32 + 3 * (16 + 512), self.ARENA, producer=m.Producer.PLUGIN)],
            purpose=m.PURPOSE_OVERLAY,
            subject="labels",
        )
        self.servicer.outbox.drain()
        self.want((0, 1))
        deadline = time.monotonic() + 3.0
        found = []
        while not found and time.monotonic() < deadline:
            found = [d for d in self.servicer._control.diagnostics if "carry" in d]
            time.sleep(0.02)
        self.assertTrue(found, "diagnosed")
        self.assertEqual(self.chunks(1, timeout=0.3), [])

    def test_a_want_that_leaves_the_set_is_forgotten_and_served_again_if_it_returns(self):
        big = np.ones((16, 16, 12), np.uint8)
        self.answers[(0, 1)] = big
        self.answers[(0, 2)] = big
        self.want((0, 1), (0, 2))
        (first,) = self.chunks(1)
        self.want((0, 1))  # the second want leaves while its publish is deferred
        self.release(first)
        self.assertEqual(self.chunks(1, timeout=0.3), [], "no longer wanted: not published")
        self.want((0, 1), (0, 2))
        (again,) = self.chunks(1)
        self.assertEqual((again.level, again.t), (0, 2))
        self.assertEqual([w.key for w in self.calls], [(0, 1), (0, 2), (0, 2)])

    def test_two_sets_landing_before_the_worker_wakes_both_count(self):
        """`[]` then a returning key, back to back -- a
        dataset change, or t there and back within one tick.  The served-set
        is reconciled as each set ARRIVES, so the key is served again even
        though the worker saw neither set land."""
        self.want((0, 1))
        (first,) = self.chunks(1)
        self.assertEqual((first.level, first.t), (0, 1))
        # Hold the worker off: the two sets arrive while it is parked on the
        # condition, and it wakes to the second alone.
        with self.servicer._overlay("labels")._chunk_cv:
            self.want()  # the empty set
            self.want((0, 1))
        (again,) = self.chunks(1)
        self.assertEqual((again.level, again.t), (0, 1))
        self.assertEqual([w.key for w in self.calls], [(0, 1), (0, 1)])

    def test_an_invalidate_drops_a_deferred_answer_rather_than_publishing_it_stale(self):
        """Results change while a chunk is waiting for
        arena room -- the waiting answer is the OLD result and must not go
        out under a key the invalidate is meant to re-serve."""
        big = np.ones((16, 16, 12), np.uint8)
        self.answers[(0, 1)] = big
        self.answers[(0, 2)] = big
        self.want((0, 1), (0, 2))
        (first,) = self.chunks(1)
        self.assertEqual(self.chunks(1, timeout=0.3), [], "(0, 2) is deferred")
        self.assertEqual(len(self.calls), 2)
        self.answers[(0, 1)] = np.full((1, 1, 1), 5, np.uint8)
        self.answers[(0, 2)] = np.full((1, 1, 1), 6, np.uint8)
        self.servicer._overlay("labels").invalidate_chunks()
        self.release(first)
        served = self.chunks(2)
        by_key = {(c.level, c.t): c for c in served}
        self.assertEqual(set(by_key), {(0, 1), (0, 2)})
        self.assertEqual(
            list(by_key[(0, 2)].labels.shape), [1, 1, 1], "the NEW answer, not the held one"
        )
        self.assertEqual(list(by_key[(0, 1)].labels.shape), [1, 1, 1])
        self.assertEqual(len(self.calls), 4, "both re-served from the source")

    def test_a_want_withdrawn_while_its_source_runs_is_recomputed_when_it_returns(self):
        """A dataset change restates the set `[]` and then names the same
        (level, t) for the NEXT dataset, while the source is still computing
        it for the last one.  The answer that returns is the old dataset's:
        published, it became the new dataset's segmentation and marked the
        key served, so the source was never asked again.  The key leaving the
        set retires the work in flight; its return is a fresh ask."""
        entered = threading.Event()
        proceed = threading.Event()

        @s.chunk_source("labels")
        def source(ctx, want):
            self.calls.append(want)
            entered.set()
            proceed.wait(3.0)
            return np.full((1, 1, 1), len(self.calls), np.uint8)

        self.want((0, 1))
        self.assertTrue(entered.wait(2.0))
        self.want()  # the dataset changed
        self.want((0, 1))  # the same key, for the next one
        proceed.set()
        (chunk,) = self.chunks(1)
        self.assertEqual((chunk.level, chunk.t), (0, 1))
        self.assertEqual([w.key for w in self.calls], [(0, 1), (0, 1)], "asked again")
        self.assertEqual(
            self.segment.loaned(1).outstanding, 1, "the withdrawn answer was never published"
        )
        self.assertEqual(self.chunks(1, timeout=0.3), [], "and nothing is served twice")

    def test_a_want_that_stays_through_a_restated_set_is_not_recomputed(self):
        """The mirror: t scrubbing restates the set every tick with the key
        still in it, and the work in flight is still the right ask."""
        entered = threading.Event()
        proceed = threading.Event()

        @s.chunk_source("labels")
        def source(ctx, want):
            self.calls.append(want)
            entered.set()
            proceed.wait(3.0)
            return np.ones((1, 1, 1), np.uint8)

        self.want((0, 1), (0, 2))
        self.assertTrue(entered.wait(2.0))
        self.want((0, 1), (0, 3))  # (0, 2) leaves; (0, 1), in flight, stays
        proceed.set()
        served = self.chunks(2)
        self.assertEqual({(c.level, c.t) for c in served}, {(0, 1), (0, 3)})
        self.assertEqual([w.key for w in self.calls], [(0, 1), (0, 3)], "(0, 1) computed once")

    def test_a_static_want_carries_no_t_and_an_empty_answer_is_an_empty_chunk(self):
        self.answers[(0, None)] = None
        self.want((0, None))
        (chunk,) = self.chunks(1)
        self.assertFalse(chunk.HasField("t"))
        self.assertFalse(chunk.HasField("labels"))
        self.assertEqual(self.segment.loaned(1).outstanding, 0)

    def test_the_label_table_is_inline_and_keyed_by_id(self):
        overlay = self.servicer._overlay("labels")
        lut = np.zeros((256, 4), np.uint8)
        lut[1] = (255, 0, 0, 255)
        overlay.set_label_table(
            lut, label_names={1: "soma", 256: "neurite"}, label_visible={256: False}, opacity=0.5
        )
        kind, msg = self.drained()[-1]
        self.assertEqual(kind, "overlay_label_table")
        table = msg.overlay_label_table
        self.assertEqual(table.colormap.WhichOneof("source"), "inline")
        self.assertEqual(list(table.colormap.shape), [256, 4])
        self.assertEqual(table.label_names[256], "neurite")
        self.assertFalse(table.label_visible[256])
        self.assertEqual(table.style.opacity, 0.5)
        with self.assertRaises(ValueError):
            overlay.set_label_table(np.zeros((4, 4), np.uint8))

    def test_invalidating_serves_every_current_want_again(self):
        self.want((0, 4))
        self.chunks(1)
        self.answers[(0, 4)] = np.full((1, 1, 1), 9, np.uint8)
        self.servicer._overlay("labels").invalidate_chunks()
        (again,) = self.chunks(1)
        self.assertEqual(list(again.labels.shape), [1, 1, 1])
        self.assertEqual(len(self.calls), 2)

    def test_a_source_that_raises_is_diagnosed_and_the_next_want_still_served(self):
        def bad(ctx, want):
            if want.key == (0, 1):
                raise RuntimeError("boom")
            return np.ones((1, 1, 1), np.uint8)

        s._CHUNK_SOURCES["labels"] = bad
        self.want((0, 1), (0, 2))
        (chunk,) = self.chunks(1)
        self.assertEqual((chunk.level, chunk.t), (0, 2))
        self.assertTrue(
            any("chunk source" in d and "boom" in d for d in self.servicer._control.diagnostics)
        )

    def test_without_a_source_the_wants_are_readable_and_publish_chunk_pushes_by_hand(self):
        s._CHUNK_SOURCES.clear()
        self.want((0, 4))
        overlay = self.servicer._overlay("labels")
        self.assertEqual(overlay.wants, [s.ChunkWant(0, 4)])
        self.assertTrue(any("no @chunk_source" in d for d in self.servicer._control.diagnostics))
        overlay.publish_chunk(
            s.ChunkWant(0, 4), np.ones((1, 1, 1), np.uint16), position_zyx=(0, 1, 0)
        )
        (chunk,) = self.chunks(1)
        self.assertEqual(list(chunk.position_zyx), [0, 1, 0])
        with self.assertRaises(ValueError):
            overlay.publish_chunk(s.ChunkWant(0, 4), np.ones((1, 1, 1), np.float32))
        with self.assertRaises(s.NoRegion):
            overlay.publish_chunk(s.ChunkWant(0, 4), np.ones((32, 32, 8), np.uint8))


if __name__ == "__main__":
    unittest.main()


class PickerWaitTest(unittest.TestCase):
    """The blocking pickers wait on a person; their default is
    the ports' 30 minutes, not a request's 30 s (§8.9: an SDK MUST NOT refuse
    a command that waits on a picker)."""

    def test_the_blocking_pickers_wait_thirty_minutes_by_default(self):
        import inspect

        self.assertEqual(s.PICKER_TIMEOUT_S, 1800.0)
        for name in ("request_asset", "request_save_path"):
            method = getattr(s.HostClient, name)
            default = inspect.signature(method).parameters["timeout"].default
            self.assertEqual(default, s.PICKER_TIMEOUT_S, name)


class RawReads(unittest.TestCase):
    def test_read_volume_asks_for_raw_voxels_only_when_told(self):
        """`read_volume(raw=True)` sets `ReadDataVolume.raw`."""
        import itertools

        sent = []

        class _Stop(Exception):
            pass

        def request(msg, timeout):
            sent.append(msg.read_data_volume)
            raise _Stop

        client = s.HostClient.__new__(s.HostClient)
        client._seq = itertools.count()
        client._request = request
        for raw in (False, True):
            with self.assertRaises(_Stop):
                client.read_volume(0, 1, raw=raw)
        self.assertEqual([m.raw for m in sent], [False, True])


class PerAxisPitch(unittest.TestCase):
    """A file may record some axes' pitch and not others; the host sends
    NaN for an unknown axis, and the accessors read it per axis."""

    def _state(self, spacing, unit, provenance):
        return s.HostViewState(
            t=0, z=0, c=0, level=0, shape_tczyx=(1, 1, 1, 1, 1), dtype="u16", has_crop=False,
            crop_min_zyx=None, crop_max_zyx=None, mip=False, levels=(), dataset_uri="",
            dataset_name="", open_generation=1, spacing_zyx=spacing, spacing_unit=unit,
            spacing_provenance=provenance,
        )  # fmt: skip

    def test_partly_known(self):
        state = self._state((float("nan"), 0.32, 0.32), "um", "FILE")
        self.assertTrue(state.any_axis_known)
        self.assertEqual(state.axis_known_zyx, (False, True, True))
        self.assertEqual(state.measured_spacing_zyx, (None, 0.32, 0.32))

    def test_fully_known(self):
        state = self._state((2.0, 0.5, 0.5), "um", "OVERRIDE")
        self.assertEqual(state.axis_known_zyx, (True, True, True))
        self.assertEqual(state.measured_spacing_zyx, (2.0, 0.5, 0.5))

    def test_the_display_fallback_measures_nothing(self):
        state = self._state((3.0, 1.0, 1.0), "", "DISPLAY_FALLBACK")
        self.assertFalse(state.any_axis_known)
        self.assertEqual(state.axis_known_zyx, (False, False, False))
        self.assertEqual(state.measured_spacing_zyx, (None, None, None))

    def test_no_dataset(self):
        state = self._state(None, "", "UNSPECIFIED")
        self.assertEqual(state.measured_spacing_zyx, (None, None, None))

    def test_only_a_positive_finite_measured_axis_is_known(self):
        """Finiteness alone let 0 and -1 through as pitches, and a
        fallback that carried a unit as a measurement."""
        cases = {
            "partly recorded": (((float("nan"), 0.32, 0.32), "um", "FILE"), (False, True, True)),
            "all three": (((2.0, 0.5, 0.5), "um", "FILE"), (True, True, True)),
            "the fallback": (((3.0, 1.0, 1.0), "", "DISPLAY_FALLBACK"), (False, False, False)),
            "a fallback carrying a unit": (
                ((3.0, 1.0, 1.0), "um", "DISPLAY_FALLBACK"),
                (False, False, False),
            ),
            "FILE with no unit": (((2.0, 0.5, 0.5), "", "FILE"), (False, False, False)),
            "FILE 0, -1, inf": (((0.0, -1.0, float("inf")), "um", "FILE"), (False, False, False)),
            "UNSPECIFIED": (((2.0, 0.5, 0.5), "um", "UNSPECIFIED"), (False, False, False)),
        }
        for name, (args, known) in cases.items():
            with self.subTest(name):
                state = self._state(*args)
                self.assertEqual(state.axis_known_zyx, known)
                self.assertEqual(state.any_axis_known, any(known))
                for value, k in zip(state.measured_spacing_zyx, known):
                    if k:
                        self.assertTrue(value is not None and value > 0)
                    else:
                        self.assertIsNone(value, "nothing leaks through")


class VersionGating(_Harness):
    """§3.1: `engines.vizi` is the floor, `Ready` the negotiated ceiling.  The
    plugin reads `Hello`, sends the lower of the two minors, and gates every
    feature on the minor the session runs at."""

    def quiet(self):
        import contextlib
        import io

        ctx = contextlib.redirect_stderr(io.StringIO())
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def first(self, stream, timeout: float = 2.0):
        """``next(stream)`` on a thread, so a stream that never yields fails
        the test instead of hanging it.  ``StopIteration`` comes back as
        ``None``."""
        out: list = []

        def pull():
            try:
                out.append(next(stream))
            except StopIteration:
                out.append(None)

        thread = threading.Thread(target=pull, daemon=True)
        thread.start()
        thread.join(timeout)
        self.assertFalse(thread.is_alive(), "the stream yielded nothing")
        return out[0]

    def test_ready_waits_for_hello_and_carries_the_lower_minor(self):
        """A host newer than this SDK gets this SDK's minor; the session runs
        at it, and the host's own stays readable beside it."""
        stream = self.servicer.Session(self.feed, None)
        out: list = []
        thread = threading.Thread(target=lambda: out.append(next(stream)), daemon=True)
        thread.start()
        thread.join(0.2)
        self.assertEqual(out, [], "no Ready before Hello")
        self.feed.send(_hello(minor=s.PROTOCOL_MINOR + 3))
        thread.join(2)
        ready = out[0]
        self.assertEqual(ready.WhichOneof("msg"), "ready")
        self.assertEqual(ready.ready.protocol_major, s.PROTOCOL_MAJOR)
        self.assertEqual(ready.ready.protocol_minor, s.PROTOCOL_MINOR)
        host = self.servicer._host_client
        self.assertEqual(host.host_protocol, (s.PROTOCOL_MAJOR, s.PROTOCOL_MINOR + 3))
        self.assertEqual(host.session_protocol, (s.PROTOCOL_MAJOR, s.PROTOCOL_MINOR))

    def test_ready_never_exceeds_the_hosts_minor(self):
        """An SDK newer than the host clamps down to the host's minor."""
        with mock.patch.object(s._features, "PROTOCOL_MINOR", 2):
            stream = self.servicer.Session(self.feed, None)
            self.feed.send(_hello(minor=1))
            ready = self.first(stream)
        self.assertEqual(ready.ready.protocol_minor, 1)
        self.assertEqual(self.servicer._host_client.session_protocol, (s.PROTOCOL_MAJOR, 1))

    def test_what_was_queued_before_hello_still_leaves_after_ready(self):
        """§3.1 lets nothing precede the handshake, and the outbox drains by
        class -- so `Ready` is held aside, not queued behind a push."""
        self.servicer._emit(pb2.PluginMsg(epoch_ack=pb2.EpochAck()))
        stream = self.servicer.Session(self.feed, None)
        self.feed.send(_hello())
        self.assertEqual(self.first(stream).WhichOneof("msg"), "ready")
        self.assertEqual(self.first(stream).WhichOneof("msg"), "epoch_ack")

    def test_the_response_headers_go_out_when_the_call_opens(self):
        """§3.1: reading `Hello` first must not hold the headers back."""
        sent = threading.Event()
        context = types.SimpleNamespace(send_initial_metadata=lambda md: sent.set())
        stream = self.servicer.Session(self.feed, context)
        threading.Thread(target=lambda: next(stream, None), daemon=True).start()
        self.assertTrue(sent.wait(2), "headers before Hello")
        self.feed.close()

    def test_a_session_below_the_manifests_floor_is_refused_before_ready(self):
        """The plugin's half of the floor: no `Ready`, the session failed, and
        the sentence every SDK uses."""
        self.quiet()
        with mock.patch.object(s._features, "PROTOCOL_MINOR", 2):
            servicer = s._Servicer(None, "test", engines="1.2")
            self.addCleanup(servicer.close)
            stream = servicer.Session(self.feed, None)
            self.feed.send(_hello(minor=1))
            self.assertIsNone(self.first(stream), "the stream ends with nothing sent")
        self.assertEqual(
            servicer.handshake_error,
            "this plugin's manifest requires protocol 1.2 but its code speaks 1.1",
        )
        self.assertIsNone(servicer._host_client.session_protocol)
        self.assertFalse(servicer._host_client.supports("follow"))

    def test_a_floor_at_the_session_minor_is_met(self):
        servicer = s._Servicer(None, "test", engines=f"{s.PROTOCOL_MAJOR}.{s.PROTOCOL_MINOR}")
        self.addCleanup(servicer.close)
        stream = servicer.Session(self.feed, None)
        self.feed.send(_hello())
        self.assertEqual(self.first(stream).WhichOneof("msg"), "ready")
        self.assertIsNone(servicer.handshake_error)

    def test_a_malformed_floor_fails_at_start_up(self):
        with self.assertRaises(s._manifest.ManifestError):
            s._Servicer(None, "test", engines="1.0.3")

    def test_supports_and_require_answer_from_the_session(self):
        host = self.servicer._host_client
        self.assertFalse(host.supports("follow"), "no handshake yet")
        with self.assertRaises(s._features.FeatureUnavailable) as caught:
            host.require("follow")
        self.assertEqual(
            str(caught.exception),
            'feature "follow" needs protocol 1.0; the session has no handshake yet',
        )
        stream = self.servicer.Session(self.feed, None)
        self.feed.send(_hello())
        self.first(stream)
        for feature in s._features.FEATURES:
            with self.subTest(feature=feature):
                self.assertTrue(host.supports(feature))
                host.require(feature)
        self.assertFalse(host.supports("telepathy"))
        with self.assertRaises(RuntimeError) as caught:
            host.require("telepathy")
        self.assertEqual(str(caught.exception), 'feature "telepathy" is not one this SDK knows')
        later = dict(s._features.FEATURES, test_future=s.PROTOCOL_MINOR + 1)
        with mock.patch.object(s._features, "FEATURES", later):
            self.assertFalse(host.supports("test_future"))
            with self.assertRaises(s._features.FeatureUnavailable) as caught:
                host.require("test_future")
        self.assertEqual(
            str(caught.exception),
            f'feature "test_future" needs protocol 1.{s.PROTOCOL_MINOR + 1}; '
            f"this session speaks 1.{s.PROTOCOL_MINOR}",
        )
        self.assertEqual(caught.exception.needs, s.PROTOCOL_MINOR + 1)
        self.assertEqual(caught.exception.session_minor, s.PROTOCOL_MINOR)

    def test_serve_reads_the_floor_from_the_manifest_beside_it(self):
        """An undeclared plugin's floor is the manifest the host launched it
        beside -- only when that manifest is this plugin's."""
        import json

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, s.MANIFEST_FILE).write_text(
                json.dumps({"id": "org.test.x", "version": "1", "engines": {"vizi": "^1.0"}})
            )
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with mock.patch.object(s._declare, "identity", return_value=None):
                    self.assertEqual(s._engines_floor("org.test.x"), "^1.0")
                    self.assertIsNone(s._engines_floor("org.test.other"))
                with mock.patch.object(s._declare, "identity", return_value=("org.test.x", "1")):
                    with mock.patch.object(s._declare, "engines", return_value="1.0"):
                        self.assertEqual(s._engines_floor("org.test.x"), "1.0")
            finally:
                os.chdir(cwd)
