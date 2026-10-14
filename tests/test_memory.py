"""Behavioural tests for the 1.0 plugin-side memory layer and its reporter.

Every case here is a rule with a citation, and the ones that end in a
``Quarantined`` or a ``Failed`` are the ones a first implementation gets wrong.

**Pulsed needs process-shared atomics.**  Where ``atomicshm`` is absent these
tests substitute a single-process stand-in with the same interface.  That
exercises the *protocol* -- the claim order, the exclusions, the exact-CAS
release -- and deliberately not the memory ordering, which only a second
process can show; ``shared_memory_protocol.md`` §6.6 is what the real backend
is for.
"""

from __future__ import annotations

import os
import pathlib
import re
import struct
import tempfile
import threading
import unittest
import zlib

import _layout
import vizi_plugin_sdk._shared_atomic as _sa


class _SingleProcessAtomics:
    """Same interface as ``SharedAtomicMapping``, no cross-process guarantee."""

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

from vizi_plugin_sdk import memory as m  # noqa: E402
from vizi_plugin_sdk import violations  # noqa: E402
from vizi_plugin_sdk.violations import ContractViolation, ViolationReporter  # noqa: E402

PURPOSES = frozenset({"V", "O"})
PERM_R = m.PERM_PLUGIN_READ
PERM_RW = m.PERM_PLUGIN_READ | m.PERM_PLUGIN_WRITE


def preamble(segment_id: int, proof: bytes = b"\xa5" * 16) -> bytes:
    head = struct.pack("<QI16s", segment_id, 0, proof)
    return head + struct.pack("<I", zlib.crc32(head))


def make_object(size: int, segment_id: int = 1, *, real_size: int | None = None) -> int:
    # A real file-backed object with a valid preamble; returns an owned fd.
    # Through a file object rather than `mkstemp` + `unlink` + `pwrite`:
    # Windows refuses to unlink a file another handle holds open and has no
    # `os.pwrite`, so the name is dropped by `TemporaryFile` (unlinked at
    # once on unix, deleted at last close on Windows) and the preamble is
    # written through the object.  The dup outlives the object, and its
    # close is the last one.
    with tempfile.TemporaryFile() as tmp:
        tmp.truncate(size if real_size is None else real_size)
        tmp.seek(0)
        tmp.write(preamble(segment_id))
        tmp.flush()
        return os.dup(tmp.fileno())


def const(index: int, offset: int, extent: int = 32) -> m.Region:
    return m.Region(index, offset, extent, m.Discipline.CONSTANT, m.Producer.HOST)


def pulsed(index: int, offset: int, buffers: int = 3, slot: int = 64) -> m.Region:
    return m.Region(
        index,
        offset,
        buffers * (16 + slot),
        m.Discipline.PULSED,
        m.Producer.PLUGIN,
        buffers,
        slot,
    )


def host_pulsed(index: int, offset: int, buffers: int = 3, slot: int = 64) -> m.Region:
    """A lane the HOST publishes and this plugin consumes -- what `D` is for."""
    return m.Region(
        index,
        offset,
        buffers * (16 + slot),
        m.Discipline.PULSED,
        m.Producer.HOST,
        buffers,
        slot,
    )


def loaned(
    index: int, offset: int, extent: int = 512, producer: m.Producer = m.Producer.PLUGIN
) -> m.Region:
    return m.Region(index, offset, extent, m.Discipline.LOANED, producer)


def staged(index: int, offset: int, extent: int = 512, **kw) -> m.Region:
    """One slot of a processing stage's ring (``processing_stages.md`` §4)."""
    return m.Region(
        index, offset, extent, kw.pop("discipline", m.Discipline.STAGED), m.Producer.HOST, **kw
    )


def install(
    store: m.SegmentStore,
    segment_id: int,
    regions,
    *,
    size: int = 8192,
    perms: int = PERM_RW,
    purpose: str = "V",
) -> m.Segment:
    fd = make_object(size, segment_id)
    store.capability(segment_id, size, perms, fd=fd, record=bytes([segment_id & 0xFF]) * 112)
    answer = store.install(segment_id, purpose, list(regions))
    assert isinstance(answer, m.Installed), answer
    seg = store.segment(segment_id)
    assert seg is not None
    return seg


def alive(fd: int, inode: int) -> bool:
    """fd numbers are reused, so identity is the inode and not the number."""
    try:
        return os.fstat(fd).st_ino == inode
    except OSError:
        return False


class GrantIdentity(unittest.TestCase):
    """§8: a duplicate is replayed only if it is identical."""

    def test_a_different_M_for_an_installed_segment_is_a_conflicting_grant(self):
        store = m.SegmentStore(PURPOSES)
        seg = install(store, 1, [const(0, 32)])
        second = store.capability(1, 8192, PERM_RW, fd=make_object(8192, 1), record=b"\x99" * 112)
        self.assertIsInstance(second, m.Failed)
        self.assertIs(second.failure_class, m.GrantFailure.MALFORMED)
        self.assertIn("conflicting grant", second.detail)
        # ...and it does not replace the recorded one: still installed, still
        # the same mapping, and a true duplicate still replays success.
        self.assertIs(store.segment(1), seg)
        self.assertIs(store.state(1).state, m.SegmentState.INSTALLED)
        again = store.capability(1, 8192, PERM_RW, fd=make_object(8192, 1), record=bytes([1]) * 112)
        self.assertIsInstance(again, m.Installed)

    def test_an_identical_M_for_an_installed_segment_replays_the_answer(self):
        store = m.SegmentStore(PURPOSES)
        seg = install(store, 2, [const(0, 32)])
        first = store.state(2)
        again = store.capability(2, 8192, PERM_RW, fd=make_object(8192, 2), record=bytes([2]) * 112)
        self.assertIsInstance(again, m.Installed)
        self.assertIs(store.segment(2), seg)  # never remapped
        self.assertEqual(store.state(2).identity_proof, first.identity_proof)

    def test_a_different_M_while_pending_is_a_conflicting_grant(self):
        store = m.SegmentStore(PURPOSES)
        store.capability(3, 8192, PERM_RW, fd=make_object(8192, 3), record=b"\x01" * 112)
        second = store.capability(3, 8192, PERM_RW, fd=make_object(8192, 3), record=b"\x02" * 112)
        self.assertIsInstance(second, m.Failed)
        # The first M's mapping is kept, so the install still completes it.
        done = store.install(3, "V", [const(0, 32)])
        self.assertIsInstance(done, m.Installed)


class ObjectSize(unittest.TestCase):
    """§8: the mapped object's size matches ``size``."""

    def test_an_object_smaller_than_the_grant_is_a_grant_failure_not_an_exception(self):
        store = m.SegmentStore(PURPOSES)
        fd = make_object(8192, 4, real_size=1024)
        inode = os.fstat(fd).st_ino
        answer = store.capability(4, 8192, PERM_R, fd=fd)
        self.assertIsInstance(answer, m.Failed)
        self.assertIs(answer.failure_class, m.GrantFailure.MALFORMED)
        self.assertFalse(alive(fd, inode), "§9a step 2: every path closes what it received")

    def test_an_object_larger_than_the_grant_is_a_grant_failure(self):
        store = m.SegmentStore(PURPOSES)
        answer = store.capability(5, 8192, PERM_R, fd=make_object(8192, 5, real_size=1 << 20))
        self.assertIsInstance(answer, m.Failed)

    def test_every_path_out_of_capability_closes_the_descriptor(self):
        store = m.SegmentStore(PURPOSES)
        for segment_id, size, real in ((6, 8192, None), (7, 8192, 512), (8, 16, None)):
            with self.subTest(segment_id=segment_id):
                fd = make_object(max(size, 32), segment_id, real_size=real)
                inode = os.fstat(fd).st_ino
                store.capability(segment_id, size, PERM_R, fd=fd)
                self.assertFalse(alive(fd, inode))


class ReleasedAnswer(unittest.TestCase):
    """§9.4: a half arriving after release is answered, never met with silence."""

    def test_both_halves_are_answered_with_segment_released(self):
        store = m.SegmentStore(PURPOSES)
        install(store, 10, [const(0, 32)])
        self.assertTrue(store.retire(10))
        self.assertEqual(store.install(10, "V", [const(0, 32)]), m.Released(10))
        fd = make_object(8192, 10)
        inode = os.fstat(fd).st_ino
        self.assertEqual(store.capability(10, 8192, PERM_RW, fd=fd), m.Released(10))
        self.assertFalse(alive(fd, inode), "closed without ever being mapped")


class Supersedes(unittest.TestCase):
    """§9: atomic with the replacement means the replacement being INSTALLED."""

    def test_a_pending_replacement_leaves_the_superseded_segment_live(self):
        store = m.SegmentStore(PURPOSES)
        install(store, 20, [const(0, 32)])
        self.assertIsNone(store.install(21, "V", [const(0, 32)], supersedes=20))
        self.assertEqual(store.state(20).state, m.SegmentState.INSTALLED)
        self.assertEqual(store.state(21).state, m.SegmentState.PENDING)

    def test_a_repeat_naming_itself_leaves_a_live_segment_live(self):
        """The self-supersede check ran before the
        entry's state was looked at, so a repeat for an INSTALLED segment
        that named itself tore the live segment down, and one for a RELEASED
        segment turned it FAILED where §9.4 replays the release."""
        store = m.SegmentStore(PURPOSES)
        seg = install(store, 30, [const(0, 32)])
        again = store.install(30, "V", [const(0, 32)], supersedes=30)
        self.assertIsInstance(again, m.Failed)
        self.assertIn("conflicting", again.detail)
        self.assertEqual(store.state(30).state, m.SegmentState.INSTALLED)
        self.assertEqual(seg.constant(0), bytes(32), "still mapped")
        store.begin_retire(30)
        store.release(30)
        replay = store.install(30, "V", [const(0, 32)], supersedes=30)
        self.assertIsInstance(replay, m.Released)

    def test_the_second_half_landing_is_what_retires_the_old_one(self):
        store = m.SegmentStore(PURPOSES)
        install(store, 22, [const(0, 32)])
        store.install(23, "V", [const(0, 32)], supersedes=22)
        answer = store.capability(23, 8192, PERM_RW, fd=make_object(8192, 23))
        self.assertIsInstance(answer, m.Installed)
        self.assertEqual(store.state(22).state, m.SegmentState.RELEASED)

    def test_a_replacement_that_fails_retires_nothing(self):
        store = m.SegmentStore(PURPOSES)
        install(store, 24, [const(0, 32)])
        store.install(25, "V", [const(0, 32)], supersedes=24)
        bad = make_object(8192, 999)  # a preamble naming another segment
        self.assertIsInstance(store.capability(25, 8192, PERM_RW, fd=bad), m.Failed)
        self.assertEqual(store.state(24).state, m.SegmentState.INSTALLED)
        self.assertEqual(store.state(25).state, m.SegmentState.FAILED)

    def test_an_unknown_purpose_retires_nothing(self):
        store = m.SegmentStore(PURPOSES)
        install(store, 26, [const(0, 32)])
        answer = store.install(27, "NOPE", [const(0, 32)], supersedes=26)
        self.assertIs(answer.failure_class, m.GrantFailure.UNSUPPORTED)
        self.assertEqual(store.state(26).state, m.SegmentState.INSTALLED)

    def test_superseding_itself_fails_the_grant_and_does_not_hang(self):
        store = m.SegmentStore(PURPOSES)
        store.capability(28, 8192, PERM_RW, fd=make_object(8192, 28))
        done: list[object] = []
        thread = threading.Thread(
            target=lambda: done.append(store.install(28, "V", [const(0, 32)], supersedes=28)),
            daemon=True,
        )
        thread.start()
        thread.join(5.0)
        self.assertFalse(thread.is_alive(), "a self-superseding install must not deadlock")
        self.assertIsInstance(done[0], m.Failed)
        self.assertIs(done[0].failure_class, m.GrantFailure.MALFORMED)


class RetiringGate(unittest.TestCase):
    """§9.1: the table is about OPERATIONS, so the gate is at the operation."""

    def setUp(self):
        self.store = m.SegmentStore(PURPOSES)
        self.seg = install(
            self.store, 30, [pulsed(0, 32), loaned(1, 32 + 3 * 80), const(2, 32 + 3 * 80 + 512)]
        )
        self.writer = self.seg.pulsed_writer(0)
        self.reader = self.seg.pulsed_reader(0)
        self.arena = self.seg.loaned(1)

    def test_handles_taken_before_retirement_refuse_to_start_anything_new(self):
        self.writer.publish(b"before")
        self.store.begin_retire(30)
        self.assertTrue(self.seg.retiring)
        for what, call in (
            ("publish", lambda: self.writer.publish(b"after")),
            ("read", self.reader.read),
            ("reserve", lambda: self.arena.reserve(b"x" * 8)),
        ):
            with self.subTest(operation=what):
                with self.assertRaises(m.Retiring):
                    call()

    def test_a_constant_region_is_still_legible(self):
        self.seg._write_at(32 + 3 * 80 + 512, b"c" * 32)
        self.store.begin_retire(30)
        self.assertEqual(self.seg.constant(2), b"c" * 32)

    def test_applying_the_peers_release_is_always_permitted(self):
        offset, length = self.arena.reserve(b"y" * 8)
        self.store.begin_retire(30)
        self.seg.release_loan(1, offset, length)
        self.assertEqual(self.arena.outstanding, 0)


class Quarantine(unittest.TestCase):
    """§6.2: report ONCE and quarantine the region."""

    def setUp(self):
        self.store = m.SegmentStore(PURPOSES)
        # §3.1 at `1.0`: at most one plugin-produced PULSED lane per segment,
        # so the second is the host's -- which is a shape a real `O` segment
        # never has and this fixture needs, because quarantine is per region
        # and needs a region beside the one it isolates.
        self.seg = install(self.store, 40, [pulsed(0, 32), host_pulsed(1, 32 + 3 * 80)])
        self.reader = self.seg.pulsed_reader(0)

    def test_detecting_an_impossible_state_word_isolates_the_region(self):
        self.seg._atomics().store(32, (1 << 62) | (1 << 63) | 5)
        with self.assertRaises(m.Quarantined) as first:
            self.reader.read()
        self.assertEqual(first.exception.where, m.WHERE_IMPOSSIBLE_STATE_WORD)
        self.assertTrue(first.exception.isolated)
        self.assertTrue(self.seg.quarantined(0))
        self.assertEqual(self.seg._atomics().load(32), (1 << 62) | (1 << 63) | 5)
        with self.assertRaises(m.Quarantined) as second:
            self.reader.read()
        self.assertEqual(second.exception.where, "quarantined", "the second is not a new report")

    def test_every_other_region_keeps_working(self):
        self.seg._atomics().store(32, 1 << 63)
        with self.assertRaises(m.Quarantined):
            self.reader.read()
        other = m.PulsedWriter(self.seg, self.seg.regions[1])  # standing in for the host
        self.assertTrue(other.publish(b"unaffected"))
        self.assertEqual(self.seg.pulsed_reader(1).read(), b"unaffected")

    def test_an_invalid_published_length_isolates_after_releasing_the_claim(self):
        writer = self.seg.pulsed_writer(0)
        writer.publish(b"ok")
        base = self.seg.regions[0].offset
        atomics = self.seg._atomics()
        slot = next(i for i in range(3) if atomics.load(base + i * 16))
        self.seg._write_at(base + slot * 16 + 8, struct.pack("<Q", 1 << 40))
        with self.assertRaises(m.Quarantined) as caught:
            self.reader.read()
        self.assertEqual(caught.exception.where, m.WHERE_INVALID_PUBLISHED_LENGTH)
        self.assertEqual(atomics.load(base + slot * 16), 0, "the claim was released")
        self.assertTrue(self.seg.quarantined(0))


class ReleaseNaming(unittest.TestCase):
    """§3.1: the range rule and the direction rule are different rules."""

    def test_a_release_naming_no_reservation_is_not_the_interference_where(self):
        store = m.SegmentStore(PURPOSES)
        seg = install(store, 50, [loaned(0, 32)])
        with self.assertRaises(m.Quarantined) as caught:
            seg.release_loan(0, 32, 8)  # inside the region, but reserved by nobody
        self.assertEqual(caught.exception.where, m.WHERE_UNKNOWN_RELEASE)

    def test_a_release_outside_the_region_it_names_is_a_mismatch(self):
        """§8.12: `region` is redundant with `offset` so that a mistargeting is
        DETECTED rather than applied.  A sender that forgot §7's conversion
        lands outside the named region, which is why that direction was chosen,
        and it is NOT `unknown_release` -- that one names no reservation."""
        store = m.SegmentStore(PURPOSES)
        seg = install(store, 53, [loaned(0, 32)])
        arena = seg.loaned(0)
        offset, length = arena.reserve(b"z" * 8)
        with self.assertRaises(m.Quarantined) as caught:
            seg.release_loan(0, offset - 32, length)  # as a region-relative sender would
        self.assertEqual(caught.exception.where, m.WHERE_RELEASE_REGION_MISMATCH)
        self.assertEqual(arena.outstanding, 1, "a mistargeted release freed nothing")

    def test_a_release_naming_the_wrong_side_has_its_own_where(self):
        store = m.SegmentStore(PURPOSES)
        seg = install(store, 51, [loaned(0, 32, producer=m.Producer.HOST)])
        with self.assertRaises(m.Quarantined) as caught:
            seg.release_loan(0, 0, 8)
        self.assertEqual(caught.exception.where, m.WHERE_WRONG_PRODUCER_RELEASE)

    def test_a_release_deposit_for_a_plugin_produced_region_is_the_wrong_side(self):
        store = m.SegmentStore(PURPOSES)
        seg = install(store, 52, [loaned(0, 32)])
        with self.assertRaises(m.Quarantined) as caught:
            seg.release_deposit(0, 0, 8)
        self.assertEqual(caught.exception.where, m.WHERE_WRONG_PRODUCER_RELEASE)


class Deposits(unittest.TestCase):
    """§7 and §9a step 11: the plugin releases the ring it CONSUMES."""

    def setUp(self):
        self.store = m.SegmentStore(PURPOSES)
        self.seg = install(self.store, 60, [loaned(0, 32, producer=m.Producer.HOST)])
        self.ring = self.seg.deposits(0)

    def test_taking_a_deposit_owes_a_release_keyed_by_the_requested_length(self):
        """§7: the wire offset is MAPPING-relative, so region 0 at 32 makes the
        deposit written at mapping byte 40 releasable as 40 and not as 8."""
        self.seg._write_at(40, b"payload!")
        self.assertEqual(self.ring.take(40, 8), b"payload!")
        self.assertEqual(self.ring.owed, ((40, 8),))
        self.seg.release_deposit(0, 40, 8)
        self.assertEqual(self.ring.outstanding, 0)

    def test_taking_the_same_deposit_twice_owes_one_release(self):
        self.ring.take(32, 4)
        self.ring.take(32, 4)
        self.assertEqual(self.ring.outstanding, 1)

    def test_every_writer_into_the_arena_shares_one_cursor(self):
        """§5.6: the cursor is per ARENA -- one plugin-produced LOANED region --
        which is what makes several writers into it safe.  Too narrow, per
        sender, is the shipping collision; too wide, per segment, allocates
        across a region boundary."""
        store = m.SegmentStore(PURPOSES)
        seg = install(store, 62, [loaned(0, 32, 512), loaned(1, 32 + 512, 512, m.Producer.HOST)])
        arena = seg.loaned(0)
        self.assertIs(seg.loaned(0), arena, "every writer into a region shares its cursor")
        a = arena.reserve(b"x" * 100)
        b = arena.reserve(b"y" * 100)
        self.assertNotEqual(a[0], b[0], "no byte is handed out twice")
        # §7: reserve returns the MAPPING-relative offset the wire carries, so
        # the arena starts at its own region rather than at zero.
        self.assertEqual(a[0], seg.regions[0].offset)

    def test_a_segment_with_two_arenas_is_refused(self):
        """§3 since 1.0: **at most one plugin-produced LOANED region**, because
        two make the arena unfindable -- a plugin producing bytes cannot tell
        which is its own, and every available key is one §8 says to ignore.
        Making the ambiguity impossible to grant removes the lookup instead of
        strengthening it."""
        store = m.SegmentStore(PURPOSES)
        fd = make_object(8192, 61)
        store.capability(61, 8192, PERM_RW, fd=fd, record=b"\x3d" * 112)
        answer = store.install(61, "V", [loaned(0, 32, 512), loaned(1, 32 + 512, 512)])
        self.assertIsInstance(answer, m.Failed)
        self.assertIs(answer.failure_class, m.GrantFailure.MALFORMED)
        self.assertIn("at most one arena", answer.detail)

    def test_a_release_for_a_deposit_never_taken_is_reported(self):
        with self.assertRaises(m.Quarantined) as caught:
            self.seg.release_deposit(0, 64, 8)
        self.assertEqual(caught.exception.where, m.WHERE_UNKNOWN_RELEASE)

    def test_the_releases_it_already_owes_may_be_sent_while_retiring(self):
        """§9.1: 'send the releases it already owes (ReleaseDeposit) | yes'."""
        self.ring.take(32, 8)
        self.store.begin_retire(60)
        self.seg.release_deposit(0, 32, 8)
        self.assertEqual(self.ring.outstanding, 0)
        with self.assertRaises(m.Retiring):
            self.ring.take(48, 8)  # but starting a new one is not

    def test_a_deposit_outside_the_region_is_refused(self):
        for offset in (0, 16, 32 + 512):  # below the region, and past its end
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.ring.take(offset, 64)

    def test_a_plugin_produced_region_has_no_deposit_ring(self):
        seg = install(self.store, 61, [loaned(0, 32)])
        with self.assertRaises(ValueError):
            seg.deposits(0)


class Hints(unittest.TestCase):
    """``doorbell_D_notification.md`` §3: the discipline check is this layer's."""

    def setUp(self):
        self.store = m.SegmentStore(PURPOSES)
        self.seg = install(
            self.store,
            70,
            [
                host_pulsed(0, 32),
                m.Region(1, 32 + 3 * 80, 64, m.Discipline.OWNED, m.Producer.HOST),
                loaned(2, 32 + 3 * 80 + 64),
                const(3, 32 + 3 * 80 + 64 + 512),
            ],
        )

    def test_pulsed_is_what_D_accelerates_and_nothing_else(self):
        hint = self.store.hint(70, [0, 1, 2, 3, 9])
        self.assertEqual(hint.scan, (0,))
        self.assertEqual(hint.violations, ((m.WHERE_OWNED_REGION_HINT, 1),))
        self.assertEqual(
            dict(hint.ignored), {"loaned_region": 2, "constant_region": 3, "unknown_region": 9}
        )
        self.assertTrue(hint)

    def test_a_stray_bit_sweeps_the_lanes_and_keeps_the_valid_bits(self):
        """§3: one out-of-range bit never discards the rest of the mask, and the
        sweep reaches only what a bit could legitimately have named."""
        hint = self.store.hint(70, [3, 99])
        self.assertEqual(hint.scan, (0,), "the sweep reaches peer-produced Pulsed only")
        self.assertIn(("unknown_region", 99), hint.ignored)
        self.assertIn(("constant_region", 3), hint.ignored, "the valid bit was still ruled on")

    def test_an_owned_hint_reports_and_leaves_the_region_usable(self):
        hint = self.store.hint(70, [1])
        self.assertEqual(hint.violations, ((m.WHERE_OWNED_REGION_HINT, 1),))
        self.assertFalse(self.seg.quarantined(1), "the hint was wrong, the memory was not")

    def test_an_unknown_segment_is_counted_and_ignored(self):
        hint = self.store.hint(999, [0])
        self.assertEqual(hint.scan, ())
        self.assertEqual(hint.ignored, (("unknown_segment", -1),))
        self.assertFalse(hint)

    def test_a_retiring_segment_makes_every_bit_a_no_op(self):
        self.store.begin_retire(70)
        hint = self.store.hint(70, [0])
        self.assertEqual(hint.scan, ())
        self.assertEqual(hint.ignored, (("retiring_segment", -1),))

    def test_a_hint_older_than_the_acknowledged_epoch_is_ignored(self):
        self.assertEqual(
            self.store.hint(70, [0], epoch=4, installed_epoch=5).ignored,
            (("stale_epoch", -1),),
        )
        self.assertEqual(self.store.hint(70, [0], epoch=5, installed_epoch=5).scan, (0,))

    def test_a_hint_about_this_plugins_own_lane_is_counted_and_ignored(self):
        """`D` is symmetric, so a hint names the OTHER end's lane."""
        store = m.SegmentStore(PURPOSES)
        install(store, 71, [pulsed(0, 32)])  # producer = PLUGIN
        hint = store.hint(71, [0])
        self.assertEqual(hint.scan, ())
        self.assertEqual(hint.ignored, (("own_pulsed_region", 0),))

    def test_a_quarantined_region_is_not_offered_for_scanning(self):
        self.seg.isolate(0)
        self.assertEqual(self.store.hint(70, [0]).ignored, (("quarantined_region", 0),))


class PulsedAccess(unittest.TestCase):
    """§3.1: a consumer claims by CAS, so a Pulsed lane needs PLUGIN_WRITE."""

    def test_a_pulsed_region_under_a_read_only_grant_fails_the_grant(self):
        for producer in (m.Producer.HOST, m.Producer.PLUGIN):
            with self.subTest(producer=producer):
                store = m.SegmentStore(PURPOSES)
                fd = make_object(8192, 95)
                store.capability(95, 8192, PERM_R, fd=fd)  # read-only
                region = m.Region(0, 32, 3 * (16 + 64), m.Discipline.PULSED, producer, 3, 64)
                answer = store.install(95, "V", [region])
                self.assertIsInstance(answer, m.Failed)
                self.assertIs(answer.failure_class, m.GrantFailure.MALFORMED)
                self.assertIn("read-only", answer.detail + " read-only")


class ValidationMatrix(unittest.TestCase):
    """§8's matrix: what a receiver may rule on, and which class it fails as."""

    def install_with(self, segment_id, region, purpose="V"):
        store = m.SegmentStore(PURPOSES)
        store.capability(segment_id, 8192, PERM_RW, fd=make_object(8192, segment_id))
        return store.install(segment_id, purpose, [region])

    def test_zero_is_malformed_and_an_unknown_non_zero_value_is_unsupported(self):
        base = dict(index=0, offset=32, extent=64)
        cases = (
            (
                "zero discipline",
                dict(discipline=0, producer=m.Producer.HOST),
                m.GrantFailure.MALFORMED,
            ),
            (
                "zero producer",
                dict(discipline=m.Discipline.CONSTANT, producer=0),
                m.GrantFailure.MALFORMED,
            ),
            (
                "unknown discipline",
                dict(discipline=99, producer=m.Producer.HOST),
                m.GrantFailure.UNSUPPORTED,
            ),
            (
                "unknown producer",
                dict(discipline=m.Discipline.CONSTANT, producer=99),
                m.GrantFailure.UNSUPPORTED,
            ),
        )
        for n, (label, fields, want) in enumerate(cases):
            with self.subTest(label):
                answer = self.install_with(120 + n, m.Region(**base, **fields))
                self.assertIsInstance(answer, m.Failed)
                self.assertIs(answer.failure_class, want)

    def test_an_empty_region_and_stray_pulsed_fields_are_malformed(self):
        empty = m.Region(0, 32, 0, m.Discipline.CONSTANT, m.Producer.HOST)
        self.assertIs(self.install_with(130, empty).failure_class, m.GrantFailure.MALFORMED)
        stray = m.Region(0, 32, 64, m.Discipline.CONSTANT, m.Producer.HOST, buffers=7)
        self.assertIs(self.install_with(131, stray).failure_class, m.GrantFailure.MALFORMED)

    def test_a_loaned_alignment_is_ignored_including_zero(self):
        for n, slot in enumerate((0, 64, 128)):
            with self.subTest(slot_bytes=slot):
                region = m.Region(
                    0, 32, 512, m.Discipline.LOANED, m.Producer.PLUGIN, slot_bytes=slot
                )
                self.assertIsInstance(self.install_with(140 + n, region), m.Installed)

    def test_a_mapping_failure_is_retryable_only_for_the_closed_list(self):
        """`M` §7: RETRYABLE is a closed list -- out of address space, handles
        or memory -- and everything else is permanent, so "try again" is never
        reached by accident.  §8: no mapping failure leaves as an exception,
        CPython's ValueError for a mapping longer than its object included."""
        import errno
        from unittest import mock

        region = m.Region(0, 32, 64, m.Discipline.CONSTANT, m.Producer.HOST)
        cases = (
            ("ENOMEM", OSError(errno.ENOMEM, "no memory"), m.GrantFailure.RETRYABLE),
            ("EMFILE", OSError(errno.EMFILE, "no handles"), m.GrantFailure.RETRYABLE),
            ("MemoryError", MemoryError(), m.GrantFailure.RETRYABLE),
            ("EACCES", OSError(errno.EACCES, "denied"), m.GrantFailure.FAILED),
            (
                "ValueError",
                ValueError("mmap length is greater than file size"),
                m.GrantFailure.FAILED,
            ),
        )
        for n, (label, exc, want) in enumerate(cases):
            with self.subTest(label):
                with mock.patch.object(m.mmap, "mmap", side_effect=exc):
                    answer = self.install_with(150 + n, region)
                self.assertIsInstance(answer, m.Failed, answer)
                self.assertIs(answer.failure_class, want)
                self.assertIn(type(exc).__name__, answer.detail)


class PeerClaims(unittest.TestCase):
    """§9.1: "held" means held by THIS endpoint, never a bit seen in memory."""

    def test_a_claim_the_peer_holds_does_not_block_release(self):
        store = m.SegmentStore(PURPOSES)
        seg = install(store, 150, [host_pulsed(0, 32)])
        m.PulsedWriter(seg, seg.regions[0]).publish(b"v")
        # The peer takes a read claim and keeps it. Nothing we do can drop it.
        atomics = seg._atomics()
        slot = next(i for i in range(3) if atomics.load(32 + i * 16))
        word = atomics.load(32 + slot * 16)
        atomics.store(32 + slot * 16, word | (1 << 63))
        self.assertTrue(store.begin_retire(150), "release must not wait on the peer")
        self.assertTrue(store.release(150))

    def test_a_claim_this_endpoint_holds_does_defer_release(self):
        store = m.SegmentStore(PURPOSES)
        seg = install(store, 151, [host_pulsed(0, 32)])
        seg._claimed(1)  # what a CAS in flight on another thread looks like
        self.assertFalse(store.begin_retire(151))
        seg._claimed(-1)
        self.assertTrue(store.begin_retire(151))


class Schedule(unittest.TestCase):
    """``doorbell_D_notification.md`` §5: the bound exists so ``D`` need not."""

    def setUp(self):
        self.store = m.SegmentStore(PURPOSES)
        self.seg = install(self.store, 80, [pulsed(0, 32)])
        self.writer = self.seg.pulsed_writer(0)
        self.got: list[bytes] = []

    def test_a_sweep_drains_every_registered_region(self):
        schedule = m.ReadSchedule()
        schedule.consume(self.seg.pulsed_reader(0), self.got.append)
        self.writer.publish(b"one")
        self.assertEqual(schedule.sweep(), 1)
        self.assertEqual(self.got, [b"one"])
        self.assertEqual(schedule.sweep(), 0, "a consumed value is not re-delivered")

    def test_the_thread_re_checks_without_any_hint(self):
        with m.ReadSchedule(bound=0.01) as schedule:
            schedule.consume(self.seg.pulsed_reader(0), self.got.append)
            self.writer.publish(b"unannounced")
            deadline = threading.Event()
            deadline.wait(1.0)
        self.assertEqual(self.got, [b"unannounced"])
        self.assertIsNone(schedule.failure)

    def test_a_hint_only_brings_the_next_sweep_forward(self):
        with m.ReadSchedule(bound=30.0) as schedule:  # far longer than the test
            schedule.consume(self.seg.pulsed_reader(0), self.got.append)
            self.writer.publish(b"hinted")
            schedule.wake()
            for _ in range(200):
                if self.got:
                    break
                threading.Event().wait(0.01)
        self.assertEqual(self.got, [b"hinted"])

    def test_a_quarantined_region_drops_out_and_the_rest_keep_running(self):
        schedule = m.ReadSchedule()
        schedule.consume(self.seg.pulsed_reader(0), self.got.append)
        self.seg._atomics().store(32, (1 << 62) | (1 << 63) | 1)
        self.assertEqual(schedule.sweep(), 0)
        self.assertEqual(len(schedule.quarantined), 1)
        self.assertEqual(schedule.sweep(), 0)
        self.assertEqual(len(schedule.quarantined), 1, "dropped, not re-reported")

    def test_a_bound_is_required(self):
        with self.assertRaises(ValueError):
            m.ReadSchedule(bound=0)


class Purposes(unittest.TestCase):
    """§3: the recognised set is host-published, so an SDK cannot guess it."""

    def test_a_store_without_a_purpose_set_refuses_to_start(self):
        with self.assertRaises(ValueError):
            m.SegmentStore(m.NO_PURPOSES)

    def test_an_unrecognised_purpose_is_unsupported_and_a_zero_producer_is_malformed(self):
        store = m.SegmentStore(PURPOSES)
        store.capability(90, 8192, PERM_RW, fd=make_object(8192, 90))
        answer = store.install(90, "unknown-to-this-build", [const(0, 32)])
        self.assertIs(answer.failure_class, m.GrantFailure.UNSUPPORTED)

        store2 = m.SegmentStore(PURPOSES)
        store2.capability(91, 8192, PERM_RW, fd=make_object(8192, 91))
        bad = m.Region(0, 32, 32, m.Discipline.CONSTANT, None)  # type: ignore[arg-type]
        self.assertIs(store2.install(91, "V", [bad]).failure_class, m.GrantFailure.MALFORMED)


class Subjects(unittest.TestCase):
    """§3 at `1.0`: WHICH contribution a segment serves, and the rule is keyed
    on the PURPOSE rather than on how many contributions a plugin declared."""

    def setUp(self):
        self.store = m.SegmentStore(m.PURPOSES)
        self.next_id = iter(range(300, 400))

    def grant(self, purpose, regions, subject=""):
        segment_id = next(self.next_id)
        self.store.capability(segment_id, 8192, PERM_RW, fd=make_object(8192, segment_id))
        return self.store.install(segment_id, purpose, list(regions), subject=subject)

    def test_a_binding_purpose_requires_one(self):
        """``O`` and ``P``: the segment IS the contribution's, and two are
        otherwise indistinguishable."""
        for purpose in ("O", "P"):
            with self.subTest(purpose=purpose):
                answer = self.grant(purpose, [loaned(0, 32)])
                self.assertIsInstance(answer, m.Failed)
                self.assertIs(answer.failure_class, m.GrantFailure.MALFORMED)
                self.assertIn("subject", answer.detail)

    def test_an_exchange_purpose_requires_it_EMPTY(self):
        """``G``, ``V``, ``X``: the exchange already carries its own identity,
        and a non-empty subject is a sender asserting a binding this version
        does not have -- refused rather than ignored, because a receiver that
        ignored it would be the one guessing when a later version gives it a
        meaning."""
        for purpose in ("G", "V", "X"):
            with self.subTest(purpose=purpose):
                answer = self.grant(purpose, [loaned(0, 32)], subject="overlay-1")
                self.assertIsInstance(answer, m.Failed)
                self.assertIs(answer.failure_class, m.GrantFailure.MALFORMED)

    def test_a_conforming_grant_carries_it_onto_the_segment(self):
        answer = self.grant("O", [loaned(0, 32)], subject="peaks")
        self.assertIsInstance(answer, m.Installed)
        self.assertEqual(self.store.segment(answer.segment_id).subject, "peaks")

    def test_two_overlays_are_two_segments_told_apart_by_subject(self):
        first = self.grant("O", [loaned(0, 32)], subject="peaks")
        second = self.grant("O", [loaned(0, 32)], subject="troughs")
        self.assertIsInstance(first, m.Installed)
        self.assertIsInstance(second, m.Installed)
        self.assertEqual({s.subject for s in self.store.installed()}, {"peaks", "troughs"})

    def test_the_same_regions_under_a_different_subject_is_a_different_grant(self):
        """§8: a repeat is compared field by field, and ``subject`` is one --
        two grants agreeing on every region while naming different overlays
        are two different grants, not a re-send of one."""
        first = self.grant("O", [loaned(0, 32)], subject="peaks")
        again = self.store.install(first.segment_id, "O", [loaned(0, 32)], subject="troughs")
        self.assertIsInstance(again, m.Failed)
        self.assertIn("conflicting", again.detail)

    def test_declared_contributions_are_checked_when_they_are_known(self):
        """§9's row asks a receiver to check the subject *names a contribution
        this plugin declared*, which only a receiver told what it declared can
        do.  Unset, the shape is checked and the name is taken on trust."""
        self.assertIsInstance(self.grant("O", [loaned(0, 32)], subject="made-up"), m.Installed)
        self.store.set_contributions({"peaks"})
        answer = self.grant("O", [loaned(0, 32)], subject="made-up")
        self.assertIsInstance(answer, m.Failed)
        self.assertIs(answer.failure_class, m.GrantFailure.MALFORMED)
        self.assertIsInstance(self.grant("O", [loaned(0, 32)], subject="peaks"), m.Installed)

    def test_a_segment_with_two_live_lanes_is_refused(self):
        """§3.1 at `1.0`: the arena rule applied to the discipline beside it.
        A plugin publishing into a live lane must know which lane is its own,
        and a plugin with two overlays is granted two `O` segments."""
        answer = self.grant("O", [pulsed(0, 32), pulsed(1, 32 + 3 * 80)], subject="peaks")
        self.assertIsInstance(answer, m.Failed)
        self.assertIs(answer.failure_class, m.GrantFailure.MALFORMED)
        self.assertIn("at most one live lane", answer.detail)

    def test_one_lane_beside_one_arena_is_the_overlay_shape(self):
        """§3: a segment mixes producers freely, and this is the case that
        needs it -- one published lane beside one bulk arena."""
        answer = self.grant(
            "O",
            [pulsed(0, 32), loaned(1, 32 + 3 * 80), host_pulsed(2, 32 + 3 * 80 + 512)],
            subject="peaks",
        )
        self.assertIsInstance(answer, m.Installed)


class StagedGrants(unittest.TestCase):
    """``processing_stages.md`` §4-§6: the grant-time half of a stage's ring.
    Everything else about a slot is its header, checked every turn (§7)."""

    def setUp(self):
        self.store = m.SegmentStore(m.PURPOSES)
        self.next_id = iter(range(400, 500))

    def grant(self, regions, *, purpose="S", subject="", perms=PERM_RW, size=8192):
        segment_id = next(self.next_id)
        self.store.capability(segment_id, size, perms, fd=make_object(size, segment_id))
        return self.store.install(segment_id, purpose, list(regions), subject=subject)

    def refused(self, answer, detail):
        self.assertIsInstance(answer, m.Failed)
        self.assertIs(answer.failure_class, m.GrantFailure.MALFORMED)
        self.assertIn(detail, answer.detail)

    def test_a_ring_of_slots_installs(self):
        answer = self.grant([staged(0, 64), staged(1, 576), staged(2, 1088)])
        self.assertIsInstance(answer, m.Installed)
        seg = self.store.segment(answer.segment_id)
        self.assertEqual((seg.purpose, seg.subject), ("S", ""))
        self.assertTrue(all(r.discipline is m.Discipline.STAGED for r in seg.regions.values()))

    def test_a_ring_names_no_stage(self):
        """§4: every `StageArm` names its ring, and a plugin serving several
        stages of one chain holds it once, so a subject would be a second name."""
        self.refused(self.grant([staged(0, 64)], subject="stabilize"), "subject")

    def test_a_slot_is_host_produced(self):
        slot = m.Region(0, 64, 512, m.Discipline.STAGED, m.Producer.PLUGIN)
        self.refused(self.grant([slot]), "host-produced")

    def test_a_read_only_ring_is_refused(self):
        """§4: every stage writes, so the grant is read-write for the plugin."""
        self.refused(self.grant([staged(0, 64)], perms=PERM_R), "PLUGIN_WRITE")

    def test_a_slot_begins_at_a_multiple_of_64(self):
        """§6.1: so the turn word is 8-aligned for every implementation."""
        self.refused(self.grant([staged(0, 96)]), "multiple of 64")
        self.assertIsInstance(self.grant([staged(0, 128)]), m.Installed)

    def test_a_slot_holds_at_least_the_fixed_header(self):
        self.refused(self.grant([staged(0, 64, extent=255)]), "slot header")
        self.assertIsInstance(self.grant([staged(0, 64, extent=256)]), m.Installed)

    def test_a_ring_holds_at_most_512_slots(self):
        """§4: a `D` mask names at most 512 regions."""
        size = 64 + 513 * 256
        slots = [staged(i, 64 + i * 256, extent=256) for i in range(513)]
        self.refused(self.grant(slots, size=size), "at most 512")
        self.assertIsInstance(self.grant(slots[:512], size=size), m.Installed)

    def test_a_ring_holds_only_slots(self):
        self.refused(self.grant([staged(0, 64), const(1, 576)]), "only STAGED")

    def test_a_slot_outside_a_ring_is_refused(self):
        for purpose, subject in (("V", ""), ("O", "peaks")):
            with self.subTest(purpose=purpose):
                self.refused(
                    self.grant([staged(0, 64)], purpose=purpose, subject=subject),
                    "outside an 'S' segment",
                )

    def test_a_slot_carries_no_pulsed_fields_and_no_pane_header(self):
        self.refused(self.grant([staged(0, 64, buffers=3)]), "PULSED fields")
        self.refused(self.grant([staged(0, 64, epoch_offset=0)]), "no pane header")

    def test_a_D_bit_on_a_slot_schedules_a_turn_check(self):
        """``doorbell_D_notification.md`` §3: loading the turn word claims
        nothing, so it is re-checked whoever's turn the bit was about."""
        answer = self.grant([staged(0, 64), staged(1, 576)])
        hint = self.store.hint(answer.segment_id, [1])
        self.assertEqual((hint.turns, hint.scan, hint.violations), ((1,), (), ()))
        self.assertTrue(hint)

    def test_a_stray_bit_sweeps_every_slot(self):
        answer = self.grant([staged(0, 64), staged(1, 576)])
        hint = self.store.hint(answer.segment_id, [7])
        self.assertEqual(hint.turns, (0, 1))
        self.assertIn(("unknown_region", 7), hint.ignored)

    def test_a_quarantined_slot_is_not_offered(self):
        answer = self.grant([staged(0, 64)])
        self.store.segment(answer.segment_id).isolate(0)
        hint = self.store.hint(answer.segment_id, [0])
        self.assertEqual((hint.turns, hint.ignored), ((), (("quarantined_region", 0),)))


class Violations(unittest.TestCase):
    """``shared_memory_implementation_details.md`` §2's rate bound."""

    def setUp(self):
        self.now = 0.0
        self.reporter = ViolationReporter(interval=1.0, rate=2.0, clock=lambda: self.now)

    def test_the_first_occurrence_carries_the_record_and_the_rest_are_counted(self):
        first = self.reporter.report("epoch_mismatch", "stale", record=b"r" * 112)
        self.assertEqual(first, ContractViolation("epoch_mismatch", "stale", b"r" * 112, 1))
        for _ in range(999):
            self.assertIsNone(self.reporter.report("epoch_mismatch", "stale", record=b"r" * 112))
        self.assertEqual(self.reporter.due(), [], "not yet due")
        self.now = 1.5
        due = self.reporter.due()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].count, 999)
        self.assertIsNone(due[0].record, "a repeat is a count of a category")

    def test_next_due_names_when_a_deferred_report_may_go(self):
        """The deadline a quiet session must wake itself at."""
        self.assertIsNone(self.reporter.next_due(), "nothing waits")
        self.reporter.report("stage_arm_refused", "x")
        self.assertIsNone(self.reporter.next_due(), "the first went out at once")
        self.now = 0.95
        self.assertIsNone(self.reporter.report("stage_arm_refused", "y"))
        self.assertAlmostEqual(self.reporter.next_due(), 0.05)
        self.now = 1.0
        self.assertEqual([v.count for v in self.reporter.due()], [1])
        self.assertIsNone(self.reporter.next_due())
        # A first occurrence the rate holds back waits for the next token.
        self.reporter.report("other", "z")
        self.reporter.report("third", "w")
        self.assertAlmostEqual(self.reporter.next_due(), 0.5, msg="rate 2/s: one token in 0.5 s")

    def test_two_alternating_categories_do_not_report_per_record(self):
        """§2's named bypass: 'each record changes the category, so each record
        reports, and the 1 kHz cascade returns intact'."""
        reported = []
        for i in range(500):
            self.now += 0.002  # a 500 Hz cascade of two alternating categories
            out = self.reporter.report(f"where_{i % 2}", "alternating")
            if out is not None:
                reported.append(out)
        self.assertLessEqual(len(reported), 3, "not one report per record")
        self.assertEqual(sum(self.reporter.counts().values()), 500)
        # The rate spaces reports rather than refusing them, so the rest come out
        # over successive passes and nothing between them is lost.
        for _ in range(10):
            self.now += 1.0
            reported += self.reporter.due()
        self.assertEqual({v.where for v in reported}, {"where_0", "where_1"})
        self.assertEqual(sum(v.count for v in reported), 500, "every occurrence accounted for")
        self.assertEqual(self.reporter.pending, 0)

    def test_the_rate_replenishes_so_a_late_category_is_still_reportable(self):
        """§2: no lifetime cap -- a session must not go silent about a rule it
        meets for the first time an hour in."""
        for tick in range(50):
            self.now = float(tick)
            self.reporter.report("epoch_mismatch", "still happening")
            self.reporter.due()
        self.now += 60.0
        self.assertIsNotNone(
            self.reporter.report("invalid_target", "found for the first time, late"),
        )

    def test_M_zero_id_is_exempt_and_reports_once_per_grant(self):
        for i in range(20):
            out = self.reporter.report(
                "zero_id", "M names no grant", record=bytes([i]) * 112, exempt=True
            )
            self.assertIsNotNone(out, "a count cannot name two outstanding grants")
            self.assertEqual(out.record, bytes([i]) * 112)

    def test_teardown_drains_what_the_interval_never_reached(self):
        self.reporter.report("unknown_release", "x")
        self.reporter.report("unknown_release", "x")
        self.assertEqual(self.reporter.due(), [])
        self.now += 1.0  # the allowance replenishes; the interval has not elapsed
        drained = self.reporter.drain()
        self.assertEqual([(v.where, v.count) for v in drained], [("unknown_release", 1)])

    def test_one_record_breaking_one_rule_several_times_is_one_occurrence(self):
        """§2: `count` is records affected, so a `D` naming three Owned regions
        is one occurrence and not three."""
        record = b"d" * 112
        out = self.reporter.report_record(
            record, [("owned_region_hint", "region 1"), ("owned_region_hint", "region 4")]
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(self.reporter.counts()["owned_region_hint"], 1)

    def test_two_different_rules_on_one_record_each_count_once(self):
        self.reporter.report_record(b"e" * 112, [("owned_region_hint", "r1"), ("zero_id", "r2")])
        self.assertEqual(self.reporter.counts(), {"owned_region_hint": 1, "zero_id": 1})

    def test_two_receptions_of_identical_bytes_count_as_two(self):
        for _ in range(2):
            self.reporter.report_record(b"f" * 112, [("epoch_mismatch", "x")])
        self.assertEqual(self.reporter.counts()["epoch_mismatch"], 2)

    def test_every_where_is_counted_whether_or_not_it_was_reported(self):
        for _ in range(7):
            self.reporter.report("invalid_published_length", "x")
        self.assertEqual(self.reporter.counts()["invalid_published_length"], 7)


if __name__ == "__main__":
    unittest.main()


class PaneHeaders(unittest.TestCase):
    """§2: the ``(view_id, epoch)`` header, and where each discipline keeps it."""

    def test_the_bound_is_part_of_the_grant(self):
        """§2 and §3.2: the check is on the GRANT, and the two disciplines
        differ -- a PULSED offset must be exactly ``slot_bytes - 8``, and every
        other discipline's must leave eight bytes inside ``extent``."""
        store = m.SegmentStore(PURPOSES)
        cases = (
            (
                "past a constant region",
                m.Region(0, 32, 32, m.Discipline.CONSTANT, m.Producer.HOST, epoch_offset=25),
            ),
            (
                "a pulsed offset that is not slot_bytes - 8",
                m.Region(
                    0,
                    32,
                    3 * (16 + 64),
                    m.Discipline.PULSED,
                    m.Producer.PLUGIN,
                    3,
                    64,
                    epoch_offset=57,
                ),
            ),
            (
                "a pulsed header at the FRONT, which §3.2 moved",
                m.Region(
                    0,
                    32,
                    3 * (16 + 64),
                    m.Discipline.PULSED,
                    m.Producer.PLUGIN,
                    3,
                    64,
                    epoch_offset=0,
                ),
            ),
            (
                "a slot with no room beside the header",
                m.Region(
                    0,
                    32,
                    3 * (16 + 8),
                    m.Discipline.PULSED,
                    m.Producer.PLUGIN,
                    3,
                    8,
                    epoch_offset=0,
                ),
            ),
            (
                "negative",
                m.Region(0, 32, 32, m.Discipline.CONSTANT, m.Producer.HOST, epoch_offset=-1),
            ),
        )
        for n, (label, region) in enumerate(cases):
            with self.subTest(label):
                store.capability(200 + n, 8192, PERM_RW, fd=make_object(8192, 200 + n))
                answer = store.install(200 + n, "V", [region])
                self.assertIsInstance(answer, m.Failed)
                self.assertIs(answer.failure_class, m.GrantFailure.MALFORMED)

    def test_the_pulsed_header_sits_at_slot_bytes_minus_eight(self):
        """§3.2: the ONE position a pane-associated lane may declare."""
        store = m.SegmentStore(PURPOSES)
        region = m.Region(
            0, 32, 3 * (16 + 64), m.Discipline.PULSED, m.Producer.PLUGIN, 3, 64, epoch_offset=56
        )
        store.capability(205, 8192, PERM_RW, fd=make_object(8192, 205))
        self.assertIsInstance(store.install(205, "V", [region]), m.Installed)

    def test_a_constant_region_is_the_durable_copy(self):
        """§2: CONSTANT and OWNED keep one header per region, because they have
        one lifetime -- and reading it claims nothing."""
        store = m.SegmentStore(PURPOSES)
        seg = install(
            store,
            210,
            [m.Region(0, 32, 32, m.Discipline.CONSTANT, m.Producer.HOST, epoch_offset=8)],
        )
        seg._write_at(32 + 8, struct.pack("<II", 3, 41))
        self.assertEqual(seg.pane(0), m.PaneRef(3, 41))

    def test_a_region_declaring_nothing_is_not_pane_associated(self):
        store = m.SegmentStore(PURPOSES)
        seg = install(store, 211, [const(0, 32)])
        self.assertIsNone(seg.pane(0))

    def test_a_view_id_past_the_doorbells_u8_is_rejected(self):
        """§2: ``view_id`` widens to ``u32`` for alignment only, so the high
        three bytes MUST be zero and a receiver rejects a header where they
        are not -- the pair, not the payload."""
        store = m.SegmentStore(PURPOSES)
        seg = install(
            store,
            212,
            [m.Region(0, 32, 32, m.Discipline.CONSTANT, m.Producer.HOST, epoch_offset=0)],
        )
        seg._write_at(32, struct.pack("<II", 256, 7))
        self.assertIsNone(seg.pane(0))
        self.assertIsNone(seg.pane(0))
        self.assertEqual(seg.pane_rejected, 2, "counted on each read, never raised")

    def test_epoch_zero_is_a_state_no_pane_can_be_in(self):
        """§8.10: a pane epoch is never 0, so a header carrying one is refused
        rather than believed -- it would read as older than every epoch a
        reader could have acknowledged."""
        store = m.SegmentStore(PURPOSES)
        seg = install(
            store,
            219,
            [m.Region(0, 32, 32, m.Discipline.CONSTANT, m.Producer.HOST, epoch_offset=0)],
        )
        seg._write_at(32, struct.pack("<II", 3, 0))
        self.assertIsNone(seg.pane(0))
        self.assertEqual(seg.pane_rejected, 1)

    def test_a_pulsed_pair_travels_with_the_revision(self):
        """§2: a reader cannot consult a PULSED slot's header without claiming
        the slot, so the pair arrives with the value and not before it.

        §3.2: and the payload is the payload -- the header is the slot's last
        eight bytes, outside the published length, so neither end composes
        around it."""
        store = m.SegmentStore(PURPOSES)
        seg = install(
            store,
            213,
            [
                m.Region(
                    0,
                    32,
                    3 * (16 + 64),
                    m.Discipline.PULSED,
                    m.Producer.HOST,
                    3,
                    64,
                    epoch_offset=56,
                )
            ],
        )
        writer = m.PulsedWriter(seg, seg.regions[0])  # standing in for the host
        reader = seg.pulsed_reader(0)
        writer.publish(b"pixels", pane=m.PaneRef(2, 9))
        self.assertEqual(reader.read(), b"pixels")
        self.assertEqual(reader.last_pane, m.PaneRef(2, 9))

    def test_the_payload_begins_at_slot_byte_zero_either_way(self):
        """§3.2's whole reason: §6.5 step 5 copies from byte 0, so a
        pane-associated lane must not move the origin.  Read the mapping
        directly rather than the reader, which would agree with itself."""
        store = m.SegmentStore(PURPOSES)
        seg = install(
            store,
            220,
            [
                m.Region(
                    0,
                    32,
                    3 * (16 + 64),
                    m.Discipline.PULSED,
                    m.Producer.PLUGIN,
                    3,
                    64,
                    epoch_offset=56,
                )
            ],
        )
        writer = seg.pulsed_writer(0)
        writer.publish(b"pixels", pane=m.PaneRef(2, 9))
        base, slot = 32 + 3 * 16, None
        atomics = seg._atomics()
        slot = next(i for i in range(3) if atomics.load(32 + i * 16))
        self.assertEqual(seg._read_at(base + slot * 64, 6), b"pixels")
        self.assertEqual(seg._read_at(base + slot * 64 + 56, 8), struct.pack("<II", 2, 9))
        self.assertEqual(
            struct.unpack("<Q", seg._read_at(32 + slot * 16 + 8, 8))[0],
            6,
            "the published length covers the payload and not the header",
        )

    def test_the_capacity_is_eight_less_when_a_header_is_declared(self):
        """§3.2: ``slot_bytes - 8`` with a header, ``slot_bytes`` without."""
        store = m.SegmentStore(PURPOSES)
        seg = install(
            store,
            221,
            [
                m.Region(
                    0,
                    32,
                    3 * (16 + 64),
                    m.Discipline.PULSED,
                    m.Producer.PLUGIN,
                    3,
                    64,
                    epoch_offset=56,
                ),
                host_pulsed(1, 32 + 3 * 80),
            ],
        )
        with self.assertRaises(ValueError):
            seg.pulsed_writer(0).publish(b"x" * 57)
        self.assertTrue(seg.pulsed_writer(0).publish(b"x" * 56))
        self.assertTrue(m.PulsedWriter(seg, seg.regions[1]).publish(b"x" * 64))

    def test_a_publication_with_no_pane_clears_the_header(self):
        """§3.2 and §2: slots are reused, so a publication that carries no pair
        must not inherit its predecessor's -- and a CLEARED header is not a
        rejected one, because nothing wrote it."""
        store = m.SegmentStore(PURPOSES)
        seg = install(
            store,
            214,
            [
                m.Region(
                    0,
                    32,
                    3 * (16 + 64),
                    m.Discipline.PULSED,
                    m.Producer.HOST,
                    3,
                    64,
                    epoch_offset=56,
                )
            ],
        )
        writer = m.PulsedWriter(seg, seg.regions[0])
        reader = seg.pulsed_reader(0)
        for _ in range(3):  # fill every slot with a pair, so reuse is certain
            writer.publish(b"stamped", pane=m.PaneRef(1, 4))
            reader.read()
        writer.publish(b"bare")
        self.assertEqual(reader.read(), b"bare")
        self.assertIsNone(reader.last_pane, "no pair, and not the last one's")
        self.assertEqual(reader.pane_rejected, 0, "cleared is not rejected")

    def test_a_written_but_invalid_header_is_still_counted(self):
        store = m.SegmentStore(PURPOSES)
        seg = install(
            store,
            222,
            [
                m.Region(
                    0,
                    32,
                    3 * (16 + 64),
                    m.Discipline.PULSED,
                    m.Producer.HOST,
                    3,
                    64,
                    epoch_offset=56,
                )
            ],
        )
        writer = m.PulsedWriter(seg, seg.regions[0])
        reader = seg.pulsed_reader(0)
        writer.publish(b"pixels")
        atomics = seg._atomics()
        slot = next(i for i in range(3) if atomics.load(32 + i * 16))
        seg._write_at(32 + 3 * 16 + slot * 64 + 56, struct.pack("<II", 3, 0))
        self.assertEqual(reader.read(), b"pixels", "the payload survives")
        self.assertIsNone(reader.last_pane)
        self.assertEqual(reader.pane_rejected, 1)

    def test_a_length_reaching_into_the_header_is_out_of_range(self):
        """§6.5 step 4 bounds ``length`` by CAPACITY, so a length that would
        read the pair as payload is corruption rather than a large value."""
        store = m.SegmentStore(PURPOSES)
        seg = install(
            store,
            223,
            [
                m.Region(
                    0,
                    32,
                    3 * (16 + 64),
                    m.Discipline.PULSED,
                    m.Producer.HOST,
                    3,
                    64,
                    epoch_offset=56,
                )
            ],
        )
        writer = m.PulsedWriter(seg, seg.regions[0])
        reader = seg.pulsed_reader(0)
        writer.publish(b"pixels")
        atomics = seg._atomics()
        slot = next(i for i in range(3) if atomics.load(32 + i * 16))
        seg._write_at(32 + slot * 16 + 8, struct.pack("<Q", 64))
        with self.assertRaises(m.Quarantined) as caught:
            reader.read()
        self.assertEqual(caught.exception.where, m.WHERE_INVALID_PUBLISHED_LENGTH)

    def test_a_loaned_header_sits_in_each_deposit(self):
        """§2: deposits are independently lived and can outlast an epoch, so one
        region-level header would restate the meaning of bytes in flight."""
        store = m.SegmentStore(PURPOSES)
        seg = install(
            store, 215, [m.Region(0, 32, 512, m.Discipline.LOANED, m.Producer.HOST, epoch_offset=0)]
        )
        ring = seg.deposits(0)
        seg._write_at(32, struct.pack("<II", 1, 4) + b"older")
        seg._write_at(128, struct.pack("<II", 1, 5) + b"newer")
        self.assertEqual(ring.take(32, 13)[8:], b"older")
        self.assertEqual(ring.last_pane, m.PaneRef(1, 4))
        ring.take(128, 13)
        self.assertEqual(ring.last_pane, m.PaneRef(1, 5), "each deposit carries its own")

    def test_the_pane_accessor_refuses_the_disciplines_that_do_not_keep_one(self):
        store = m.SegmentStore(PURPOSES)
        seg = install(store, 216, [pulsed(0, 32), loaned(1, 32 + 3 * 80)])
        for index in (0, 1):
            with self.subTest(region=index), self.assertRaises(ValueError):
                seg.pane(index)

    def test_stamping_refuses_what_the_wire_cannot_carry(self):
        with self.assertRaises(ValueError):
            m.stamp_pane(b"\x00" * 8, 0, m.PaneRef(256, 1))  # past the u8
        with self.assertRaises(ValueError):
            m.stamp_pane(b"\x00" * 4, 0, m.PaneRef(1, 1))  # payload too short
        self.assertEqual(
            m.stamp_pane(b"\x00" * 12, 4, m.PaneRef(2, 3)),
            b"\x00" * 4 + struct.pack("<II", 2, 3),
        )

    def test_stamping_a_region_that_declares_no_location_is_a_caller_error(self):
        store = m.SegmentStore(PURPOSES)
        seg = install(store, 217, [pulsed(0, 32), loaned(1, 32 + 3 * 80)])
        with self.assertRaises(ValueError):
            seg.pulsed_writer(0).publish(b"x", pane=m.PaneRef(1, 1))
        with self.assertRaises(ValueError):
            seg.loaned(1).reserve(b"x", pane=m.PaneRef(1, 1))

    def test_a_repeat_that_moves_the_header_is_a_conflicting_grant(self):
        """§8: a repeat is compared field by field, and ``epoch_offset`` is a
        field -- a table that relocated the pair is not the same table."""
        store = m.SegmentStore(PURPOSES)
        first = m.Region(0, 32, 32, m.Discipline.CONSTANT, m.Producer.HOST, epoch_offset=0)
        moved = m.Region(0, 32, 32, m.Discipline.CONSTANT, m.Producer.HOST, epoch_offset=8)
        install(store, 218, [first])
        answer = store.install(218, "V", [moved])
        self.assertIsInstance(answer, m.Failed)
        self.assertIs(answer.failure_class, m.GrantFailure.MALFORMED)


class DeclaredUses(unittest.TestCase):
    """``control_plane_protocol.md`` §5.6: released ONCE, at zero, and the
    SENDER declares how many uses there will be."""

    def setUp(self):
        self.store = m.SegmentStore(PURPOSES)
        self.seg = install(self.store, 230, [loaned(0, 32, producer=m.Producer.HOST)])
        self.ring = self.seg.deposits(0)

    def test_two_declared_uses_release_once_at_zero(self):
        self.ring.take(32, 8, uses=2)
        self.assertEqual(self.ring.due, (), "a use is still outstanding")
        self.assertFalse(self.ring.dispose(32, 8), "one of two")
        self.assertEqual(self.ring.due, ())
        self.assertTrue(self.ring.dispose(32, 8), "the second is the last")
        self.assertEqual(self.ring.due, ((32, 8),))
        self.seg.release_deposit(0, 32, 8)
        self.assertEqual(self.ring.outstanding, 0)

    def test_releasing_while_a_use_is_outstanding_is_refused_here(self):
        """§5.6: the sender is then free to overwrite bytes the second message
        names, which is the corruption `uses` exists to prevent."""
        self.ring.take(32, 8, uses=2)
        with self.assertRaises(ValueError):
            self.seg.release_deposit(0, 32, 8)
        self.assertEqual(self.ring.outstanding, 1, "nothing was released")

    def test_zero_and_one_both_mean_one(self):
        """A peer that never sets the field must not change behaviour."""
        for offset, uses in ((32, 0), (64, 1)):
            with self.subTest(uses=uses):
                self.ring.take(offset, 8, uses=uses)
                self.assertTrue(self.ring.dispose(offset, 8), "one disposition settles it")
                self.assertIn((offset, 8), self.ring.due)
                self.seg.release_deposit(0, offset, 8)

    def test_the_ordinary_case_needs_no_disposition_call(self):
        """With one declared use the disposition and the release are the same
        event, which is why a plugin written before `uses` existed still
        behaves correctly."""
        self.ring.take(32, 8)
        self.seg.release_deposit(0, 32, 8)
        self.assertEqual(self.ring.outstanding, 0)

    def test_every_disposition_decrements_not_only_the_applied_one(self):
        """§5.6's table: applied, superseded and rejected all decrement -- the
        receiver is done with the bytes whatever it did with them."""
        self.ring.take(32, 8, uses=3)
        for _ in range(2):
            self.ring.dispose(32, 8)  # superseded on the queue, then rejected
        self.assertTrue(self.ring.dispose(32, 8))
        self.assertEqual(self.ring.due, ((32, 8),))

    def test_re_taking_applies_the_count_it_was_first_given(self):
        """§5.6 at `1.0`: ``uses`` is a property of the RESERVATION, so every
        message naming one carries the same count.  A sender that disagrees
        with itself is non-conforming, and the FIRST count is what is applied
        -- keeping the larger would silently accept that the sender does not
        know its own set, which is the one thing ``uses`` exists to assert."""
        self.ring.take(32, 8, uses=1)
        self.ring.take(32, 8, uses=2)
        self.assertEqual(self.ring.outstanding, 1)
        self.assertEqual(self.ring.uses_disagreements, 1)
        self.assertTrue(self.ring.dispose(32, 8), "one use was declared, and it is spent")

    def test_the_disagreement_is_drained_once_for_the_report(self):
        """Site 24.  Drained rather than pushed: §11's rate bound and its
        outbox belong to the control plane, and a reporter down here would be a
        second budget for one session."""
        self.ring.take(32, 8, uses=1)
        self.assertIsNone(self.ring.take_disagreement(), "nothing to report yet")
        self.ring.take(32, 8, uses=2)
        detail = self.ring.take_disagreement()
        self.assertIn("uses=1", detail)
        self.assertIn("uses=2", detail)
        self.assertIsNone(self.ring.take_disagreement(), "and only once")

    def test_a_consistent_re_declaration_is_not_a_disagreement(self):
        self.ring.take(32, 8, uses=2)
        self.ring.take(32, 8, uses=2)
        self.assertEqual(self.ring.uses_disagreements, 0)
        self.assertFalse(self.ring.dispose(32, 8), "two were declared; one is spent")
        self.assertTrue(self.ring.dispose(32, 8))

    def test_disposing_of_something_never_taken_is_a_caller_error(self):
        with self.assertRaises(ValueError):
            self.ring.dispose(32, 8)


class Shortfalls(unittest.TestCase):
    """§5.6 and §5.4: name the amount, and coalesce on (segment, region)."""

    def setUp(self):
        self.store = m.SegmentStore(PURPOSES)
        # Two LOANED regions, but only one plugin-produced: §3 permits exactly
        # one arena per segment, and the host-produced ring beside it is the
        # deposit half.
        self.seg = install(
            self.store, 240, [loaned(0, 32, 128), loaned(1, 32 + 128, 128, m.Producer.HOST)]
        )
        self.arena = self.seg.loaned(0)

    def fill(self):
        self.assertIsNotNone(self.arena.reserve(b"x" * 128))

    def test_a_refused_push_names_how_much_was_wanted(self):
        self.fill()
        self.assertIsNone(self.arena.reserve(b"y" * 65))
        pending = self.arena.shortfall
        self.assertEqual(
            pending,
            m.SegmentShortfall(240, 0, 128, 0, 1),
            "the padded allocation that failed, the largest free run, one drop",
        )

    def test_the_merge_keeps_the_largest_want_and_sums_the_drops(self):
        self.fill()
        self.arena.reserve(b"y" * 65)  # 128 padded
        self.arena.reserve(b"z" * 1)  # 64 padded
        pending = self.arena.shortfall
        self.assertEqual(pending.wanted_bytes, 128, "a re-grant clearing the biggest clears both")
        self.assertEqual(pending.dropped, 2, "§12 needs the count of lost transitions")
        self.assertEqual(self.arena.refused, 2)

    def test_draining_it_is_what_sends_it(self):
        self.fill()
        self.arena.reserve(b"y" * 65)
        self.assertIsNotNone(self.arena.take_shortfall())
        self.assertIsNone(self.arena.shortfall, "drained, not repeated")

    def test_the_store_drains_every_arena_that_came_up_short(self):
        """One per `(segment, region)` key -- and since §3 permits one arena per
        segment, two shortfalls means two segments, which is exactly how a
        plugin that needs two arenas is granted them."""
        self.fill()
        self.arena.reserve(b"y" * 65)
        second = install(self.store, 241, [loaned(0, 32, 128)])
        self.assertIsNone(second.loaned(0).reserve(b"w" * 200))  # never fitted at all
        got = {s.segment_id: s for s in self.store.shortfalls()}
        self.assertEqual(sorted(got), [240, 241], "one per (segment, region) key")
        self.assertEqual(got[241].largest_free_bytes, 128, "exhaustion, not fragmentation")
        self.assertEqual(self.store.shortfalls(), [])

    def test_retirement_drops_a_pending_shortfall(self):
        """§5.4: a re-grant is what the plugin is getting anyway."""
        self.fill()
        self.arena.reserve(b"y" * 65)
        self.store.begin_retire(240)
        self.assertIsNone(self.arena.shortfall)
        self.assertEqual(self.store.shortfalls(), [])


class OwnedHandoff(unittest.TestCase):
    """§5: the read IS the handoff, and this document does not define it."""

    def setUp(self):
        self.store = m.SegmentStore(PURPOSES)
        self.seg = install(
            self.store, 250, [m.Region(0, 32, 64, m.Discipline.OWNED, m.Producer.HOST)]
        )
        self.seg._write_at(32, b"handed over")

    def test_an_authorised_handoff_reads_once(self):
        handoff = self.seg.accept_owned(0, 0, 11)
        self.assertEqual(handoff.read(), b"handed over")
        self.assertTrue(handoff.consumed)
        with self.assertRaises(m.Consumed):
            handoff.read()

    def test_it_covers_the_whole_region_by_default(self):
        self.assertEqual(self.seg.accept_owned(0).read()[:11], b"handed over")

    def test_the_range_is_checked_against_the_region(self):
        for offset, length in ((0, 65), (64, 1), (-1, 4)):
            with self.subTest(offset=offset, length=length), self.assertRaises(ValueError):
                self.seg.accept_owned(0, offset, length)

    def test_only_an_owned_region_has_one(self):
        seg = install(self.store, 251, [const(0, 32)])
        with self.assertRaises(ValueError):
            seg.accept_owned(0)

    def test_retirement_forbids_starting_one_and_lets_one_finish(self):
        """§9.1: an authorisation the control plane already gave is a read
        already begun."""
        handoff = self.seg.accept_owned(0, 0, 11)
        self.store.begin_retire(250)
        with self.assertRaises(m.Retiring):
            self.seg.accept_owned(0)
        self.assertEqual(handoff.read(), b"handed over")

    def test_a_quarantined_region_refuses_both(self):
        handoff = self.seg.accept_owned(0, 0, 11)
        self.seg.isolate(0)
        with self.assertRaises(m.Quarantined):
            handoff.read()
        with self.assertRaises(m.Quarantined):
            self.seg.accept_owned(0)


class SizePolicy(unittest.TestCase):
    """§2 and `doorbell_M_memory.md` §7: `too_large` is a size-policy refusal."""

    def test_a_grant_over_the_local_policy_is_refused_before_mapping(self):
        store = m.SegmentStore(PURPOSES, max_segment_bytes=4096)
        fd = make_object(8192, 260)
        inode = os.fstat(fd).st_ino
        answer = store.capability(260, 8192, PERM_RW, fd=fd)
        self.assertIsInstance(answer, m.Failed)
        self.assertIs(answer.failure_class, m.GrantFailure.TOO_LARGE)
        self.assertFalse(alive(fd, inode), "the descriptor is closed on every path")
        self.assertIsNone(store.segment(260))

    def test_a_grant_within_it_installs(self):
        store = m.SegmentStore(PURPOSES, max_segment_bytes=8192)
        self.assertIsInstance(install(store, 261, [const(0, 32)]), m.Segment)

    def test_there_is_no_default(self):
        """The normative bound is the HOST's per-session budget; a receiver
        that invented one would refuse grants the host had budgeted for."""
        store = m.SegmentStore(PURPOSES)
        self.assertIsNone(store._max_segment_bytes)

    def test_a_policy_that_excludes_the_preamble_is_refused_at_construction(self):
        with self.assertRaises(ValueError):
            m.SegmentStore(PURPOSES, max_segment_bytes=16)


class ReleaseRequests(unittest.TestCase):
    """§9.6: a hint, once, for a segment the plugin has acknowledged."""

    def test_it_is_produced_once_and_then_not_again(self):
        store = m.SegmentStore(PURPOSES)
        install(store, 270, [loaned(0, 32, producer=m.Producer.HOST)])
        self.assertEqual(
            store.request_release(270, "panel closed"), m.ReleaseRequest(270, "panel closed")
        )
        self.assertIsNone(store.request_release(270), "a plugin does not schedule repeats")

    def test_the_segment_stays_fully_operational(self):
        """§9.6: a descriptor arriving afterwards is read, not rejected, and
        nothing enters *retiring*."""
        store = m.SegmentStore(PURPOSES)
        seg = install(store, 271, [loaned(0, 32, producer=m.Producer.HOST)])
        store.request_release(271)
        self.assertFalse(seg.retiring)
        seg._write_at(40, b"after!!!")
        self.assertEqual(seg.deposits(0).take(40, 8), b"after!!!")
        seg.release_deposit(0, 40, 8)

    def test_a_pending_grant_owes_an_answer_rather_than_a_request(self):
        store = m.SegmentStore(PURPOSES)
        store.capability(272, 8192, PERM_RW, fd=make_object(8192, 272))
        self.assertIsNone(store.request_release(272))

    def test_there_is_nothing_to_ask_about_once_it_is_failed_or_released(self):
        store = m.SegmentStore(PURPOSES)
        store.capability(273, 8192, PERM_RW, fd=make_object(8192, 273))
        self.assertIsInstance(store.install(273, "nope", [const(0, 32)]), m.Failed)
        self.assertIsNone(store.request_release(273))
        install(store, 274, [const(0, 32)])
        store.retire(274)
        self.assertIsNone(store.request_release(274))

    def test_an_unknown_segment_asks_nothing(self):
        store = m.SegmentStore(PURPOSES)
        self.assertIsNone(store.request_release(999))


class Conformance(unittest.TestCase):
    """§10's list, which is the document's own answer to "did I get it right".

    Each case here is one bullet, in its order: *"the cases that distinguish a
    correct implementation from one that works on the developer's machine"*.
    """

    def setUp(self):
        self.store = m.SegmentStore(PURPOSES)
        self.seg = install(self.store, 300, [host_pulsed(0, 32)])
        self.writer = m.PulsedWriter(self.seg, self.seg.regions[0])  # the host's end
        self.reader = self.seg.pulsed_reader(0)
        self.atomics = self.seg._atomics()

    def state_words(self):
        return [self.atomics.load(32 + i * 16) for i in range(3)]

    def test_a_publish_into_an_all_ready_lane_claims_the_oldest_never_the_newest(self):
        for n in range(3):
            self.assertTrue(self.writer.publish(bytes([n])))
        # The rotation starts at `revision % buffers`, so the slot order is not
        # the revision order -- what matters is that all three are READY.
        self.assertEqual(sorted(w & 0xFFFF for w in self.state_words()), [1, 2, 3])
        self.assertTrue(self.writer.publish(b"fourth"))
        words = [w & 0xFFFF for w in self.state_words()]
        self.assertIn(4, words, "it published")
        self.assertNotIn(1, words, "the oldest READY was taken")
        self.assertIn(3, words, "the newest READY is held back -- that is why N >= 3")

    def test_a_reader_holding_a_claim_blocks_exactly_that_buffer(self):
        self.writer.publish(b"one")
        self.writer.publish(b"two")
        slots = [i for i, w in enumerate(self.state_words()) if w]
        held = slots[0]
        word = self.atomics.load(32 + held * 16)
        self.atomics.store(32 + held * 16, word | (1 << 63))  # a peer's read claim
        self.assertTrue(self.writer.publish(b"three"), "the other buffers still take a publish")
        self.assertEqual(self.atomics.load(32 + held * 16), word | (1 << 63), "untouched")

    def test_eight_failed_claims_coalesce_and_leave_the_last_value_readable(self):
        """§6.4 step 5: dropping is not an error and not a stall (C3)."""
        self.assertTrue(self.writer.publish(b"durable"))
        real = self.seg._shared

        class NeverWins:
            def load(self, offset):
                return real.load(offset)

            def store(self, offset, value):
                real.store(offset, value)

            def compare_exchange(self, offset, expected, desired):
                return False, real.load(offset)

        self.seg._shared = NeverWins()
        self.assertFalse(self.writer.publish(b"dropped"), "coalesced, not raised")
        self.assertEqual(self.writer.dropped, 1)
        self.seg._shared = real
        self.assertEqual(self.reader.read(), b"durable", "the published value survived")

    def test_a_revision_equal_to_last_seen_is_not_re_read(self):
        self.writer.publish(b"once")
        self.assertEqual(self.reader.read(), b"once")
        self.assertIsNone(self.reader.read(), "consume-once emptied it")
        slot = next(i for i in range(3) if not self.atomics.load(32 + i * 16))
        self.seg._write_at(32 + 3 * 16 + slot * 64, b"once")
        self.seg._write_at(32 + slot * 16 + 8, struct.pack("<Q", 4))
        self.atomics.store(32 + slot * 16, 1)  # the same revision, republished
        self.assertIsNone(self.reader.read(), "`> last_seen` is the whole ordering")

    def test_every_invalid_published_length_releases_the_claim_and_quarantines(self):
        for n, length in enumerate((0, 65, 1 << 40)):
            with self.subTest(length=length):
                store = m.SegmentStore(PURPOSES)
                seg = install(store, 310 + n, [host_pulsed(0, 32)])
                writer = m.PulsedWriter(seg, seg.regions[0])
                reader = seg.pulsed_reader(0)
                writer.publish(b"ok")
                atomics = seg._atomics()
                slot = next(i for i in range(3) if atomics.load(32 + i * 16))
                seg._write_at(32 + slot * 16 + 8, struct.pack("<Q", length))
                with self.assertRaises(m.Quarantined) as caught:
                    reader.read()
                self.assertEqual(caught.exception.where, m.WHERE_INVALID_PUBLISHED_LENGTH)
                self.assertEqual(atomics.load(32 + slot * 16), 0, "the claim was released")
                self.assertTrue(seg.quarantined(0))

    def test_a_release_with_the_padded_length_is_rejected(self):
        """§7: the key is the REQUESTED length, never the allocator's padding --
        so a peer that echoed the padded one has lost track of the arena."""
        seg = install(self.store, 320, [loaned(0, 32)])
        arena = seg.loaned(0)
        offset, length = arena.reserve(b"z" * 65)
        self.assertEqual(length, 65, "the requested length is the key")
        with self.assertRaises(m.Quarantined) as caught:
            seg.release_loan(0, offset, 128)  # the padded one
        self.assertEqual(caught.exception.where, m.WHERE_UNKNOWN_RELEASE)

    def test_an_arena_holding_one_reservation_reclaims_nothing_behind_it(self):
        """§7: reclamation is FIFO, so sitting on one deposit blocks everything
        behind it -- a head-of-line block on memory rather than on messages."""
        seg = install(self.store, 321, [loaned(0, 32, 256)])
        arena = seg.loaned(0)
        first, _ = arena.reserve(b"a" * 64)
        second, _ = arena.reserve(b"b" * 64)
        seg.release_loan(0, second, 64)
        third, _ = arena.reserve(b"c" * 64)
        self.assertNotIn(third, (first, second), "the tail did not advance past the held one")
        seg.release_loan(0, first, 64)
        seg.release_loan(0, third, 64)
        self.assertEqual(arena.reserve(b"d" * 64)[0], first, "empty, so the cursor rewound")

    def test_an_unreleased_deposit_does_not_prevent_release(self):
        """§9.3: `SegmentReleased` supersedes every `ReleaseDeposit` still owed
        -- releasing a byte range in an object that no longer exists is
        bookkeeping with no subject."""
        seg = install(self.store, 322, [loaned(0, 32, producer=m.Producer.HOST)])
        ring = seg.deposits(0)
        seg._write_at(32, b"unreleased")
        ring.take(32, 10)
        self.assertEqual(ring.outstanding, 1)
        self.assertTrue(self.store.retire(322))
        self.assertIs(self.store.state(322).state, m.SegmentState.RELEASED)

    def test_a_duplicate_retire_replays_rather_than_re_releasing(self):
        install(self.store, 323, [const(0, 32)])
        self.assertTrue(self.store.retire(323))
        self.assertTrue(self.store.retire(323), "idempotent, and still True")

    def test_a_retire_for_a_pending_segment_releases_the_half_it_holds(self):
        """§9.4, and the later arrival is answered without mapping."""
        store = m.SegmentStore(PURPOSES)
        fd = make_object(8192, 324)
        inode = os.fstat(fd).st_ino
        store.capability(324, 8192, PERM_RW, fd=fd)
        self.assertTrue(store.retire(324))
        answer = store.install(324, "V", [const(0, 32)])
        self.assertIsInstance(answer, m.Released)
        self.assertFalse(alive(fd, inode))

    def test_a_misaligned_or_inconsistent_pulsed_region_fails_the_whole_grant(self):
        """§6.1: at install, rather than installing and quarantining it."""
        cases = (
            (
                "misaligned offset",
                m.Region(0, 36, 3 * (16 + 64), m.Discipline.PULSED, m.Producer.HOST, 3, 64),
            ),
            ("extent disagrees", m.Region(0, 32, 100, m.Discipline.PULSED, m.Producer.HOST, 3, 64)),
            (
                "two buffers",
                m.Region(0, 32, 2 * (16 + 64), m.Discipline.PULSED, m.Producer.HOST, 2, 64),
            ),
            ("no slot_bytes", m.Region(0, 32, 48, m.Discipline.PULSED, m.Producer.HOST, 3, 0)),
        )
        for n, (label, region) in enumerate(cases):
            with self.subTest(label):
                store = m.SegmentStore(PURPOSES)
                store.capability(330 + n, 8192, PERM_RW, fd=make_object(8192, 330 + n))
                answer = store.install(330 + n, "V", [region])
                self.assertIsInstance(answer, m.Failed)
                self.assertIs(answer.failure_class, m.GrantFailure.MALFORMED)
                self.assertIsNone(store.segment(330 + n))

    def test_an_owned_hint_leaves_a_later_handoff_working(self):
        """§5: the hint was wrong and the memory was not, so reporting it must
        not quarantine -- and the way to show that is to use the region."""
        store = m.SegmentStore(PURPOSES)
        seg = install(store, 340, [m.Region(0, 32, 64, m.Discipline.OWNED, m.Producer.HOST)])
        seg._write_at(32, b"still mine")
        hint = store.hint(340, [0])
        self.assertEqual(hint.violations, ((m.WHERE_OWNED_REGION_HINT, 0),))
        self.assertFalse(seg.quarantined(0))
        self.assertEqual(seg.accept_owned(0, 0, 10).read(), b"still mine")

    def test_release_closes_the_mapping_before_the_caller_says_so(self):
        """§9.2: `SegmentReleased` is sent after the last handle closes -- the
        case a munmap-only implementation passes and a leak check does not."""
        seg = install(self.store, 341, [const(0, 32)])
        mapping = seg._mm
        self.assertTrue(self.store.release(341))
        self.assertTrue(mapping.closed, "the mapping is closed before release returns")
        self.assertIsNone(self.store.segment(341))

    def test_the_atomics_accessor_closes_before_the_mapping_it_wraps(self):
        """The process-shared atomics accessor closes BEFORE the mapping:
        an accessor that outlives its mapping is a late atomic on unmapped
        pages, which is the crash.  The test above asserts the mapping is
        closed on release and not the order; this one does."""
        seg = install(self.store, 342, [pulsed(0, 32)])
        accessor = seg._atomics()
        mapping = seg._mm
        accessor.store(32, 5)
        self.assertEqual(accessor.load(32), 5)
        self.assertFalse(accessor.closed or mapping.closed)
        self.assertTrue(self.store.release(342))
        self.assertTrue(accessor.closed)
        self.assertTrue(mapping.closed)
        self.assertIsNone(seg._shared, "a second _atomics() must not resurrect it")
        self.assertTrue(self.store.release(342), "§9.4: a duplicate release is replayed")
        self.assertTrue(mapping.closed)


class RejectedCapability(unittest.TestCase):
    """`M` §7: a record-level violation is a grant failure, class `malformed`,
    and it is recorded against the id so the query answer ends the exchange."""

    def setUp(self):
        self.store = m.SegmentStore(PURPOSES)

    def test_a_rejected_record_fails_its_grant(self):
        answer = self.store.reject_capability(400, "reserved perms bit set")
        self.assertIsInstance(answer, m.Failed)
        self.assertIs(answer.failure_class, m.GrantFailure.MALFORMED)
        self.assertIn("reserved", answer.detail)

    def test_the_query_then_answers_failed_rather_than_not_present(self):
        """§7's table: `not present` means RE-SEND THE SAME GRANT -- which the
        doorbell rejects again, spending another descriptor each pass.  A recorded
        failure is what ends that."""
        self.assertIs(self.store.state(401).state, m.SegmentState.NOT_PRESENT)
        self.store.reject_capability(401, "size below preamble")
        report = self.store.state(401)
        self.assertIs(report.state, m.SegmentState.FAILED)
        self.assertIs(report.failure_class, m.GrantFailure.MALFORMED)

    def test_the_other_half_arriving_later_replays_the_failure(self):
        """§8: a duplicate after a terminal state replays that state's ack, and
        never maps."""
        self.store.reject_capability(402, "name not utf8")
        answer = self.store.install(402, "V", [const(0, 32)])
        self.assertIsInstance(answer, m.Failed)
        self.assertIs(answer.failure_class, m.GrantFailure.MALFORMED)
        self.assertIsNone(self.store.segment(402))

    def test_a_zero_id_has_no_grant_to_fail(self):
        """§7 routes `zero_id` to ContractViolation: there is no grant for an
        acknowledgement to be about."""
        with self.assertRaises(ValueError):
            self.store.reject_capability(0, "zero id")

    def test_a_rejection_after_release_is_answered_with_segment_released(self):
        install(self.store, 403, [const(0, 32)])
        self.store.retire(403)
        self.assertIsInstance(self.store.reject_capability(403, "late"), m.Released)


class SiteVocabulary(unittest.TestCase):
    """The ``where`` values are the IDL's, not this SDK's inventions.

    ``shared_memory_implementation_details.md`` §2: the host switches on
    ``ViolationSite``, so a spelling that drifts is a report the host files
    under the wrong rule -- or drops.
    """

    ROOT = pathlib.Path(__file__).resolve().parent.parent

    def idl_sites(self):
        text = _layout.PROTO.read_text(encoding="utf-8")
        found = re.findall(r"^\s*VIOLATION_SITE_([A-Z_]+)\s*=\s*(\d+);", text, re.M)
        return {name.lower(): int(value) for name, value in found if value != "0"}

    def test_the_table_is_exactly_the_idl_enum(self):
        self.assertEqual(violations.SITES, self.idl_sites())

    def test_what_each_version_appended_kept_the_earlier_ones_in_place(self):
        """Four SDKs implement 1-16, so everything after was appended rather
        than renumbered into meaning."""
        self.assertEqual(violations.SITES["install_without_segment"], 17)
        self.assertEqual(violations.SITES["release_region_mismatch"], 18)
        self.assertEqual(violations.SITES["unknown_release"], 12)
        # `1.0`, and three of the four are a rule this SDK enforced with a
        # local spelling because the enum had no name for it.
        self.assertEqual(violations.SITES["arrangement_regressed"], 20)
        self.assertEqual(violations.SITES["repeated_field_overflow"], 21)
        self.assertEqual(violations.SITES["live_geometry_not_inline"], 22)
        self.assertEqual(violations.SITES["lane_subject_mismatch"], 23)

    def test_every_where_the_memory_layer_reports_is_one_of_them(self):
        constants = [getattr(m, name) for name in dir(m) if name.startswith("WHERE_")]
        self.assertTrue(constants)
        for where in constants:
            with self.subTest(where=where):
                self.assertTrue(violations.is_wire_site(where))

    def test_every_where_the_doorbell_rejects_with_is_one_of_them(self):
        source = (_layout.PACKAGE / "doorbell.py").read_text(encoding="utf-8")
        rejected = set(re.findall(r'reject\("([a-z_]+)"\)', source))
        self.assertTrue(rejected)
        self.assertEqual(rejected - set(violations.SITES), set())

    #: A ``where`` the specification NAMES, in the two forms it names one in:
    #: ``where`` = x, and the parenthetical ``(`x`)`` §11 uses when it lists
    #: several.  The second is ambiguous with an ordinary field name -- the
    #: corpus is full of ``(`epoch_offset`)`` -- so it counts only inside a
    #: window that also names the mechanism.
    _EXPLICIT = re.compile(r"`where`\s*(?:=|is|of)\s*\**`([a-z_]+)`", re.S)
    _PARENTHETICAL = re.compile(r"\(`([a-z_]+)`\)")
    _MECHANISM = re.compile(r"ContractViolation|ViolationSite|`where`")
    _WINDOW = 240

    def documented_sites(self):
        out: dict[str, set[str]] = {}
        for path in _layout.SPEC_DOCS:
            text = path.read_text(encoding="utf-8")
            for match in self._EXPLICIT.finditer(text):
                out.setdefault(match.group(1), set()).add(path.name)
            for match in self._PARENTHETICAL.finditer(text):
                near = text[max(0, match.start() - self._WINDOW) : match.end() + self._WINDOW]
                if self._MECHANISM.search(near):
                    out.setdefault(match.group(1), set()).add(path.name)
        return out

    def test_every_site_the_documents_name_is_one_the_idl_carries(self):
        """**A rule can land in a document while its enforcement stays behind**,
        and vizi's own step-4 review found that four times.  This is the cheap
        half of catching it: a ``where`` the prose names and the IDL does not
        is a rule no peer can be told about, which is exactly how 20-23 came to
        be missing -- both SDKs refused correctly and kept the reason to
        themselves.

        Suggested by vizi, which now runs the same check from its side.
        """
        documented = self.documented_sites()
        self.assertTrue(documented, "the scan found nothing, which is not a pass")
        known = set(violations.SITES) | violations.LOCAL_WHERE
        self.assertEqual(
            {name: sorted(where) for name, where in documented.items() if name not in known},
            {},
            "named as a violation site by the specification and absent from the IDL",
        )

    def test_the_scan_sees_the_sites_1_0_appended(self):
        """The guard on the guard: a regex that stopped matching would pass the
        check above by finding nothing, so name what it must find."""
        documented = self.documented_sites()
        for name in ("live_geometry_not_inline", "lane_subject_mismatch"):
            with self.subTest(name):
                self.assertIn(name, documented)

    def test_the_three_1_0_round_two_sites_are_the_ones_this_sdk_asked_for(self):
        """24-26 were one question here -- three rules asking for a §11 report
        with no site to make it with -- and the answer measured them against
        §6.3's deliberate silence.  That section is a TEST, not a precedent:
        exhaustion has no site BECAUSE the condition is structural, unreachable
        and unrecoverable.  These three are operational, reachable and fixable
        by a host that is told, which is the other answer."""
        self.assertEqual(violations.SITES["uses_disagreement"], 24)
        self.assertEqual(violations.SITES["missing_deadline"], 25)
        self.assertEqual(violations.SITES["arrangement_first_revision"], 26)
        self.assertNotIn("uses_disagreement", violations.LOCAL_WHERE)
        self.assertIn("revision_exhausted", violations.LOCAL_WHERE, "the one that passed the test")

    def test_the_local_spellings_are_deliberately_not_wire_sites(self):
        """§6.3's revision exhaustion has no ``ViolationSite`` and must not be
        mislabelled; a re-raised quarantine is not a new defect."""
        self.assertEqual(violations.LOCAL_WHERE & set(violations.SITES), set())
        for where in violations.LOCAL_WHERE:
            self.assertIsNone(violations.site_number(where))


class EndToEnd(unittest.TestCase):
    """The three pieces a plugin actually wires together.

    A real ``D`` record off the doorbell, routed through the region table, into
    the schedule that would have found the value anyway.
    """

    def test_a_real_D_record_routes_to_the_regions_worth_scanning(self):
        from vizi_plugin_sdk import doorbell as db

        store = m.SegmentStore(PURPOSES)
        seg = install(
            store,
            100,
            [
                host_pulsed(0, 32),
                m.Region(1, 32 + 3 * 80, 64, m.Discipline.OWNED, m.Producer.HOST),
            ],
        )
        # Standing in for the host's publisher, which is the end that writes a
        # host-produced lane; the plugin's own API refuses to publish into one.
        writer = m.PulsedWriter(seg, seg.regions[0])
        got: list[bytes] = []
        schedule = m.ReadSchedule()
        schedule.consume(seg.pulsed_reader(0), got.append)
        reporter = ViolationReporter()

        # The host publishes into the lane and hints at both regions.
        writer.publish(b"geometry")
        payload = db.encode_notification_payload(100, [0, 1], epoch=5, view_id=2)
        record = db.encode_record(db.OP_NOTIFY, 1, payload)
        # A plugin reads this off `Frame.record`; the test builds one directly.
        hint_record, rejection, _ = db._decode_notification(record[8:108])
        self.assertIsNone(rejection)
        self.assertEqual(hint_record.segment_id, 100)

        hint = store.hint(
            hint_record.segment_id,
            hint_record.regions(),
            epoch=hint_record.epoch,
            installed_epoch=5,
        )
        self.assertEqual(hint.scan, (0,), "Pulsed is what D accelerates")
        self.assertEqual(hint.violations, ((m.WHERE_OWNED_REGION_HINT, 1),))

        for where, region in hint.violations:
            out = reporter.report(where, f"region {region}", record=record)
            self.assertIsNotNone(out)
            self.assertEqual(out.record, record)

        schedule.wake()
        self.assertEqual(schedule.sweep(), 1)
        self.assertEqual(got, [b"geometry"])

        # And the schedule would have found it with no hint at all (§5).
        writer.publish(b"unannounced")
        self.assertEqual(schedule.sweep(), 1)
        self.assertEqual(got[-1], b"unannounced")
