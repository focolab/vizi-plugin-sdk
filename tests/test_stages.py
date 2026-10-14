"""Processing stages (``processing_stages.md``): the slot header, the author's
view, and the runner that takes turns -- against an in-process host double.

The double maps the ring through its OWN descriptor and its own atomics, so the
plugin and the host share bytes and nothing else, as two processes would.
"""

from __future__ import annotations

import dataclasses
import mmap
import os
import struct
import tempfile
import time
import unittest
import zlib

import numpy as np
import vizi_plugin_sdk._shared_atomic as _sa

try:  # pragma: no cover - depends on the environment
    import atomicshm  # noqa: F401
except ImportError:
    # test_memory's stand-in, whole: the patch is module-wide, so a partial
    # one would replace the complete one for every test collected with it.

    class _SingleProcessAtomics:
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

    _sa.SharedAtomicMapping = _SingleProcessAtomics

from vizi_plugin_sdk import control as c  # noqa: E402
from vizi_plugin_sdk import declare, manifest, server  # noqa: E402
from vizi_plugin_sdk import memory as m  # noqa: E402
from vizi_plugin_sdk import stages as s  # noqa: E402
from vizi_plugin_sdk._proto import vizi_plugin_pb2 as pb2  # noqa: E402
from vizi_plugin_sdk.violations import ViolationReporter  # noqa: E402

PERM_RW = m.PERM_PLUGIN_READ | m.PERM_PLUGIN_WRITE
U8, U16, F32 = 1, 2, 3


class HostRing:
    """The host's half of one ring: it creates the object, fills slots, and
    reads what the stages left.  ``grant`` hands the plugin its own fd."""

    def __init__(self, segment_id=0x7_0000_0001, slots=2, slot_bytes=64 * 1024):
        self.segment_id = segment_id
        self.slot_bytes = slot_bytes
        self.offsets = [64 + i * slot_bytes for i in range(slots)]
        self.size = 64 + slots * slot_bytes
        tmp = tempfile.TemporaryFile()
        tmp.truncate(self.size)
        self.fd = os.dup(tmp.fileno())
        tmp.close()
        self.mm = mmap.mmap(self.fd, self.size)
        head = struct.pack("<QI16s", segment_id, 0, b"\x5a" * 16)
        self.mm[:32] = head + struct.pack("<I", zlib.crc32(head))
        self.atomics = _sa.SharedAtomicMapping(self.mm)
        self.iterations = [0] * slots

    def regions(self):
        return [
            pb2.Region(
                index=i,
                offset=off,
                extent=self.slot_bytes,
                discipline=pb2.RegionDiscipline.REGION_DISCIPLINE_STAGED,
                producer=pb2.RegionProducer.REGION_PRODUCER_HOST,
            )
            for i, off in enumerate(self.offsets)
        ]

    def grant(self, plane: c.ControlPlane) -> None:
        plane.capability(self.segment_id, self.size, PERM_RW, fd=os.dup(self.fd))
        plane.receive(
            pb2.HostMsg(
                install_segment=pb2.InstallSegment(
                    segment_id=self.segment_id, purpose="S", regions=self.regions()
                )
            )
        )

    def view(self, slot):
        return memoryview(self.mm)[self.offsets[slot] : self.offsets[slot] + self.slot_bytes]

    def fill(self, slot, data, channels, *, pipeline=7, position=1, plane_cap=4, t=3, **kw):
        """§6.1: write everything, then the turn word with release ordering."""
        data = np.asarray(data)
        planes, z, y, x = data.shape
        dtype = {np.dtype(np.uint8): U8, np.dtype(np.uint16): U16, np.dtype(np.float32): F32}[
            data.dtype
        ]
        capacity = self.slot_bytes - s.header_bytes(plane_cap)
        header = s.SlotHeader(
            t=t,
            dtype=dtype,
            planes=planes,
            plane_cap=plane_cap,
            alloc=(z, y, x),
            capacity=capacity,
            extent=(0, z, 0, y, 0, x),
            origin=(0.0, 0.0, 0.0),
            open_generation=11,
            channels=tuple(channels),
        )
        header = dataclasses.replace(header, **kw) if kw else header
        buf = self.view(slot)
        s.write_header(buf, header)
        hb = s.header_bytes(plane_cap)
        buf[hb : hb + data.nbytes] = data.tobytes()
        buf.release()
        self.iterations[slot] += 1
        self.hand(slot, pipeline, position)

    def hand(self, slot, pipeline, position):
        word = s.encode_turn(self.iterations[slot], pipeline, position)
        self.atomics.store(self.offsets[slot], word)

    def turn(self, slot) -> s.Turn:
        return s.decode_turn(self.atomics.load(self.offsets[slot]))

    def header(self, slot) -> s.SlotHeader:
        buf = self.view(slot)
        try:
            return s.read_header(buf)
        finally:
            buf.release()

    def data(self, slot):
        h = self.header(slot)
        z, y, x = h.alloc
        n = h.planes * z * y * x
        hb = s.header_bytes(h.plane_cap)
        raw = bytes(self.mm[self.offsets[slot] + hb : self.offsets[slot] + hb + n * 8])
        return np.frombuffer(raw, s.DTYPES[h.dtype], count=n).reshape(h.planes, z, y, x)

    def close(self):
        self.atomics.close()
        self.mm.close()
        os.close(self.fd)


class Stage:
    """A stage whose ``process`` is whatever the test gives it."""

    def __init__(self, fn):
        self.fn = fn
        self.calls = 0

    def process(self, slot):
        self.calls += 1
        return self.fn(slot)


class Plugin:
    """One SDK instance: its own store, control plane and runner."""

    def __init__(self, ring: HostRing, stages: dict, needs=None):
        self.store = m.SegmentStore(m.PURPOSES)
        self.plane = c.ControlPlane(memory=self.store, reporter=ViolationReporter())
        self.notified: list = []
        self.stages = stages
        self.runner = s.StageRunner(
            memory=self.store,
            control=self.plane,
            stage_for=stages.get,
            needs_for=(lambda arm: needs) if needs is not None else (lambda arm: None),
            notify=lambda seg, regions: self.notified.append((seg, list(regions))),
        )
        ring.grant(self.plane)
        self.ring = ring

    def arm(self, stage_id, *, pipeline=7, position=1, dtype=U16, zyx=(2, 4, 4), revision=1, **kw):
        self.plane.receive(
            pb2.HostMsg(
                stage_arm=pb2.StageArm(
                    stage_id=stage_id,
                    revision=revision,
                    segment_id=self.ring.segment_id,
                    pipeline=pipeline,
                    position=position,
                    input=pb2.StageShape(
                        channels=[0, 1], z=zyx[0], y=zyx[1], x=zyx[2], dtype=dtype
                    ),
                    **{"output": pb2.StageShape(z=zyx[0], y=zyx[1], x=zyx[2], dtype=dtype), **kw},
                )
            )
        )

    def violations(self):
        return [
            msg.contract_violation.where
            for msg in self.plane.outbox.drain()
            if msg.WhichOneof("msg") == "contract_violation"
        ]

    def close(self):
        self.runner.stop()
        self.store.close()


def volume(planes=2, dtype=np.uint16):
    return np.arange(planes * 2 * 4 * 4, dtype=dtype).reshape(planes, 2, 4, 4)


class Codec(unittest.TestCase):
    """§5.1 and §6.1, byte for byte."""

    def test_the_turn_word_carries_a_signed_position(self):
        for position in (0, 1, 2, 32766, 32767, -1, -32766):
            with self.subTest(position=position):
                word = s.encode_turn(0xFFFF_FFFF, 0xFFFF, position)
                self.assertEqual(s.decode_turn(word), s.Turn(0xFFFF_FFFF, 0xFFFF, position))
        self.assertEqual(s.encode_turn(5, 7, -1) >> 48, 0xFFFF)
        self.assertEqual(s.encode_turn(5, 7, 3), 5 | 7 << 32 | 3 << 48)

    def test_the_header_is_variable_length_and_aligned(self):
        self.assertEqual(s.header_bytes(1), 320)
        self.assertEqual(s.header_bytes(32), 320)
        self.assertEqual(s.header_bytes(33), 384)
        self.assertEqual(s.header_bytes(65536), 131328)

    def test_a_header_round_trips_at_the_spec_offsets(self):
        header = s.SlotHeader(
            outcome=1,
            t=9,
            done_through=2,
            reason=0,
            dtype=F32,
            planes=2,
            plane_cap=3,
            alloc=(4, 5, 6),
            capacity=10_000,
            extent=(1, 3, 0, 5, 2, 6),
            origin=(0.5, -1.0, 2.25),
            pose_r=tuple(float(i) for i in range(9)),
            pose_t=(7.0, 8.0, 9.0),
            open_generation=2**40,
            channels=(4, 17),
        )
        buf = bytearray(1024)
        s.write_header(buf, header)
        self.assertEqual(s.read_header(buf), header)
        self.assertEqual(struct.unpack_from("<I", buf, 28)[0], F32, "dtype at 28")
        self.assertEqual(struct.unpack_from("<Q", buf, 56)[0], 10_000, "capacity at 56")
        self.assertEqual(struct.unpack_from("<Q", buf, 208)[0], 2**40, "open_generation at 208")
        self.assertEqual(struct.unpack_from("<2H", buf, 256), (4, 17), "channels at 256")


class Validation(unittest.TestCase):
    """§7's minimum, one rule at a time."""

    def good(self, **kw):
        base = s.SlotHeader(
            dtype=U16,
            planes=2,
            plane_cap=2,
            alloc=(2, 4, 4),
            capacity=256,
            extent=(0, 2, 0, 4, 0, 4),
            channels=(0, 1),
        )
        return dataclasses.replace(base, **kw)

    def check(self, header, extent=1024, armed=None):
        return s.validate(header, extent, armed)

    def test_a_good_header_passes(self):
        self.assertEqual(self.check(self.good()), [])

    def test_each_rule(self):
        armed = pb2.StageShape(z=2, y=4, x=4, dtype=U16)
        cases = {
            "reserved": (self.good(reserved_zero=False), 1024, None),
            "dtype": (self.good(dtype=9), 1024, None),
            "planes over cap": (self.good(planes=3), 1024, None),
            "does not fit the slot": (self.good(), 320 + 255, None),
            "planes over capacity": (self.good(capacity=127), 1024, None),
            "extent reversed": (self.good(extent=(1, 0, 0, 4, 0, 4)), 1024, None),
            "extent past the allocation": (self.good(extent=(0, 3, 0, 4, 0, 4)), 1024, None),
            "non-finite origin": (self.good(origin=(float("nan"), 0.0, 0.0)), 1024, None),
            "non-finite pose": (self.good(pose_t=(float("inf"), 0.0, 0.0)), 1024, None),
            "dtype not the arm's": (self.good(dtype=U8, capacity=128), 1024, armed),
            "bigger than the arm": (self.good(alloc=(2, 4, 8), capacity=512), 2048, armed),
            # Plane i is channel channels[i], so one channel
            # twice leaves a plane unnamed.
            "a channel twice": (self.good(channels=(1, 1)), 1024, None),
        }
        for label, (header, extent, arm) in cases.items():
            with self.subTest(label):
                self.assertNotEqual(self.check(header, extent, arm), [])

    def test_plane_cap_is_bounded_and_trailing_space_is_legal(self):
        """§5.1's largest legal ``plane_cap`` is 65 536,
        enforced even when the region is big enough to hold the header; and
        a region larger than ``header_bytes + capacity`` is fine -- the header
        must FIT, as §7 says (§5.1 now says so too)."""
        big = self.good(plane_cap=s.MAX_PLANE_CAP + 1, planes=1, capacity=64)
        self.assertNotEqual(self.check(big, s.header_bytes(s.MAX_PLANE_CAP + 1) + 64), [])
        at_cap = self.good(plane_cap=s.MAX_PLANE_CAP, planes=1, capacity=64)
        self.assertEqual(self.check(at_cap, s.header_bytes(s.MAX_PLANE_CAP) + 64), [])
        gap = self.good(plane_cap=2, capacity=256)
        self.assertEqual(self.check(gap, s.header_bytes(2) + 256 + 64), [])

    def test_a_slot_smaller_than_the_armed_input_is_fine(self):
        """A plan states the LARGEST output (§3)."""
        armed = pb2.StageShape(z=4, y=8, x=8, dtype=U16)
        self.assertEqual(self.check(self.good(), 1024, armed), [])


class Turns(unittest.TestCase):
    """§6: a turn is taken, handed on, and never taken twice."""

    def setUp(self):
        self.ring = HostRing()
        self.doubled = Stage(lambda slot: slot.planes.__imul__(2))
        self.plugin = Plugin(self.ring, {"double": self.doubled})
        self.plugin.arm("double")

    def tearDown(self):
        self.plugin.close()
        self.ring.close()

    def test_a_processed_turn_edits_in_place_and_hands_to_position_plus_one(self):
        self.ring.fill(0, volume(), [0, 1])
        self.assertEqual(self.plugin.runner.pass_once(), 1)
        self.assertEqual(self.ring.turn(0), s.Turn(1, 7, 2))
        header = self.ring.header(0)
        self.assertEqual((header.outcome, header.reason, header.done_through), (1, 0, 1))
        np.testing.assert_array_equal(self.ring.data(0), volume() * 2)
        self.assertEqual(self.plugin.notified, [(self.ring.segment_id, [0])])

    def test_the_same_iteration_is_not_taken_twice_and_the_next_one_is(self):
        """§6.2: the turn back at the same position with the same iteration is
        the same volume; a new fill is a new one."""
        self.ring.fill(0, volume(), [0, 1])
        self.plugin.runner.pass_once()
        self.ring.hand(0, 7, 1)  # the host puts the SAME volume back
        self.assertEqual(self.plugin.runner.pass_once(), 0)
        self.ring.fill(0, volume(), [0, 1])  # a new iteration
        self.assertEqual(self.plugin.runner.pass_once(), 1)
        self.assertEqual(self.doubled.calls, 2)

    def test_turns_that_are_not_ours_are_left_alone(self):
        self.ring.fill(0, volume(), [0, 1], position=2)
        self.ring.fill(1, volume(), [0, 1], pipeline=8)
        self.assertEqual(self.plugin.runner.pass_once(), 0)
        self.ring.hand(0, 7, 0)
        self.assertEqual(self.plugin.runner.pass_once(), 0, "the host holds it")

    def test_a_never_filled_slot_is_ignored_whatever_its_position(self):
        """§6.1: iteration 0 means never filled."""
        self.ring.atomics.store(self.ring.offsets[0], s.encode_turn(0, 7, 1))
        self.assertEqual(self.plugin.runner.pass_once(), 0)

    def test_a_refusal_stores_minus_position_and_touches_nothing_else(self):
        def refuse(slot):
            raise s.RefuseVolume(s.REFUSE_CAUSAL, "needs the previous frame")

        self.plugin.stages["double"] = Stage(refuse)
        self.ring.fill(0, volume(), [0, 1])
        self.plugin.runner.pass_once()
        self.assertEqual(self.ring.turn(0).position, -1)
        header = self.ring.header(0)
        self.assertEqual((header.outcome, header.reason, header.done_through), (2, 1, 0))
        np.testing.assert_array_equal(self.ring.data(0), volume())

    def test_an_exception_is_failed_internal(self):
        def boom(slot):
            raise RuntimeError("the author's bug")

        self.plugin.stages["double"] = Stage(boom)
        self.ring.fill(0, volume(), [0, 1])
        self.plugin.runner.pass_once()
        header = self.ring.header(0)
        self.assertEqual((header.outcome, header.reason), (3, s.FAIL_INTERNAL))
        self.assertEqual(self.ring.turn(0).position, -1)
        self.assertTrue(any("the author's bug" in d for d in self.plugin.plane.diagnostics))

    def test_an_invalid_header_fails_and_is_reported(self):
        """§7: *failed*, reason 1, at STAGE_SLOT_INVALID; the author never runs."""
        self.ring.fill(0, volume(), [0, 1], extent=(0, 3, 0, 4, 0, 4))
        self.plugin.runner.pass_once()
        header = self.ring.header(0)
        self.assertEqual((header.outcome, header.reason), (3, s.FAIL_INVALID_HEADER))
        self.assertEqual(self.doubled.calls, 0)
        self.assertIn(pb2.ViolationSite.VIOLATION_SITE_STAGE_SLOT_INVALID, self.plugin.violations())

    def test_a_missing_input_is_refused_before_the_author_runs(self):
        """§7: refused, reason 2, and not a violation."""
        plugin = Plugin(HostRing(segment_id=0x7_0000_0002), {"double": self.doubled}, needs={0, 5})
        try:
            plugin.arm("double")
            plugin.ring.fill(0, volume(), [0, 1])
            plugin.runner.pass_once()
            header = plugin.ring.header(0)
            self.assertEqual((header.outcome, header.reason), (2, s.REFUSE_MISSING_INPUT))
            self.assertEqual(self.doubled.calls, 0)
            self.assertEqual(plugin.violations(), [])
        finally:
            plugin.close()
            plugin.ring.close()

    def test_a_re_arm_during_a_pass_holds_the_next_slot_to_the_new_revision(self):
        """The pass snapshotted every arm, so slot 1 ran
        under revision 1 after revision 2 was installed and acknowledged."""
        seen: list[int] = []

        def rearm_once(slot):
            seen.append(slot._arm.revision)
            if len(seen) == 1:
                self.plugin.arm("double", revision=2)

        self.plugin.stages["double"] = Stage(rearm_once)
        self.ring.fill(0, volume(), [0, 1])
        self.ring.fill(1, volume(), [0, 1])
        self.assertEqual(self.plugin.runner.pass_once(), 2)
        self.assertEqual(seen, [1, 2])

    def test_a_stage_that_outgrows_its_armed_output_fails(self):
        """Nothing held a processed slot to ``arm.output``,
        so a stage armed for u16 could finish as float32, *processed*.  It is
        failed, reason 2 -- the author broke its own plan."""
        plugin = Plugin(
            HostRing(segment_id=0x7_0000_0003),
            {"widen": Stage(lambda slot: slot.relayout(dtype=np.float32))},
        )
        try:
            plugin.arm("widen", output=pb2.StageShape(z=2, y=4, x=4, dtype=U16))
            plugin.ring.fill(0, volume(), [0, 1])
            plugin.runner.pass_once()
            header = plugin.ring.header(0)
            self.assertEqual((header.outcome, header.reason), (3, s.FAIL_INTERNAL))
            self.assertEqual(plugin.ring.turn(0).position, -1)
            self.assertTrue(any("arm's output" in d for d in plugin.plane.diagnostics))
        finally:
            plugin.close()
            plugin.ring.close()

    def test_the_thread_takes_a_turn_on_a_wake(self):
        self.plugin.runner.start()
        self.ring.fill(0, volume(), [0, 1])
        self.plugin.runner.wake()
        for _ in range(200):
            if self.ring.turn(0).position == 2:
                break
            time.sleep(0.01)
        self.assertEqual(self.ring.turn(0).position, 2)


class SlotEdits(unittest.TestCase):
    """§5.2-§5.5: what an author may change, and what is written back."""

    def setUp(self):
        self.ring = HostRing()

    def tearDown(self):
        self.plugin.close()
        self.ring.close()

    def run_stage(self, fn, **arm):
        self.plugin = Plugin(self.ring, {"st": Stage(fn)})
        self.plugin.arm("st", **arm)
        self.ring.fill(0, volume(), [0, 1])
        self.plugin.runner.pass_once()
        self.assertEqual(self.ring.turn(0).position, 2, self.plugin.plane.diagnostics)
        return self.ring.header(0)

    def test_an_added_channel_gets_the_assigned_index(self):
        def add(slot):
            slot.add_channel("flow")[...] = slot.plane(1) + 1

        header = self.run_stage(add, new_channels={"flow": 9})
        self.assertEqual((header.planes, header.channels), (3, (0, 1, 9)))
        np.testing.assert_array_equal(self.ring.data(0)[2], volume()[1] + 1)

    def test_a_widening_crop_repacks_and_keeps_every_voxel_placed(self):
        """§5.3 and §5.4: u16 -> f32 over a (1, 2, 2) window at (1, 1, 2)."""

        def widen(slot):
            slot.relayout(np.float32, (1, 2, 2), offset=(1, 1, 2))

        header = self.run_stage(widen, output=pb2.StageShape(z=1, y=2, x=2, dtype=F32))
        self.assertEqual((header.dtype, header.alloc), (F32, (1, 2, 2)))
        self.assertEqual(header.extent, (0, 1, 0, 2, 0, 2))
        self.assertEqual(header.origin, (1.0, 1.0, 2.0))
        np.testing.assert_array_equal(
            self.ring.data(0), volume()[:, 1:2, 1:3, 2:4].astype(np.float32)
        )

    def test_the_extent_only_shrinks(self):
        def shrink(slot):
            with self.assertRaises(ValueError):
                slot.extent = (0, 3, 0, 4, 0, 4)
            slot.extent = (0, 1, 1, 3, 0, 4)

        self.assertEqual(self.run_stage(shrink).extent, (0, 1, 1, 3, 0, 4))

    def test_a_relative_motion_composes_into_the_absolute_pose(self):
        """§5.5: one simultaneous update, with R from before the stage."""
        quarter = np.array([[1.0, 0, 0], [0, 0, -1], [0, 1, 0]])

        def move(slot):
            slot.pose = (quarter, (1.0, 2.0, 3.0))
            slot.compose_pose(quarter, (0.0, 0.0, 1.0))

        header = self.run_stage(move)
        r, t = np.array(header.pose_r).reshape(3, 3), np.array(header.pose_t)
        np.testing.assert_allclose(r, quarter @ quarter)
        np.testing.assert_allclose(t, quarter @ [0.0, 0.0, 1.0] + [1.0, 2.0, 3.0])

    def test_a_kept_array_is_diagnosed(self):
        kept = []
        self.run_stage(lambda slot: kept.append(slot.planes))
        self.assertTrue(any("outlived process()" in d for d in self.plugin.plane.diagnostics))
        kept.clear()


class Chains(unittest.TestCase):
    """§6.1: consecutive positions hand straight to each other, and one stage
    may be armed at two of them."""

    def test_one_plugin_at_positions_1_and_2(self):
        ring = HostRing()
        plus = Stage(lambda slot: slot.planes.__iadd__(1))
        plugin = Plugin(ring, {"plus": plus})
        try:
            plugin.arm("plus", position=1)
            plugin.arm("plus", position=2)
            ring.fill(0, volume(), [0, 1])
            plugin.runner.pass_once()
            plugin.runner.pass_once()
            self.assertEqual(ring.turn(0).position, 3, "the host holds the end")
            self.assertEqual(ring.header(0).done_through, 2)
            np.testing.assert_array_equal(ring.data(0), volume() + 2)
        finally:
            plugin.close()
            ring.close()


if __name__ == "__main__":
    unittest.main()


class TwoPlugins(unittest.TestCase):
    """§4: one ring, granted to two sessions under ONE id, each mapping it
    through its own descriptor -- a chain across two SDK instances."""

    def setUp(self):
        self.ring = HostRing()
        self.first = Plugin(self.ring, {"plus": Stage(lambda slot: slot.planes.__iadd__(1))})
        self.second = Plugin(self.ring, {"times": Stage(lambda slot: slot.planes.__imul__(3))})
        for plugin in (self.first, self.second):
            self.addCleanup(plugin.close)
        self.addCleanup(self.ring.close)
        self.assertEqual(
            self.first.store.segment(self.ring.segment_id).segment_id,
            self.second.store.segment(self.ring.segment_id).segment_id,
        )

    def test_consecutive_positions_hand_straight_across(self):
        """The second stage takes the turn the moment it loads it; no host
        step between them."""
        self.first.arm("plus", position=1)
        self.second.arm("times", position=2)
        self.ring.fill(0, volume(), [0, 1])
        self.assertEqual(self.second.runner.pass_once(), 0, "not its turn yet")
        self.assertEqual(self.first.runner.pass_once(), 1)
        self.assertEqual(self.second.runner.pass_once(), 1)
        self.assertEqual(self.ring.turn(0), s.Turn(1, 7, 3))
        self.assertEqual(self.ring.header(0).done_through, 2)
        np.testing.assert_array_equal(self.ring.data(0), (volume() + 1) * 3)

    def test_an_unarmed_position_between_them_is_the_hosts_look(self):
        self.first.arm("plus", position=1)
        self.second.arm("times", position=3)
        self.ring.fill(0, volume(), [0, 1])
        self.first.runner.pass_once()
        self.assertEqual(self.ring.turn(0).position, 2)
        self.assertEqual(self.second.runner.pass_once(), 0, "position 2 is the host's")
        np.testing.assert_array_equal(self.ring.data(0), volume() + 1)
        self.ring.hand(0, 7, 3)
        self.assertEqual(self.second.runner.pass_once(), 1)
        self.assertEqual(self.ring.turn(0).position, 4)

    def test_a_refusal_ends_the_chain_and_names_its_stage(self):
        def refuse(slot):
            raise s.RefuseVolume(s.REFUSE_CAUSAL)

        self.first.stages["plus"] = Stage(refuse)
        self.first.arm("plus", position=1)
        self.second.arm("times", position=2)
        self.ring.fill(0, volume(), [0, 1])
        self.first.runner.pass_once()
        self.assertEqual(self.second.runner.pass_once(), 0)
        self.assertEqual(self.ring.turn(0).position, -1)
        np.testing.assert_array_equal(self.ring.data(0), volume())

    def test_both_threads_run_a_stream_of_volumes(self):
        self.first.arm("plus", position=1)
        self.second.arm("times", position=2)
        self.first.runner.start()
        self.second.runner.start()
        for n in range(6):
            slot = n % 2
            deadline = time.monotonic() + 5
            while self.ring.iterations[slot] and self.ring.turn(slot).position != 3:
                self.assertLess(time.monotonic(), deadline, "a turn never came back")
                time.sleep(0.002)
            self.ring.fill(slot, volume() + n, [0, 1])
        for slot in (0, 1):
            deadline = time.monotonic() + 5
            while self.ring.turn(slot).position != 3:
                self.assertLess(time.monotonic(), deadline, "a turn never came back")
                time.sleep(0.002)
        np.testing.assert_array_equal(self.ring.data(0), (volume() + 4 + 1) * 3)
        np.testing.assert_array_equal(self.ring.data(1), (volume() + 5 + 1) * 3)


class _Doubler:
    """Rewrites channel 1 in place, needing channels 0 and 1."""

    def plan(self, inp, params):
        return s.StagePlan(modifies={1: (0, 1)})

    def process(self, slot):
        slot.plane(1)[...] *= 2


class Served(unittest.TestCase):
    """``@stage`` through a whole servicer: ``PlanStage``, the arm, the turn."""

    def setUp(self):
        saved = dict(server._STAGES)
        self.addCleanup(lambda: (server._STAGES.clear(), server._STAGES.update(saved)))
        server._STAGES.clear()
        server._STAGES["double"] = _Doubler
        self.servicer = server._Servicer(None, "test")
        self.addCleanup(self.servicer.close)

    def plan(self, stage_id="double", channels=(0, 1), dtype=U16):
        return self.servicer._on_plan_stage(
            pb2.HostMsg(
                id="p1",
                plan_stage=pb2.PlanStage(
                    stage_id=stage_id,
                    input=pb2.StageShape(channels=list(channels), z=2, y=4, x=4, dtype=dtype),
                ),
            )
        )

    def test_a_plan_answers_the_largest_output(self):
        reply = self.plan()
        self.assertEqual(reply.id, "p1")
        planned = reply.stage_planned
        self.assertEqual(
            (planned.output.z, planned.output.y, planned.output.x, planned.output.dtype),
            (2, 4, 4, U16),
        )
        self.assertEqual([(m.channel, list(m.needs)) for m in planned.modifies], [(1, [0, 1])])

    def test_a_channel_only_read_is_planned_and_needed(self):
        """A stabilizer computes its pose from channel 0 and
        writes no channel.  ``reads`` says so, and counts in its needs."""

        class Stabilizer:
            def plan(self, inp, params):
                return s.StagePlan(reads=(0,))

        server._STAGES["stab"] = Stabilizer
        planned = self.plan("stab").stage_planned
        self.assertEqual(list(planned.reads), [0])
        self.assertEqual((list(planned.modifies), list(planned.adds)), ([], []))
        self.assertEqual(s.StagePlan(reads=(0,), modifies={1: (1,)}).needs(), {0, 1})

    def test_a_plan_that_cannot_be_made_is_an_error(self):
        class Grows:
            def plan(self, inp, params):
                return s.StagePlan(alloc=(2, 4, 8))

        class Declines:
            def plan(self, inp, params):
                raise server.Refusal(pb2.Error.UNSUPPORTED, "needs two channels")

        class Huge:
            def plan(self, inp, params):
                return s.StagePlan(modifies={0x10000: ()})

        class Empty:
            def plan(self, inp, params):
                return s.StagePlan(alloc=(0, 4, 4))

        class Absent:
            def plan(self, inp, params):
                return s.StagePlan(reads=(7,))

        class ReadsHuge:
            def plan(self, inp, params):
                return s.StagePlan(reads=(0x10000,))

        server._STAGES.update(
            grows=Grows,
            declines=Declines,
            huge=Huge,
            empty=Empty,
            absent=Absent,
            reads_huge=ReadsHuge,
        )
        cases = {
            "undeclared": (self.plan("nope"), pb2.Error.NOT_FOUND),
            "index above 65535": (self.plan(channels=(0, 0x10000)), pb2.Error.PROTOCOL),
            "unknown dtype": (self.plan(dtype=9), pb2.Error.PROTOCOL),
            "grows a dimension": (self.plan("grows"), pb2.Error.INTERNAL),
            # §4 makes an index above 65 535 PROTOCOL
            # wherever it is named, the author's plan included.
            "plan names 65536": (self.plan("huge"), pb2.Error.PROTOCOL),
            # An empty allocation holds no voxel.
            "a zero axis": (self.plan("empty"), pb2.Error.INTERNAL),
            "reads a channel the input lacks": (self.plan("absent"), pb2.Error.INTERNAL),
            "reads 65536": (self.plan("reads_huge"), pb2.Error.PROTOCOL),
            "declines": (self.plan("declines"), pb2.Error.UNSUPPORTED),
        }
        for label, (reply, kind) in cases.items():
            with self.subTest(label):
                self.assertEqual((reply.WhichOneof("msg"), reply.error.kind), ("error", kind))

    def test_an_arm_keeps_the_needs_it_was_planned_with(self):
        """Needs were read from a table keyed by (stage,
        input), so planning the same input for another position rewrote an
        installed arm's needs.  §4: the arm is what the stage is held to."""
        wanted = iter([(0,), (1,)])

        class Moody:
            def plan(self, inp, params):
                return s.StagePlan(modifies={1: next(wanted)})

        server._STAGES["moody"] = Moody
        self.servicer._control.declared_stages = frozenset(server._STAGES)
        ring = HostRing()
        self.addCleanup(ring.close)
        ring.grant(self.servicer._control)

        def arm(position):
            self.servicer._control.receive(
                pb2.HostMsg(
                    stage_arm=pb2.StageArm(
                        stage_id="moody",
                        revision=1,
                        segment_id=ring.segment_id,
                        pipeline=7,
                        position=position,
                        input=pb2.StageShape(channels=[0, 1], z=2, y=4, x=4, dtype=U16),
                        output=pb2.StageShape(z=2, y=4, x=4, dtype=U16),
                    )
                )
            )
            return self.servicer._control.stage_arm_at(7, position)

        self.plan("moody")
        first = arm(1)
        self.plan("moody")
        second = arm(2)
        self.assertEqual(set(self.servicer._stage_needs(first)), {0, 1})
        self.assertEqual(set(self.servicer._stage_needs(second)), {1})

    def test_an_arm_no_plan_stands_behind_is_refused(self):
        """§4 serializes plan then arm, and the plan is where
        the arm's needs come from; an arm without one had its dependency
        check skipped.  Refused, not acknowledged."""
        ring = HostRing()
        self.addCleanup(ring.close)
        ring.grant(self.servicer._control)
        routing = self.servicer._control.receive(
            pb2.HostMsg(
                stage_arm=pb2.StageArm(
                    stage_id="double",
                    revision=1,
                    segment_id=ring.segment_id,
                    pipeline=7,
                    position=1,
                    input=pb2.StageShape(channels=[0, 1], z=2, y=4, x=4, dtype=U16),
                    output=pb2.StageShape(z=2, y=4, x=4, dtype=U16),
                )
            )
        )
        self.assertIn("no PlanStage", routing.violations[0][1])
        self.assertIsNone(self.servicer._control.stage_arm_at(7, 1))

    def test_an_undeclared_stage_is_not_armed(self):
        self.assertEqual(self.servicer._control.declared_stages, frozenset({"double"}))

    def test_a_turn_runs_on_the_stage_worker_once_armed(self):
        ring = HostRing()
        self.addCleanup(ring.close)
        ring.grant(self.servicer._control)
        self.plan()
        self.servicer._control.receive(
            pb2.HostMsg(
                stage_arm=pb2.StageArm(
                    stage_id="double",
                    revision=1,
                    segment_id=ring.segment_id,
                    pipeline=7,
                    position=1,
                    input=pb2.StageShape(channels=[0, 1], z=2, y=4, x=4, dtype=U16),
                    output=pb2.StageShape(z=2, y=4, x=4, dtype=U16),
                )
            )
        )
        ring.fill(0, volume(), [0, 1])
        ring.fill(1, volume()[:1], [1])  # channel 0 is missing: refused, reason 2
        self.servicer.stages.wake()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and 1 in (ring.turn(0).position, ring.turn(1).position):
            time.sleep(0.01)
        self.assertEqual(ring.turn(0).position, 2)
        expected = volume()
        expected[1] *= 2
        np.testing.assert_array_equal(ring.data(0), expected)
        self.assertEqual(ring.turn(1).position, -1)
        self.assertEqual(ring.header(1).reason, s.REFUSE_MISSING_INPUT)


class Declared(unittest.TestCase):
    """§2 and ``manifest_schema.md`` §3.7: a declared stage reaches the manifest."""

    def test_a_declared_stage_is_in_the_built_manifest(self):
        saved = dict(server._STAGES)
        self.addCleanup(lambda: (server._STAGES.clear(), server._STAGES.update(saved)))
        with declare._fresh():
            declare.plugin(id="org.test.stages", name="Stages", version="1")
            server.stage("stabilize", name="Stabilize (rigid)")(_Doubler)
            server.stage("denoise")(_Doubler)
            data = declare.build(["py"])
        self.assertEqual(
            data["contributions"]["stages"],
            [{"id": "stabilize", "name": "Stabilize (rigid)"}, {"id": "denoise"}],
        )
        parsed = manifest.parse_manifest(data)
        self.assertEqual(
            [(st.id, st.name) for st in parsed.stages],
            [("stabilize", "Stabilize (rigid)"), ("denoise", "denoise")],
        )

    def test_the_parser_refuses_a_malformed_stage(self):
        base = {
            "manifest_version": manifest.BINDING_MANIFEST_VERSION,
            "id": "org.test.stages",
            "name": "S",
            "version": "1",
            "engines": {"vizi": f"{manifest.HOST_API_MAJOR}.{manifest.HOST_API_MINOR}"},
            "entry": {"command": ["py"]},
        }
        for label, stages in {
            "not a list": {"id": "a"},
            "not an object": ["a"],
            "blank id": [{"id": " "}],
            "duplicate": [{"id": "a"}, {"id": "a"}],
            "name not a string": [{"id": "a", "name": 3}],
        }.items():
            with self.subTest(label):
                with self.assertRaises(manifest.ManifestError):
                    manifest.parse_manifest({**base, "contributions": {"stages": stages}})


class _Blur:
    """Adds ``sigma`` to every voxel: a stand-in whose effect shows its value."""

    def plan(self, inp, params):
        return s.StagePlan(modifies={c: (c,) for c in inp.channels})

    def process(self, slot):
        slot.planes[...] += int(slot.params["sigma"])


BLUR_PARAMS = [
    {"key": "sigma", "name": "σ", "type": "float", "value": 2.0, "min": 0.5, "max": 50},
    {"key": "mode", "type": "choice", "options": ["reflect", "constant"], "value": "reflect"},
    {"key": "passes", "type": "int", "value": 1, "min": 1},
    {"key": "clip", "type": "bool", "value": False},
]


def wire(**values):
    """A ``map<string, Value>`` from Python values, by the declared kinds."""
    kinds = {"sigma": "f", "mode": "s", "passes": "i", "clip": "b"}
    return {k: pb2.Value(**{kinds.get(k, "f"): v}) for k, v in values.items()}


def full(**over):
    values = {"sigma": 2.0, "mode": "reflect", "passes": 1, "clip": False}
    values.update(over)
    return wire(**values)


class Params(unittest.TestCase):
    """§2: parameters are declared flat in the manifest, sent for every key,
    and belong to the position."""

    def declared(self):
        return manifest.parse_stage_params(BLUR_PARAMS, "stage 'blur'")

    def test_the_declaration_parses_with_defaults(self):
        sigma, mode, passes, clip = self.declared()
        self.assertEqual((sigma.key, sigma.name, sigma.value, sigma.min), ("sigma", "σ", 2.0, 0.5))
        self.assertEqual((mode.name, mode.options), ("mode", ("reflect", "constant")))
        self.assertEqual((passes.type, passes.value, passes.max), ("int", 1, None))
        self.assertIs(clip.value, False)

    def test_each_declaration_rule(self):
        cases = {
            "blank key": [{"key": " ", "type": "bool", "value": True}],
            "duplicate": [{"key": "a", "type": "bool", "value": True}] * 2,
            "unknown type": [{"key": "a", "type": "text", "value": "x"}],
            "no value": [{"key": "a", "type": "bool"}],
            "a field its type does not take": [{"key": "a", "type": "bool", "value": 1, "min": 0}],
            "bool not a bool": [{"key": "a", "type": "bool", "value": 1}],
            "no options": [{"key": "a", "type": "choice", "value": "x"}],
            "repeated options": [
                {"key": "a", "type": "choice", "options": ["x", "x"], "value": "x"}
            ],
            "value not an option": [{"key": "a", "type": "choice", "options": ["x"], "value": "y"}],
            "int not whole": [{"key": "a", "type": "int", "value": 1.5}],
            "float not finite": [{"key": "a", "type": "float", "value": float("inf")}],
            "min over max": [{"key": "a", "type": "float", "value": 1, "min": 2, "max": 1}],
            "value out of range": [{"key": "a", "type": "int", "value": 0, "min": 1}],
        }
        for label, raw in cases.items():
            with self.subTest(label):
                with self.assertRaises(manifest.ManifestError):
                    manifest.parse_stage_params(raw, "stage 's'")

    def test_a_string_param_is_text_bounded_in_utf8_bytes(self):
        # A setting that is text, such as a tool's own option syntax.
        (opts,) = manifest.parse_stage_params(
            [{"key": "opts", "type": "string", "value": "--run=3", "max_length": 8}], "stage 's'"
        )
        self.assertEqual((opts.type, opts.value, opts.max_length), ("string", "--run=3", 8))
        (plain,) = manifest.parse_stage_params(
            [{"key": "a", "type": "string", "value": ""}], "stage 's'"
        )
        self.assertEqual(plain.max_length, manifest.STRING_PARAM_MAX_BYTES)
        cases = {
            "not a string": [{"key": "a", "type": "string", "value": 3}],
            "takes no options": [{"key": "a", "type": "string", "value": "", "options": ["x"]}],
            "zero max_length": [{"key": "a", "type": "string", "value": "", "max_length": 0}],
            "max_length past the ceiling": [
                {"key": "a", "type": "string", "value": "", "max_length": 4097}
            ],
            "max_length a bool": [{"key": "a", "type": "string", "value": "", "max_length": True}],
            "é is two bytes": [{"key": "a", "type": "string", "value": "é", "max_length": 1}],
        }
        for label, raw in cases.items():
            with self.subTest(label):
                with self.assertRaises(manifest.ManifestError):
                    manifest.parse_stage_params(raw, "stage 's'")
        self.assertEqual(
            s.check_params((opts,), {"opts": pb2.Value(s="--run=12")}), {"opts": "--run=12"}
        )
        bad = {
            "longer than max_length": {"opts": pb2.Value(s="--run=123")},
            "wrong kind": {"opts": pb2.Value(i=3)},
        }
        for label, params in bad.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    s.check_params((opts,), params)

    def test_the_wire_carries_every_declared_key_of_its_kind_in_range(self):
        declared = self.declared()
        self.assertEqual(
            s.check_params(declared, full(sigma=3.5)),
            {"sigma": 3.5, "mode": "reflect", "passes": 1, "clip": False},
        )
        bad = {
            "undeclared": {**full(), **wire(extra=1.0)},
            "missing": wire(sigma=2.0),
            "wrong kind": {**full(), "passes": pb2.Value(f=1.0)},
            "below min": full(sigma=0.1),
            "not an option": full(mode="wrap"),
            "not finite": full(sigma=float("nan")),
        }
        for label, params in bad.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    s.check_params(declared, params)


class ServedParams(unittest.TestCase):
    """The same stage at two positions with its own settings at each."""

    def setUp(self):
        saved = dict(server._STAGES), dict(server._STAGE_PARAMS)

        def restore():
            for registry, copy in zip((server._STAGES, server._STAGE_PARAMS), saved):
                registry.clear()
                registry.update(copy)

        self.addCleanup(restore)
        with declare._fresh():
            server.stage("blur", name="Blur", params=BLUR_PARAMS)(_Blur)
        self.servicer = server._Servicer(None, "test")
        self.addCleanup(self.servicer.close)
        self.ring = HostRing()
        self.addCleanup(self.ring.close)
        self.ring.grant(self.servicer._control)
        self.shape = pb2.StageShape(channels=[0, 1], z=2, y=4, x=4, dtype=U16)

    def plan(self, params):
        return self.servicer._on_plan_stage(
            pb2.HostMsg(
                id="p", plan_stage=pb2.PlanStage(stage_id="blur", input=self.shape, params=params)
            )
        )

    def arm(self, position, params, revision=1):
        return self.servicer._control.receive(
            pb2.HostMsg(
                stage_arm=pb2.StageArm(
                    stage_id="blur",
                    revision=revision,
                    segment_id=self.ring.segment_id,
                    pipeline=7,
                    position=position,
                    input=self.shape,
                    output=pb2.StageShape(
                        z=self.shape.z, y=self.shape.y, x=self.shape.x, dtype=self.shape.dtype
                    ),
                    params=params,
                )
            )
        )

    def test_a_plan_is_made_for_the_values_sent_and_refuses_bad_ones(self):
        self.assertEqual(self.plan(full()).WhichOneof("msg"), "stage_planned")
        reply = self.plan(full(sigma=99.0))
        self.assertEqual(reply.error.kind, pb2.Error.INVALID_ARGUMENT)
        self.assertIn("sigma", reply.error.message)

    def test_an_arm_with_bad_params_is_refused(self):
        routing = self.arm(1, wire(sigma=2.0))
        self.assertEqual([w for w, _ in routing.violations], ["stage_arm_refused"])
        self.assertEqual(self.servicer._control.stage_arms, {})

    def test_blur_2_then_blur_5(self):
        """The case that put parameters on the position: one stage id, two
        positions, two settings."""
        self.plan(full(sigma=2.0))
        self.plan(full(sigma=5.0))
        self.arm(1, full(sigma=2.0))
        self.arm(2, full(sigma=5.0))
        self.ring.fill(0, volume(), [0, 1])
        self.servicer.stages.pass_once()
        self.servicer.stages.pass_once()
        self.assertEqual(self.ring.turn(0).position, 3)
        np.testing.assert_array_equal(self.ring.data(0), volume() + 7)

    def test_a_declared_stage_carries_its_params_into_the_manifest(self):
        with declare._fresh():
            declare.plugin(id="org.test.blur", name="Blur", version="1")
            server.stage("blur", params=BLUR_PARAMS)(_Blur)
            data = declare.build(["py"])
        (entry,) = data["contributions"]["stages"]
        self.assertEqual(entry["params"], BLUR_PARAMS)
        (parsed,) = manifest.parse_manifest(data).stages
        self.assertEqual([p.key for p in parsed.params], ["sigma", "mode", "passes", "clip"])

    def test_a_bad_declaration_fails_at_the_decorator(self):
        with self.assertRaises(declare.DeclarationError):
            server.stage("bad", params=[{"key": "a", "type": "bool"}])


def _runner_over(outcomes):
    """A runner whose passes produce ``outcomes`` in turn (an exception to
    raise, or None for a pass that succeeds), then stop."""
    from types import SimpleNamespace

    said = []
    runner = s.StageRunner(
        memory=None,
        control=SimpleNamespace(diagnose=said.append),
        stage_for=lambda _stage: None,
        needs_for=lambda _arm: None,
        notify=lambda *_a: None,
    )
    runner.RECHECK_S = 0.0
    queue = list(outcomes)

    def pass_once():
        if not queue:
            runner._stopping = True
            return 0
        outcome = queue.pop(0)
        if outcome is not None:
            raise outcome
        return 0

    runner.pass_once = pass_once
    runner._wake.set()
    runner._run()
    return said


def test_a_lasting_pass_failure_is_said_once_until_it_changes_or_clears():
    """Every failed pass was diagnosed, and passes run every
    40 ms -- about 25 lines a second for a failure that does not go away."""
    boom, other = ValueError("boom"), KeyError("other")
    said = _runner_over([boom, boom, boom, other, other, None, boom])
    assert said == [
        f"stage runner pass raised: {boom!r}",
        f"stage runner pass raised: {other!r}",
        f"stage runner pass raised: {boom!r}",
    ]


def test_a_missing_package_stops_the_runner_and_is_said_once():
    missing = ImportError("the plugin SDK's shared-memory state words need atomicshm")
    said = _runner_over([missing, missing, missing])
    assert said == [f"stage runner stopped: {missing}"]


def test_the_dependency_messages_name_every_package_and_what_needs_it():
    """`MISSING_DEPS` named two of the three packages, and a
    stage-only plugin without atomicshm was told `set_items_live` needed it."""
    from vizi_plugin_sdk import _lazy

    assert all(name in _lazy.MISSING_DEPS for name in ("grpcio", "protobuf", "atomicshm"))
    import inspect

    from vizi_plugin_sdk import _shared_atomic

    source = inspect.getsource(_shared_atomic._import_atomicshm)
    assert "set_items_live" not in source and "stage" in source
