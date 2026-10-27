"""``ContractViolation`` and the rate bound that makes it safe to send.

Several rules across the doorbell and the memory layer say a peer "reports it
over gRPC" and keeps the session.  ``shared_memory_implementation_details.md``
§2 is where that message is defined, because its rules are that layer's, and it
is emphatic that the bound is **a requirement rather than an optimization**:

    Its highest-rate producers are decided *per record*, ahead of any
    interaction state [...] At mouse rate that is ~1 kHz of control-plane
    messages each carrying a 112-byte record, **on the channel the session needs
    in order to recover**, which is exactly what ``plugin_principles.md`` 9's
    corollary forbids: communication of failure must not itself become a point
    of failure.

So this module is not a convenience wrapper.  A plugin that reports every
violation it detects has built the cascade, and one that reports the first and
drops the rest has hidden a fault that is still happening.

**`ContractViolation` is SYMMETRIC since 1.0**, and this module is written for
either direction.  Three of its four memory-layer sites -- a ``D`` bit naming an
Owned region, an impossible Pulsed state word, a failed release CAS -- are
equally detectable in a *plugin's* traffic as in a host's, so both ends now
carry the arm, under the same rate bound and the same rule that a repeat is not
a new defect (``shared_memory_implementation_details.md`` §2,
``control_plane_protocol.md`` §8.12).  ``ViolationSite``'s ``E``-record members
stay plugin→host in practice, because ``E`` flows one way.  A plugin
*receiving* one keeps the session and surfaces it: it is a report about the
plugin's own traffic, and the arriving message is the diagnostic.

**It produces messages; it does not send them.**  The control plane sends
what its reporter hands back (`ControlPlane._report`, `report_due`), and the
host sends its own through the symmetric arm; this module owns the rate bound
and the aggregation both ends apply.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

__all__ = [
    "ContractViolation",
    "ViolationReporter",
    "SITES",
    "LOCAL_WHERE",
    "site_number",
    "is_wire_site",
]

#: ``ViolationSite`` in ``proto/vizi_plugin.proto``, name to wire number.
#:
#: **The numbers are the contract and the spellings are this SDK's key**: the
#: host switches on the enum, four SDKs implement these numbers, and everything
#: from 17 up was appended after the first sixteen for exactly that reason
#: rather than renumbered into place.  19 exists because §12 aggregates per
#: ``where`` under a budget a change of ``where`` does not reset: an
#: arrangement re-sent with different content sharing ``interaction_mismatch``
#: — an ``E``-record replay whose bytes changed — would merge two unrelated
#: defects into one counter and one retained exemplar.  Two SDKs reached for a
#: shared code before it existed.
#:
#: 20-23 name rules an SDK could otherwise enforce only with a *local*
#: spelling.  A local spelling is invisible to the host, so each would be a
#: rule the peer could break and never be told about.
#:
#: **24-26 are sites by the same argument.**  The test they were measured
#: against is §6.3's
#: deliberate silence, and it is a real test rather than a precedent: exhaustion
#: has no site BECAUSE the ceiling is 2^62 publish attempts, so the condition is
#: structural, unreachable and unrecoverable and a value for it could never be
#: sent.  All three of these are operational, reachable, and fixable by a host
#: that is told -- which is the other answer.
#:
#: A ``where`` outside this table is a *local* spelling (:data:`LOCAL_WHERE`)
#: and must not be sent as one of these.
SITES: dict[str, int] = {
    "epoch_mismatch": 1,
    "interaction_mismatch": 2,
    "missing_arrangement": 3,
    "zero_id": 4,
    "miss_with_picked": 5,
    "unknown_tool": 6,
    "owned_region_hint": 7,
    "impossible_state_word": 8,
    "invalid_published_length": 9,
    "release_cas_failed": 10,
    "wrong_producer_release": 11,
    "unknown_release": 12,
    "seq_on_open": 13,
    "position_tail": 14,
    "nonfinite_position": 15,
    "invalid_target": 16,
    "install_without_segment": 17,
    "release_region_mismatch": 18,
    "arrangement_content_mismatch": 19,
    "arrangement_regressed": 20,
    "repeated_field_overflow": 21,
    "live_geometry_not_inline": 22,
    "lane_subject_mismatch": 23,
    "uses_disagreement": 24,
    "missing_deadline": 25,
    "arrangement_first_revision": 26,
    "unusable_context": 27,
    # HOST -> plugin only: an overlay push the host refused, with the item and
    # the rule in `detail`.  In the table because the table IS the IDL
    # enum; this SDK receives it and never sends it.
    "overlay_item_refused": 28,
    # HOST -> plugin only, per panel-patch OP.
    "panel_patch_refused": 29,
    # HOST -> plugin only: a chunked overlay's chunk or table the host did not
    # apply, or an `OverlayUpdate` sent to a chunked overlay (§5.7, §8.5).
    "overlay_chunk_refused": 30,
    # A processing stage found a STAGED slot's header invalid
    # (processing_stages.md §7): handed back `failed`, reason 1.
    "stage_slot_invalid": 31,
    # A StageArm this plugin cannot use: no installed `S` ring, an undeclared
    # stage, or a pipeline/position out of range (processing_stages.md §4).
    "stage_arm_refused": 32,
    # HOST -> plugin only (§8.8): a shape proposal or answer out of step
    # with the edits the host began, or one whose positions it did not apply.
    "shape_out_of_step": 33,
    "shape_refused": 34,
}

#: Conditions this SDK reports to a developer that the wire enum has **no value
#: for**, so they are never sent as a ``ContractViolation``.
#:
#: - ``revision_exhausted``: ``shared_memory_protocol.md`` §6.3 requires the
#:   writer to stop and report, and no ``ViolationSite`` exists for it.  It is
#:   not corruption and must not be mislabeled ``impossible_state_word``.
#: - ``quarantined``: a region isolated by an *earlier* violation, raised on
#:   every subsequent call.  Reporting it would turn §2's "report once" into
#:   once per attempt, which is the cascade the aggregation exists to prevent.
#:
#: ``uses_disagreement`` was here and is **not** any more: it was reported as a
#: finding, measured against ``revision_exhausted`` above, and came back as
#: site 24.  That is what the distinction is for — the question to ask of a
#: silent rule is whether the condition is structural and unreachable (stay
#: local) or operational and fixable by a host that is told (earn a site).
LOCAL_WHERE: frozenset[str] = frozenset({"revision_exhausted", "quarantined"})


def site_number(where: str) -> int | None:
    """The wire enum value for ``where``, or ``None`` if it has none."""
    return SITES.get(where)


def is_wire_site(where: str) -> bool:
    """Whether ``where`` is a ``ViolationSite`` and may travel as one."""
    return where in SITES


#: ``shared_memory_implementation_details.md`` §2.  Neither is a wire constant:
#: the document requires *a* bound and leaves its size to the implementation.
DEFAULT_INTERVAL = 1.0
#: Reports per second, **replenishing**.  §2: the budget is a rate and not a
#: lifetime total -- a session that spent a total early would go permanently
#: silent about a category found late, which is the developer-findable surface
#: deleting itself.
DEFAULT_RATE = 10.0


@dataclass(frozen=True)
class ContractViolation:
    """One report, in the shape §2 defines.

    It is **not** a ``DoorbellFault``: that one is sent immediately before
    closing the doorbell and ends the session, and this one explicitly does not
    (``doorbell_protocol.md`` §8 -- framing faults tear down, semantic
    disagreements report and drop).
    """

    #: Which rule, as a stable enum.  The values are fixed in
    #: ``shared_memory_implementation_details.md`` §2 and
    #: ``doorbell_E_input.md`` §2; an SDK does not invent one.
    where: str
    #: Human-readable, for the Plugin Manager.  Never parsed.
    detail: str
    #: The offending 112-byte doorbell record, when there was one.  Carried on
    #: the first occurrence of a ``where`` and not on the aggregated repeats,
    #: because the record is what identifies a *particular* failure and a count
    #: is about a category.
    record: bytes | None = None
    #: Occurrences of this ``where`` since the last report, at least 1.  **The
    #: host must not read a repeat as a new defect**: this is the number of
    #: records affected, not the number of faults.
    count: int = 1


@dataclass
class _Pending:
    #: Occurrences waiting for a repeat report.
    count: int = 0
    detail: str = ""
    last_sent: float = 0.0
    #: A first report the rate has not let out yet.  It keeps its record and its
    #: ``count = 1``: §2 says the first occurrence of a ``where`` carries the
    #: record, and a delay does not change which occurrence that was.
    first: ContractViolation | None = None
    seen: bool = False


class ViolationReporter:
    """Aggregates violations into a bounded stream of reports.

    The rule, verbatim from §2:

        First occurrence of a ``where`` carries the record and ``count = 1``.
        After that, occurrences are counted per ``where`` and re-reported on a
        bounded interval, under a per-session report budget that a change of
        ``where`` does not reset.

    **The budget is what makes it a bound**, and the reason is worth keeping in
    view: re-reporting whenever ``where`` changes is bypassed by two violations
    alternating, since each record changes the category and so each record
    reports, and the 1 kHz cascade returns intact.

    Thread-safe: the doorbell's listener thread and the memory layer both feed
    one of these.
    """

    def __init__(
        self,
        *,
        interval: float = DEFAULT_INTERVAL,
        rate: float = DEFAULT_RATE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval <= 0:
            raise ValueError("the re-report interval must be a positive number of seconds")
        if rate <= 0:
            raise ValueError("the report rate must be positive")
        self.interval = interval
        self.rate = rate
        self._clock = clock
        self._allowance = 1.0
        self._last_refill = clock()
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._spent = 0
        self._seen: dict[str, int] = {}

    # -- what the budget did ------------------------------------------------

    @property
    def spent(self) -> int:
        """Reports emitted this session.  Not a cap -- see :attr:`rate`."""
        with self._lock:
            return self._spent

    @property
    def pending(self) -> int:
        """Occurrences observed and not yet reported.

        Under a rate bound nothing is lost, so this is a backlog rather than a
        casualty count -- but a backlog that never falls is worth surfacing: it
        says violations are arriving faster than the session may describe them.
        """
        with self._lock:
            return sum(s.count + (1 if s.first is not None else 0) for s in self._pending.values())

    def counts(self) -> dict[str, int]:
        """Every ``where`` seen this session and how often, reported or not."""
        with self._lock:
            return dict(self._seen)

    # -- the two ways a violation gets out ----------------------------------

    def report_record(
        self,
        record: bytes | None,
        findings: "Iterable[tuple[str, str]]",
    ) -> list[ContractViolation]:
        """Report every ``(where, detail)`` one received record produced.

        **This is the reporting unit**, because §2 counts an occurrence as one
        ``(received record, where)`` pair: a single ``D`` naming three ``OWNED``
        regions is one occurrence and not three, since one record was affected.
        Several *different* rules on one record each count once.  A detector that
        finds several regions per record must batch them here, or it cannot count
        correctly at all.
        """
        seen: dict[str, str] = {}
        for where, detail in findings:
            seen.setdefault(where, detail)
        return [
            v
            for where, detail in seen.items()
            for v in (self.report(where, detail, record=record),)
            if v is not None
        ]

    def report(
        self,
        where: str,
        detail: str,
        *,
        record: bytes | None = None,
        exempt: bool = False,
    ) -> ContractViolation | None:
        """Record one occurrence, and return the message to send now, if any.

        ``None`` means it was folded into a count that :meth:`due` will emit --
        which is the ordinary case and not a failure.

        ``exempt`` is ``M`` with ``zero_id`` (§2), and it is the one violation
        that reports **once per affected grant** rather than being aggregated:
        the host needs the raw record to identify *which* outstanding grant to
        close out, and a count cannot name two of them -- the second would stay
        pending, enter missing-ack recovery, and escalate the session, which is
        the cascade the special handling exists to prevent.  It bypasses the
        budget for the same reason, and cannot recreate the flood because ``M``
        is session-rate.
        """
        with self._lock:
            self._seen[where] = self._seen.get(where, 0) + 1
            if exempt:
                self._spent += 1
                return ContractViolation(where, detail, record, 1)
            state = self._pending.setdefault(where, _Pending())
            if not state.seen and state.first is None:
                # First occurrence of this `where`: it carries the record, and
                # if the rate cannot let it out now it WAITS rather than being
                # dropped -- the rate delays reports, it does not refuse them.
                state.first = ContractViolation(where, detail, record, 1)
                return self._emit_first(state)
            state.count += 1
            state.detail = detail
            return None

    def _emit_first(self, state: _Pending) -> ContractViolation | None:
        if state.first is None or not self._afford():
            return None
        out, state.first = state.first, None
        state.seen = True
        state.last_sent = self._clock()
        return out

    def due(self) -> list[ContractViolation]:
        """Every report the rate and the interval now permit, and clear them.

        Call it from whatever loop owns the control-plane stream.  It never
        blocks.  **Nothing it cannot emit is discarded** -- a report the rate
        defers stays pending and comes out on a later pass, which is what makes
        the bound a rate rather than a loss.
        """
        now = self._clock()
        out: list[ContractViolation] = []
        with self._lock:
            for where, state in self._pending.items():
                first = self._emit_first(state)
                if first is not None:
                    out.append(first)
                    continue
                if state.first is not None or state.count == 0:
                    continue
                if now - state.last_sent < self.interval or not self._afford():
                    continue
                # No record on a repeat: it is a count of a category, and the
                # first report already carried the one record that names a
                # particular failure.
                out.append(ContractViolation(where, state.detail, None, state.count))
                state.count = 0
                state.last_sent = now
        return out

    def next_due(self) -> "float | None":
        """Seconds until :meth:`due` could let a waiting report out, or
        ``None`` when nothing waits.  A report deferred on a
        session that then goes quiet is a loss unless something asks again
        at this deadline -- ``due`` is only called when traffic arrives."""
        now = self._clock()
        with self._lock:
            allowance = min(1.0, self._allowance + (now - self._last_refill) * self.rate)
            token = 0.0 if allowance >= 1.0 else (1.0 - allowance) / self.rate
            soonest = None
            for state in self._pending.values():
                if state.first is not None:
                    wait = token
                elif state.count:
                    wait = max(token, state.last_sent + self.interval - now)
                else:
                    continue
                soonest = wait if soonest is None else min(soonest, wait)
            return None if soonest is None else max(0.0, soonest)

    def drain(self) -> list[ContractViolation]:
        """Everything the rate permits right now, regardless of the interval.

        For session teardown, where the next interval will never arrive.  The
        rate still applies -- a flood at the end of a session is still a flood --
        so a caller with a lot pending calls this more than once, and
        :attr:`pending` says whether anything is left.
        """
        out: list[ContractViolation] = []
        with self._lock:
            for where, state in self._pending.items():
                first = self._emit_first(state)
                if first is not None:
                    out.append(first)
                    continue
                if state.first is not None or state.count == 0 or not self._afford():
                    continue
                out.append(ContractViolation(where, state.detail, None, state.count))
                state.count = 0
        return out

    def _afford(self) -> bool:
        """One token from a replenishing session-wide allowance (§2).

        A change of ``where`` does not reset it -- that is what stops two
        violations alternating from reporting per record -- and it has no
        lifetime cap, so a category first seen late in a long session can still
        be reported.  Charges only on success, and a refusal defers rather than
        drops: the caller sees the backlog in :attr:`pending`.
        """
        now = self._clock()
        self._allowance = min(1.0, self._allowance + (now - self._last_refill) * self.rate)
        self._last_refill = now
        if self._allowance < 1.0:
            return False
        self._allowance -= 1.0
        self._spent += 1
        return True
