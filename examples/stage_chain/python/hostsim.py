#!/usr/bin/env python3
"""Drive the stage-chain example over the real transport, with no vizi.

    .venv/bin/python hostsim.py                                   # Python on both sides
    .venv/bin/python hostsim.py --plugin-dir ../rust              # one implementation, two processes
    .venv/bin/python hostsim.py --plugin-dir ../go --other-dir ../rust   # a mixed chain

Every scenario runs a chain across TWO plugin sessions, ``A`` and ``B``, on
one `S` ring (`processing_stages.md` §4): ``--plugin-dir`` launches ``A``,
``--other-dir`` launches ``B`` (default: the same directory, a second
process).  Stages sit in both, handed from A to B and from B to A, so one run
exercises both orders of a pair.

The host is `vizi_hostsim.chain.Chain`.  The expected values are computed
here from ``../SPEC.md``'s arithmetic, never by calling the plugin's code.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # examples/, for vizi_hostsim

from vizi_hostsim import Dataset, PluginError, check, failures  # noqa: E402
from vizi_hostsim.chain import Chain, ChainError, decode_turn  # noqa: E402
from vizi_plugin_sdk import pb2  # noqa: E402

# -- the dataset (SPEC, "Acceptance") ------------------------------------------

T, C, Z, Y, X = 6, 2, 4, 16, 16


def blob_centre(t: int) -> tuple[int, int, int]:
    return 2, 5 + t, 4 + t % 3


def source(t: int, c: int) -> np.ndarray:
    z, y, x = np.meshgrid(np.arange(Z), np.arange(Y), np.arange(X), indexing="ij")
    if c == 0:
        bz, by, bx = blob_centre(t)
        blob = (z == bz) & (abs(y - by) <= 1) & (abs(x - bx) <= 1)
        v = 10 + (x + 2 * y + 3 * z) % 7 + 3000 * blob
    else:
        v = (x * y + 5 * z + 40 * t) % 1000 + 1
    return v.astype(np.uint16)


DATASET = Dataset(
    shape_tczyx=(T, C, Z, Y, X),
    np_dtype=np.dtype(np.uint16),
    wire_dtype=pb2.DTYPE_U16,
    open_generation=7,
    volume=source,
    spacing_zyx=(1.0, 1.0, 1.0),
)

# -- SPEC's arithmetic, independently ------------------------------------------


def exp_gain(a: np.ndarray, factor: float) -> np.ndarray:
    scaled = a.astype(np.float64) * factor
    if a.dtype == np.uint16:
        return np.clip(np.rint(scaled), 0, 65535).astype(np.uint16)
    if a.dtype == np.uint8:
        return np.clip(np.rint(scaled), 0, 255).astype(np.uint8)
    return scaled.astype(a.dtype)


def exp_diff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    if a.dtype.kind == "u":
        return np.abs(a.astype(np.int64) - b.astype(np.int64)).astype(a.dtype)
    return np.abs(a - b)


def exp_widen(a: np.ndarray, crop: int) -> np.ndarray:
    return a[:, crop : a.shape[1] - crop, crop : a.shape[2] - crop].astype(np.float32)


def exp_centroid(a: np.ndarray, origin=(0.0, 0.0, 0.0)) -> tuple[float, float, float]:
    """SPEC: exact integer sums, one f64 division per axis, placed by the
    origin (extent start 0 throughout this driver)."""
    w = a.astype(np.uint64)
    total = int(w.sum())
    idx = np.meshgrid(*(np.arange(n, dtype=np.uint64) for n in a.shape), indexing="ij")
    return tuple(float(o) + (int((w * i).sum()) / total - 0) for o, i in zip(origin, idx))


def equal(got: np.ndarray, want: np.ndarray, what: str) -> None:
    assert got.dtype == want.dtype, f"{what}: dtype {got.dtype}, want {want.dtype}"
    assert got.shape == want.shape, f"{what}: shape {got.shape}, want {want.shape}"
    if not np.array_equal(got, want):
        bad = np.argwhere(got != want)
        first = tuple(bad[0])
        raise AssertionError(f"{what}: {len(bad)} voxels differ, first at {first}: {got[first]} != {want[first]}")


def reported(host) -> int:
    """Violations reported, summed over `count`: an SDK may report two at one
    site as one `ContractViolation` with `count` 2 (§12)."""
    return sum(max(v.count, 1) for v in host.violations)


def await_violations(host, count: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while reported(host) < count:
        assert time.monotonic() < deadline, f"{reported(host)} violations reported within {timeout:g}s, want {count}"
        time.sleep(0.01)


def clean(chain: Chain, *, violations: int = 0) -> None:
    """Nothing went wrong that the scenario did not provoke."""
    assert not chain.diagnostics, chain.diagnostics
    for host in chain.hosts:
        assert not host.unhandled, (host.session_epoch, host.unhandled)
        assert not host.diagnostics, (host.session_epoch, host.diagnostics)
    seen = sum(reported(h) for h in chain.hosts)
    assert seen == violations, [(h.session_epoch, list(h.violations)) for h in chain.hosts]


# -- scenarios -----------------------------------------------------------------


class Scenarios:
    def __init__(self, a_dir: Path, b_dir: Path) -> None:
        self.a_dir, self.b_dir = a_dir, b_dir

    def chain(self, **kw) -> tuple[Chain, object, object]:
        chain = Chain(DATASET, **kw)
        try:
            a = chain.launch(self.a_dir)
            b = chain.launch(self.b_dir)
        except BaseException:
            chain.__exit__(None, None, None)
            raise
        return chain, a, b

    # §3: planning, with the manifest's params and every error kind.
    def planning(self) -> str:
        chain, a, b = self.chain()
        with chain:
            src = chain.source_shape((0, 1))
            for host in (a, b):
                g = chain.add(host, "gain", 1, factor=1.5)
                got = chain.plan_stage(g, src)
                assert [(m.channel, list(m.needs)) for m in got.modifies] == [(0, [0])], got
                assert not got.adds and not got.reads, got
                assert (got.output.z, got.output.y, got.output.x, got.output.dtype) == (Z, Y, X, pb2.DTYPE_U16), got
                d = chain.add(host, "diff", 1)
                got = chain.plan_stage(d, src)
                assert [(n.key, sorted(n.needs)) for n in got.adds] == [("absdiff", [0, 1])], got
                assert not got.modifies and not got.reads, got
                w = chain.add(host, "widen", 1, crop=2)
                got = chain.plan_stage(w, src)
                assert (got.output.z, got.output.y, got.output.x, got.output.dtype) == (Z, Y - 4, X - 4, pb2.DTYPE_F32), got
                assert sorted(m.channel for m in got.modifies) == [0, 1], got
                r = chain.add(host, "drift", 1)
                got = chain.plan_stage(r, src)
                # a pose-only stage names what it reads, rewrites nothing
                assert list(got.reads) == [0] and not got.modifies and not got.adds, got
                errors = []
                for step, inp, want in (
                    (chain.add(host, "gain", 1, raw_params={"factor": pb2.Value(s="x")}), src, pb2.Error.INVALID_ARGUMENT),
                    (chain.add(host, "gain", 1, factor=100.0), src, pb2.Error.INVALID_ARGUMENT),
                    (chain.add(host, "gain", 1, raw_params={"stall_ms": None}), src, pb2.Error.INVALID_ARGUMENT),
                    (chain.add(host, "gain", 1, raw_params={"nope": pb2.Value(i=1)}), src, pb2.Error.INVALID_ARGUMENT),
                    (chain.add(host, "gain", 1, raw_params={"factor": pb2.Value(f=float("nan"))}), src, pb2.Error.INVALID_ARGUMENT),
                    (chain.add(host, "nonesuch", 1), src, pb2.Error.NOT_FOUND),
                    (chain.add(host, "gain", 1, channel=5), src, pb2.Error.UNSUPPORTED),
                    (chain.add(host, "widen", 1, crop=8), src, pb2.Error.UNSUPPORTED),
                    (chain.add(host, "drift", 1), pb2.StageShape(channels=[0], z=Z, y=Y, x=X, dtype=pb2.DTYPE_F32), pb2.Error.UNSUPPORTED),
                ):
                    try:
                        chain.plan_stage(step, inp)
                    except PluginError as exc:
                        errors.append(exc.kind == want or (step.stage_id, pb2.Error.Kind.Name(exc.kind), pb2.Error.Kind.Name(want)))
                    else:
                        errors.append((step.stage_id, "answered", pb2.Error.Kind.Name(want)))
                assert all(e is True for e in errors), [e for e in errors if e is not True]
            chain.steps.clear()
            clean(chain)
        return "both sessions: four plans as SPEC states them, nine refusals of the right kind"

    # §4, §6: two processes, one ring under one id, consecutive positions.
    def two_processes(self) -> str:
        chain, a, b = self.chain()
        with chain:
            chain.add(a, "gain", 1, factor=1.5)
            chain.add(b, "drift", 2)
            chain.add(a, "diff", 3)
            chain.plan()
            ring = chain.build_ring(slots=2)
            epochs = {a.session_epoch, b.session_epoch}
            assert ring.segment_id >> 32 not in epochs, (ring.segment_id, epochs)
            for host in (a, b):
                assert host.acks[ring.segment_id].WhichOneof("outcome") == "installed"
            chain.arm()
            assert chain.plane_cap == 3 and chain.load == (0, 1), (chain.plane_cap, chain.load)
            ref = None
            for t in range(3):
                got = chain.run(t)
                assert got.outcome == "processed", got
                h = got.header
                assert h.done_through == 3 and h.outcome == 1 and h.t == t and h.open_generation == 7, h
                assert h.channels[: h.planes] == (0, 1, 2), h.channels
                g0 = exp_gain(source(t, 0), 1.5)
                equal(got.plane(0), g0, f"t={t} gained channel 0")
                equal(got.plane(1), source(t, 1), f"t={t} channel 1")
                equal(got.plane(2), exp_diff(g0, source(t, 1)), f"t={t} added |0 - 1|")
                c = exp_centroid(g0)
                ref = c if ref is None else ref
                want_t = tuple(r - x for r, x in zip(ref, c))
                assert h.pose_t == want_t, (t, h.pose_t, want_t)
                assert h.pose_r == (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0), h.pose_r
            pairs = {(f, to) for f, to, _, _ in chain.relays}
            assert (a.session_epoch, b.session_epoch) in pairs and (b.session_epoch, a.session_epoch) in pairs, chain.relays
            clean(chain)
        return f"ring {ring.segment_id:#x} in sessions {sorted(epochs)}; 3 volumes; hints relayed both ways"

    # §6.2: the next stage polls sooner than the relay arrives.
    def before_the_relay(self) -> str:
        chain, a, b = self.chain()
        with chain:
            chain.add(a, "gain", 1, factor=2.0)
            chain.add(b, "gain", 2, channel=1, factor=0.5)
            chain.plan()
            chain.build_ring(slots=1)
            chain.arm()
            chain.relay_delay = 3.0
            got = chain.run(0)
            assert got.outcome == "processed", got
            assert got.elapsed < 2.0, f"took {got.elapsed:.2f}s: B waited for the relayed D"
            equal(got.plane(0), exp_gain(source(0, 0), 2.0), "channel 0")
            equal(got.plane(1), exp_gain(source(0, 1), 0.5), "channel 1")
            clean(chain)
        return f"B took position 2 on its own schedule; the pass took {got.elapsed * 1000:.0f} ms against a 3 s relay"

    # §4: stages at odd positions, the host at the even ones.
    def odd_positions(self) -> str:
        chain, a, b = self.chain()
        seen = []

        def inspect(position, header, planes):
            seen.append((position, header.done_through, header.dtype, header.alloc, header.channels[: header.planes]))

        with chain:
            chain.inspect = inspect
            chain.add(a, "gain", 1, factor=2.0)
            chain.add(b, "diff", 3)
            chain.add(a, "widen", 5, crop=3)
            chain.plan()
            chain.build_ring(slots=2)
            chain.arm()
            got = chain.run(1)
            assert got.outcome == "processed" and got.trail == [1, 2, 3, 4, 5, 6], (got.outcome, got.trail)
            g0 = exp_gain(source(1, 0), 2.0)
            d = exp_diff(g0, source(1, 1))
            at2, at4 = got.inspections
            equal(at2.planes[0], g0, "at 2, channel 0")
            equal(at4.planes[2], d, "at 4, the added channel")
            assert seen == [(2, 1, pb2.DTYPE_U16, (Z, Y, X), (0, 1)), (4, 3, pb2.DTYPE_U16, (Z, Y, X), (0, 1, 2))], seen
            h = got.header
            assert h.done_through == 5 and h.dtype == pb2.DTYPE_F32 and h.alloc == (Z, Y - 6, X - 6), h
            assert h.extent == (0, Z, 0, Y - 6, 0, X - 6) and h.origin == (0.0, 3.0, 3.0), (h.extent, h.origin)
            equal(got.plane(2), exp_widen(d, 3), "widened |0 - 1|")
            clean(chain)
        return "the host looked at 2 and 4 and handed on; 1, 3, 5 ran in two sessions"

    # §6.3: a refusal ends the chain.
    def refusal(self) -> str:
        chain, a, b = self.chain()
        with chain:
            chain.add(a, "gain", 1, factor=1.5)
            chain.add(b, "drift", 2)
            chain.add(a, "diff", 3)
            chain.plan()
            chain.build_ring(slots=2)
            chain.arm()
            for t in (0, 1):
                assert chain.run(t).outcome == "processed"
            got = chain.run(3)
            h = got.header
            assert got.outcome == "refused" and got.position == -2, (got.outcome, got.position)
            assert (h.outcome, h.reason, h.done_through) == (2, 1, 1), (h.outcome, h.reason, h.done_through)
            assert h.planes == 2, "diff at 3 must not have run"
            equal(got.plane(0), exp_gain(source(3, 0), 1.5), "the data as far as the chain got")
            nxt = chain.run(4)
            assert nxt.outcome == "processed" and nxt.header.pose_t == (0.0, 0.0, 0.0), (nxt.outcome, nxt.header.pose_t)
            clean(chain)
        return "t=3 after t=1 refused at 2 (-2, causal, done_through 1); t=4 started over"

    # §3, §5.2, §5.3: an added channel, then a widened type.
    def add_then_widen(self) -> str:
        chain, a, b = self.chain()
        with chain:
            chain.add(a, "diff", 1)
            chain.add(b, "widen", 2, crop=2)
            chain.plan()
            want_cap = max(2 * Z * Y * X * 2, 3 * Z * (Y - 4) * (X - 4) * 4)
            assert chain.plane_cap == 3 and chain.capacity == want_cap, (chain.plane_cap, chain.capacity, want_cap)
            chain.build_ring(slots=1)
            chain.arm()
            got = chain.run(2)
            h = got.header
            assert got.outcome == "processed", got
            assert h.dtype == pb2.DTYPE_F32 and h.alloc == (Z, Y - 4, X - 4) and h.channels[:3] == (0, 1, 2), h
            assert h.extent == (0, Z, 0, Y - 4, 0, X - 4) and h.origin == (0.0, 2.0, 2.0), (h.extent, h.origin)
            for c in (0, 1):
                equal(got.plane(c), exp_widen(source(2, c), 2), f"channel {c}")
            equal(got.plane(2), exp_widen(exp_diff(source(2, 0), source(2, 1)), 2), "the added channel")
            clean(chain)
        return f"3 planes of u16 {Z}x{Y}x{X} repacked as f32 {Z}x{Y - 4}x{X - 4}; capacity {chain.capacity} bytes"

    # §2, §4: one stage at two positions with different params; a bad arm.
    def params(self) -> str:
        chain, a, b = self.chain()
        with chain:
            first = chain.add(a, "gain", 1, factor=2.0)
            second = chain.add(a, "gain", 2, factor=0.25)
            chain.add(b, "gain", 3, channel=1, factor=3.0)
            chain.plan()
            chain.build_ring(slots=1)
            chain.arm()
            got = chain.run(0)
            equal(got.plane(0), exp_gain(exp_gain(source(0, 0), 2.0), 0.25), "channel 0 after x2 then x0.25")
            equal(got.plane(1), exp_gain(source(0, 1), 3.0), "channel 1 after x3")
            # A value out of range: refused at site 32, never acknowledged.
            arm = chain.send_arm(second, params={**second.wire_params(), "factor": pb2.Value(f=99.0)})
            acked = chain.await_arm_ack(second, arm.revision, timeout=1.0)
            assert not acked, "an arm with factor 99 (max 16) was acknowledged"
            # The report is its own push: wait for it, not for the ack's absence.
            await_violations(a, 1)
            assert {v.where for v in a.violations} == {pb2.VIOLATION_SITE_STAGE_ARM_REFUSED}, list(a.violations)
            # A value of the wrong kind, likewise.
            arm = chain.send_arm(first, params={**first.wire_params(), "factor": pb2.Value(i=2)})
            assert not chain.await_arm_ack(first, arm.revision, timeout=1.0)
            await_violations(a, 2)
            assert {v.where for v in a.violations} == {pb2.VIOLATION_SITE_STAGE_ARM_REFUSED}, list(a.violations)
            assert reported(a) == 2 and not b.violations, (list(a.violations), list(b.violations))
            # Re-armed properly, the chain runs again.
            chain.arm([1, 2])
            got = chain.run(1)
            equal(got.plane(0), exp_gain(exp_gain(source(1, 0), 2.0), 0.25), "after the re-arm")
            clean(chain, violations=2)
        return "gain x2 at 1 and x0.25 at 2 in one session; factor 99 and an int factor refused at site 32, unacknowledged"

    # §7: a missed deadline quarantines the slot.
    def deadline(self) -> str:
        chain, a, b = self.chain(deadline=0.4)
        with chain:
            slow = chain.add(a, "gain", 1, factor=2.0, stall_ms=1500)
            chain.add(b, "gain", 2, channel=1, factor=2.0)
            chain.plan()
            chain.build_ring(slots=2)
            chain.arm()
            got = chain.run(0)
            assert got.outcome == "deadline" and got.position == 1 and got.planes is None, got
            assert chain.quarantined == {got.slot}, chain.quarantined
            other = chain.free_slot()
            assert other is not None and other != got.slot, (other, got.slot)
            deadline = time.monotonic() + 5
            while got.slot in chain.quarantined and time.monotonic() < deadline:
                time.sleep(0.02)
            assert chain.unquarantined == [got.slot], "the slot never came back once the stage handed it on"
            # §2: a changed parameter is a new plan and a new arm revision.
            slow.params["stall_ms"] = 0
            chain.plan()
            chain.arm([1])
            nxt = chain.run(1)
            assert nxt.outcome == "processed", nxt
            equal(nxt.plane(0), exp_gain(source(1, 0), 2.0), "after the quarantine")
            clean(chain)
        return f"slot {got.slot} held past 0.4 s, quarantined, slot {other} free meanwhile, slot {got.slot} back once handed on"

    # §4: a re-arm while the stage holds a turn is acknowledged at once; the
    # turn in progress finishes under the old revision.
    def rearm_while_held(self) -> str:
        chain, a, b = self.chain()
        with chain:
            first = chain.add(a, "gain", 1, factor=2.0, stall_ms=800)
            chain.add(b, "gain", 2, channel=1, factor=2.0)
            chain.plan()
            chain.build_ring(slots=2)
            chain.arm()
            ticket = chain.start(0)
            time.sleep(0.2)  # A is inside its 800 ms stall, holding position 1
            first.params.update(factor=3.0, stall_ms=0)
            chain.plan()  # §2: a new value is a new plan, then a new arm
            began = time.monotonic()
            chain.arm([1])
            took = time.monotonic() - began
            _, _, held = decode_turn(chain.ring.load_turn(ticket.slot))
            assert held == 1, f"the turn moved to {held} before the re-arm was acknowledged"
            got = chain.wait(ticket)
            assert got.outcome == "processed", got
            equal(got.plane(0), exp_gain(source(0, 0), 2.0), "the turn in progress, under the old revision")
            nxt = chain.run(1)
            equal(nxt.plane(0), exp_gain(source(1, 0), 3.0), "the next turn, under the new revision")
            clean(chain)
        return f"acknowledged in {took * 1000:.0f} ms with the turn still held; x2 finished, the next volume x3"

    # §7: a session that ends while it holds a turn loses it.
    def session_ends(self) -> str:
        chain, a, b = self.chain(deadline=10.0)
        with chain:
            chain.add(a, "gain", 1, factor=2.0)
            chain.add(b, "gain", 2, channel=1, factor=2.0, stall_ms=5000)
            chain.plan()
            chain.build_ring(slots=2)
            chain.arm()
            ticket = chain.start(0)
            deadline = time.monotonic() + 5
            while decode_turn(chain.ring.load_turn(ticket.slot))[2] != 2:
                assert time.monotonic() < deadline, "the turn never reached B"
                time.sleep(0.01)
            time.sleep(0.2)  # B is inside its stall
            chain.kill_session(b)
            got = chain.wait(ticket, timeout=5)
            assert (got.outcome, got.position, got.note) == ("failed", -2, "session ended"), (got.outcome, got.position, got.note)
            assert got.planes is None, "a volume from a lost turn is discarded"
            _, _, now = decode_turn(chain.ring.load_turn(ticket.slot))
            assert now == -2, f"the turn word reads {now}, not the host's -2"
            chain.remove(2)
            chain.plan()
            chain.arm()
            nxt = chain.run(1)
            assert nxt.outcome == "processed" and nxt.header.done_through == 1, (nxt.outcome, nxt.header)
            equal(nxt.plane(0), exp_gain(source(1, 0), 2.0), "A alone, afterwards")
            clean(chain)
        return "B killed holding position 2: the host stored -2 and discarded the volume; A alone carried on"

    # §4: the chain grows past its ring; a larger one supersedes it.
    def larger_ring(self) -> str:
        chain, a, b = self.chain()
        with chain:
            chain.add(a, "gain", 1, factor=1.5)
            chain.add(b, "drift", 2)
            chain.plan()
            old = chain.build_ring(slots=2).segment_id
            chain.arm()
            first = chain.run(0)
            assert first.outcome == "processed" and first.header.planes == 2, first
            chain.add(a, "diff", 3)
            chain.plan()
            assert not chain.fits(), (chain.plane_cap, chain.ring_plane_cap)
            new = chain.regrow().segment_id
            assert new != old and new >> 32 == old >> 32, (hex(old), hex(new))
            for host in (a, b):
                assert host.acks[new].WhichOneof("outcome") == "installed", host.acks[new]
                assert old in host.retired, f"session {host.session_epoch} sent no SegmentReleased for the old ring"
            got = chain.run(1)
            h = got.header
            assert got.outcome == "processed" and h.planes == 3 and h.plane_cap == 3, (got.outcome, h)
            g0 = exp_gain(source(1, 0), 1.5)
            equal(got.plane(2), exp_diff(g0, source(1, 1)), "the added channel, on the larger ring")
            # drift's per-arm state survived the re-arm: t=1 follows t=0.
            want = tuple(r - c for r, c in zip(exp_centroid(exp_gain(source(0, 0), 1.5)), exp_centroid(g0)))
            assert h.pose_t == want, (h.pose_t, want)
            clean(chain)
        return f"ring {old:#x} superseded by {new:#x} in both sessions, both released the old; every position re-armed"


SCENARIOS = [
    ("planning: four plans and nine refusals, in both sessions", "planning"),
    ("two processes on one ring under one id, consecutive positions, hints relayed", "two_processes"),
    ("the next stage takes its turn before the relayed D", "before_the_relay"),
    ("odd positions; the host inspects at the even ones", "odd_positions"),
    ("a refusal ends the chain (-k, done_through)", "refusal"),
    ("an added channel, then a widened type (a relayout)", "add_then_widen"),
    ("params per position, and an arm with a bad value refused", "params"),
    ("a missed deadline: the slot quarantined, not reused, then back", "deadline"),
    ("a re-arm while the stage holds a turn", "rearm_while_held"),
    ("a session that ends while it holds a turn loses it", "session_ends"),
    ("the chain outgrows its ring: a larger one supersedes it", "larger_ring"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-dir", type=Path, default=HERE, help="session A's plugin (default: this one)")
    parser.add_argument("--other-dir", type=Path, default=None, help="session B's plugin (default: the same as A)")
    parser.add_argument("--only", action="append", default=[], help="run only these scenarios (method names)")
    args = parser.parse_args()
    a_dir = args.plugin_dir.resolve()
    b_dir = (args.other_dir or args.plugin_dir).resolve()
    print(f"session A: {a_dir}\nsession B: {b_dir}")
    runner = Scenarios(a_dir, b_dir)
    for label, name in SCENARIOS:
        if args.only and name not in args.only:
            continue
        check(label, getattr(runner, name))
    if failures:
        print(f"\n{len(failures)} failed")
        return 1
    print("\nall passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
