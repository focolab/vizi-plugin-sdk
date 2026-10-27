"""Run the host->plugin control-plane cases through the Python SDK and check
every outcome the specification states.

    python -I -B scripts/control_cases/consume.py [--cases DIR] [-v]

Each case runs in a fresh receiver, and so does its baseline.  A single
message goes through ``vizi_plugin_sdk.control.ControlPlane.receive`` with a
violation reporter and an empty segment store; a request of the plugin's own
that a case names as in flight waits on the SDK's ``HostClient``, which sends
it nowhere, so a reply under its id meets the SDK's own matching.  A ``Hello``
case's message is the session's first, so it goes through the servicer's
dispatch with the session awaiting ``Hello`` (``_route_one``): the handshake
check and the rule that a first message of any other kind, an unknown one
included, fails the session.  A ``PlanStage`` goes through the servicer's
planner.  Those rules live there.

Checked per ``expected``:

  ignored              every observation matches the baseline's: the routing,
                       the violations, the bytes sent, what reached a waiter,
                       and whether anything raised.  Local diagnostics may
                       differ.
  not-a-violation      no violation the baseline did not have, and nothing
                       raised that the baseline did not raise.
  item-refused         that one item refused and nothing else: a request
                       answered Error{UNSUPPORTED} echoing its id, an
                       arrangement not acknowledged (nothing sent); no
                       violation the baseline did not have, nothing raised.
  unsupported          Error{UNSUPPORTED} echoing the id, nothing else.
  diagnostic-only      nothing sent and nothing delivered.
  session-continues    the handshake check passes, records the host's
                       (major, minor) and settles a Ready at the lower of
                       the host's minor and this SDK's.
  session-faulted      the receiver ends the session: the handshake check
                       fails, or the message raises, naming the reused id
                       where the case gives one; nothing is sent for it.
  deduplicated         (a sequence) the second, byte-identical copy is
                       dropped: nothing sent, nothing delivered, no
                       violation, nothing raised, and a routing other than
                       the first copy's (the receiver says it did not
                       dispatch it again).
  UNSTATED             the outcome is printed and never fails the run.

A mismatch listed in ``known.json`` is reported as known.  A ``known.json``
entry that matches no mismatch fails the run, so the list stays exact.  The
exit status is non-zero on any other mismatch.
"""

from __future__ import annotations

import argparse
import base64
import collections
import contextlib
import gzip
import io
import json
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from vizi_plugin_sdk import control as _control  # noqa: E402
from vizi_plugin_sdk import manifest as _manifest  # noqa: E402
from vizi_plugin_sdk import memory as _memory  # noqa: E402
from vizi_plugin_sdk import server as _server  # noqa: E402
from vizi_plugin_sdk import stages as _stages  # noqa: E402
from vizi_plugin_sdk._proto import vizi_plugin_pb2 as pb2  # noqa: E402
from vizi_plugin_sdk.violations import ViolationReporter  # noqa: E402

PERM_RW = _memory.PERM_PLUGIN_READ | _memory.PERM_PLUGIN_WRITE
PARAM_TYPES = {"i": ("int", 0), "f": ("float", 0.0), "b": ("bool", False), "s": ("string", "")}


def decode(b64: str):
    return pb2.HostMsg.FromString(base64.b64decode(b64))


def segment_fd(segment_id: int, size: int) -> int:
    """A file-backed object with a valid preamble (shared_memory_protocol.md)."""
    head = struct.pack("<QI16s", segment_id, 0, b"\x5a" * 16)
    with tempfile.TemporaryFile() as tmp:
        tmp.truncate(size)
        tmp.seek(0)
        tmp.write(head + struct.pack("<I", zlib.crc32(head)))
        tmp.flush()
        return os.dup(tmp.fileno())


class Receiver:
    """One fresh plugin end: a control plane, and a servicer where the case
    needs the handshake or the stage planner.  With ``first_message`` the
    servicer's session is awaiting ``Hello``, and the first message observed
    goes through its dispatch."""

    def __init__(self, *, servicer: bool, outstanding=(), first_message: bool = False):
        self.first_message = first_message and servicer
        self.outstanding: set[str] = set()
        self.pending: dict = {}
        self.delivered: list[str] = []
        self.servicer = None
        if servicer:
            self.servicer = _server._Servicer(None, "conformance")
            self.plane = self.servicer._control
            store = self.servicer._memory
            # The waiters are this harness's own: the servicer's belong to
            # plugin code this harness does not run.
            self.plane._deliver_reply = self._deliver
        else:
            store = _memory.SegmentStore(_memory.PURPOSES)
            self.plane = _control.ControlPlane(memory=store, reporter=ViolationReporter(), deliver_reply=self._deliver)
        # The SDK's own request client, sending nowhere: a request this end has
        # in flight waits on it, so a reply meets the SDK's matching.
        self.host = _server.HostClient(lambda msg: None, store, self.plane)
        for request in outstanding:
            self.expect(request)

    def expect(self, request) -> None:
        """This plugin's own request, in flight and awaiting its reply."""
        try:
            self.pending[request.id] = self.host._start(request)
        except Exception:  # noqa: BLE001 - a request the client will not take is simulated by id
            self.outstanding.add(request.id)

    def _deliver(self, hm) -> bool:
        pending = self.pending.get(hm.id)
        if pending is not None:
            took = self.host.route_reply(hm)  # a SessionFault here is the receiver faulting
            if took and pending.done():
                # A waiter takes one reply: a second message under its id finds none.
                del self.pending[hm.id]
                self.delivered.append(hm.id)
            return took
        if hm.id in self.outstanding:
            self.outstanding.discard(hm.id)
            self.delivered.append(hm.id)
            return True
        return False

    def close(self):
        if self.servicer is not None:
            self.servicer.close()

    def grant(self, segment_id: int, purpose: "str | None", regions=None) -> None:
        regions = list(regions) if regions else [
            pb2.Region(index=0, offset=64, extent=4096, discipline=pb2.RegionDiscipline.REGION_DISCIPLINE_STAGED,
                       producer=pb2.RegionProducer.REGION_PRODUCER_HOST)
        ]
        size = max(r.offset + r.extent for r in regions)
        size = (size + 4095) // 4096 * 4096
        self.plane.capability(segment_id, size, PERM_RW, fd=segment_fd(segment_id, size))
        if purpose is not None:
            self.plane.receive(pb2.HostMsg(install_segment=pb2.InstallSegment(segment_id=segment_id, purpose=purpose, regions=regions)))
        self.plane.outbox.drain()

    def observe(self, hm) -> dict:
        """What this end did with one message."""
        before = len(self.delivered)
        o = {"kind": None, "action": None, "violations": [], "sent": [], "delivered": [], "raised": None, "handshake": None}
        try:
            kind = hm.WhichOneof("msg")
            if self.first_message:
                # §3.1: the session's first message, through the servicer's own
                # dispatch, which fails the session on anything but a good Hello.
                self.first_message = False
                s = self.servicer
                s._awaiting_hello = True
                went_on = s._route_one(hm)
                o["kind"] = kind
                o["action"] = "HELLO" if kind == "hello" else None
                o["handshake"] = "ok" if went_on and s.handshake_error is None and s.session_fault is None else "failed"
                o["host_protocol"] = list(self.plane.host_protocol) if self.plane.host_protocol else None
                o["ready_minor"] = s._ready.ready.protocol_minor if s._ready is not None else None
                return self._finish(o, before)
            routing = self.plane.receive(hm)
            o["kind"], o["action"] = routing.kind, routing.action.name
            o["violations"] = sorted(w for w, _ in routing.violations)
            if kind == "plan_stage" and routing.action is _control.Action.HOST_REQUEST and self.servicer is not None:
                self.plane.outbox.send(self.servicer._on_plan_stage(hm))
        except Exception as exc:  # noqa: BLE001 - a raise is an observation
            o["raised"] = type(exc).__name__
            o["raised_detail"] = str(exc)
        return self._finish(o, before)

    def _finish(self, o: dict, before: int) -> dict:
        sent = self.plane.outbox.drain()
        o["sent"] = [base64.b64encode(m.SerializeToString(deterministic=True)).decode() for m in sent]
        o["sent_kinds"] = [m.WhichOneof("msg") for m in sent]
        o["violations"] += sorted(
            f"site:{m.contract_violation.where}" for m in sent if m.WhichOneof("msg") == "contract_violation"
        )
        o["unsupported"] = [m.id for m in sent if m.WhichOneof("msg") == "error" and m.error.kind == pb2.Error.UNSUPPORTED]
        o["delivered"] = self.delivered[before:]
        return o


def setup_receiver(case, *, servicer: bool, outstanding) -> Receiver:
    # A Hello case's message replaces the handshake message: it is the first.
    rx = Receiver(servicer=servicer, outstanding=outstanding, first_message=case["family"] == "HostMsg.hello")
    setup = case.get("setup") or {}
    if "stage_ring" in setup:
        rx.grant(setup["stage_ring"]["segment_id"], setup["stage_ring"]["purpose"])
    if "capability" in setup:
        # The doorbell's half only; the InstallSegment under test is the other.
        segment_id = setup["capability"]["segment_id"]
        rx.plane.capability(segment_id, 8192, PERM_RW, fd=segment_fd(segment_id, 8192))
        rx.plane.outbox.drain()
    for step in setup.get("prior", ()):
        rx.observe(decode(step["b64"]))
    return rx


@contextlib.contextmanager
def declared_stage(case):
    """The stage a case arms, declared with the params its arm sends and a
    plan that needs nothing."""
    decl = (case.get("setup") or {}).get("declare_stage")
    if decl is None:
        yield
        return

    class Stage:
        def plan(self, inp, params):
            return _stages.StagePlan()

        def process(self, slot):
            pass

    sid = decl["stage_id"]
    params = [{"key": p["key"], "type": PARAM_TYPES[p["kind"]][0], "value": PARAM_TYPES[p["kind"]][1]} for p in decl["params"]]
    saved = dict(_server._STAGES), dict(_server._STAGE_PARAMS)
    _server._STAGES[sid] = Stage
    _server._STAGE_PARAMS[sid] = _manifest.parse_stage_params(params, f"stage {sid!r}")
    try:
        yield
    finally:
        for registry, copy in zip((_server._STAGES, _server._STAGE_PARAMS), saved):
            registry.clear()
            registry.update(copy)


def own_requests(*contexts) -> list:
    """The plugin's own requests among ``contexts``, under the ids they went
    out with: each is in flight, awaiting its reply."""
    out = []
    for ctx in contexts:
        if ctx and ctx["direction"] == "plugin->host":
            request = pb2.PluginMsg.FromString(base64.b64decode(ctx["b64"]))
            request.id = ctx["id"]
            out.append(request)
    return out


def run_one(case, b64: str) -> dict:
    """One message in a fresh receiver, after whatever the case puts in flight."""
    outstanding = own_requests(case.get("precondition"), case.get("request_context"))
    servicer = case["family"] == "HostMsg.hello" or "declare_stage" in (case.get("setup") or {})
    with declared_stage(case):
        rx = setup_receiver(case, servicer=servicer, outstanding=outstanding)
        try:
            ctx = case.get("request_context")
            if ctx and ctx["direction"] == "host->plugin":
                rx.observe(decode(ctx["b64"]))  # the host's request, received first
            return rx.observe(decode(b64))
        finally:
            rx.close()


def run_sequence(case, seq) -> list:
    outstanding = own_requests(case.get("precondition"))
    with declared_stage(case):
        rx = setup_receiver(case, servicer=True, outstanding=outstanding)
        try:
            return [rx.observe(decode(step["b64"])) for step in seq]
        finally:
            rx.close()


def comparable(o: dict) -> dict:
    return {k: v for k, v in o.items() if k not in ("unsupported", "sent_kinds", "raised_detail")}


def label(o: dict) -> str:
    if o["raised"]:
        return f"raised {o['raised']}"
    if o["delivered"]:
        return "delivered-to-waiter"
    if o["unsupported"]:
        return "Error{UNSUPPORTED}"
    if o["handshake"] == "failed":
        return "handshake failed"
    parts = [o["action"] or "?"]
    if o["violations"]:
        parts.append("violations " + ",".join(o["violations"]))
    parts.append("sent " + (",".join(o["sent_kinds"]) or "nothing"))
    return "; ".join(parts)


def faulted(o: dict, fault_id: "str | None") -> bool:
    if o["handshake"] == "failed":
        return True
    named = fault_id is None or repr(fault_id) in (o.get("raised_detail") or "") or f'"{fault_id}"' in (o.get("raised_detail") or "")
    return bool(o["raised"]) and named and not o["unsupported"] and not o["delivered"]


def judge(case) -> "tuple[str, str]":
    """(verdict, what happened): verdict is pass, fail or unstated."""
    exp = case["expected"]
    if "sequence" in case and exp in ("deduplicated", "session-faulted"):
        got = run_sequence(case, case["sequence"])
        what = " | ".join(label(o) for o in got)
        first, second = got
        if exp == "deduplicated":
            ok = (not second["sent"] and not second["delivered"] and not second["violations"] and not second["raised"]
                  and second["action"] != first["action"])
        else:
            ok = faulted(second, case.get("fault_id"))
        return ("pass" if ok else "fail"), what
    if "sequence" in case:
        base = run_sequence(case, case["baseline_sequence"])
        got = run_sequence(case, case["sequence"])
        what = " | ".join(label(o) for o in got)
        same = [comparable(a) == comparable(b) for a, b in zip(got, base)]
        if exp == "UNSTATED":
            return "unstated", ("as baseline: " if all(same) else "differs from baseline: ") + what
        if exp == "ignored":
            if all(same):
                return "pass", what
            return "fail", f"{what}  (baseline: {' | '.join(label(o) for o in base)})"
        raise SystemExit(f"{case['case_id']}: no rule for a paired {exp!r}")

    got = run_one(case, case["envelope_b64"])
    what = label(got)
    if exp == "UNSTATED":
        return "unstated", what
    if exp == "unsupported":
        hm = decode(case["envelope_b64"])
        ok = got["unsupported"] == [hm.id] and len(got["sent"]) == 1 and not got["delivered"] and not got["raised"]
        return ("pass" if ok else "fail"), what
    if exp == "diagnostic-only":
        ok = not got["sent"] and not got["delivered"] and not got["raised"]
        return ("pass" if ok else "fail"), what
    if exp == "session-faulted":
        fault_id = case.get("fault_id")
        if fault_id is None and case["injection"].startswith("id-collision:"):
            fault_id = decode(case["envelope_b64"]).id
        return ("pass" if faulted(got, fault_id) else "fail"), what
    if exp == "session-continues":
        hm = decode(case["envelope_b64"])
        ready = min(hm.hello.protocol_minor, _server.PROTOCOL_MINOR)
        ok = (got["handshake"] == "ok" and got.get("host_protocol") == [hm.hello.protocol_major, hm.hello.protocol_minor]
              and got.get("ready_minor") == ready and not got["raised"])
        return ("pass" if ok else "fail"), (what + f"; handshake {got['handshake']}, host protocol "
                                            f"{got.get('host_protocol')}, Ready minor {got.get('ready_minor')}")
    base = run_one(case, case["baseline_b64"])
    if exp == "ignored":
        if comparable(got) == comparable(base):
            return "pass", what
        return "fail", f"{what}  (baseline: {label(base)})"
    if exp == "item-refused":
        new = set(got["violations"]) - set(base["violations"])
        if case["class"] == "HOST_REQUEST":
            hm = decode(case["envelope_b64"])
            answered = got["unsupported"] == [hm.id] and len(got["sent"]) == 1
        else:
            answered = not got["sent"]
        if answered and not new and not got["raised"] and not got["delivered"]:
            return "pass", what
        return "fail", f"{what}  (baseline: {label(base)})"
    if exp == "not-a-violation":
        new = set(got["violations"]) - set(base["violations"])
        raised = got["raised"] and got["raised"] != base["raised"]
        if not new and not raised:
            return "pass", what
        return "fail", f"{what}  (baseline: {label(base)})"
    raise SystemExit(f"{case['case_id']}: unknown expected outcome {exp!r}")


def known_entry(known, case):
    for i, k in enumerate(known):
        sel = k["match"]
        if all(
            (case.get(f) or "").startswith(v[:-1]) if isinstance(v, str) and v.endswith("*") else case.get(f) == v
            for f, v in sel.items()
        ):
            return i
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", type=Path, default=HERE / "cases")
    ap.add_argument("--known", type=Path, default=HERE / "known.json")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every unstated outcome")
    a = ap.parse_args()

    known = json.loads(a.known.read_text())["known"]
    cases = [json.loads(line) for line in gzip.decompress((a.cases / "manifest.jsonl.gz").read_bytes()).splitlines()]
    tally = collections.defaultdict(collections.Counter)
    known_hits = collections.Counter()
    failures, unstated = [], collections.defaultdict(collections.Counter)
    skipped = collections.Counter()
    sink = io.StringIO()  # the SDK's diagnostics go to stderr by design (§12)
    for case in cases:
        if case["direction"] != "host->plugin" or case["receiver"] != "plugin":
            skipped[case["receiver"]] += 1
            continue
        inj = case["injection"].split(":")[0]
        with contextlib.redirect_stderr(sink):
            verdict, what = judge(case)
        if verdict == "fail":
            k = known_entry(known, case)
            if k is not None:
                known_hits[k] += 1
                verdict = "known"
            else:
                failures.append((case, what))
        if verdict == "unstated":
            unstated[case["injection"]][what] += 1
        tally[inj][verdict] += 1

    print(f"{sum(sum(v.values()) for v in tally.values())} host->plugin cases run; "
          f"{sum(skipped.values())} not run here (receiver: {dict(skipped)})")
    print(f"{'injection':<22} {'pass':>6} {'known':>6} {'unstated':>9} {'fail':>6}")
    for inj in sorted(tally):
        t = tally[inj]
        print(f"{inj:<22} {t['pass']:>6} {t['known']:>6} {t['unstated']:>9} {t['fail']:>6}")
    print("\nunstated outcomes (reported, never failed):")
    for inj in sorted(unstated):
        outcomes = unstated[inj]
        shown = outcomes.most_common() if a.verbose else outcomes.most_common(3)
        print(f"  {inj}: {sum(outcomes.values())} cases")
        for what, n in shown:
            print(f"    {n:>4}  {what}")
    if known:
        print("\nknown divergences:")
        for i, k in enumerate(known):
            print(f"  {known_hits[i]:>4}  {k['reason']}")
    stale = [k for i, k in enumerate(known) if not known_hits[i]]
    for k in stale:
        print(f"STALE known.json entry (matches no mismatch; remove it): {k['match']}")
    for case, what in failures:
        print(f"FAIL {case['case_id']} {case['family']} {case['path']} {case['injection']}: expected {case['expected']}, got {what}")
    if failures or stale:
        print(f"\n{len(failures)} unexpected mismatches, {len(stale)} stale known entries")
        return 1
    print("\nevery stated outcome matched or is listed in known.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
