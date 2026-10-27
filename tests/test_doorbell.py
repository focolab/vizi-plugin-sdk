"""Behavioral tests for the v2 doorbell transport, plugin side.

Each test stands up a real socketpair: the transport owns one end and the
test plays the host on the other, writing records built with the module's own
encoder (or deliberately broken ones).  Run with::

    uv run python -m unittest tests.test_doorbell -v
"""

from __future__ import annotations

import errno
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest

from vizi_plugin_sdk import doorbell as db
from vizi_plugin_sdk.doorbell import (
    Cancellation,
    Disposition,
    Fault,
    Frame,
    SendResult,
    Teardown,
    encode_notification_payload,
    encode_record,
)

HAS_AF_UNIX = hasattr(socket, "AF_UNIX")
#: Descriptor passing is a unix message-transport property; Windows has none.
HAS_FD_PASSING = HAS_AF_UNIX and hasattr(socket, "SCM_RIGHTS")


def loopback_tcp_pair():
    """A connected loopback TCP pair — the Windows shape of every socket this
    module needs, and what socket.socketpair() emulates there."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client = socket.create_connection(listener.getsockname())
        server, _ = listener.accept()
    finally:
        listener.close()
    return client, server


def make_pair(transport_class):
    """The socket pair for one transport class, skipping where the platform
    has no such type rather than failing."""
    if transport_class == "stream":
        if HAS_AF_UNIX:
            return socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        return loopback_tcp_pair()
    if not HAS_AF_UNIX:
        raise unittest.SkipTest(f"{transport_class} needs AF_UNIX")
    if transport_class == "seqpacket":
        stype = getattr(socket, "SOCK_SEQPACKET", None)
        if stype is None:
            raise unittest.SkipTest("SOCK_SEQPACKET does not exist on this platform")
    else:
        stype = socket.SOCK_DGRAM
    try:
        return socket.socketpair(socket.AF_UNIX, stype)
    except OSError as exc:
        # The constant is not the capability: Darwin defines SOCK_SEQPACKET
        # and refuses it for AF_UNIX.  Only the pair itself can say.
        raise unittest.SkipTest(f"AF_UNIX {transport_class} pair unavailable here: {exc}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def e_payload(
    *,
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
    payload_kind=db.KIND_NONE,
    position_flags=0,
    position=b"",
    action=0,
    pad2=bytes(8),
) -> bytes:
    head = db._E_HEAD.pack(
        gen,
        t,
        interaction,
        seq,
        epoch,
        target_id,
        target_part,
        tool,
        channels,
        phase,
        view_id,
        level,
        mods,
        click_count,
        target_kind,
        payload_kind,
        position_flags,
    )
    pos = position + bytes(48 - len(position))
    return head + db._E_ACTION.pack(action) + pos + pad2


def m_payload(
    *, segment_id=7, size=4096, perms=db.PERM_PLUGIN_READ, pad=0, name=b"", name_len=None
) -> bytes:
    if name_len is None:
        name_len = len(name)
    head = db._M_HEAD.pack(segment_id, size, perms, pad, name_len)
    body = (head + name + b"\0")[:100]
    return body + bytes(100 - len(body))


class Host:
    """The far end of the socketpair: builds and writes host->plugin records."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.next_id = 1

    def raw(self, data: bytes, fds=()) -> None:
        if fds:
            self.sock.sendmsg(
                [data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack(f"{len(fds)}i", *fds))]
            )
        else:
            self.sock.send(data)

    def record(self, op: int, payload: bytes, *, record_id=None, fds=()) -> bytes:
        if record_id is None:
            record_id = self.next_id
            self.next_id += 1
        rec = encode_record(op, record_id, payload)
        self.raw(rec, fds)
        return rec

    def e(self, record_id=None, **kw) -> bytes:
        return self.record(db.OP_INPUT, e_payload(**kw), record_id=record_id)

    def d(self, segment_id=5, regions=(0,), epoch=0, view_id=0, **kw) -> bytes:
        return self.record(
            db.OP_NOTIFY, encode_notification_payload(segment_id, regions, epoch, view_id), **kw
        )

    def m(self, fd=None, **kw) -> bytes:
        return self.record(db.OP_MEMORY, m_payload(**kw), fds=(fd,) if fd is not None else ())


class Collector:
    def __init__(self):
        self.items: list = []
        self.cv = threading.Condition()
        self.claim_next = False
        self.raise_next = False
        self.block = None  # an Event the callback waits on, for deadline tests

    def __call__(self, outcome):
        if self.block is not None:
            self.block.wait()
        with self.cv:
            self.items.append(outcome)
            self.cv.notify_all()
        if isinstance(outcome, Frame) and outcome.descriptor is not None and self.claim_next:
            self.claim_next = False
            outcome.descriptor.claim()
        if self.raise_next:
            self.raise_next = False
            raise RuntimeError("consumer bug")

    def wait(self, n: int, timeout=2.0) -> list:
        deadline = time.monotonic() + timeout
        with self.cv:
            while len(self.items) < n:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"timed out with {len(self.items)} of {n} outcomes: {self.items}"
                    )
                self.cv.wait(remaining)
            return list(self.items)

    def wait_next(self, send, timeout=2.0):
        """The outcome of the record ``send()`` writes: the count is taken
        BEFORE the send, under the lock.  ``wait(len(self.items) + 1)`` on
        the line after a send read the count after it -- and when the
        listener thread had already appended the outcome by then, the test
        waited for one more than would ever come, "timed out with N of N+1".
        Every push on Windows 3.11 and twice elsewhere."""
        with self.cv:
            n = len(self.items)
        send()
        return self.wait(n + 1, timeout)[-1]

    def settle(self, quiet=0.05) -> list:
        time.sleep(quiet)
        with self.cv:
            return list(self.items)


class TransportCase(unittest.TestCase):
    #: The platform's own class, so these run on macOS (DGRAM) and Windows
    #: (stream) too; a test that needs a property one class lacks says so with
    #: `needs_close` or `needs_datagrams`.
    transport_class = db.platform_transport_class()

    def needs_close(self):
        """A test about a peer's close: a DGRAM doorbell cannot carry one
        (`doorbell_protocol.md` §6 rule 7)."""
        if self.transport_class == "dgram":
            self.skipTest("a DGRAM doorbell cannot carry the peer's close")

    def assertPeerSeesClose(self, sock, msg=None):
        """The one assertion a DGRAM doorbell cannot make: that the peer reads
        EOF.  Gated here rather than by ``needs_close`` on the whole test, so
        everything before it still runs on macOS."""
        if self.transport_class == "dgram":
            return
        sock.settimeout(2.0)
        self.assertEqual(sock.recv(120), b"", msg)

    def needs_datagrams(self):
        """A test about datagram lengths or descriptors: a stream has neither."""
        if self.transport_class == "stream":
            self.skipTest("a stream doorbell has no datagram boundaries or descriptors")

    def setUp(self):
        plugin_end, host_end = make_pair(self.transport_class)
        self.host = Host(host_end)
        self.out = Collector()
        self.t = db.DoorbellTransport(
            plugin_end, self.transport_class, self.out, handup_deadline=0.5
        )
        self.t.handshake_complete(db.PROTOCOL_MAJOR, db.PROTOCOL_MINOR)
        self.t.start()

    def tearDown(self):
        self.t.stop()
        self.t.join(2.0)
        self.host.sock.close()

    def ready(self, tools=(1,), epochs=((0, 1),)):
        for h in tools:
            self.assertTrue(self.t.tool_activated(h))
        for view, ep in epochs:
            self.assertTrue(self.t.epoch_acknowledged(view, ep))

    def frames(self):
        return [o for o in self.out.settle() if isinstance(o, Frame)]

    def assertTerminal(self, cls, **attrs):
        outs = self.out.wait(1)
        terms = [o for o in outs if isinstance(o, (Fault, Teardown))]
        self.assertEqual(len(terms), 1, outs)
        term = terms[-1]
        self.assertIsInstance(term, cls)
        for k, v in attrs.items():
            self.assertEqual(getattr(term, k), v, term)
        self.assertIs(self.out.settle()[-1], term)  # nothing after the terminal
        return term


# ---------------------------------------------------------------------------
# the record itself
# ---------------------------------------------------------------------------


class RecordTests(unittest.TestCase):
    def test_crc_check_value(self):
        self.assertEqual(db.crc32(b"123456789"), db.CRC_CHECK_VALUE)

    def test_encode_layout(self):
        rec = encode_record(db.OP_NOTIFY, 0x01020304, bytes(100))
        self.assertEqual(len(rec), 112)
        self.assertEqual(rec[0], 0x44)
        self.assertEqual(rec[1], 2)
        self.assertEqual(rec[2:4], b"\0\0")
        self.assertEqual(rec[4:8], b"\x04\x03\x02\x01")
        self.assertEqual(struct.unpack_from("<I", rec, 108)[0], db.crc32(rec[:108]))

    def test_encode_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            encode_record(0xFF, 1, bytes(100))
        with self.assertRaises(ValueError):
            encode_record(db.OP_NOTIFY, 1, bytes(99))

    def test_notification_mask_bit_order(self):
        payload = encode_notification_payload(9, [0, 7, 8, 511])
        self.assertEqual(payload[8], 0b1000_0001)  # regions 0 and 7 in byte 8, LSB first
        self.assertEqual(payload[9], 0b0000_0001)  # region 8 is bit 0 of byte 9
        self.assertEqual(payload[71], 0b1000_0000)  # region 511 is bit 7 of the last mask byte
        note, rejection, _ = db._decode_notification(payload)
        self.assertIsNone(rejection)
        self.assertEqual(note.regions(), (0, 7, 8, 511))
        self.assertTrue(note.names(7))
        self.assertFalse(note.names(6))

    def test_notification_encoder_refuses_invalid(self):
        for bad in (
            dict(segment_id=0, regions=[1]),
            dict(segment_id=1, regions=[512]),
            dict(segment_id=1, regions=[1], view_id=3),
        ):
            with self.assertRaises(ValueError):
                encode_notification_payload(**bad)


# ---------------------------------------------------------------------------
# framing and faults
# ---------------------------------------------------------------------------


class FramingTests(TransportCase):
    def test_decoded_notification(self):
        self.host.d(segment_id=5, regions=[1, 2], epoch=3, view_id=0)
        (f,) = self.out.wait(1)
        self.assertEqual(f.disposition, Disposition.DECODED)
        self.assertEqual(f.record.segment_id, 5)
        self.assertEqual(f.record.regions(), (1, 2))
        self.assertEqual(f.record.epoch, 3)
        self.assertEqual(f.header.id, 1)
        self.assertEqual(len(f.raw), 112)

    def test_unknown_op_is_skipped_and_framing_continues(self):
        self.host.record(0x5A, bytes(100))
        self.host.d()
        a, b = self.out.wait(2)
        self.assertEqual(a.disposition, Disposition.UNKNOWN_OP)
        self.assertEqual(b.disposition, Disposition.DECODED)
        self.assertEqual(self.t.counters["unknown_op"], 1)
        self.assertIsNone(self.t.terminal)

    @unittest.skipUnless(HAS_FD_PASSING, "needs SCM_RIGHTS")
    def test_unknown_op_with_descriptor_closes_it(self):
        r, w = os.pipe()
        self.host.raw(encode_record(0x5A, 1, bytes(100)), fds=(w,))
        os.close(w)
        (f,) = self.out.wait(1)
        self.assertEqual(f.disposition, Disposition.UNKNOWN_OP)
        # The transport's copy is closed: the read end sees EOF.
        self.assertEqual(os.read(r, 1), b"")
        os.close(r)

    def test_bad_crc_is_terminal(self):
        rec = bytearray(encode_record(db.OP_NOTIFY, 1, encode_notification_payload(5, [0])))
        rec[20] ^= 1
        self.host.raw(bytes(rec))
        fault = self.assertTerminal(Fault, failure_class="bad_crc", observed_len=112, ordinal=1)
        self.assertEqual(fault.capture, bytes(rec))
        self.assertNotEqual(fault.expected_crc, fault.observed_crc)
        frame = self.out.settle()[0]
        self.assertEqual(frame.disposition, Disposition.FRAMING_REJECTION)
        self.assertEqual(frame.reason, "bad_crc")
        # Both directions are closed: sends are refused and the host sees EOF.
        self.assertEqual(self.t.send_notification(5, [0]), SendResult.CLOSED)
        self.assertPeerSeesClose(self.host.sock)

    def test_header_faults(self):
        for mutate, cls in ((1, "bad_ver"), (2, "bad_flags"), (3, "bad_reserved")):
            with self.subTest(cls):
                plugin_end, host_end = make_pair(self.transport_class)
                out = Collector()
                t = db.DoorbellTransport(plugin_end, self.transport_class, out, handup_deadline=0.5)
                t.start()
                body = bytearray(
                    db._HEADER.pack(db.OP_NOTIFY, 2, 0, 0, 1) + encode_notification_payload(5, [0])
                )
                body[mutate] = 0x7
                rec = bytes(body) + struct.pack("<I", db.crc32(bytes(body)))
                host_end.sendall(rec)
                outs = out.wait(2)
                self.assertIsInstance(outs[-1], Fault)
                self.assertEqual(outs[-1].failure_class, cls)
                t.join(2.0)
                host_end.close()

    def test_reserved_op_faults(self):
        body = db._HEADER.pack(0xFF, 2, 0, 0, 1) + bytes(100)
        self.host.raw(body + struct.pack("<I", db.crc32(body)))
        self.assertTerminal(Fault, failure_class="reserved_op")

    def test_undersized_and_oversized_datagrams(self):
        self.needs_datagrams()
        self.host.raw(b"\x44" * 50)
        self.assertTerminal(Fault, failure_class="bad_length", observed_len=50, expected_crc=None)

    def test_oversized_datagram(self):
        self.needs_datagrams()
        self.host.raw(encode_record(db.OP_NOTIFY, 1, encode_notification_payload(5, [0])) + b"\0")
        self.assertTerminal(Fault, failure_class="bad_length", observed_len=113)

    def test_fixed_padding_faults(self):
        self.host.e(pad2=b"\1" + bytes(7))
        self.assertTerminal(Fault, failure_class="bad_padding")

    def test_the_bytes_before_the_fixed_padding_are_the_position_unions(self):
        # [88..92) carries RAY_AND_PICKED's back_frac; on a shorter kind a
        # stray byte there is this record's error, not a different record format.
        self.ready()
        self.host.e(
            interaction=1,
            phase=db.PHASE_FIRE,
            payload_kind=db.KIND_VOXEL,
            position=struct.pack("<fff", 1, 2, 3) + bytes(32) + b"\1",
        )
        (bad,) = self.out.wait(1)
        self.assertEqual(bad.violation, "position_tail")

    def test_d_fixed_padding_faults(self):
        payload = bytearray(encode_notification_payload(5, [0]))
        payload[99] = 1
        self.host.record(db.OP_NOTIFY, bytes(payload))
        self.assertTerminal(Fault, failure_class="bad_padding")

    @unittest.skipUnless(HAS_FD_PASSING, "needs SCM_RIGHTS")
    def test_m_fixed_padding_faults(self):
        r, w = os.pipe()
        self.host.m(fd=w, pad=1)
        os.close(w)
        self.assertTerminal(Fault, failure_class="bad_padding")
        self.assertEqual(os.read(r, 1), b"")  # descriptor closed on the fault path
        os.close(r)

    @unittest.skipUnless(HAS_FD_PASSING, "needs SCM_RIGHTS")
    def test_e_with_descriptor_is_wrong_fd_count(self):
        self.needs_datagrams()
        r, w = os.pipe()
        self.host.raw(encode_record(db.OP_INPUT, 1, e_payload()), fds=(w,))
        os.close(w)
        self.assertTerminal(Fault, failure_class="wrong_fd_count")
        self.assertEqual(os.read(r, 1), b"")
        os.close(r)

    def test_m_without_descriptor_is_wrong_fd_count(self):
        self.needs_datagrams()
        self.host.m()
        self.assertTerminal(Fault, failure_class="wrong_fd_count")

    @unittest.skipUnless(HAS_FD_PASSING, "needs SCM_RIGHTS")
    def test_two_descriptors_is_terminal(self):
        self.needs_datagrams()
        r1, w1 = os.pipe()
        r2, w2 = os.pipe()
        self.host.raw(encode_record(db.OP_MEMORY, 1, m_payload()), fds=(w1, w2))
        os.close(w1)
        os.close(w2)
        self.assertTerminal(Fault, failure_class="wrong_fd_count")
        self.assertEqual(os.read(r1, 1), b"")
        self.assertEqual(os.read(r2, 1), b"")
        os.close(r1)
        os.close(r2)

    @unittest.skipUnless(HAS_FD_PASSING, "needs SCM_RIGHTS")
    def test_three_descriptors_is_ctrunc(self):
        self.needs_datagrams()
        pipes = [os.pipe() for _ in range(3)]
        self.host.raw(encode_record(db.OP_MEMORY, 1, m_payload()), fds=tuple(w for _, w in pipes))
        for _, w in pipes:
            os.close(w)
        fault = self.assertTerminal(Fault, msg_ctrunc=True)
        self.assertEqual(fault.failure_class, "ctrunc")
        for r, _ in pipes:
            os.close(r)

    def test_id_gap_and_regression_are_diagnostics_only(self):
        self.host.d(record_id=1)
        self.host.d(record_id=2)
        self.host.d(record_id=4)
        self.host.d(record_id=3)
        self.host.d(record_id=5)
        outs = self.out.wait(5)
        self.assertEqual([o.disposition for o in outs], [Disposition.DECODED] * 5)
        self.assertEqual(outs[2].diagnostics, ("id_gap:1",))
        self.assertEqual(outs[3].diagnostics, ("id_regression",))
        self.assertEqual(outs[4].diagnostics, ())  # expected-next did not rewind
        self.assertEqual(self.t.counters["id_gaps"], 1)
        self.assertEqual(self.t.counters["id_regressions"], 1)

    def test_id_wraps_through_zero(self):
        self.t._expected_next = 0xFFFFFFFF
        self.host.d(record_id=0xFFFFFFFF)
        self.host.d(record_id=0)  # no skipped value at the wrap
        self.host.d(record_id=1)
        self.host.d(record_id=0xFFFFFFFF)  # behind by 3: a regression, never a gap
        outs = self.out.wait(4)
        self.assertEqual([o.diagnostics for o in outs], [(), (), (), ("id_regression",)])
        self.assertEqual(self.t.counters["id_gaps"], 0)

    def test_first_id_far_ahead_is_behind(self):
        # (0xFFFFFFFF - 1) mod 2**32 >= 2**31, so it is "behind" (§4), not a huge gap.
        self.host.d(record_id=0xFFFFFFFF)
        self.host.d(record_id=1)
        outs = self.out.wait(2)
        self.assertEqual([o.diagnostics for o in outs], [("id_regression",), ()])

    @unittest.skipIf(
        db.platform_transport_class() == "dgram", "a datagram peer close is not an EOF (§8)"
    )
    def test_peer_close_is_teardown_not_fault(self):
        self.needs_close()
        self.host.d()
        self.host.sock.close()
        outs = self.out.wait(2)
        self.assertEqual(outs[0].disposition, Disposition.DECODED)
        self.assertIsInstance(outs[1], Teardown)
        self.assertEqual(outs[1].reason, "peer_closed")
        self.assertTrue(self.t.join(2.0))
        # A later stop() emits nothing.
        self.t.stop()
        self.assertEqual(len(self.out.settle()), 2)

    def test_stop_emits_exactly_one_teardown(self):
        self.t.stop()
        self.assertTerminal(Teardown, reason="stopped")
        self.assertTrue(self.t.join(2.0))
        self.t.stop()
        self.assertEqual(len(self.out.settle()), 1)

    def test_stop_from_inside_callback(self):
        class Stopper:
            def __init__(s, t, col):
                s.t, s.col = t, col

            def __call__(s, outcome):
                s.col(outcome)
                if isinstance(outcome, Frame):
                    s.t.stop()

        self.t._on_outcome = Stopper(self.t, self.out)
        self.host.d()
        outs = self.out.wait(2)
        self.assertIsInstance(outs[1], Teardown)
        self.assertTrue(self.t.join(2.0))

    def test_consumer_exception_does_not_kill_listener(self):
        self.out.raise_next = True
        self.host.d()
        self.host.d()
        outs = self.out.wait(2)
        self.assertEqual([o.disposition for o in outs], [Disposition.DECODED] * 2)
        self.assertEqual(self.t.counters["consumer_errors"], 1)

    def test_fault_reaches_its_sink_and_releases_the_socket_past_a_blocked_consumer(self):
        # The socket is released once the FAULT SINK has accepted the report —
        # never behind a consumer callback, and without needing the deadline.
        plugin_end, host_end = make_pair(self.transport_class)
        out = Collector()
        gate = threading.Event()
        out.block = gate
        seen = []
        t = db.DoorbellTransport(
            plugin_end, self.transport_class, out, fault_sink=seen.append, handup_deadline=30.0
        )
        t.start()
        try:
            rec = bytearray(encode_record(db.OP_NOTIFY, 1, bytes(100)))
            rec[50] ^= 1
            host_end.send(bytes(rec))
            fault = t.faults.get(timeout=2.0)
            self.assertEqual(fault.failure_class, "bad_crc")
            self.assertEqual(seen, [fault])
            self.assertPeerSeesClose(host_end, "released while the consumer blocks")
            self.assertEqual(
                t.counters["handup_deadline_expired"], 0
            )  # the escape hatch was not needed
            self.assertEqual(out.items, [])  # the consumer has still received nothing
        finally:
            gate.set()
            t.join(2.0)
            host_end.close()
        self.assertIsInstance(out.settle()[-1], Fault)  # and it still gets its one terminal outcome

    def test_the_mailbox_and_the_stream_carry_one_fault_not_two(self):
        # A supervisor reads both routes; the guarantee that lets it report once
        # is identity, not equality.
        rec = bytearray(encode_record(db.OP_NOTIFY, 1, encode_notification_payload(5, [1])))
        rec[40] ^= 1
        self.host.raw(bytes(rec))
        mailbox = self.t.faults.get(timeout=2.0)
        outs = self.out.wait(2)
        stream = [o for o in outs if isinstance(o, Fault)]
        self.assertEqual(len(stream), 1)
        self.assertIs(stream[0], mailbox, "the two routes must carry the same object")
        self.assertTrue(self.t.faults.empty(), "exactly one fault per terminal event")
        self.assertFalse(any(isinstance(o, Teardown) for o in self.out.settle()))

    def test_send_cannot_cross_the_terminal_latch(self):
        # A fault closes the channel to traffic BEFORE anyone is told, so a
        # send already in flight completes and no later record joins it.
        class SlowSend:
            """Delegates to the real socket, pausing inside send()."""

            def __init__(self, sock, entered, release):
                self._s, self._entered, self._release = sock, entered, release

            def send(self, data, flags=0):
                self._entered.set()
                self._release.wait(2.0)
                return self._s.send(data, flags)

            def __getattr__(self, name):
                return getattr(self._s, name)

        entered, release = threading.Event(), threading.Event()
        real = self.t._sock
        self.t._sock = SlowSend(real, entered, release)
        results = []
        sender = threading.Thread(target=lambda: results.append(self.t.send_notification(1, [0])))
        sender.start()
        self.assertTrue(entered.wait(2.0))
        probe = Fault("bad_crc", "host_to_plugin", self.transport_class, "probe")
        latcher = threading.Thread(target=self.t._publish_terminal, args=(probe,))
        latcher.start()
        time.sleep(0.1)
        self.assertIsNone(self.t.terminal)  # the latch waits for the in-flight send
        release.set()
        sender.join(2.0)
        latcher.join(2.0)
        self.assertEqual(results, [SendResult.SENT])
        self.assertIsNotNone(self.t.terminal)
        self.assertIs(self.t.send_notification(1, [0]), SendResult.CLOSED)
        self.host.sock.settimeout(2.0)
        self.assertEqual(len(self.host.sock.recv(120)), 112)  # the in-flight record, and only it
        self.assertPeerSeesClose(self.host.sock)


# ---------------------------------------------------------------------------
# M
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAS_FD_PASSING, "SCM_RIGHTS grants need a unix message transport")
class MemoryGrantTests(TransportCase):
    def test_grant_with_descriptor_unclaimed_is_closed(self):
        self.needs_datagrams()
        r, w = os.pipe()
        self.host.m(
            fd=w,
            segment_id=0x1_0000_0003,
            size=1 << 20,
            perms=db.PERM_PLUGIN_READ | db.PERM_HOST_WRITE,
        )
        os.close(w)
        (f,) = self.out.wait(1)
        self.assertEqual(f.disposition, Disposition.DECODED)
        self.assertEqual(f.record.segment_id, 0x1_0000_0003)
        self.assertTrue(f.record.host_may_write)
        self.assertFalse(f.record.plugin_may_write)
        self.assertIsNone(f.record.name)
        self.assertIsNotNone(f.descriptor)
        self.assertEqual(os.read(r, 1), b"")  # closed after the emit returned unclaimed
        os.close(r)

    def test_grant_descriptor_claimed_stays_open(self):
        self.needs_datagrams()
        r, w = os.pipe()
        self.out.claim_next = True
        self.host.m(fd=w)
        os.close(w)
        (f,) = self.out.wait(1)
        self.assertTrue(f.descriptor.claimed)
        fd = f.descriptor.fd
        self.assertFalse(os.get_inheritable(fd))  # never inheritable (§7.6)
        os.write(fd, b"!")
        self.assertEqual(os.read(r, 1), b"!")
        os.close(fd)
        os.close(r)

    def test_grant_descriptor_claimed_even_if_consumer_raises(self):
        self.needs_datagrams()
        r, w = os.pipe()
        self.out.claim_next = True
        self.out.raise_next = True
        self.host.m(fd=w)
        os.close(w)
        (f,) = self.out.wait(1)
        os.write(f.descriptor.fd, b"!")
        os.close(f.descriptor.fd)
        os.close(r)

    def test_record_local_rejections_close_descriptor(self):
        self.needs_datagrams()
        cases = [
            (dict(segment_id=0), "zero_id", False),
            (dict(size=31), "size_below_preamble", True),
            (dict(perms=db.PERM_PLUGIN_WRITE), "no_plugin_read", True),
            (dict(perms=db.PERM_PLUGIN_READ | 0x10), "reserved_perms_bit", True),
        ]
        for kw, where, grant_failure in cases:
            with self.subTest(where):
                r, w = os.pipe()
                f = self.out.wait_next(lambda: self.host.m(fd=w, **kw))
                os.close(w)
                self.assertEqual(f.disposition, Disposition.SEMANTIC_REJECTION)
                self.assertEqual(f.violation, where)
                self.assertEqual(f.grant_failure, grant_failure)
                self.assertIsNone(f.descriptor)
                self.assertEqual(f.record.segment_id, kw.get("segment_id", 7))
                self.assertEqual(os.read(r, 1), b"")
                os.close(r)
        self.assertIsNone(self.t.terminal)

    def test_name_on_unix_is_diagnostic_only(self):
        self.needs_datagrams()
        r, w = os.pipe()
        self.host.m(fd=w, name=b"Local\\vizi-abc")
        os.close(w)
        (f,) = self.out.wait(1)
        self.assertEqual(f.disposition, Disposition.DECODED)
        self.assertEqual(f.diagnostics, ("name_field_on_unix:14",))
        self.assertIsNotNone(f.descriptor)
        os.close(r)


class StreamGrantTests(TransportCase):
    """A unix stream socket stands in for Windows loopback TCP: same framing,
    no descriptors, the name is the capability."""

    sock_type = socket.SOCK_STREAM
    transport_class = "stream"

    def test_windows_name_validation(self):
        good = b"Local\\vizi-0123456789abcdef0123456789abcdef"
        self.host.m(name=good)
        (f,) = self.out.wait(1)
        self.assertEqual(f.disposition, Disposition.DECODED)
        self.assertEqual(f.record.name, good.decode())
        self.assertIsNone(f.descriptor)
        cases = [
            (dict(name=b""), "empty_name"),
            (dict(name=b"x" * 81), "name_too_long"),
            (dict(name=b"abc", name_len=2), "name_unterminated"),
            (dict(name=b"a\0b"), "nul_in_name"),
            (dict(name=b"\xff\xfe"), "name_not_utf8"),
        ]
        for kw, where in cases:
            with self.subTest(where):
                f = self.out.wait_next(lambda: self.host.m(**kw))
                self.assertEqual(f.disposition, Disposition.SEMANTIC_REJECTION, f)
                self.assertEqual(f.violation, where)
                self.assertTrue(f.grant_failure)
        # a non-zero byte after the terminator
        payload = bytearray(m_payload(name=b"abc"))
        payload[50] = 1
        f = self.out.wait_next(lambda: self.host.record(db.OP_MEMORY, bytes(payload)))
        self.assertEqual(f.violation, "name_tail_not_zero")
        self.assertIsNone(self.t.terminal)

    def test_arbitrary_chunking_reassembles_records(self):
        a = encode_record(db.OP_NOTIFY, 1, encode_notification_payload(5, [1]))
        b = encode_record(db.OP_NOTIFY, 2, encode_notification_payload(5, [2]))
        stream = a + b
        for i in range(0, len(stream), 7):
            self.host.sock.send(stream[i : i + 7])
            time.sleep(0.001)
        outs = self.out.wait(2)
        self.assertEqual([o.record.regions() for o in outs], [(1,), (2,)])

    def test_eof_mid_record_is_teardown_with_partial(self):
        self.host.sock.send(b"\x44" * 30)
        self.host.sock.close()
        term = self.assertTerminal(Teardown, reason="peer_closed")
        self.assertEqual(term.partial, b"\x44" * 30)

    def test_fault_stops_reading_buffered_records(self):
        bad = bytearray(encode_record(db.OP_NOTIFY, 1, encode_notification_payload(5, [1])))
        bad[30] ^= 1
        good = encode_record(db.OP_NOTIFY, 2, encode_notification_payload(5, [2]))
        self.host.sock.send(bytes(bad) + good)
        self.assertTerminal(Fault, failure_class="bad_crc")
        self.assertEqual(
            len(
                [
                    o
                    for o in self.out.settle()
                    if isinstance(o, Frame) and o.disposition is Disposition.DECODED
                ]
            ),
            0,
        )


# ---------------------------------------------------------------------------
# D
# ---------------------------------------------------------------------------


class NotificationTests(TransportCase):
    def test_zero_segment_is_contract_violation(self):
        payload = bytearray(encode_notification_payload(1, [0]))
        payload[0:8] = bytes(8)
        self.host.record(db.OP_NOTIFY, bytes(payload))
        (f,) = self.out.wait(1)
        self.assertEqual(f.disposition, Disposition.SEMANTIC_REJECTION)
        self.assertEqual(f.violation, "zero_id")
        self.assertFalse(f.grant_failure)

    def test_empty_mask_is_ignored(self):
        self.host.record(db.OP_NOTIFY, encode_notification_payload(3, 0))
        (f,) = self.out.wait(1)
        self.assertEqual(f.disposition, Disposition.IGNORED)
        self.assertEqual(f.reason, "empty_mask")

    def test_view_without_epoch_is_diagnostic(self):
        payload = bytearray(encode_notification_payload(3, [1]))
        payload[76] = 4
        self.host.record(db.OP_NOTIFY, bytes(payload))
        (f,) = self.out.wait(1)
        self.assertEqual(f.disposition, Disposition.DECODED)
        self.assertEqual(f.diagnostics, ("view_id_without_epoch",))

    def test_send_notification_frames_and_burns_ids(self):
        self.needs_datagrams()
        self.assertEqual(self.t.send_notification(9, [3, 64], epoch=2, view_id=1), SendResult.SENT)
        self.assertEqual(self.t.send_notification(9, []), SendResult.SUPPRESSED)
        self.assertEqual(self.t.send_notification(9, [4]), SendResult.SENT)
        self.host.sock.settimeout(2.0)
        one = self.host.sock.recv(120)
        two = self.host.sock.recv(120)
        self.assertEqual(len(one), 112)
        self.assertEqual(struct.unpack_from("<I", one, 108)[0], db.crc32(one[:108]))
        self.assertEqual(db.Header(*db._HEADER.unpack_from(one, 0)).id, 1)
        self.assertEqual(
            db.Header(*db._HEADER.unpack_from(two, 0)).id, 2
        )  # SUPPRESSED never reached the writer
        note, _, _ = db._decode_notification(one[8:108])
        self.assertEqual(
            (note.segment_id, note.regions(), note.epoch, note.view_id), (9, (3, 64), 2, 1)
        )

    def test_a_bare_int_is_not_a_region_list(self):
        """The union signature read `region.index`
        as a mask, so index 0 rang nothing and index 1 rang region 0.  An int
        is refused; the mask form has its own keyword."""
        with self.assertRaises(TypeError):
            self.t.send_notification(9, 0)
        with self.assertRaises(TypeError):
            self.t.send_notification(9, 1)
        with self.assertRaises(TypeError):
            self.t.send_notification(9, [1], mask=1)
        self.assertEqual(self.t.counters["send_suppressed_empty_mask"], 0)
        self.assertEqual(self.t.send_notification(9, mask=1 << 7), SendResult.SENT)
        self.assertEqual(self.t.send_notification(9, mask=0), SendResult.SUPPRESSED)

    def test_refused_send_is_dropped_and_burns_its_id(self):
        self.needs_datagrams()
        sent = 0
        while True:
            r = self.t.send_notification(1, [0])
            if r is SendResult.REFUSED:
                break
            self.assertIs(r, SendResult.SENT)
            sent += 1
            self.assertLess(sent, 100_000)
        self.assertEqual(self.t.counters["send_refused"], 1)
        self.host.sock.settimeout(2.0)
        ids = [db._HEADER.unpack_from(self.host.sock.recv(120), 0)[4] for _ in range(sent)]
        self.assertEqual(ids, list(range(1, sent + 1)))
        self.assertIs(self.t.send_notification(1, [0]), SendResult.SENT)
        self.assertEqual(db._HEADER.unpack_from(self.host.sock.recv(120), 0)[4], sent + 2)

    def test_send_after_peer_gone_is_teardown(self):
        self.needs_close()
        self.host.sock.close()
        self.out.wait(1)  # the listener notices first, as EOF
        self.assertIs(self.t.send_notification(1, [0]), SendResult.CLOSED)
        self.assertTerminal(Teardown)


# ---------------------------------------------------------------------------
# E
# ---------------------------------------------------------------------------


class InputTests(TransportCase):
    def test_record_before_epoch_is_epoch_mismatch(self):
        self.t.tool_activated(1)
        self.host.e()
        (f,) = self.out.wait(1)
        self.assertEqual(f.disposition, Disposition.SEMANTIC_REJECTION)
        self.assertEqual(f.violation, "epoch_mismatch")

    def test_gesture_lifecycle(self):
        self.ready()
        self.host.e(
            interaction=1,
            seq=0,
            phase=db.PHASE_PRESS,
            payload_kind=db.KIND_VOXEL,
            position=struct.pack("<fff", 1, 2, 3),
        )
        self.host.e(interaction=1, seq=1, phase=db.PHASE_DRAG)
        self.host.e(interaction=1, seq=3, phase=db.PHASE_DRAG)  # seq 2 lost
        self.host.e(interaction=1, seq=3, phase=db.PHASE_DRAG)  # duplicate
        self.host.e(interaction=1, seq=4, phase=db.PHASE_RELEASE)
        self.host.e(interaction=1, seq=5, phase=db.PHASE_CANCEL)  # late terminal
        press, drag1, drag3, dup, rel, late = self.out.wait(6)
        self.assertEqual(press.disposition, Disposition.DECODED)
        self.assertEqual(press.record.position, db.Voxel(1.0, 2.0, 3.0))
        self.assertIsNone(press.implicitly_closed)
        self.assertEqual(drag1.seq_gap, 0)
        self.assertEqual(drag3.seq_gap, 1)
        self.assertIn("seq_gap:1", drag3.diagnostics)
        self.assertEqual((dup.disposition, dup.reason), (Disposition.IGNORED, "duplicate_seq"))
        self.assertEqual(rel.closed_interaction, 1)
        self.assertEqual(
            (late.disposition, late.reason), (Disposition.IGNORED, "unknown_interaction")
        )
        self.assertEqual(self.t.counters["seq_gap_samples"], 1)

    def test_an_action_fire_lands_inside_the_open_gesture(self):
        # [42..44) carries `action` (E §1): a FIRE naming action 3 arrives mid-
        # drag, decodes as such, and closes nothing -- the drag goes on.  Before
        # the field those bytes were padding and a non-zero one was a fault.
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_PRESS)
        self.host.e(interaction=2, phase=db.PHASE_FIRE, action=3)
        self.host.e(interaction=1, seq=1, phase=db.PHASE_DRAG)
        press, fire, drag = self.out.wait(3)
        self.assertEqual(press.record.action, 0)
        self.assertEqual(
            (fire.disposition, fire.record.phase, fire.record.action),
            (Disposition.DECODED, db.PHASE_FIRE, 3),
        )
        self.assertEqual(drag.disposition, Disposition.DECODED)

    def test_fire_is_complete_and_deduped(self):
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_FIRE, record_id=1)
        self.host.e(interaction=1, phase=db.PHASE_FIRE, record_id=1)  # a retry reuses its id
        self.host.e(interaction=1, seq=1, phase=db.PHASE_DRAG)
        fire, dup, drag = self.out.wait(3)
        self.assertEqual(fire.disposition, Disposition.DECODED)
        self.assertEqual((dup.disposition, dup.reason), (Disposition.IGNORED, "duplicate_opening"))
        self.assertEqual(
            (drag.disposition, drag.reason), (Disposition.IGNORED, "unknown_interaction")
        )

    def test_press_implicitly_closes_prior_gesture_without_cancellation(self):
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_PRESS)
        self.host.e(interaction=2, phase=db.PHASE_PRESS)
        self.host.e(interaction=1, seq=1, phase=db.PHASE_RELEASE)
        a, b, c = self.out.wait(3)
        self.assertEqual(b.implicitly_closed, 1)
        self.assertEqual((c.disposition, c.reason), (Disposition.IGNORED, "unknown_interaction"))
        self.assertFalse(any(isinstance(o, Cancellation) for o in (a, b, c)))

    def test_rejected_press_still_performs_implicit_close(self):
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_PRESS)
        self.host.e(interaction=2, seq=5, phase=db.PHASE_PRESS)  # seq_on_open
        self.host.e(interaction=3, phase=db.PHASE_PRESS)
        _, bad, cancel, next_press = self.out.wait(4)
        self.assertEqual(bad.violation, "seq_on_open")
        self.assertEqual(cancel, Cancellation(1, 1, 1, 0, "implicit_close_rejected_press"))
        self.assertEqual(next_press.disposition, Disposition.DECODED)
        self.assertIsNone(next_press.implicitly_closed)
        self.assertEqual(self.t._watermark, 3)

    def test_duplicate_press_closes_nothing(self):
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_PRESS, record_id=1)
        self.host.e(interaction=1, phase=db.PHASE_PRESS, record_id=1)  # byte-identical retry
        _, dup = self.out.wait(2)
        self.assertEqual(dup.reason, "duplicate_opening")
        self.host.e(interaction=1, seq=1, phase=db.PHASE_DRAG)
        drag = self.out.wait(3)[-1]
        self.assertEqual(drag.disposition, Disposition.DECODED)
        self.assertFalse(any(isinstance(o, Cancellation) for o in self.out.settle()))

    def test_reused_live_interaction_with_different_bytes_is_mismatch(self):
        # A duplicate is an exact replay, not merely a familiar id: a reused id
        # carrying a DIFFERENT press must not be dropped while its drags are
        # accepted as continuations of the old gesture (E §2).
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_PRESS, record_id=1, click_count=1)
        self.host.e(interaction=1, phase=db.PHASE_PRESS, record_id=1, click_count=2)
        _, bad, cancel = self.out.wait(3)
        self.assertEqual(bad.disposition, Disposition.SEMANTIC_REJECTION)
        self.assertEqual(bad.violation, "interaction_mismatch")
        self.assertEqual(cancel, Cancellation(1, 1, 1, 0, "implicit_close_rejected_press"))
        self.host.e(interaction=1, seq=1, phase=db.PHASE_DRAG)
        self.assertEqual(self.out.wait(4)[-1].reason, "unknown_interaction")

    def test_duplicate_opening_for_a_closed_interaction_still_drops(self):
        # Nothing to compare and nothing to merge with, so the drop stands.
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_PRESS, record_id=1)
        self.host.e(interaction=1, seq=1, phase=db.PHASE_RELEASE)
        self.host.e(interaction=1, phase=db.PHASE_PRESS, record_id=9, click_count=2)
        outs = self.out.wait(3)
        self.assertEqual(
            (outs[2].disposition, outs[2].reason), (Disposition.IGNORED, "duplicate_opening")
        )
        self.assertFalse(any(isinstance(o, Cancellation) for o in self.out.settle()))

    def test_watermark_precedes_the_structural_zero_checks(self):
        # An old, already-closed opening with a structural defect is ignored as
        # a duplicate rather than reported as zero_id (E §2 steps 2 and 3).
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_FIRE, record_id=1)
        self.host.e(interaction=1, phase=db.PHASE_FIRE, record_id=1, tool=0)
        good, stale = self.out.wait(2)
        self.assertEqual(good.disposition, Disposition.DECODED)
        self.assertEqual(
            (stale.disposition, stale.reason), (Disposition.IGNORED, "duplicate_opening")
        )
        self.assertEqual(self.t.counters["violations:zero_id"], 0)

    def test_rejected_terminal_cancels_its_interaction(self):
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_PRESS)
        self.host.e(
            interaction=1,
            seq=1,
            phase=db.PHASE_RELEASE,
            payload_kind=db.KIND_VOXEL,
            position=struct.pack("<fff", 1, 2, 3) + b"\1",
        )
        _, rel, cancel = self.out.wait(3)
        self.assertEqual(rel.violation, "position_tail")
        self.assertEqual(cancel.reason, "rejected_record")
        self.assertEqual(cancel.interaction, 1)
        self.host.e(interaction=1, seq=2, phase=db.PHASE_CANCEL)
        self.assertEqual(self.out.wait(4)[-1].reason, "unknown_interaction")

    def test_rejected_record_naming_a_closed_interaction_cancels_nothing(self):
        # "Identifies none" means interaction == 0 and nothing else: a non-zero
        # id the index does not hold names a gesture that is simply already
        # over, and canceling the tool's current one would destroy a newer,
        # unrelated interaction on the strength of a late record (E §2).
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_PRESS)
        self.host.e(interaction=1, seq=1, phase=db.PHASE_RELEASE)  # 1 is now closed
        self.host.e(interaction=2, phase=db.PHASE_PRESS)  # a newer, live gesture
        self.out.wait(3)
        # A malformed DRAG for the CLOSED interaction: rejected before the index
        # lookup, so the rejection rule decides what it cancels.
        self.host.e(
            interaction=1,
            seq=2,
            phase=db.PHASE_DRAG,
            payload_kind=db.KIND_VOXEL,
            position=struct.pack("<fff", 1, 2, 3) + b"\1",
        )
        bad = self.out.wait(4)[-1]
        self.assertEqual(bad.violation, "position_tail")
        self.assertFalse(any(isinstance(o, Cancellation) for o in self.out.settle()))
        # The live gesture is untouched.
        self.host.e(interaction=2, seq=1, phase=db.PHASE_DRAG)
        self.assertEqual(self.out.wait(5)[-1].disposition, Disposition.DECODED)

    def test_epoch_zero_is_never_acknowledged(self):
        self.ready()
        self.assertFalse(self.t.epoch_acknowledged(0, 0))
        self.assertFalse(self.t.epoch_acknowledged(7, 0))  # not even for a fresh pane
        self.host.e(interaction=1, phase=db.PHASE_PRESS)
        self.assertEqual(self.out.wait(1)[0].disposition, Disposition.DECODED)

    def test_zero_interaction_cancels_tools_open_gesture(self):
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_PRESS)
        self.host.e(interaction=0, seq=1, phase=db.PHASE_DRAG)
        _, bad, cancel = self.out.wait(3)
        self.assertEqual(bad.violation, "zero_id")
        self.assertEqual(cancel.interaction, 1)

    def test_nonfinite_and_entry_frac_range(self):
        self.ready()
        nan = struct.pack("<fff", float("nan"), 0, 0)
        self.host.e(interaction=1, phase=db.PHASE_FIRE, payload_kind=db.KIND_VOXEL, position=nan)
        ray = struct.pack("<ffffffff", 0, 0, 0, 1, 1, 1, 1.5, 0)
        self.host.e(interaction=2, phase=db.PHASE_FIRE, payload_kind=db.KIND_RAY, position=ray)
        a, b = self.out.wait(2)
        self.assertEqual(a.violation, "nonfinite_position")
        self.assertEqual(b.violation, "nonfinite_position")
        self.assertEqual(self.t._watermark, 0)  # a malformed opening record does not advance it

    def test_ray_decode(self):
        self.ready()
        ray = struct.pack("<ffffffff", 0, 0, 0, 10, 0, 0, 0.25, 1.5)
        self.host.e(
            interaction=1,
            phase=db.PHASE_FIRE,
            payload_kind=db.KIND_RAY_AND_PICKED,
            position=ray + struct.pack("<ffff", 5, 0, 0, 0.75),
            position_flags=db.FLAG_ROLL_REF_Y,
        )
        (f,) = self.out.wait(1)
        pos = f.record.position
        self.assertIsInstance(pos, db.RayAndPicked)
        self.assertEqual(pos.ray.entry, db.Voxel(2.5, 0.0, 0.0))
        self.assertEqual(pos.picked, db.Voxel(5.0, 0.0, 0.0))
        self.assertFalse(pos.is_point)
        # The back is measured from the picked FRONT (5 of the ray's 10): three
        # quarters of the 5 that are left behind it.
        self.assertEqual(pos.front, db.Voxel(5.0, 0.0, 0.0))
        self.assertEqual(pos.back, db.Voxel(8.75, 0.0, 0.0))
        self.assertTrue(f.record.roll_ref_y)
        self.assertFalse(f.record.miss)

    def test_a_tool_reads_the_ray_the_same_way_with_or_without_a_pick(self):
        """A tool takes either record whichever it declared, so
        the ray and the pick are read without branching on the kind."""
        self.ready()
        ray = struct.pack("<ffffffff", 0, 0, 0, 10, 0, 0, 0.25, 1.5)
        self.host.e(interaction=1, phase=db.PHASE_FIRE, payload_kind=db.KIND_RAY, position=ray)
        self.host.e(
            interaction=2,
            phase=db.PHASE_FIRE,
            payload_kind=db.KIND_RAY_AND_PICKED,
            position=ray + struct.pack("<ffff", 5, 0, 0, 0.5),
        )
        self.host.e(
            interaction=3,
            phase=db.PHASE_FIRE,
            payload_kind=db.KIND_VOXEL,
            position=struct.pack("<fff", 1, 2, 3),
        )
        bare, picked, voxel = (f.record for f in self.out.wait(3))
        self.assertIsInstance(bare.ray, db.Ray)
        self.assertEqual(bare.ray, picked.ray)
        self.assertIsNone(bare.pick)
        self.assertEqual(picked.pick.picked, db.Voxel(5.0, 0.0, 0.0))
        self.assertIsNone(voxel.ray)
        self.assertIsNone(voxel.pick)

    def test_a_point_pick_has_no_back_and_back_frac_is_a_fraction(self):
        self.ready()
        ray = struct.pack("<ffffffff", 0, 0, 0, 10, 0, 0, 0.25, 0)
        for interaction, back in ((1, float("nan")), (2, 1.25), (3, -0.5)):
            self.host.e(
                interaction=interaction,
                phase=db.PHASE_FIRE,
                payload_kind=db.KIND_RAY_AND_PICKED,
                position=ray + struct.pack("<ffff", 5, 0, 0, back),
            )
        point, high, low = self.out.wait(3)
        self.assertTrue(point.record.position.is_point)
        self.assertIsNone(point.record.position.back)
        # 0.0 is NOT a point: a real pick of no depth, its back at its front --
        # and what a sender that predates the field leaves there.
        self.host.e(
            interaction=4,
            phase=db.PHASE_FIRE,
            payload_kind=db.KIND_RAY_AND_PICKED,
            position=ray + struct.pack("<ffff", 5, 0, 0, 0.0),
        )
        flat = self.out.wait(4)[-1].record.position
        self.assertFalse(flat.is_point)
        self.assertEqual(flat.back, db.Voxel(5.0, 0.0, 0.0))
        self.assertEqual(high.violation, "nonfinite_position")
        self.assertEqual(low.violation, "nonfinite_position")

    def test_ray_with_camera_equal_to_far_is_rejected(self):
        # far == camera destroys the direction and leaves roll uninterpretable.
        self.ready()
        degenerate = struct.pack("<ffffffff", 3, 4, 5, 3, 4, 5, 0.0, 0)
        self.host.e(
            interaction=1, phase=db.PHASE_FIRE, payload_kind=db.KIND_RAY, position=degenerate
        )
        self.assertEqual(self.out.wait(1)[0].violation, "nonfinite_position")

    def test_miss_with_picked(self):
        self.ready()
        self.host.e(
            interaction=1,
            phase=db.PHASE_FIRE,
            payload_kind=db.KIND_PICKED,
            position=struct.pack("<fff", 1, 2, 3),
            position_flags=db.FLAG_MISS,
        )
        self.assertEqual(self.out.wait(1)[0].violation, "miss_with_picked")

    def test_miss_on_voxel_is_fine_and_miss_on_none_ignored(self):
        self.ready()
        self.host.e(
            interaction=1,
            phase=db.PHASE_FIRE,
            payload_kind=db.KIND_VOXEL,
            position=struct.pack("<fff", -5, 2, 3),
            position_flags=db.FLAG_MISS,
        )
        self.host.e(
            interaction=2,
            phase=db.PHASE_FIRE,
            payload_kind=db.KIND_NONE,
            position_flags=db.FLAG_MISS,
        )
        a, b = self.out.wait(2)
        self.assertTrue(a.record.miss)
        self.assertEqual(b.disposition, Disposition.DECODED)
        self.assertFalse(b.record.miss)

    def test_target_invariant(self):
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_FIRE, target_kind=db.TARGET_ROI, target_id=0)
        self.host.e(interaction=2, phase=db.PHASE_FIRE, target_kind=db.TARGET_NONE, target_part=3)
        self.host.e(
            interaction=3,
            phase=db.PHASE_FIRE,
            target_kind=db.TARGET_ROI,
            target_id=9,
            target_part=2,
        )
        self.host.e(interaction=4, phase=db.PHASE_FIRE, target_kind=99, target_id=0, target_part=7)
        a, b, c, d = self.out.wait(4)
        self.assertEqual(a.violation, "invalid_target")
        self.assertEqual(b.violation, "invalid_target")
        self.assertEqual(c.disposition, Disposition.DECODED)
        self.assertEqual(d.disposition, Disposition.DECODED)
        self.assertIn("unknown_target_kind:99", d.diagnostics)

    def test_unknown_payload_kind_and_phase_and_reserved_bits(self):
        self.ready()
        self.host.e(
            interaction=1,
            phase=db.PHASE_FIRE,
            payload_kind=42,
            position=b"\7" * 44,
            mods=0x81,
            position_flags=0x80,
        )
        self.host.e(interaction=2, phase=9)
        a, b = self.out.wait(2)
        self.assertEqual(a.disposition, Disposition.DECODED)
        self.assertIsNone(a.record.position)
        self.assertEqual(
            set(a.diagnostics),
            {"unknown_payload_kind:42", "reserved_bit:mods", "reserved_bit:position_flags"},
        )
        self.assertEqual((b.disposition, b.reason), (Disposition.IGNORED, "unknown_phase"))

    def test_tool_status_three_way(self):
        self.ready(tools=(1, 3))
        self.assertTrue(self.t.tool_retired(3))
        self.host.e(interaction=1, phase=db.PHASE_FIRE, tool=3)  # retired
        self.host.e(
            interaction=2, phase=db.PHASE_FIRE, tool=2
        )  # gap: never registered, classifies as retired
        self.host.e(interaction=3, phase=db.PHASE_FIRE, tool=4)  # never seen
        a, b, c = self.out.wait(3)
        self.assertEqual((a.disposition, a.reason), (Disposition.IGNORED, "retired_tool"))
        self.assertEqual((b.disposition, b.reason), (Disposition.IGNORED, "retired_tool"))
        self.assertEqual(c.violation, "unknown_tool")
        self.assertFalse(self.t.tool_activated(3))  # retired handles never come back
        self.assertFalse(self.t.tool_activated(0))
        self.assertTrue(self.t.tool_activated(1))  # a live repeat is a no-op
        # Retirement is idempotent where the state it asks for already holds.
        self.assertTrue(self.t.tool_retired(3))  # already retired
        self.assertTrue(self.t.tool_retired(2))  # a gap that was never registered
        self.assertFalse(self.t.tool_retired(0))
        self.assertFalse(self.t.tool_retired(99))  # above the high-water mark
        self.assertTrue(self.t.tool_activated(5))  # retirement never advanced the mark

    def test_tool_retired_cancels_open_gesture_without_doorbell_traffic(self):
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_PRESS)
        self.out.wait(1)
        # From another thread, with the doorbell idle: the cancellation must
        # still be delivered (transport §8's liveness requirement).
        threading.Thread(target=self.t.tool_retired, args=(1,)).start()
        cancel = self.out.wait(2)[-1]
        self.assertEqual(cancel, Cancellation(1, 1, 1, 0, "tool_retired"))
        self.host.e(interaction=1, seq=1, phase=db.PHASE_RELEASE)
        self.assertEqual(self.out.wait(3)[-1].reason, "retired_tool")

    def test_epoch_install_closes_pane_and_stale_records_are_discarded(self):
        self.ready(tools=(1, 2), epochs=((0, 1), (1, 1)))
        self.host.e(interaction=1, phase=db.PHASE_PRESS, tool=1, view_id=0)
        self.host.e(interaction=2, phase=db.PHASE_PRESS, tool=2, view_id=1)
        self.out.wait(2)
        self.assertTrue(self.t.epoch_acknowledged(0, 5))
        cancel = self.out.wait(3)[-1]
        self.assertEqual(cancel, Cancellation(1, 1, 1, 0, "epoch_installed"))
        self.host.e(interaction=1, seq=1, phase=db.PHASE_DRAG, tool=1, view_id=0, epoch=1)  # limbo
        self.host.e(
            interaction=3, phase=db.PHASE_PRESS, tool=1, view_id=0, epoch=1
        )  # stale opening
        self.host.e(
            interaction=2, seq=1, phase=db.PHASE_DRAG, tool=2, view_id=1, epoch=1
        )  # other pane, untouched
        self.host.e(interaction=4, phase=db.PHASE_PRESS, tool=1, view_id=0, epoch=5)
        stale_drag, stale_press, other, new = self.out.wait(7)[3:]
        self.assertEqual(stale_drag.reason, "stale_epoch")
        self.assertEqual(stale_press.reason, "stale_epoch")
        self.assertEqual(other.disposition, Disposition.DECODED)
        self.assertEqual(new.disposition, Disposition.DECODED)
        self.assertEqual(self.t._watermark, 4)
        # equal is a no-op that closes nothing; older is rejected
        self.assertTrue(self.t.epoch_acknowledged(0, 5))
        self.assertFalse(self.t.epoch_acknowledged(0, 4))
        self.assertFalse(self.t.epoch_acknowledged(0, 0))
        self.assertEqual(len(self.out.settle()), 7)

    def test_future_epoch_is_mismatch_and_cancels(self):
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_PRESS)
        self.host.e(interaction=1, seq=1, phase=db.PHASE_RELEASE, epoch=2)
        _, bad, cancel = self.out.wait(3)
        self.assertEqual(bad.violation, "epoch_mismatch")
        self.assertEqual(cancel.interaction, 1)

    def test_interaction_mismatch(self):
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_PRESS, gen=1)
        self.host.e(interaction=1, seq=1, phase=db.PHASE_DRAG, gen=2)
        _, bad, cancel = self.out.wait(3)
        self.assertEqual(bad.violation, "interaction_mismatch")
        self.assertEqual(cancel.interaction, 1)

    def test_supervisor_retractions(self):
        self.ready(tools=(1, 2), epochs=((0, 1), (3, 1)))
        self.host.e(interaction=1, phase=db.PHASE_PRESS, tool=1, view_id=0)
        self.host.e(interaction=2, phase=db.PHASE_PRESS, tool=2, view_id=3)
        self.out.wait(2)
        self.assertTrue(self.t.cancel_interaction(1))
        self.assertFalse(self.t.cancel_interaction(1))
        self.assertEqual(self.out.wait(3)[-1], Cancellation(1, 1, 1, 0, "cancel_interaction"))
        self.assertEqual(self.t.cancel_interactions(3), 1)
        self.assertEqual(self.out.wait(4)[-1], Cancellation(2, 2, 1, 3, "pane_cancelled"))
        self.assertEqual(self.t.cancel_interactions(3), 0)

    def test_terminal_cancels_open_interactions_first(self):
        self.needs_close()
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_PRESS)
        self.out.wait(1)
        self.host.sock.close()
        _, cancel, term = self.out.wait(3)
        self.assertEqual(cancel.reason, "transport_ended")
        self.assertIsInstance(term, Teardown)
        self.assertFalse(self.t.tool_activated(2))  # lifecycle inputs are refused after the latch

    def test_concurrent_tools_interleave(self):
        self.ready(tools=(1, 2))
        self.host.e(interaction=1, phase=db.PHASE_PRESS, tool=1)
        self.host.e(interaction=2, phase=db.PHASE_PRESS, tool=2)
        self.host.e(interaction=1, seq=1, phase=db.PHASE_DRAG, tool=1)
        self.host.e(interaction=2, seq=1, phase=db.PHASE_RELEASE, tool=2)
        self.host.e(interaction=1, seq=2, phase=db.PHASE_RELEASE, tool=1)
        outs = self.out.wait(5)
        self.assertTrue(all(o.disposition is Disposition.DECODED for o in outs))
        self.assertIsNone(outs[1].implicitly_closed)
        self.assertEqual(self.t._index, {})


class DatagramClassTests(TransportCase):
    """The class macOS ships; ``make_pair`` skips where the pair cannot be made."""

    sock_type = socket.SOCK_DGRAM
    transport_class = "dgram"

    def test_empty_datagram_is_bad_length_not_teardown(self):
        self.host.d()
        self.host.sock.send(b"")
        outs = self.out.wait(3)
        self.assertEqual(outs[0].disposition, Disposition.DECODED)
        self.assertEqual(
            (outs[1].disposition, outs[1].reason), (Disposition.FRAMING_REJECTION, "bad_length")
        )
        self.assertIsInstance(outs[2], Fault)
        self.assertEqual(outs[2].failure_class, "bad_length")
        self.assertEqual(outs[2].observed_len, 0)


class TerminalSealTests(TransportCase):
    """The terminal is the last thing a consumer sees.

    A fault latches on whichever thread raised it, so the listener can be
    part-way through decoding a frame when it happens.  That frame must not be
    delivered behind the terminal, and must not touch E's state either
    (transport §8: the first terminal event stops both directions).
    """

    def test_a_frame_decoded_after_the_latch_is_not_appended(self):
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_PRESS)
        self.out.wait(1)
        probe = Fault("ambiguous_send", "plugin_to_host", self.transport_class, "probe")
        self.assertTrue(self.t._publish_terminal(probe))
        delivered = len(self.out.wait(2))
        # Whatever the listener was doing, nothing may join the queue now.
        self.t._emit(Frame(b"x" * 112, 99, Disposition.DECODED))
        self.assertEqual(len(self.out.settle()), delivered)
        self.assertEqual(self.t.counters["outcomes_after_terminal"], 1)
        self.assertIsInstance(self.out.settle()[-1], Fault)

    def test_queued_records_are_not_processed_once_a_terminal_latches(self):
        # The consumer raises the terminal from inside its own callback, which
        # is the interleaving that a send fault on another thread produces.
        self.ready()
        collector = self.out

        def latch_on_first(outcome):
            collector(outcome)
            if isinstance(outcome, Frame) and outcome.disposition is Disposition.DECODED:
                self.t._publish_terminal(
                    Fault("ambiguous_send", "plugin_to_host", self.transport_class, "probe")
                )

        self.t._on_outcome = latch_on_first
        # Every record is QUEUED before the first is processed: the consumer
        # is held on the first until the rest are sent.  Otherwise the latch
        # can release the socket mid-loop, and on a DGRAM doorbell (macOS) a
        # send to a released peer is ECONNRESET at the host end -- a test
        # artifact, not what this checks.
        gate = threading.Event()
        collector.block = gate
        for n in range(1, 6):
            self.host.e(interaction=n, phase=db.PHASE_FIRE, record_id=n)
        gate.set()
        outs = self.out.wait(2)
        self.assertEqual(outs[0].disposition, Disposition.DECODED)
        self.assertIsInstance(outs[1], Fault)
        self.assertEqual(len(self.out.settle()), 2, "no frame may follow the terminal")
        self.assertTrue(self.t.join(2.0))

    def test_interaction_state_is_untouched_after_a_terminal(self):
        self.ready()
        self.host.e(interaction=1, phase=db.PHASE_PRESS)
        self.out.wait(1)
        watermark = self.t._watermark
        self.t._publish_terminal(
            Fault("ambiguous_send", "plugin_to_host", self.transport_class, "probe")
        )
        # The listener may itself count a frame after the latch -- a released
        # DGRAM socket (macOS) can hand it an empty one -- so the baseline is
        # taken once it has stopped, and only the fed frame is counted here.
        self.assertTrue(self.t.join(2.0))
        before = self.t.counters["frames_after_terminal"]
        # The socket is already released, so feed the frame the way a listener
        # mid-decode would have: straight into the processing path.
        later = encode_record(db.OP_INPUT, 9, e_payload(interaction=7, phase=db.PHASE_PRESS))
        self.t._process(later, [], False)
        self.assertEqual(self.t._watermark, watermark, "E state must not advance after the latch")
        self.assertNotIn(7, self.t._index)
        self.assertEqual(self.t.counters["frames_after_terminal"], before + 1)


class StopReleasesSocketTests(TransportCase):
    """stop() returns once the request is recorded AND the socket released
    (transport §6 rule 3), so it cannot be left waiting on a listener that is
    inside a slow consumer callback."""

    def test_stop_releases_the_socket_while_the_listener_is_blocked(self):
        gate = threading.Event()
        self.out.block = gate
        self.host.d()  # the listener is now stuck inside the callback
        time.sleep(0.1)
        self.t.stop()  # must not wait for the callback
        self.assertPeerSeesClose(self.host.sock, "the peer should see the release at once")
        self.assertIs(self.t.send_notification(1, [0]), SendResult.CLOSED)
        gate.set()
        self.assertTrue(self.t.join(2.0))

    def test_stop_before_start_releases_everything(self):
        plugin_end, host_end = make_pair(self.transport_class)
        self.addCleanup(host_end.close)
        out = Collector()
        t = db.DoorbellTransport(plugin_end, self.transport_class, out, handup_deadline=0.5)
        wake_r, wake_w = t._wake_r, t._wake_w
        t.stop()  # never started: open_doorbell() hands back an unstarted transport
        self.assertPeerSeesClose(host_end)
        self.assertEqual(plugin_end.fileno(), -1, "the doorbell socket must be closed")
        self.assertEqual(wake_r.fileno(), -1)
        self.assertEqual(wake_w.fileno(), -1)
        self.assertTrue(t.join(1.0))
        self.assertEqual([type(o).__name__ for o in out.items], ["Teardown"])
        self.assertEqual(out.items[0].reason, "stopped")
        with self.assertRaises(RuntimeError):
            t.start()


class HandshakeGateTests(unittest.TestCase):
    """No record may be sent before the handshake completes (§3.1a).

    The version selects the framing — one protocol_major.protocol_minor implies
    exactly one doorbell format — so a send beforehand is a guess about which
    format the peer reads."""

    def transport(self):
        plugin_end, host_end = make_pair("seqpacket")
        self.addCleanup(host_end.close)
        t = db.DoorbellTransport(plugin_end, "seqpacket", lambda o: None, handup_deadline=0.5)
        self.addCleanup(t.stop)
        return t, host_end

    def test_send_is_refused_until_the_handshake_settles(self):
        t, host_end = self.transport()
        t.start()
        self.assertFalse(t.ready)
        self.assertIs(t.send_notification(5, [1]), SendResult.NOT_READY)
        self.assertEqual(t.counters["send_before_handshake"], 1)
        t.handshake_complete(db.PROTOCOL_MAJOR, db.PROTOCOL_MINOR)
        self.assertTrue(t.ready)
        self.assertIs(t.send_notification(5, [1]), SendResult.SENT)
        host_end.settimeout(2.0)
        record = host_end.recv(120)
        # The refused send was never admitted, so it burned no id (§4).
        self.assertEqual(db._HEADER.unpack_from(record, 0)[4], 1)

    def test_a_major_mismatch_fails_whole_and_names_both(self):
        """§3.2: a MAJOR bump breaks.  A `0.x` peer is on the far side of one,
        and `2.0` is a future that cannot be assumed compatible."""
        for major, minor in ((0, 12), (0, 13), (2, 0)):
            with self.subTest(f"{major}.{minor}"):
                t, _ = self.transport()
                with self.assertRaises(db.ProtocolMismatch) as caught:
                    t.handshake_complete(major, minor)
                text = str(caught.exception)
                self.assertIn(f"{major}.{minor}", text)
                self.assertIn(f"{db.PROTOCOL_MAJOR}.{db.PROTOCOL_MINOR}", text)
                self.assertFalse(t.ready)
                self.assertIs(t.send_notification(5, [1]), SendResult.NOT_READY)

    def test_a_newer_minor_settles_the_contract(self):
        """§3.2: a minor bump is backward-compatible and may not change the
        framing, which is what makes accepting one safe rather than hopeful."""
        t, _ = self.transport()
        t.handshake_complete(db.PROTOCOL_MAJOR, db.PROTOCOL_MINOR + 4)
        self.assertTrue(t.ready)
        self.assertIs(t.send_notification(5, [1]), SendResult.SENT)

    def test_the_policy_is_a_pair_comparison_and_0_x_stays_exact(self):
        """A test on the minor alone -- the obvious `MINOR >= N` -- is
        permanently false at the next major, whose minor is 0.  And within
        `0.x` the match stays exact: those minors were allowed to change
        behavior."""
        self.assertTrue(db.compatible(db.PROTOCOL_MAJOR, db.PROTOCOL_MINOR))
        self.assertTrue(db.compatible(db.PROTOCOL_MAJOR, db.PROTOCOL_MINOR + 99))
        self.assertFalse(db.compatible(db.PROTOCOL_MAJOR + 1, db.PROTOCOL_MINOR))
        self.assertFalse(db.compatible(0, 12))
        self.assertFalse(db.compatible(0, 13))

    def test_a_mismatch_is_a_launch_failure_not_a_doorbell_fault(self):
        t, _ = self.transport()
        with self.assertRaises(db.DoorbellSetupError):
            t.handshake_complete(0, 12)  # a differing MAJOR
        self.assertIsNone(t.terminal)  # nothing to fault on; the session never began

    def test_session_token_echo_is_verified(self):
        token = "0123456789abcdef0123456789abcdef"
        plugin_end, host_end = make_pair("stream")
        self.addCleanup(host_end.close)
        out = Collector()
        t = db.DoorbellTransport(
            plugin_end, "stream", out, handup_deadline=0.5, session_token=token
        )
        self.addCleanup(t.stop)
        t.start()
        t.handshake_complete(db.PROTOCOL_MAJOR, db.PROTOCOL_MINOR, token)
        self.assertTrue(t.ready)

    def test_a_mismatched_token_echo_fails_the_session(self):
        token = "0123456789abcdef0123456789abcdef"
        wrong = "ffffffffffffffffffffffffffffffff"
        for supplied in (wrong, None):
            with self.subTest(str(supplied)):
                plugin_end, host_end = make_pair("stream")
                self.addCleanup(host_end.close)
                out = Collector()
                t = db.DoorbellTransport(
                    plugin_end, "stream", out, handup_deadline=0.5, session_token=token
                )
                self.addCleanup(t.stop)
                t.start()
                with self.assertRaises(db.DoorbellSetupError):
                    t.handshake_complete(db.PROTOCOL_MAJOR, db.PROTOCOL_MINOR, supplied)
                self.assertFalse(t.ready)
                # The doorbell is already open by then, so this ends the session
                # rather than failing the launch.
                self.assertIsInstance(t.terminal, Teardown)
                self.assertEqual(t.terminal.reason, "handshake_mismatch")
                self.assertIs(t.send_notification(5, [1]), SendResult.CLOSED)
                host_end.settimeout(2.0)
                self.assertEqual(host_end.recv(120), b"")

    def test_a_token_is_refused_where_the_transport_used_none(self):
        t, _ = self.transport()
        with self.assertRaises(db.DoorbellSetupError):
            t.handshake_complete(
                db.PROTOCOL_MAJOR, db.PROTOCOL_MINOR, "0123456789abcdef0123456789abcdef"
            )
        self.assertFalse(t.ready)

    def test_the_handshake_cannot_be_settled_after_the_end(self):
        t, _ = self.transport()
        t.start()
        t.stop()
        self.assertTrue(t.join(2.0))
        with self.assertRaises(db.DoorbellSetupError):
            t.handshake_complete(db.PROTOCOL_MAJOR, db.PROTOCOL_MINOR)


class WinsockClassificationTests(unittest.TestCase):
    """Windows behavior, reachable from any platform.

    CPython raises Winsock errors through PyErr_SetExcFromWindowsErr, which
    puts the raw WSAE* code on `winerror` and its POSIX translation on `errno`.
    Which of the two a given interpreter hands us is not the protocol's
    business, so both spellings must classify identically — and getting this
    wrong turns an ordinary Windows teardown, or our own shutdown(), into a
    spurious DoorbellFault (transport §6 rule 7).
    """

    def transport(self):
        plugin_end, host_end = make_pair("stream")
        self.addCleanup(host_end.close)
        t = db.DoorbellTransport(plugin_end, "stream", lambda o: None, handup_deadline=0.5)

        def shut():
            t.stop()
            if t._thread is None:  # never started: release the socket ourselves
                t._sock.close()
                t._wake_r.close()
                t._wake_w.close()
            else:
                t.join(2.0)

        self.addCleanup(shut)
        return t

    @staticmethod
    def winsock(code):
        exc = OSError()  # errno stays None, exactly as the winerror-only form
        exc.winerror = code
        return exc

    @staticmethod
    def posix(code):
        return OSError(code, os.strerror(code))

    def test_send_classification_matches_for_both_spellings(self):
        cases = [
            ("posix ECONNABORTED", self.posix(errno.ECONNABORTED), Teardown, "peer_gone"),
            ("wsa WSAECONNABORTED", self.winsock(10053), Teardown, "peer_gone"),
            ("posix ECONNRESET", self.posix(errno.ECONNRESET), Teardown, "peer_gone"),
            ("wsa WSAECONNRESET", self.winsock(10054), Teardown, "peer_gone"),
            ("posix ESHUTDOWN", self.posix(errno.ESHUTDOWN), Teardown, "locally_closed"),
            ("wsa WSAESHUTDOWN", self.winsock(10058), Teardown, "locally_closed"),
        ]
        for label, exc, cls, reason in cases:
            with self.subTest(label):
                t = self.transport()
                self.assertIs(t._classify_send_error(exc), SendResult.CLOSED)
                term = t.terminal
                self.assertIsInstance(term, cls, label)
                self.assertEqual(term.reason, reason, label)

    def test_backpressure_for_both_spellings(self):
        for label, exc in (
            ("posix", self.posix(errno.EWOULDBLOCK)),
            ("wsa", self.winsock(10035)),
            ("posix nobufs", self.posix(errno.ENOBUFS)),
            ("wsa nobufs", self.winsock(10055)),
        ):
            with self.subTest(label):
                t = self.transport()
                self.assertIs(t._classify_send_error(exc), SendResult.REFUSED)
                self.assertIsNone(t.terminal)  # backpressure is never terminal

    def test_os_error_carries_the_raw_code_not_its_translation(self):
        # §12 asks for the raw errno / WSA code.  On Windows CPython puts the
        # POSIX translation on .errno, so the winerror is the raw one.
        t = self.transport()
        t._classify_send_error(self.winsock(10053))
        self.assertEqual(t.terminal.os_error, 10053, "the WSA code, not ECONNABORTED")
        t2 = self.transport()
        t2._classify_send_error(self.posix(errno.ECONNRESET))
        self.assertEqual(t2.terminal.os_error, errno.ECONNRESET)

    def test_short_write_reports_zero_rather_than_absent(self):
        class ShortSend:
            def __init__(self, sock):
                self._s = sock

            def send(self, data, flags=0):
                self._s.send(data[:50], flags)
                return 50  # a torn record: the fault this ban exists to expose

            def __getattr__(self, name):
                return getattr(self._s, name)

        plugin_end, host_end = make_pair("stream")
        self.addCleanup(host_end.close)
        got = []
        t = db.DoorbellTransport(plugin_end, "stream", got.append, handup_deadline=0.5)
        t.handshake_complete(db.PROTOCOL_MAJOR, db.PROTOCOL_MINOR)
        self.addCleanup(t.stop)
        t._sock = ShortSend(t._sock)
        self.assertIs(t.send_notification(5, [1]), SendResult.CLOSED)
        fault = t.faults.get(timeout=2.0)
        self.assertEqual(fault.failure_class, "short_write")
        self.assertEqual(fault.observed_len, 50)
        self.assertEqual(fault.os_error, 0, "present and zero, not absent (§12)")
        self.assertIsNone(fault.ordinal, "a send fault has examined no frames")
        self.assertIsNone(fault.capture)

    def test_unclassified_send_error_is_an_ambiguous_send_fault(self):
        t = self.transport()
        self.assertIs(t._classify_send_error(self.posix(errno.EIO)), SendResult.CLOSED)
        self.assertIsInstance(t.terminal, Fault)
        self.assertEqual(t.terminal.failure_class, "ambiguous_send")

    def test_recv_classification_matches_for_both_spellings(self):
        cases = [
            ("posix ESHUTDOWN", self.posix(errno.ESHUTDOWN), "locally_closed"),
            ("wsa WSAESHUTDOWN", self.winsock(10058), "locally_closed"),
            ("posix ECONNABORTED", self.posix(errno.ECONNABORTED), "peer_gone"),
            ("wsa WSAECONNABORTED", self.winsock(10053), "peer_gone"),
        ]
        for label, exc, reason in cases:
            with self.subTest(label):
                t = self.transport()
                t._classify_recv_error(exc)
                self.assertIsInstance(t.terminal, Teardown, label)
                self.assertEqual(t.terminal.reason, reason, label)

    def test_would_block_and_eintr_are_not_errors_on_receive(self):
        for label, exc in (
            ("posix EAGAIN", self.posix(errno.EAGAIN)),
            ("wsa", self.winsock(10035)),
            ("EINTR", self.posix(errno.EINTR)),
        ):
            with self.subTest(label):
                t = self.transport()
                t._classify_recv_error(exc)
                self.assertIsNone(t.terminal)


@unittest.skipUnless(HAS_FD_PASSING, "SCM_RIGHTS grants need a unix message transport")
class DarwinDescriptorTests(TransportCase):
    """macOS has no MSG_CMSG_CLOEXEC, so a granted descriptor arrives
    inheritable and the receiver clears it immediately (transport §7.6).  The
    fallback is forced here so the non-atomic path runs on any platform."""

    def setUp(self):
        super().setUp()
        self.t._recv_flags = 0  # as if MSG_CMSG_CLOEXEC did not exist

    def test_granted_descriptor_is_not_inheritable_without_the_atomic_flag(self):
        self.needs_datagrams()
        r, w = os.pipe()
        self.out.claim_next = True
        self.host.m(fd=w)
        os.close(w)
        (f,) = self.out.wait(1)
        self.assertEqual(f.disposition, Disposition.DECODED)
        fd = f.descriptor.fd
        self.assertFalse(os.get_inheritable(fd), "the fallback must clear it after recvmsg")
        os.write(fd, b"!")
        self.assertEqual(os.read(r, 1), b"!")
        os.close(fd)
        os.close(r)


class WindowsShapeTests(unittest.TestCase):
    """The Windows path must not touch a unix-only socket symbol.

    A subprocess hides AF_UNIX, SCM_RIGHTS, CMSG_*, MSG_CTRUNC,
    MSG_CMSG_CLOEXEC, MSG_NOSIGNAL and MSG_DONTWAIT, then imports the module
    and drives a whole stream session — grant, input event, notification, a
    plugin->host D and an orderly teardown.  It proves on any platform that
    nothing Windows lacks is reachable there.
    """

    def test_stream_session_runs_without_unix_only_symbols(self):
        probe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_windows_shape_probe.py")
        proc = subprocess.run([sys.executable, probe], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        self.assertIn("OK", proc.stdout)


class SetupTests(unittest.TestCase):
    @unittest.skipUnless(HAS_AF_UNIX, "inherited-fd adoption is unix-only")
    def test_adopt_checks_type_and_clears_inheritable(self):
        # The platform's own type is adopted; any other unix type is refused.
        expected = db._SOCK_TYPE_FOR_CLASS[db.platform_transport_class()]
        a, b = socket.socketpair(socket.AF_UNIX, expected)
        fd = a.detach()
        os.set_inheritable(fd, True)
        sock = db.adopt_inherited_doorbell(fd)
        self.assertFalse(os.get_inheritable(fd))
        self.assertFalse(sock.getblocking())
        sock.close()
        b.close()
        other = socket.SOCK_STREAM if expected == socket.SOCK_DGRAM else socket.SOCK_DGRAM
        c, d = socket.socketpair(socket.AF_UNIX, other)
        fd = c.detach()
        with self.assertRaises(db.DoorbellSetupError):
            db.adopt_inherited_doorbell(fd)
        os.close(fd)
        d.close()

    @unittest.skipUnless(HAS_AF_UNIX, "inherited-fd adoption is unix-only")
    def test_a_stranded_descriptor_is_named_as_not_inherited(self):
        """A launcher that spawned this process as a child with the runtime's
        default descriptor policy passed the NUMBER through the environment
        and not the descriptor; the failure names that, not a bare errno on
        a number (`manifest_schema.md` §2.2)."""
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        fd = a.detach()
        os.close(fd)  # what the launcher's close_fds did, seen from here
        b.close()
        with self.assertRaises(db.DoorbellSetupError) as caught:
            db.adopt_inherited_doorbell(fd)
        self.assertIn("was not inherited", str(caught.exception))
        self.assertIn(str(fd), str(caught.exception))
        # A descriptor that IS held but is not a socket: the same strand, one
        # step later (the launcher reused the number).
        r, w = os.pipe()
        try:
            with self.assertRaises(db.DoorbellSetupError) as caught:
                db.adopt_inherited_doorbell(r)
            self.assertIn("was not inherited", str(caught.exception))
        finally:
            os.close(r)
            os.close(w)

    def test_missing_env_is_setup_error(self):
        os.environ.pop("VIZI_PLUGIN_DOORBELL_FD", None)
        if os.name != "nt":
            with self.assertRaises(db.DoorbellSetupError):
                db.adopt_inherited_doorbell()


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(sys.platform == "win32", "descriptor numbers are a POSIX limit")
class TestADescriptorAbove1024(unittest.TestCase):
    """The listener waited with select(), which refuses a descriptor at or
    above FD_SETSIZE (1024): a plugin with many files open ended its own
    session."""

    def test_the_doorbell_works_on_a_socket_numbered_above_1024(self):
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard != resource.RLIM_INFINITY and hard < 1200:
            self.skipTest(f"the hard descriptor limit is {hard}")
        if soft != resource.RLIM_INFINITY and soft < 1200:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(4096, hard), hard))
            self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        held = [os.open(os.devnull, os.O_RDONLY) for _ in range(1100)]
        self.addCleanup(lambda: [os.close(fd) for fd in held])
        plugin_end, host_end = make_pair("seqpacket")
        self.assertGreaterEqual(plugin_end.fileno(), 1024)
        host = Host(host_end)
        self.addCleanup(host.sock.close)
        out = Collector()
        t = db.DoorbellTransport(plugin_end, "seqpacket", out, handup_deadline=0.5)
        t.handshake_complete(db.PROTOCOL_MAJOR, db.PROTOCOL_MINOR)
        t.start()
        self.addCleanup(t.join, 2.0)
        self.addCleanup(t.stop)
        host.d(segment_id=5, regions=[1], epoch=3, view_id=0)
        (f,) = out.wait(1)
        self.assertEqual(f.disposition, Disposition.DECODED)
        self.assertEqual(f.record.segment_id, 5)
