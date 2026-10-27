"""Protocol-1.0 shared-memory layer, plugin side.

Written from ``docs/shared_memory_protocol.md``, ``docs/doorbell_M_memory.md``
and ``docs/control_plane_protocol.md`` §8.12 alone, as the completeness probe
those documents ask for: what this file had to guess at is the finding, and the
guesses are recorded in ``docs/shared_memory_implementation_details.md``.

**It maps memory and nothing else.**  It sends no gRPC and reads no doorbell:
the supervisor hands it the two halves of a grant as data and it returns the
*content* of the acknowledgement, leaving the sending to the layer that owns a
stream.  That boundary is ``shared_memory_protocol.md`` §9a's, and keeping it
means this module is testable without a host, a socket or a plugin process.

Stdlib only, except for Pulsed, whose state word needs real process-shared
atomics (``_shared_atomic``, resolved at first use).  Constant, Owned and
Loaned regions need none and work without that dependency.
"""

from __future__ import annotations

import enum
import mmap
import os
import struct
import threading
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

__all__ = [
    "Discipline",
    "Producer",
    "GrantFailure",
    "SegmentState",
    "Region",
    "PaneRef",
    "SegmentShortfall",
    "ReleaseRequest",
    "OwnedRead",
    "Consumed",
    "stamp_pane",
    "PANE_HEADER_BYTES",
    "Installed",
    "Failed",
    "Released",
    "StateReport",
    "Quarantined",
    "UsesExceeded",
    "Retiring",
    "Hint",
    "SegmentStore",
    "Segment",
    "PulsedReader",
    "PulsedWriter",
    "LoanedArena",
    "DepositRing",
    "ReadSchedule",
    "PURPOSES",
    "PURPOSE_READER",
    "PURPOSE_VOLUME",
    "PURPOSE_OVERLAY",
    "PURPOSE_PANEL",
    "PURPOSE_EXPORT",
    "NO_PURPOSES",
    "WHERE_IMPOSSIBLE_STATE_WORD",
    "WHERE_INVALID_PUBLISHED_LENGTH",
    "WHERE_RELEASE_CAS_FAILED",
    "WHERE_UNKNOWN_RELEASE",
    "WHERE_WRONG_PRODUCER_RELEASE",
    "WHERE_OWNED_REGION_HINT",
]

#: ``doorbell_M_memory.md`` §5.  Offsets are from the start of the MAPPING.
PREAMBLE_BYTES = 32
_PREAMBLE = struct.Struct("<QI16sI")  # segment_id, padding, proof, crc
IDENTITY_PROOF_BYTES = 16

#: ``doorbell_M_memory.md`` §4.  ``PLUGIN_READ`` is required on every grant.
PERM_PLUGIN_READ = 0x01
PERM_PLUGIN_WRITE = 0x02
PERM_HOST_READ = 0x04
PERM_HOST_WRITE = 0x08
_PERM_KNOWN = PERM_PLUGIN_READ | PERM_PLUGIN_WRITE | PERM_HOST_READ | PERM_HOST_WRITE

#: ``shared_memory_protocol.md`` §6.2.
_PULSED_HEADER_BYTES = 16
_WRITE_CLAIM = 1 << 62
_READ_CLAIM = 1 << 63
_REVISION_MASK = _WRITE_CLAIM - 1
_MAX_REVISION = _REVISION_MASK - 1

#: §6.4 step 5 and §6.5 step 3.  Both give up after 8; both are coalescing.
_CLAIM_ATTEMPTS = 8

#: §7.  Reservations pad up to this; the release key is the requested length.
_LOANED_ALIGNMENT = 64

#: ``shared_memory_implementation_details.md`` §2, "The `where` values".  These
#: are what a ``ContractViolation`` carries and what the host switches on, so
#: they are the document's spellings and not this module's.
WHERE_IMPOSSIBLE_STATE_WORD = "impossible_state_word"
WHERE_INVALID_PUBLISHED_LENGTH = "invalid_published_length"
WHERE_RELEASE_CAS_FAILED = "release_cas_failed"
WHERE_UNKNOWN_RELEASE = "unknown_release"
WHERE_WRONG_PRODUCER_RELEASE = "wrong_producer_release"
#: ``control_plane_protocol.md`` §8.12: ``region`` is redundant with ``offset``
#: **on purpose**, and this is the check that redundancy exists for -- an offset
#: that does not fall inside the region it names is mistargeted, and is
#: *detected* rather than applied.  It is NOT :data:`WHERE_UNKNOWN_RELEASE`,
#: which means no reservation at all.
WHERE_RELEASE_REGION_MISMATCH = "release_region_mismatch"
WHERE_OWNED_REGION_HINT = "owned_region_hint"

#: ``shared_memory_protocol.md`` §3's five purposes, which is what this SDK
#: implements.  A receiver's recognized set **is** the set it implements: there
#: is no published vocabulary, nothing to configure and no negotiation, because
#: a receiver's ability to honor a grant is a property of its code and not of a
#: list it was handed.  A host adding a purpose adds it to §3, and an SDK that
#: does not know it fails those grants ``unsupported`` and keeps the session.
#:
#: **The producer is the load-bearing half**, and ``G`` against ``V`` is why:
#: both carry volume voxels and they run in OPPOSITE directions, so a receiver
#: that reads the two as one gets the release message backwards -- which §3.1
#: makes a ``ContractViolation``.
#:
#: =========  ==========  ====================================================
#: purpose    produced    the segment is
#: =========  ==========  ====================================================
#: ``G``      plugin      the reader's deposit: the plugin writes the volume
#:                        it decoded and the host consumes it
#: ``V``      host        volume data the plugin asked for (``ReadDataVolume``)
#: ``O``      both        overlay: plugin-published Pulsed live lanes beside
#:                        the plugin's bulk arena
#: ``P``      plugin      panel: the plugin's arena for widget images
#: ``X``      host        a sealed export segment the host writes
#: ``S``      host, then  a processing stage's ring of STAGED slots, one
#:            each stage  mapping granted to every stage in a chain
#:                        (``processing_stages.md`` §4)
#: =========  ==========  ====================================================
PURPOSES: frozenset[str] = frozenset({"G", "V", "O", "P", "X", "S"})

PURPOSE_READER, PURPOSE_VOLUME = "G", "V"
PURPOSE_OVERLAY, PURPOSE_PANEL, PURPOSE_EXPORT = "O", "P", "X"
PURPOSE_STAGE = "S"

#: §3: the purposes whose grant carries a ``subject``, and the division is
#: between a **binding** and an **exchange**.  An overlay's or a panel's
#: segment IS a contribution's -- it outlives any one message and is the only
#: thing that says which overlay a lane belongs to.  A reader's deposit ring, a
#: ``ReadDataVolume`` reply and an export segment each serve an exchange that
#: already carries its own identity, and a second name for something already
#: named is the failure §8 avoids by making ``Region.id`` mean nothing.
#: A stage's ring is an exchange: every ``StageArm`` names it by segment id, and
#: a plugin serving several stages of one chain holds it once.
_SUBJECT_PURPOSES: frozenset[str] = frozenset({PURPOSE_OVERLAY, PURPOSE_PANEL})

#: ``processing_stages.md`` §4 and §5.1: at most 512 slots, because a ``D`` mask
#: names at most 512 regions; each slot at a multiple of 64 so its turn word is
#: 8-aligned for every implementation's atomics; and room for the fixed header.
STAGED_MAX_SLOTS = 512
STAGED_ALIGN = 64
STAGED_FIXED_HEADER_BYTES = 256

#: An empty set, for a test that wants a receiver recognizing nothing.  It was
#: once the *only* legal argument's opposite -- §3 required an SDK with no
#: configured set to refuse to start, for a set it could not have acted on --
#: and that rule is deleted.
NO_PURPOSES: frozenset[str] = frozenset()


def stamp_pane(payload: bytes, epoch_offset: int, pane: "PaneRef") -> bytes:
    """Write §2's header into ``payload`` at ``epoch_offset``.

    The pair is **part of the deposit the producer composes**, not a field
    beside it (§2: "the location is declared once and the value changes per
    publication"), so this is a helper for composing one rather than a
    protocol step: it returns the bytes to reserve.

    **LOANED only.**  A PULSED lane's header is not in its payload at all --
    §3.2 puts it in the last eight bytes of the *slot*, outside the published
    length -- and :meth:`PulsedWriter.publish` writes it there itself.  The two
    disciplines differ because the unit does: a deposit's length is not known
    until it arrives, so its header travels inside it; a slot's is fixed by the
    grant, so its header can have a fixed home that never moves the payload.
    """
    if not 0 <= pane.view_id <= 0xFF:
        raise ValueError(f"view_id {pane.view_id} does not fit the u8 the doorbell carries")
    if epoch_offset < 0 or epoch_offset + PANE_HEADER_BYTES > len(payload):
        raise ValueError(
            f"a {len(payload)}-byte payload cannot carry a pane header at {epoch_offset}"
        )
    out = bytearray(payload)
    _PANE_HEADER.pack_into(out, epoch_offset, pane.view_id, pane.epoch)
    return bytes(out)


class Discipline(enum.Enum):
    CONSTANT = 1
    OWNED = 2
    PULSED = 3
    LOANED = 4
    STAGED = 5


class Producer(enum.Enum):
    """Which end FILLS a region (``shared_memory_protocol.md`` §3.1).

    There is no ``UNSPECIFIED`` member on purpose: a caller decoding the wire
    turns a zero into a grant failure, and a value that cannot be constructed
    cannot be defaulted into by accident.
    """

    HOST = 1
    PLUGIN = 2


class GrantFailure(enum.Enum):
    """``doorbell_M_memory.md`` §7.  ``RETRYABLE`` is a CLOSED list."""

    RETRYABLE = "retryable"
    TOO_LARGE = "too_large"
    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class SegmentState(enum.Enum):
    """``shared_memory_protocol.md`` §8, plus the retired terminal of §9."""

    NOT_PRESENT = "not_present"
    PENDING = "pending"
    INSTALLED = "installed"
    FAILED = "failed"
    RELEASED = "released"


@dataclass(frozen=True)
class Region:
    index: int
    offset: int
    extent: int
    discipline: Discipline
    producer: Producer
    buffers: int = 0
    slot_bytes: int = 0
    id: str = ""
    #: §2: where this region's 8-byte ``(view_id, epoch)`` header sits, counted
    #: from the **unit of independent ownership** -- each *slot* for PULSED,
    #: each *deposit* for LOANED, the *region* for CONSTANT and OWNED.
    #:
    #: ``None`` is **absent**, which is how a region says it is not
    #: pane-associated; the field is optional rather than 0-means-none because
    #: 0 is a legal offset.  ``epoch_offset + 8`` must fit -- within
    #: ``slot_bytes`` for PULSED, within the deposit for LOANED, within
    #: ``extent`` otherwise -- and a grant that violates it is ``malformed``,
    #: on the same footing as the ``offset >= 32`` floor.
    epoch_offset: int | None = None


#: §2: the header is ``u32 view_id`` then ``u32 epoch``, little-endian.
PANE_HEADER_BYTES = 8
#: The ``slot_bytes`` vizi's host grants for a plugin-published live lane (an
#: ``O`` segment's PULSED region).  HOST policy, shared here so both ends
#: spell one number: the SDK
#: never assumes it -- :class:`PulsedWriter` reads ``slot_bytes`` off the
#: grant it was given, and a host that grants another size is simply obeyed.
DEFAULT_LIVE_SLOT_BYTES = 64 * 1024
_PANE_HEADER = struct.Struct("<II")
#: §3.2: what a PULSED writer leaves in a pane-associated slot's header when
#: the publication carries no pair.  Not a legal header -- ``epoch`` is never 0
#: (``control_plane_protocol.md`` §8.10) -- which is exactly why it can mean
#: "cleared" without colliding with one.
_CLEARED_PANE = bytes(PANE_HEADER_BYTES)


@dataclass(frozen=True)
class PaneRef:
    """§2's embedded ``(view_id, epoch)``, read out of a payload.

    ``view_id`` is a ``u8`` on the doorbell's ``D`` and ``E`` records and widens
    to ``u32`` here for alignment, so a header whose high three bytes are
    non-zero is rejected rather than truncated -- see :meth:`Segment.pane`.
    """

    view_id: int
    epoch: int


def _decode_pane(raw: bytes) -> PaneRef | None:
    """§2's header, or ``None`` when it is one this document rejects.

    **What "rejects" costs is the PAIR and not the payload.**  §2 says a
    receiver rejects a header whose ``view_id`` does not fit the ``u8`` the
    doorbell carries, and says nothing further: there is no ``ViolationSite``
    for it, and the bytes it sits in are otherwise valid.  So the pair is
    treated as absent and counted (:attr:`PulsedReader.pane_rejected` and
    :attr:`DepositRing.pane_rejected`), which is the narrowest reading that
    still refuses to believe it.

    **Epoch 0 is refused for the same reason.**  ``control_plane_protocol.md``
    §8.10 fixes a pane epoch as *"never 0, monotonic per view_id"*, so a header
    carrying 0 names a state no pane can be in.  Believing it would hand a
    reader a pair that compares older than every epoch it could have
    acknowledged -- stale by accident rather than by evidence, which is the
    kind of agreement that stops being one the moment the rule changes.
    """
    view_id, epoch = _PANE_HEADER.unpack(raw)
    if view_id > 0xFF or epoch == 0:
        return None
    return PaneRef(view_id, epoch)


@dataclass(frozen=True)
class Installed:
    """A success acknowledgement.  ``proof`` was read OUT OF the mapping."""

    segment_id: int
    identity_proof: bytes


@dataclass(frozen=True)
class Failed:
    segment_id: int
    failure_class: GrantFailure
    detail: str


@dataclass(frozen=True)
class SegmentShortfall:
    """``control_plane_protocol.md`` §5.6, §8.12: the arena came up short, and
    **by how much**.

    It is *not* a request to grow one in place -- an arena changes size by
    being retired and re-granted at a new ``segment_id`` -- it is what tells
    the host a re-grant is wanted and how large it has to be.  Naming the
    amount is the whole point: a host watching release pressure learns *that* a
    plugin is short and never by how much, so it could only grow by doubling.

    **Coalescing** (§5.4), keyed on ``(segment_id, region)`` and merged in
    place: the merge keeps the **largest** ``wanted_bytes``, because a re-grant
    clearing the biggest shortfall clears the smaller ones, and **sums**
    ``dropped``, because §12 wants the count of lost transitions and a replace
    would discard exactly the number this message exists to carry.
    """

    segment_id: int
    region: int
    #: The allocation that failed, **padded to §7's 64-byte alignment** -- the
    #: bytes the allocator actually needed, since a re-grant of the unpadded
    #: length can still fail by up to 63 bytes.  Never 0.
    wanted_bytes: int
    #: Largest contiguous free run at the time, so the host can tell exhaustion
    #: from fragmentation; the two want different re-grants.
    largest_free_bytes: int
    #: Pushes dropped for want of space since this key was last reported, >= 1.
    dropped: int = 1


@dataclass(frozen=True)
class ReleaseRequest:
    """§9.6: *"I have no further use for this."*  A HINT, and nothing more.

    Sending it does not enter *retiring* and changes no obligation: every
    discipline is honored exactly as before until ``RetireSegment`` actually
    arrives, and a descriptor into the segment arriving afterwards is read
    normally rather than rejected.  The host may retire immediately, later, or
    never, and none of those is a fault -- so there is no deadline, no
    escalation, and nothing to retry.
    """

    segment_id: int
    #: Diagnostic only.  §9.6: the host does not act on it, because acting on
    #: it would make a plugin-supplied, unvalidatable string load-bearing for
    #: host memory policy.
    reason: str = ""


@dataclass(frozen=True)
class Released:
    """``shared_memory_protocol.md`` §9.4: the answer to either half arriving for
    a segment that has already been released.  It is a *fourth* answer, not a
    variety of silence -- a supervisor that returns ``None`` here sends nothing,
    where the specification requires ``SegmentReleased`` again."""

    segment_id: int


@dataclass(frozen=True)
class StateReport:
    """The answer to ``GrantStateQuery`` (``doorbell_M_memory.md`` §7)."""

    segment_id: int
    state: SegmentState
    have_capability: bool | None = None
    have_install: bool | None = None
    identity_proof: bytes | None = None
    failure_class: GrantFailure | None = None
    detail: str | None = None


class UsesExceeded(ValueError):
    """A message would name a reservation more times than the sender declared.

    §5.6: a reservation's ``uses`` is the sender's to state and the receiver
    releases at that count, so an undeclared second message -- a push then a
    compile of one item built with the default ``uses=1`` -- releases the
    bytes under the message still in flight, on every timing.  This is the
    sender's own accounting, the mirror of the host's ``uses_disagreement``:
    the excess message is refused before it leaves, whichever order the
    messages are sent in, and the fix is the count on the builder.
    """


class Quarantined(Exception):
    """A region proved corrupt or a peer proved broken.

    **The region is already isolated when this is raised**, except where
    :attr:`isolated` says otherwise.  §6.2 says "report once and quarantine the
    region", and both halves are this layer's: leaving the isolation to the
    caller means a caller that retries gets the same exception forever, and
    "report once" becomes a report per attempt -- the cascade
    ``ContractViolation``'s aggregation exists to prevent.

    What is left to the caller is the report: one ``ContractViolation`` carrying
    :attr:`where`, and every other region kept working (C2).
    """

    def __init__(self, region: int, where: str, detail: str, *, isolated: bool = True) -> None:
        super().__init__(f"region {region}: {where}: {detail}")
        self.region = region
        self.where = where
        self.detail = detail
        #: False only for revision exhaustion (§6.3), where the WRITER stops and
        #: the region's already-published values stay legible to its reader.
        self.isolated = isolated


class Retiring(RuntimeError):
    """``RetireSegment`` arrived: this operation would START something.

    §9.1 permits finishing and forbids starting, and the table it says that with
    is about *operations* -- so this is raised where the operation is, not only
    where the handle was obtained.  A handle fetched before the retirement keeps
    working for what it had already begun and refuses anything new.
    """

    def __init__(self, segment_id: int, what: str) -> None:
        super().__init__(f"segment {segment_id} is retiring; {what} would start new use")
        self.segment_id = segment_id
        self.what = what


class Consumed(RuntimeError):
    """An OWNED handoff was read a second time (§5).

    The read **is** the handoff, so re-reading is not safe: by the time a
    second read happens the other end may own the range again and be writing
    it.  A handle that refuses is the only way an SDK can make that rule
    checkable, since ownership is not discoverable from the bytes.
    """

    def __init__(self, region: int) -> None:
        super().__init__(f"region {region}: this OWNED handoff was already read")
        self.region = region


class _GrantError(Exception):
    """Internal: a validation failure carrying the class it maps to."""

    def __init__(self, failure_class: GrantFailure, detail: str) -> None:
        super().__init__(detail)
        self.failure_class = failure_class
        self.detail = detail


# --------------------------------------------------------------------------
# Disciplines
# --------------------------------------------------------------------------


class _PulsedBase:
    def __init__(self, segment: "Segment", region: Region) -> None:
        if region.discipline is not Discipline.PULSED:
            raise ValueError(f"region {region.index} is not PULSED")
        self._seg = segment
        self._r = region
        self._n = region.buffers
        self._s = region.slot_bytes
        self._payload0 = region.offset + self._n * _PULSED_HEADER_BYTES
        #: §3.2: the pane header occupies the LAST eight bytes of every slot,
        #: so the payload always begins at slot byte 0 and the only thing a
        #: header changes is the maximum.  §6.5 step 4 bounds ``length`` by
        #: this rather than by ``slot_bytes``.
        self._capacity = self._s - PANE_HEADER_BYTES if region.epoch_offset is not None else self._s
        #: §2 headers this endpoint refused: a ``view_id`` past the ``u8`` the
        #: doorbell carries, or an ``epoch`` of 0 beside a ``view_id`` that was
        #: written.  Counted rather than raised -- the payload is still valid
        #: and the pair is what is missing.
        self.pane_rejected = 0

    def _pane_offset(self, i: int) -> int:
        """Where slot ``i``'s §2 header sits in the mapping.

        ``epoch_offset`` is slot-relative and §3.2 fixes it at
        ``slot_bytes - 8``; this reads the declared field rather than assuming
        the value, which is what the same section asks of a receiver.
        """
        assert self._r.epoch_offset is not None
        return self._payload_offset(i) + self._r.epoch_offset

    def _pane_at(self, raw: bytes) -> "PaneRef | None":
        """§2's pair from one slot's eight header bytes, or ``None``.

        **An all-zero header is a publication with no pair, not a rejected
        one.**  Slots are reused, so a writer publishing without a pane clears
        the header rather than leaving its predecessor's -- otherwise a stale
        pair would be read as this revision's, which is the one thing §2's
        "the value changes per publication" forbids.  Zero is the cleared
        state and does not count against :attr:`pane_rejected`; a header that
        was *written* and is still invalid does.
        """
        if raw == _CLEARED_PANE:
            return None
        pane = _decode_pane(raw)
        if pane is None:
            self.pane_rejected += 1
        return pane

    def _state_offset(self, i: int) -> int:
        return self._r.offset + i * _PULSED_HEADER_BYTES

    def _length_offset(self, i: int) -> int:
        return self._state_offset(i) + 8

    def _payload_offset(self, i: int) -> int:
        return self._payload0 + i * self._s

    def _load_states(self) -> list[int]:
        """§6.5 step 1 / §6.4 step 2: acquire-load all N, reject the impossible."""
        atomics = self._seg._atomics()
        words = [atomics.load(self._state_offset(i)) for i in range(self._n)]
        for word in words:
            self._check_state_word(word)
        return words

    def _check_state_word(self, word: int) -> None:
        """§6.2's two impossible states.  Never clears the word (C2), and
        isolates the region before raising, so "report once" can be once."""
        revision = word & _REVISION_MASK
        write = bool(word & _WRITE_CLAIM)
        read = bool(word & _READ_CLAIM)
        if write and read:
            raise self._seg._violate(
                self._r.index, WHERE_IMPOSSIBLE_STATE_WORD, "both claim flags set"
            )
        if revision == 0 and (write or read):
            raise self._seg._violate(
                self._r.index, WHERE_IMPOSSIBLE_STATE_WORD, "revision 0 with a claim flag"
            )


class PulsedWriter(_PulsedBase):
    """§6.4.  One writer per region; the revision is this object's."""

    def __init__(self, segment: "Segment", region: Region) -> None:
        super().__init__(segment, region)
        self._revision = 0
        self._lock = threading.Lock()
        self.dropped = 0  #: coalesced publishes (§6.4 step 5) -- not errors

    def publish(self, payload: bytes, *, pane: "PaneRef | None" = None) -> bool:
        """Publish one value.  ``False`` means coalesced, which is not failure.

        ``pane`` publishes §2's ``(view_id, epoch)`` header into the last eight
        bytes of the claimed slot (§3.2) -- the pair belongs to the *revision*,
        which is why it is an argument here and not state on the writer.
        Passing one for a region that declares no ``epoch_offset`` is a caller
        error, not a silent no-op; omitting one on a lane that declares one
        clears the header rather than leaving the previous publication's.

        **The payload never contains the header.**  It begins at slot byte 0
        and is bounded by §3.2's capacity, which is why a pane-associated lane
        takes eight fewer payload bytes than its ``slot_bytes`` rather than
        asking the caller to leave room.
        """
        if pane is not None:
            if self._r.epoch_offset is None:
                raise ValueError(
                    f"region {self._r.index} declares no epoch_offset; it is not pane-associated"
                )
            if not 0 <= pane.view_id <= 0xFF:
                raise ValueError(f"view_id {pane.view_id} does not fit the u8 the doorbell carries")
        if not 0 < len(payload) <= self._capacity:
            raise ValueError(f"payload must be 1..{self._capacity} bytes, got {len(payload)}")
        # §9.1: publishing a new value is STARTING, and the table is about
        # operations -- so the gate is here and not only where this object was
        # handed out, which a caller holds across a retirement.
        self._seg._guard(self._r.index, "publish")
        with self._lock:
            if self._revision >= _MAX_REVISION:
                # §6.3: the space is not wrapped.  A wrapped revision would make
                # an old buffer look newer than a new one, and `> last_seen` is
                # the whole of the reader's ordering.  The WRITER stops; the
                # region is not isolated, since what it already published stays
                # legible to its reader.
                raise Quarantined(
                    self._r.index,
                    "revision_exhausted",
                    "revision space exhausted",
                    isolated=False,
                )
            self._revision += 1
            revision = self._revision
            atomics = self._seg._atomics()
            for _ in range(_CLAIM_ATTEMPTS):
                words = self._load_states()
                claimed = self._try_claim(atomics, words, revision)
                if claimed is not None:
                    self._seg._claimed(1)
                    try:
                        self._write_claimed(atomics, claimed, revision, payload, pane)
                    finally:
                        self._seg._claimed(-1)
                    return True
            # §6.4 step 5: drop and return.  Coalescing, not an error and not a
            # stall (C3) -- the newest READY is untouched and still readable.
            self.dropped += 1
            return False

    def _candidates(self, words: list[int], revision: int) -> list[int]:
        """§6.4 step 3, in order: EMPTY in rotation, then READY oldest-first
        EXCLUDING the newest -- which is what keeps a durable value visible to a
        reader that has not arrived yet, and the reason N >= 3."""
        start = revision % self._n
        empty = [(start + k) % self._n for k in range(self._n) if words[(start + k) % self._n] == 0]
        ready = [i for i, w in enumerate(words) if w != 0 and not w & (_WRITE_CLAIM | _READ_CLAIM)]
        ready.sort(key=lambda i: words[i] & _REVISION_MASK)
        return empty + ready[:-1]

    def _try_claim(self, atomics, words: list[int], revision: int) -> int | None:
        for i in self._candidates(words, revision):
            ok, _ = atomics.compare_exchange(
                self._state_offset(i), words[i], revision | _WRITE_CLAIM
            )
            if ok:
                return i
        return None

    def _write_claimed(
        self, atomics, i: int, revision: int, payload: bytes, pane: "PaneRef | None"
    ) -> None:
        """§6.4 step 6: payload, then length, then the RELEASE store that publishes.

        The §2 header goes with the payload and before the length, because both
        are slot bytes the release store publishes together -- and it is
        written on EVERY publication into a pane-associated lane, cleared when
        there is no pair, so no revision can inherit its predecessor's.
        """
        held = revision | _WRITE_CLAIM
        try:
            self._seg._write_at(self._payload_offset(i), payload)
            if self._r.epoch_offset is not None:
                self._seg._write_at(
                    self._pane_offset(i),
                    _CLEARED_PANE if pane is None else _PANE_HEADER.pack(pane.view_id, pane.epoch),
                )
            self._seg._write_at(self._length_offset(i), struct.pack("<Q", len(payload)))
        except BaseException:
            # §6.4 step 7.  A failed release CAS is fatal to the region (C2).
            ok, _ = atomics.compare_exchange(self._state_offset(i), held, 0)
            if not ok:
                raise self._seg._violate(
                    self._r.index,
                    WHERE_RELEASE_CAS_FAILED,
                    "writer release found a foreign value",
                ) from None
            raise
        atomics.store(self._state_offset(i), revision)


class PulsedReader(_PulsedBase):
    """§6.5.  Consume-once: a successful read empties the buffer."""

    def __init__(self, segment: "Segment", region: Region) -> None:
        super().__init__(segment, region)
        self.last_seen = 0
        #: §2's pair for the value :meth:`read` last returned, or ``None`` when
        #: the region declares no ``epoch_offset``.  It travels **with** the
        #: revision -- a PULSED slot's header cannot be consulted without
        #: claiming the slot, so this is the only moment it is legible, and it
        #: is not the "durable copy" a CONSTANT or OWNED region offers.
        self.last_pane: PaneRef | None = None
        self._lock = threading.Lock()

    def read(self) -> bytes | None:
        """Return the newest unseen value, or ``None`` when there is nothing new."""
        # §9.1: claiming a buffer or re-reading a lane is STARTING.
        self._seg._guard(self._r.index, "read")
        with self._lock:
            atomics = self._seg._atomics()
            for _ in range(_CLAIM_ATTEMPTS):
                words = self._load_states()
                i = self._newest_unseen(words)
                if i is None:
                    return None  # §6.5 step 2: nothing new.
                revision = words[i] & _REVISION_MASK
                ok, _ = atomics.compare_exchange(
                    self._state_offset(i), revision, revision | _READ_CLAIM
                )
                if ok:
                    self._seg._claimed(1)
                    try:
                        return self._consume(atomics, i, revision)
                    finally:
                        self._seg._claimed(-1)
            # §6.5 step 3: a writer is churning, and the next scan will find a
            # newer value anyway.
            return None

    def _newest_unseen(self, words: list[int]) -> int | None:
        best: int | None = None
        for i, w in enumerate(words):
            if w == 0 or w & (_WRITE_CLAIM | _READ_CLAIM):
                continue
            rev = w & _REVISION_MASK
            if rev > self.last_seen and (best is None or rev > words[best] & _REVISION_MASK):
                best = i
        return best

    def _consume(self, atomics, i: int, revision: int) -> bytes:
        held = revision | _READ_CLAIM
        (length,) = struct.unpack("<Q", self._seg._read_at(self._length_offset(i), 8))
        payload_end = self._payload_offset(i) + length
        if not 0 < length <= self._capacity or payload_end > self._seg.size:
            # §6.5 step 4: the bound is the payload's CAPACITY (§3.2), which is
            # eight less than the slot on a pane-associated lane -- a length
            # that reached into the header would read a pair as payload.
            # Release by EXACT CAS first, then quarantine, so a corrupt length
            # does not also leak the claim.
            atomics.compare_exchange(self._state_offset(i), held, 0)
            raise self._seg._violate(
                self._r.index, WHERE_INVALID_PUBLISHED_LENGTH, f"length {length} out of range"
            )
        # §6.5 step 5: from slot byte 0 either way, which is why §3.2 puts the
        # header at the end.  The header is read here rather than from the copy
        # because it is no longer inside it -- and only under the claim, since
        # the release consumes the slot.
        data = self._seg._read_at(self._payload_offset(i), length)
        pane = (
            None
            if self._r.epoch_offset is None
            else self._pane_at(self._seg._read_at(self._pane_offset(i), PANE_HEADER_BYTES))
        )
        ok, _ = atomics.compare_exchange(self._state_offset(i), held, 0)
        if not ok:
            raise self._seg._violate(
                self._r.index, WHERE_RELEASE_CAS_FAILED, "reader release found a foreign value"
            )
        self.last_seen = revision
        self.last_pane = pane
        return data


class LoanedArena:
    """§7, plugin-produced: the plugin reserves and writes, the host releases.

    The allocator is ARENA-scoped rather than per-writer (§5.6), and an arena is
    one plugin-produced ``LOANED`` region: every writer into this region shares
    this cursor, so no two of them can be handed the same byte.  A cursor per
    sender is the shipping defect this replaces; a cursor per *segment* would be
    the opposite error, allocating across a region boundary.
    """

    def __init__(self, segment: "Segment", region: Region) -> None:
        if region.discipline is not Discipline.LOANED:
            raise ValueError(f"region {region.index} is not LOANED")
        if region.producer is not Producer.PLUGIN:
            raise ValueError(
                f"region {region.index} is host-produced; the host allocates and "
                "the plugin releases with ReleaseDeposit"
            )
        self._seg = segment
        self._r = region
        self._lock = threading.Lock()
        self._cursor = 0
        self._outstanding: dict[tuple[int, int], int] = {}
        #: (offset, len) -> (declared uses, messages that have named it).  The
        #: sender's own §5.6 ledger: the count it told the host, and how far
        #: it has got.
        self._uses: dict[tuple[int, int], list[int]] = {}
        #: Reservations not yet carried by every message naming them: each
        #: holds one of the segment's in-flight claims (§9.1).
        self._in_flight: set[tuple[int, int]] = set()
        self.refused = 0  #: pushes that could not allocate (§5.6) -- counted, never blocked
        #: Reservations left outstanding because a later message naming them
        #: was never admitted while an earlier one was (§5.6): the host holds
        #: a use that will not come, and the bytes wait for the session's end.
        self.stranded = 0
        self._shortfall: SegmentShortfall | None = None

    @property
    def outstanding(self) -> int:
        return len(self._outstanding)

    @property
    def largest_free(self) -> int:
        """The largest contiguous run this arena could still hand out.

        A bump cursor that rewinds only when nothing is outstanding has exactly
        one such run, which is what makes this answerable at all -- and it is
        the number ``SegmentShortfall`` carries so a host can tell exhaustion
        from fragmentation.
        """
        with self._lock:
            return self._r.extent - self._cursor

    @property
    def shortfall(self) -> SegmentShortfall | None:
        """The pending, merged ``SegmentShortfall`` for this arena, if any.

        One arena is one ``(segment_id, region)``, so §5.4's coalescing key is
        this object: there is at most one pending item, merged in place.
        """
        with self._lock:
            return self._shortfall

    def take_shortfall(self) -> SegmentShortfall | None:
        """Drain the pending shortfall for sending.  ``None`` when there is none.

        **It produces the message and does not send it**, exactly as
        :mod:`vizi_plugin_sdk.violations` does: the supervisor that owns a
        stream is where it goes, and until one exists this is the
        developer-findable surface for "pushes are being dropped".
        """
        with self._lock:
            pending, self._shortfall = self._shortfall, None
            return pending

    def _record_shortfall(self, wanted: int) -> None:
        """§5.4's merge: largest ``wanted_bytes``, summed ``dropped``.  Called
        under ``self._lock``."""
        pending = self._shortfall
        free = self._r.extent - self._cursor
        self._shortfall = SegmentShortfall(
            self._seg.segment_id,
            self._r.index,
            wanted if pending is None else max(pending.wanted_bytes, wanted),
            free,
            1 if pending is None else pending.dropped + 1,
        )

    def reserve(
        self, payload: bytes, *, pane: "PaneRef | None" = None, uses: int = 1
    ) -> tuple[int, int] | None:
        """Write ``payload`` into the arena and return its wire ``(offset, len)``.

        **The offset returned is MAPPING-relative** (§7): it is what a
        ``ReleaseLoan`` carries and what the descriptor handed to the peer says.
        The ring's own modular address stays region-relative inside this object,
        and the conversion happens here, at the allocator's boundary.

        ``None`` means the arena is full.  The caller FAILS ITS PUSH and counts
        it: it does not block and it does not reuse (§5.6).  There is no growth
        path in this direction -- the host grants, and growth does not swap with
        the roles -- so a refusal also merges a :class:`SegmentShortfall` for
        :meth:`take_shortfall`, which is how the host learns *how much* is
        missing rather than only that something is.

        ``pane`` stamps §2's header into the payload at this region's
        ``epoch_offset``.  A LOANED header sits in each **deposit** rather than
        once per region, so it is an argument to the reservation.
        """
        # §9.1: reserving a deposit is STARTING, and the write and the message
        # that names it are then FINISHING, which a retirement waits for.
        # Counted before `retiring` is read, as `hold` does, so
        # either this sees the retirement or `begin_retire` sees the write.
        # The claim is dropped when every message naming the reservation has
        # been sent (`sent`), or the reservation is released or reclaimed.
        self._seg._guard_quarantine(self._r.index)
        self._seg._claimed(1)
        if self._seg._retiring:
            self._seg._claimed(-1)
            raise Retiring(self._seg.segment_id, "reserve")
        try:
            return self._reserve_claimed(payload, pane, uses)
        except BaseException:
            self._seg._claimed(-1)
            raise

    def _reserve_claimed(self, payload: bytes, pane, uses: int) -> "tuple[int, int] | None":
        if pane is not None:
            if self._r.epoch_offset is None:
                raise ValueError(
                    f"region {self._r.index} declares no epoch_offset; it is not pane-associated"
                )
            payload = stamp_pane(payload, self._r.epoch_offset, pane)
        want = len(payload)
        padded = (want + _LOANED_ALIGNMENT - 1) // _LOANED_ALIGNMENT * _LOANED_ALIGNMENT
        with self._lock:
            if self._cursor + padded > self._r.extent:
                self.refused += 1
                # §5.6: the push FAILS and is counted -- and the count reaches
                # the host as `SegmentShortfall`, which is what turns "a plugin
                # is short" into "a plugin is short by this much".
                self._record_shortfall(padded)
                self._seg._claimed(-1)  # nothing was reserved, so nothing is in flight
                return None
            offset = self._cursor
            self._cursor += padded
            self._seg._write_at(self._r.offset + offset, payload)
            self._outstanding[(offset, want)] = padded
            self._uses[(offset, want)] = [max(int(uses), 1), 0]
            self._in_flight.add((offset, want))
            return self._r.offset + offset, want

    def name(self, offset: int, length: int) -> None:
        """A message naming this reservation is about to be sent: count it,
        and refuse the one past the declared ``uses`` (§5.6) -- before it
        leaves, in whichever order the messages come.  A reservation this
        arena does not hold is not its business and passes."""
        with self._lock:
            key = (offset - self._r.offset, length)
            uses = self._uses.get(key)
            if uses is None:
                return
            declared, named = uses
            if named >= declared:
                raise UsesExceeded(
                    f"reservation ({offset}, {length}) was declared for {declared} use(s) "
                    f"and this is message {named + 1} naming it; build the item with "
                    f"uses={named + 1} (§5.6)"
                )
            uses[1] = named + 1

    def sent(self, offset: int, length: int) -> None:
        """A message naming this reservation has been sent.  Once every use
        declared for it has gone, it is no longer in flight, and a retirement
        waiting on it may proceed."""
        with self._lock:
            key = (offset - self._r.offset, length)
            uses = self._uses.get(key)
            done = uses is None or uses[1] >= uses[0]
            landed = done and key in self._in_flight
            if landed:
                self._in_flight.discard(key)
        if landed:
            self._seg._claimed(-1)

    def unname(self, offset: int, length: int) -> None:
        """The message :meth:`name` counted was never sent.  If no message has
        named the reservation, nothing holds it and the bytes come back
        (:meth:`reclaim`); if one has, the host holds a use that will not
        arrive, and the reservation stays outstanding rather than being
        freed under the message in flight.  Counted
        in :attr:`stranded`; the bytes return at the session's end."""
        with self._lock:
            key = (offset - self._r.offset, length)
            uses = self._uses.get(key)
            if uses is None:
                return
            if uses[1] > 0:
                uses[1] -= 1
            if uses[1] == 0:
                self._release_locked(offset, length)
            else:
                self.stranded += 1
                # No further message will name it, so it is no longer in
                # flight: its claim must not hold a retirement for good.
                # The bytes stay lent to the host, as before.
                if key in self._in_flight:
                    self._in_flight.discard(key)
                    self._seg._claimed(-1)

    def reclaim(self, offset: int, length: int) -> None:
        """Take back a reservation whose message was never sent.

        The bookkeeping is :meth:`release`'s and the meaning is not: there is
        no peer that lost track of the arena, because no peer was ever told.
        A push that reserves and then fails to be admitted -- a full queue, a
        builder that raised, §5.6's duplicate refusal -- owns bytes nothing will
        ever release, and the arena is permanently short by that much.

        **Separate from :meth:`release` on purpose.**  That one applies an
        incoming ``ReleaseLoan`` and reports a range it does not recognize as a
        ``ContractViolation`` against the host; reaching for it here would file
        this end's own abandoned push against the peer.  And it is for a
        reservation NO sent message names: one an earlier message already
        carried is the host's to release, however the later one fared
        (:meth:`unname` decides).
        """
        with self._lock:
            self._release_locked(offset, length)

    def release(self, offset: int, length: int) -> None:
        """Apply a ``ReleaseLoan``.  ``offset`` is MAPPING-relative, as the wire
        carries it, and the key is the REQUESTED length (§7)."""
        with self._lock:
            self._release_locked(offset, length)

    def _release_locked(self, offset: int, length: int) -> None:
        inside = offset - self._r.offset
        if inside < 0 or length <= 0 or inside + length > self._r.extent:
            # `control_plane_protocol.md` §8.12: the `region` field is
            # redundant with `offset` so that a mistargeting is DETECTED
            # rather than applied, and this is that cross-check.  A sender
            # that forgot the conversion lands outside the named region --
            # which is exactly why §7 chose this direction.
            raise self._seg._violate(
                self._r.index,
                WHERE_RELEASE_REGION_MISMATCH,
                f"ReleaseLoan ({offset}, {length}) falls outside region "
                f"{self._r.index}, which is [{self._r.offset}, "
                f"{self._r.offset + self._r.extent})",
            )
        padded = self._outstanding.pop((inside, length), None)
        self._uses.pop((inside, length), None)
        if (inside, length) in self._in_flight:
            # Released or reclaimed before every naming message went: no
            # longer in flight either way.  Outside the claim lock's order
            # concerns -- `_claimed` takes only its own lock.
            self._in_flight.discard((inside, length))
            self._seg._claimed(-1)
        if padded is None:
            # §3.1's own rule, and NOT `release_cas_failed`: that one means
            # proven interference with a state word.  Reporting a range
            # error under it would tell the host memory was concurrently
            # modified when nothing of the kind happened.
            raise self._seg._violate(
                self._r.index,
                WHERE_UNKNOWN_RELEASE,
                f"ReleaseLoan names no outstanding reservation ({offset}, {length})",
            )
        # Reclamation is FIFO (§7): the cursor only rewinds when nothing is
        # outstanding, which is the honest form of "the tail advances while
        # the front record is released" for an arena with no ring order.
        if not self._outstanding:
            self._cursor = 0


# --------------------------------------------------------------------------
# One segment
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Hint:
    """What a ``D`` record's mask means once the region table is applied.

    ``doorbell_D_notification.md`` §3: the discipline check is **this layer's**,
    because the doorbell has no way to know a discipline and must not pretend to.
    So the transport hands up a segment id and a set of region indices, and this
    is the answer -- which of them are worth looking at, which are a rule
    the sender broke, and which are merely counted.
    """

    segment_id: int
    #: Pulsed regions to re-check NOW.  `D` accelerates these and nothing else.
    scan: tuple[int, ...] = ()
    #: ``(where, region)`` to report as a ``ContractViolation``.  The hint is
    #: ignored either way and the session continues (§5).
    violations: tuple[tuple[str, int], ...] = ()
    #: ``(reason, region)`` counted locally and not reported.  A region of -1
    #: means the reason is about the whole record.
    ignored: tuple[tuple[str, int], ...] = ()
    #: STAGED slots whose turn word to load now (``processing_stages.md``
    #: §6.2).  Separate from ``scan`` because a different consumer acts on it:
    #: the stage runner, not the read schedule.
    turns: tuple[int, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.scan or self.turns)


class DepositRing:
    """§7, HOST-produced: the host reserves and writes, the plugin consumes.

    The plugin's obligation is the mirror of :class:`LoanedArena`'s -- it owes
    ``ReleaseDeposit{segment_id, region, offset, len}`` for every deposit it
    took, keyed by the **requested** length and never the padded one, and
    reclamation is FIFO so sitting on one old deposit blocks everything behind
    it (§7, §9a step 11).

    Reading is safe to repeat: a Loaned region re-checks safely, so taking the
    same deposit twice is not an error and does not owe a second release.

    **A reservation is released ONCE, when every declared use has finished**
    (``control_plane_protocol.md`` §5.6).  ``SegmentRef.uses`` is the sender's
    count of the messages that will name one reservation, and the receiver
    cannot derive it: two messages naming the same bytes travel on paths with
    different drain priority, so a consumer that released when its own count of
    *started* uses hit zero would release before a use still in flight arrived.
    So :meth:`take` records the declared count, :meth:`dispose` decrements it on
    **every** disposition -- applied, superseded, rejected alike -- and the
    release is owed at zero.
    """

    def __init__(self, segment: "Segment", region: Region) -> None:
        if region.discipline is not Discipline.LOANED:
            raise ValueError(f"region {region.index} is not LOANED")
        if region.producer is not Producer.HOST:
            raise ValueError(f"region {region.index} is plugin-produced; the plugin allocates it")
        self._seg = segment
        self._r = region
        self._lock = threading.Lock()
        #: ``(offset, len)`` -> declared uses not yet disposed of.
        self._owed: dict[tuple[int, int], int] = {}
        #: §2's pair for the deposit :meth:`take` last returned, or ``None``.
        self.last_pane: PaneRef | None = None
        #: §2 headers refused: a ``view_id`` past the doorbell's ``u8``, or a
        #: deposit too short to reach the region's declared ``epoch_offset``.
        self.pane_rejected = 0
        #: §5.6: descriptors that re-declared a reservation's ``uses`` as a
        #: different number.  Reported as ``uses_disagreement`` (site 24) --
        #: this SDK counted it locally, reported the missing site as a finding,
        #: and the site came back.
        self.uses_disagreements = 0
        self._disagreement: str | None = None

    @property
    def outstanding(self) -> int:
        return len(self._owed)

    @property
    def owed(self) -> tuple[tuple[int, int], ...]:
        """Every ``(offset, len)`` a ``ReleaseDeposit`` is still owed for."""
        with self._lock:
            return tuple(self._owed)

    @property
    def due(self) -> tuple[tuple[int, int], ...]:
        """Reservations whose every declared use is disposed of: **send these**.

        §5.6's rows all decrement, and ``ReleaseDeposit`` goes out when the
        count reaches zero.  For the ordinary ``uses = 1`` case a disposition
        and the release are the same event, which is why a plugin that never
        calls :meth:`dispose` still behaves correctly.
        """
        with self._lock:
            return tuple(k for k, remaining in self._owed.items() if remaining == 0)

    def take(self, offset: int, length: int, *, uses: int = 1, held: bool = False) -> bytes:
        """Copy one deposit out, and record that its release is owed.

        ``offset`` is **MAPPING-relative** (§7) -- the number the descriptor
        carried, which is also the number the ``ReleaseDeposit`` echoes back.
        Finishing a read already begun is permitted while retiring; beginning
        one is not, which is why this goes through the same gate as every other
        start (§9.1).

        ``uses`` is the descriptor's ``SegmentRef.uses``, **declared by the
        sender**: 0 and 1 both mean one, so a peer that never sets it is
        unchanged.  Re-taking a reservation does not re-register it -- a second
        message naming the same bytes is one of the uses already declared.

        **`uses` is a property of the RESERVATION, so every message naming one
        carries the same count** (``control_plane_protocol.md`` §5.6 at `1.0`),
        and a sender that disagrees with itself is non-conforming.  The count
        FIRST given is the one applied, and the disagreement is counted
        (:attr:`uses_disagreements`).  Two SDKs met this and split -- one
        erroring, one keeping the larger -- and neither was safe to
        standardize on: erroring discards a descriptor over a sender's
        arithmetic, and keeping the larger silently accepts that the sender
        does not know its own set, which is the one thing ``uses`` exists to
        assert.  This SDK kept the larger until `1.0` settled it.

        ``held``: the caller holds one of the segment's in-flight claims for
        this read -- the reply naming the deposit has arrived, so the read is
        FINISHING, which a retirement permits and waits for.
        """
        if held:
            self._seg._guard_quarantine(self._r.index)
        else:
            self._seg._guard(self._r.index, "take")
        inside = offset - self._r.offset
        if inside < 0 or length <= 0 or inside + length > self._r.extent:
            raise ValueError(
                f"deposit [{offset}, {offset + length}) escapes region "
                f"{self._r.index}, which is [{self._r.offset}, "
                f"{self._r.offset + self._r.extent})"
            )
        data = self._seg._read_at(offset, length)
        pane = self._pane_of(data)
        with self._lock:
            key = (offset, length)
            declared = max(1, uses)
            if key in self._owed:
                if self._owed[key] != declared:
                    self.uses_disagreements += 1
                    self._disagreement = (
                        f"deposit {offset}+{length} was declared with uses="
                        f"{self._owed[key]} and re-declared with uses={declared}; "
                        f"applying {self._owed[key]}"
                    )
            else:
                self._owed[key] = declared
            self.last_pane = pane
        return data

    def take_disagreement(self) -> "str | None":
        """The `uses` disagreement :meth:`take` last saw, once (§5.6, site 24).

        Drained rather than pushed, like :meth:`LoanedArena.take_shortfall`: the
        memory layer holds no reporter, because §11's rate bound and its outbox
        are the control plane's and a second reporter down here would be a
        second budget.  The supervisor that owns both asks after each take.
        """
        out, self._disagreement = self._disagreement, None
        return out

    def _pane_of(self, payload: bytes) -> "PaneRef | None":
        """§2: a LOANED region's header sits in each DEPOSIT, because deposits
        are independently lived and can outlast an epoch -- one region-level
        header would retroactively restate the meaning of bytes in flight."""
        offset = self._r.epoch_offset
        if offset is None:
            return None
        if offset + PANE_HEADER_BYTES > len(payload):
            self.pane_rejected += 1
            return None
        pane = _decode_pane(payload[offset : offset + PANE_HEADER_BYTES])
        if pane is None:
            self.pane_rejected += 1
        return pane

    def dispose(self, offset: int, length: int) -> bool:
        """One disposition of a reservation (§5.6).  ``True`` when the release
        is now owed -- i.e. this was the last declared use.

        **Every** disposition counts, not only "copied out": applied,
        superseded on a coalescing queue, and rejected all release the bytes,
        because the receiver is done with them whatever it did with them.
        """
        with self._lock:
            remaining = self._owed.get((offset, length), _MISSING)
            if remaining is _MISSING:
                raise ValueError(
                    f"deposit ({offset}, {length}) was never taken, so there is "
                    "nothing to dispose of"
                )
            if remaining == 0:
                return True  # already due; a repeat is not a second release
            remaining -= 1
            self._owed[(offset, length)] = remaining
            return remaining == 0

    def settle(self, offset: int, length: int) -> bool:
        """Clear a deposit's record because its ``ReleaseDeposit`` went out.

        :meth:`released` is the version a plugin driving this ring by hand
        calls, and it enforces the two rules that catch a plugin's own mistakes
        -- a release naming no reservation, and a release owed at a count above
        zero.  This one is for a **supervisor that has already done the
        counting**: :class:`~vizi_plugin_sdk.control.ControlPlane` decrements
        §5.6's declared uses itself, because it must also count the dispositions
        of a reply nobody took, which never reach this ring at all.  So neither
        rule can be checked here without contradicting the caller that is
        authoritative for both.

        ``False`` means nothing was taken from these bytes, which is the
        ordinary abandoned-reply case and not an error.
        """
        with self._lock:
            return self._owed.pop((offset, length), _MISSING) is not _MISSING

    def released(self, offset: int, length: int) -> None:
        """Record that ``ReleaseDeposit`` was sent, keyed as the wire carries it.

        Sending one while a declared use is still outstanding is refused here
        rather than on the wire: the sender would then be free to overwrite
        bytes the second message names, which is the corruption ``uses`` exists
        to prevent (§5.6).  With the ordinary ``uses = 1`` this settles the one
        use, so "copy it out, then release" needs no :meth:`dispose` call.
        """
        with self._lock:
            remaining = self._owed.get((offset, length), _MISSING)
            if remaining is _MISSING:
                # §3.1, and the same rule the other direction states: a release
                # naming no outstanding reservation means one of the two has
                # lost track of the arena.
                raise self._seg._violate(
                    self._r.index,
                    WHERE_UNKNOWN_RELEASE,
                    f"ReleaseDeposit names no deposit this plugin took ({offset}, {length})",
                )
            if remaining > 1:
                raise ValueError(
                    f"deposit ({offset}, {length}) has {remaining} declared uses still "
                    "outstanding; release is owed only at zero (§5.6)"
                )
            del self._owed[(offset, length)]


class OwnedRead:
    """One authorised OWNED handoff, readable exactly once (§5).

    Constructed only by :meth:`Segment.accept_owned`, which is where the rule
    about *when* it may be constructed lives.
    """

    def __init__(self, segment: "Segment", region: Region, offset: int, length: int) -> None:
        self._seg = segment
        self._r = region
        self._offset = offset
        self._length = length
        self._consumed = False
        self._lock = threading.Lock()

    @property
    def consumed(self) -> bool:
        return self._consumed

    def read(self) -> bytes:
        """Copy the range out.  A second call raises :class:`Consumed`.

        Permitted while retiring: §9.1 lets a read already begun **finish**,
        and an authorisation the control plane already gave is exactly that.
        Starting a new one is what :meth:`Segment.accept_owned` refuses.
        """
        with self._lock:
            if self._consumed:
                raise Consumed(self._r.index)
            if self._seg.quarantined(self._r.index):
                raise Quarantined(
                    self._r.index, "quarantined", "region was isolated by an earlier violation"
                )
            data = self._seg._read_at(self._r.offset + self._offset, self._length)
            self._consumed = True
            return data


class ReadSchedule:
    """The unconditional re-check schedule ``doorbell_D_notification.md`` §5
    requires an SDK to provide.

        Every actively consumed Pulsed region is re-checked within a documented
        finite bound, independently of ``D`` and of any other traffic.

    That bound is :attr:`bound`, and it exists because ``D`` is advisory: a
    plugin that reads a lane only when a hint arrives has made the hint
    load-bearing and will sit on a stale value forever the moment a writer stops
    -- there is no further hint coming, and nothing is wrong.  :meth:`wake` is
    what a hint does, and all it can do is bring the next sweep forward.
    """

    def __init__(self, bound: float = 0.04, *, name: str = "vizi-read-schedule") -> None:
        if bound <= 0:
            raise ValueError("the re-check bound must be a positive number of seconds")
        self.bound = bound
        #: Readers dropped because their region was isolated (C2): the rest keep
        #: running, which is the whole point of quarantining one region.
        self.quarantined: list[Quarantined] = []
        #: The first exception from a consumer callback, after which the sweep
        #: stops.  A schedule that swallowed these would be a silent stall.
        self.failure: BaseException | None = None
        self._name = name
        self._consumers: list[tuple[PulsedReader, Callable[[bytes], None]]] = []
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._running = False
        self._due = False
        self._thread: threading.Thread | None = None

    def consume(self, reader: "PulsedReader", deliver: Callable[[bytes], None]) -> None:
        """Register one Pulsed region and where its values go."""
        with self._lock:
            self._consumers.append((reader, deliver))

    def wake(self) -> None:
        """A hint arrived: bring the next sweep forward.  Never required."""
        with self._wake:
            self._due = True
            self._wake.notify_all()

    def sweep(self) -> int:
        """One pass over every registered region.  Returns values delivered.

        Public because the schedule's *mechanism* is the SDK's choice (§5): a
        plugin with its own loop can drive this and never start the thread.
        """
        delivered = 0
        with self._lock:
            consumers = list(self._consumers)
        for reader, deliver in consumers:
            try:
                while True:
                    value = reader.read()
                    if value is None:
                        break
                    deliver(value)
                    delivered += 1
            except Quarantined as exc:
                self.quarantined.append(exc)
                self._drop(reader)
            except Retiring:
                # §9.1: the segment is retiring, so there is nothing further to
                # start here.  Dropping it is finishing, not abandoning.
                self._drop(reader)
        return delivered

    def _drop(self, reader: "PulsedReader") -> None:
        with self._lock:
            self._consumers = [c for c in self._consumers if c[0] is not reader]

    def start(self) -> "ReadSchedule":
        with self._lock:
            if self._running:
                return self
            self._running = True
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        with self._wake:
            self._running = False
            self._wake.notify_all()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    def __enter__(self) -> "ReadSchedule":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _run(self) -> None:
        while True:
            with self._wake:
                if not self._running:
                    return
                if not self._due:
                    self._wake.wait(self.bound)
                if not self._running:
                    return
                self._due = False
            try:
                self.sweep()
            except BaseException as exc:  # a consumer callback raised
                self.failure = exc
                with self._wake:
                    self._running = False
                return


_MISSING = object()


class Segment:
    """One mapped, validated grant.  Constructed only by :class:`SegmentStore`."""

    def __init__(
        self,
        segment_id: int,
        purpose: str,
        size: int,
        perms: int,
        mapping: "mmap.mmap",
        regions: dict[int, Region],
        identity_proof: bytes,
        subject: str = "",
    ) -> None:
        self.segment_id = segment_id
        self.purpose = purpose
        #: §3: WHICH contribution this segment serves, by its manifest id, and
        #: the key a plugin with two overlays tells its two `O` segments apart
        #: by.  Empty for `G`, `V` and `X`, whose exchange names itself.
        self.subject = subject
        self.size = size
        self.perms = perms
        self.regions = regions
        self.identity_proof = identity_proof
        self._mm = mapping
        self._shared = None
        self._shared_lock = threading.Lock()
        self._quarantined: set[int] = set()
        #: §2 headers this segment refused on a CONSTANT or OWNED region.
        self.pane_rejected = 0
        self._claims = 0
        self._claim_lock = threading.Lock()
        self._retiring = False
        #: Called when the last in-flight claim on a RETIRING segment ends, so
        #: the release §9.1 held back happens then rather than at the next
        #: unrelated message.  Set by the store; must not block.
        self._on_drained: "Callable[[], None] | None" = None
        self._arenas: dict[int, LoanedArena] = {}
        self._rings: dict[int, "DepositRing"] = {}
        self._readers: dict[int, PulsedReader] = {}
        self._writers: dict[int, PulsedWriter] = {}

    # -- state ------------------------------------------------------------

    @property
    def retiring(self) -> bool:
        """``RetireSegment`` arrived: finishing is permitted, starting is not."""
        return self._retiring

    def quarantined(self, index: int) -> bool:
        return index in self._quarantined

    def _require_usable(self, index: int, what: str) -> Region:
        region = self.regions.get(index)
        if region is None:
            raise KeyError(f"segment {self.segment_id} has no region {index}")
        self._guard(index, what)
        return region

    def _guard_quarantine(self, index: int) -> None:
        """§6.2 alone: the half of :meth:`_guard` a caller that counts its own
        claim before reading ``retiring`` still needs."""
        if index in self._quarantined:
            raise Quarantined(index, "quarantined", "region was isolated by an earlier violation")

    def _guard(self, index: int, what: str) -> None:
        """§9.1 and §6.2, checked where the OPERATION is.

        Handing out a lane or arena object goes through here, and so does every
        operation on one -- because a plugin fetches its handles at install and
        keeps them, so a gate only at the handout would enforce §9.1 for nobody.
        """
        if index in self._quarantined:
            # Not itself a reportable violation: the one that isolated the
            # region was already reported once, which is the point of isolating.
            raise Quarantined(index, "quarantined", "region was isolated by an earlier violation")
        if self._retiring:
            raise Retiring(self.segment_id, what)

    def _violate(self, index: int, where: str, detail: str) -> Quarantined:
        """Isolate, then hand back the exception for the caller to raise.

        §6.2's "report once and quarantine the region" is one act with two
        halves, and doing only the reporting half leaves a live region that
        raises identically on every subsequent call -- which turns "once" into
        once per attempt.
        """
        self._quarantined.add(index)
        return Quarantined(index, where, detail)

    def isolate(self, index: int) -> None:
        """Quarantine one region.  Every other region keeps working (C2)."""
        self._quarantined.add(index)

    def _arena_list(self) -> list[LoanedArena]:
        """Every arena handed out for this segment, for the store's drains."""
        return list(self._arenas.values())

    # -- raw access -------------------------------------------------------

    def _read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self.size:
            raise ValueError(f"read [{offset}, {offset + length}) escapes the segment")
        return self._mm[offset : offset + length]

    def _write_at(self, offset: int, data: bytes) -> None:
        if offset < 0 or offset + len(data) > self.size:
            raise ValueError(f"write [{offset}, {offset + len(data)}) escapes the segment")
        self._mm[offset : offset + len(data)] = data

    def _atomics(self):
        with self._shared_lock:
            if self._shared is None:
                from ._shared_atomic import SharedAtomicMapping

                self._shared = SharedAtomicMapping(self._mm)
            return self._shared

    # -- disciplines ------------------------------------------------------

    def constant(self, index: int) -> bytes:
        """A CONSTANT region: written before the grant, immutable, always safe."""
        region = self.regions.get(index)
        if region is None:
            raise KeyError(f"segment {self.segment_id} has no region {index}")
        if region.discipline is not Discipline.CONSTANT:
            raise ValueError(f"region {index} is not CONSTANT")
        if index in self._quarantined:
            raise Quarantined(index, "quarantined", "region was isolated")
        # §9.1: a CONSTANT region may be READ AGAIN while retiring, not merely
        # finished -- it has no claim, no release and no handoff, so forbidding
        # a second read would be a rule with no property behind it.
        # Deliberately NOT gated on `retiring`: re-reading a Constant region is
        # trivially safe and a read already begun must be able to finish (§9.1).
        return self._read_at(region.offset, region.extent)

    def pane(self, index: int) -> "PaneRef | None":
        """§2's ``(view_id, epoch)`` for a CONSTANT or OWNED region.

        These are the two disciplines whose header sits **in the region**, one
        lifetime and one header -- and the two for which the mapping really is
        the *durable copy* the pair is supposed to be, because reading it
        claims nothing and consumes nothing.  A PULSED pair travels with a
        revision (:attr:`PulsedReader.last_pane`) and a LOANED one with a
        deposit (:attr:`DepositRing.last_pane`); asking here for either would
        be reading a header whose owner is somebody else's claim.

        ``None`` means the region declares no ``epoch_offset``, which is §2's
        way of saying it is not pane-associated.
        """
        region = self.regions.get(index)
        if region is None:
            raise KeyError(f"segment {self.segment_id} has no region {index}")
        if region.discipline not in (Discipline.CONSTANT, Discipline.OWNED):
            raise ValueError(
                f"region {index} is {region.discipline.name}; its pane pair travels with each "
                f"{'slot' if region.discipline is Discipline.PULSED else 'deposit'} (§2)"
            )
        if index in self._quarantined:
            raise Quarantined(index, "quarantined", "region was isolated")
        if region.epoch_offset is None:
            return None
        raw = self._read_at(region.offset + region.epoch_offset, PANE_HEADER_BYTES)
        pane = _decode_pane(raw)
        if pane is None:
            self.pane_rejected += 1
        return pane

    def accept_owned(self, index: int, offset: int = 0, length: int | None = None) -> "OwnedRead":
        """§5's explicit authorisation boundary for an OWNED region.

        **Call this when the embedding control plane's own exchange actually
        hands the range over**, and never on a hint, a poll or a timer.  §5 is
        emphatic that this document does not define Owned's handoff and that an
        implementation must not invent one: it does not say which message
        transfers ownership, which transfers it back, what range a transfer
        covers, or how a replayed transfer is recognized, so **every
        application protocol using an OWNED region defines those four things
        for itself**.  What a generic SDK owes is this call and the
        consume-once handle it returns -- ownership is not discoverable from
        the region's bytes, and an SDK that polled one would be inventing the
        handoff that paragraph says is missing.

        ``offset`` is relative to the region, like :meth:`constant`, because no
        wire number is involved: the offsets §1 makes mapping-relative are the
        ones that travel, and this handoff has no defined message to travel in.
        """
        region = self._require_usable(index, "accept_owned")
        if region.discipline is not Discipline.OWNED:
            raise ValueError(f"region {index} is not OWNED")
        if length is None:
            length = region.extent - offset
        if offset < 0 or length <= 0 or offset + length > region.extent:
            raise ValueError(
                f"[{offset}, {offset + length}) escapes region {index}'s {region.extent} bytes"
            )
        return OwnedRead(self, region, offset, length)

    def pulsed_reader(self, index: int) -> PulsedReader:
        region = self._require_usable(index, "pulsed_reader")
        reader = self._readers.get(index)
        if reader is None:
            reader = self._readers[index] = PulsedReader(self, region)
        return reader

    def pulsed_writer(self, index: int) -> PulsedWriter:
        region = self._require_usable(index, "pulsed_writer")
        if region.producer is not Producer.PLUGIN:
            raise ValueError(f"region {index} is host-produced; the host publishes it")
        writer = self._writers.get(index)
        if writer is None:
            writer = self._writers[index] = PulsedWriter(self, region)
        return writer

    def loaned(self, index: int) -> LoanedArena:
        """The plugin-produced arena: the plugin reserves, the host releases.

        For a HOST-produced ring -- the deposit rings every shipping grant uses
        -- the plugin is the consumer, and :meth:`deposits` is its half.
        """
        region = self._require_usable(index, "loaned")
        arena = self._arenas.get(index)
        if arena is None:
            arena = self._arenas[index] = LoanedArena(self, region)
        return arena

    def resolve_deposit(self, offset: int, length: int) -> int:
        """Which region a mapping-relative descriptor lies in (§7, §8.12).

        ``SegmentRef`` carries no region, and ``ReleaseDeposit`` requires one --
        the redundancy §8.12 exists for is between the *release*'s two fields,
        so a receiver has to resolve the region from its own table before it
        can echo the offset back safely.  A range in no host-produced LOANED
        region is not a deposit this plugin can release, and saying so beats
        naming region 0 and having the host report a mismatch we caused.
        """
        for region in self.regions.values():
            if region.discipline is not Discipline.LOANED:
                continue
            inside = offset - region.offset
            if inside < 0 or length <= 0 or inside + length > region.extent:
                continue
            if region.producer is not Producer.HOST:
                raise ValueError(
                    f"[{offset}, {offset + length}) is in region {region.index}, which this "
                    "plugin produces; the host releases it with ReleaseLoan"
                )
            return region.index
        raise ValueError(
            f"[{offset}, {offset + length}) is in no host-produced LOANED region of "
            f"segment {self.segment_id}"
        )

    def deposits(self, index: int) -> "DepositRing":
        """The consumer half of a HOST-produced Loaned region (§7, §9a step 11).

        The host reserves and writes; the plugin reads and owes
        ``ReleaseDeposit`` for every deposit it took.  Reclamation is FIFO, so
        sitting on one old deposit blocks everything behind it.
        """
        self._require_usable(index, "deposits")
        return self._deposits_unchecked(index)

    def _deposits_unchecked(self, index: int) -> "DepositRing":
        """The ring, without §9.1's start-versus-finish gate.

        Only for applying a release the plugin already owes, which §9.1 permits
        while retiring -- and a ring that was never handed out can hold no
        deposit, so this creates one only in the case where it raises.
        """
        region = self.regions[index]
        if region.discipline is not Discipline.LOANED:
            raise ValueError(f"region {index} is not LOANED")
        if region.producer is Producer.PLUGIN:
            raise ValueError(
                f"region {index} is plugin-produced; the plugin allocates it with loaned() "
                "and the host releases with ReleaseLoan"
            )
        ring = self._rings.get(index)
        if ring is None:
            ring = self._rings[index] = DepositRing(self, region)
        return ring

    def settle_deposit(self, index: int, offset: int, length: int) -> bool:
        """:meth:`DepositRing.settle` for a region index -- the supervisor's
        form of :meth:`release_deposit`, for a release it has already counted.

        Permitted while retiring, for :meth:`release_deposit`'s reason.
        """
        region = self.regions.get(index)
        if region is None or region.producer is Producer.PLUGIN:
            return False
        return self._deposits_unchecked(index).settle(offset, length)

    def release_deposit(self, index: int, offset: int, length: int) -> None:
        """Record that ``ReleaseDeposit{segment, region, offset, len}`` was sent.

        Permitted while retiring: §9.1 lets a plugin send the releases it
        already owes, and §9.3 says ``SegmentReleased`` settles the rest.
        """
        region = self.regions.get(index)
        if region is None:
            raise KeyError(f"segment {self.segment_id} has no region {index}")
        if region.producer is Producer.PLUGIN:
            # §3.1: a release naming the wrong side.  A ReleaseDeposit for a
            # plugin-produced region asserts a role the region table denies.
            raise self._violate(
                index,
                WHERE_WRONG_PRODUCER_RELEASE,
                "ReleaseDeposit names a plugin-produced region, which the host "
                "releases with ReleaseLoan",
            )
        self._deposits_unchecked(index).released(offset, length)

    def release_loan(self, index: int, offset: int, length: int) -> None:
        """Apply an incoming ``ReleaseLoan``.

        Permitted while retiring: it is the host FINISHING with bytes it already
        holds, and refusing it would strand them.
        """
        region = self.regions.get(index)
        if region is None:
            raise KeyError(f"segment {self.segment_id} has no region {index}")
        if region.producer is not Producer.PLUGIN:
            # §3.1: a release naming the wrong side is a ContractViolation.  The
            # errant write is undetectable; this is not.
            raise self._violate(
                index,
                WHERE_WRONG_PRODUCER_RELEASE,
                "ReleaseLoan names a host-produced region, which is released with "
                "ReleaseDeposit",
            )
        self.loaned_unchecked(index).release(offset, length)

    def loaned_unchecked(self, index: int) -> LoanedArena:
        """The arena, without §9.1's start-versus-finish gate.

        Only for applying a release: an arena that was never handed out cannot
        have an outstanding loan, so this can create one only in the case where
        it immediately raises.
        """
        arena = self._arenas.get(index)
        if arena is None:
            arena = self._arenas[index] = LoanedArena(self, self.regions[index])
        return arena

    # -- STAGED (processing_stages.md §6) ----------------------------------

    def hold(self) -> bool:
        """Hold the mapping open for one pass over a stage ring.

        A turn in progress is a claim in §9.1's sense: *retiring* lets it
        finish and must not unmap under it.  The claim is counted BEFORE
        ``retiring`` is read, so either this sees the retirement or
        :meth:`SegmentStore.begin_retire` sees the claim.  ``False`` means the
        ring is retiring and nothing may start; either way pair it with
        :meth:`unhold`.
        """
        self._claimed(1)
        return not self._retiring

    def unhold(self) -> None:
        self._claimed(-1)

    def _staged(self, index: int) -> Region:
        region = self.regions.get(index)
        if region is None:
            raise KeyError(f"segment {self.segment_id} has no region {index}")
        if region.discipline is not Discipline.STAGED:
            raise ValueError(f"region {index} is not STAGED")
        return region

    def load_turn(self, index: int) -> int:
        """§6.1: one acquire-load of the slot's turn word.  Claims nothing."""
        return self._atomics().load(self._staged(index).offset)

    def store_turn(self, index: int, word: int) -> None:
        """§6.1: the release store that hands the slot on.  Everything the
        holder wrote must already be in the slot."""
        self._atomics().store(self._staged(index).offset, word)

    def slot_buffer(self, index: int) -> memoryview:
        """The slot's bytes, writable.  The caller releases the view before the
        turn ends: a live export keeps the mapping from closing (§9.2)."""
        region = self._staged(index)
        return memoryview(self._mm)[region.offset : region.offset + region.extent]

    # -- teardown ---------------------------------------------------------

    def _claimed(self, delta: int) -> None:
        """Count a claim this endpoint holds: a Pulsed CAS it took, a STAGED
        turn, or a loaned reservation or deposit read in flight (§9.1)."""
        with self._claim_lock:
            self._claims += delta
            drained = delta < 0 and self._claims == 0 and self._retiring
        if drained and self._on_drained is not None:
            self._on_drained()

    def _has_held_claims(self) -> bool:
        """§9.1: a held Pulsed claim MUST be completed before release.

        **"Held" means held by THIS endpoint**, which is why this counts what
        our own CAS took rather than scanning state words.  A claim bit is not
        self-identifying: a receiver reading memory sees the peer's claims
        exactly as it sees its own, so answering from the region would block our
        release on a claim the peer holds -- which the peer has no reason to
        drop and no way to know is blocking anything, and the segment would
        never be released at all.
        """
        with self._claim_lock:
            return self._claims > 0

    def _close(self) -> None:
        """§9.2: every mapping unmapped and every handle closed, before the
        caller says ``SegmentReleased``.  A false release is worse than none."""
        with self._shared_lock:
            if self._shared is not None:
                self._shared.close()
                self._shared = None
        self._mm.close()


# --------------------------------------------------------------------------
# The store: two halves, either order
# --------------------------------------------------------------------------


@dataclass
class _Entry:
    segment_id: int
    state: SegmentState = SegmentState.PENDING
    #: The capability half (doorbell `M`), once it has arrived.
    size: int | None = None
    perms: int | None = None
    mapping: "mmap.mmap | None" = None
    capability_bytes: bytes | None = None
    #: The meaning half (`InstallSegment`), once it has arrived.
    purpose: str | None = None
    #: §3: WHICH contribution this segment serves, by its manifest id.  Empty
    #: for the three purposes that serve an exchange rather than a binding.
    subject: str = ""
    regions: dict[int, Region] | None = None
    install_key: tuple | None = None
    #: An `InstallSegment` that carried `supersedes`, held until the replacement
    #: reaches *installed* -- §9's "atomic with the replacement".
    supersedes: int = 0
    #: §9.6: a ``ReleaseRequest`` this plugin has already produced.  Repeats
    #: are legal and idempotent on the wire, and a plugin should not schedule
    #: them -- so the SDK produces one and then declines to produce more.
    release_requested: bool = False
    #: The recorded terminal answer, replayed verbatim.
    answer: object = None
    segment: Segment | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: The loans still out when the segment was released, as ``(region,
    #: mapping offset, length)``: the only ``ReleaseLoan``s that may cross the
    #: release (§9.3), each once.
    loans_out: set = field(default_factory=set)

    @property
    def have_capability(self) -> bool:
        return self.size is not None

    @property
    def have_install(self) -> bool:
        return self.regions is not None


class SegmentStore:
    """Every grant this session has been offered, and its state.

    A ``segment_id`` is never reused (§3), so an entry lives for the session and
    needs no eviction rule beyond teardown.
    """

    def __init__(
        self, purposes: "frozenset[str] | None" = None, *, max_segment_bytes: int | None = None
    ) -> None:
        """``purposes`` is what this receiver implements, and it defaults to
        :data:`PURPOSES` -- the five §3 specifies.

        **There is nothing to configure and nothing published.**  §3 settled
        `purpose` as host-defined, with a receiver's recognized set being *"the
        set it implements"*, and there is no negotiation because there is no
        boundary to negotiate across: nothing here is installed on demand, both
        ends are userspace and trusted equally, and a plugin already asks for
        what it wants in vocabularies the IDL and manifest schema fix.
        `purpose` is in neither -- it appears only on ``InstallSegment``, as the
        label on what the ask produced.

        An SDK with no configured set does not refuse to start: it takes the
        set it implements.  Pass a narrower one only to model a receiver that
        implements less.

        ``max_segment_bytes`` is §2's *"a peer MAY also refuse a grant larger
        than it is willing to map"*, and it is **local policy with no default**.
        The normative bound is the host's per-session budget
        (``control_plane_protocol.md`` §9: the manifest's declared total, plus
        the arena headroom, plus twice the largest deposit, under a 4 GiB
        ceiling), and that is the *granting* side declining to ask.  This is the
        *receiving* side declining to serve, which is a different refusal and
        one only a deployment can size -- so an SDK that invented a number here
        would refuse grants the host had correctly budgeted for.  Set it when
        the deployment has a figure; leave it unset to accept what the host
        budgeted.

        When it is set, a larger ``size`` fails ``too_large`` **before mapping**
        -- the one class that means *re-grant smaller*, and only ever an
        explicit size-policy refusal (``doorbell_M_memory.md`` §7), never a
        mapping failure that happened to involve a large size.
        """
        purposes = PURPOSES if purposes is None else frozenset(purposes)
        if not purposes:
            raise ValueError(
                "a receiver that recognizes no purpose fails every grant `unsupported`; "
                "pass none to take the set this SDK implements (shared_memory_protocol.md §3)"
            )
        if max_segment_bytes is not None and max_segment_bytes < PREAMBLE_BYTES:
            raise ValueError("max_segment_bytes cannot exclude the 32-byte preamble")
        self._purposes = frozenset(purposes)
        self._max_segment_bytes = max_segment_bytes
        self._contributions: frozenset[str] | None = None
        self._entries: dict[int, _Entry] = {}
        self._lock = threading.Lock()
        #: Called (must not block) when a retiring segment's last in-flight
        #: claim ends, so its held-back release can be settled then.
        self.on_drained: "Callable[[], None] | None" = None
        #: §9: a `supersedes` retirement behaves as a `RetireSegment` in every
        #: respect, including the `SegmentReleased` -- which the control
        #: plane sends, not this layer.  Each ``(segment_id, released)`` the
        #: store retired on a replacement's install waits here until
        #: :meth:`take_superseded` hands it up.
        self._superseded: list[tuple[int, bool]] = []

    def take_superseded(self) -> list[tuple[int, bool]]:
        """The segments retired by a ``supersedes`` since the last call, each
        with whether it is already RELEASED (``True``) or still *retiring*
        behind a held claim (``False``, to be settled like a `RetireSegment`)."""
        with self._lock:
            out, self._superseded = self._superseded, []
        return out

    def set_contributions(self, contributions: "Iterable[str] | None") -> None:
        """Declare the manifest contribution ids a ``subject`` may name (§9).

        §9's row asks a receiver to check that an ``O`` or ``P`` grant's
        ``subject`` *"names a contribution this plugin declared"*, which only a
        receiver that has been told what it declared can do -- there is no
        manifest on this side of the wire.  Unset, the shape is still checked
        both ways and the name is taken on trust, which is where vizi's own
        hook of the same shape sits today.

        ``None`` returns to that state.
        """
        self._contributions = None if contributions is None else frozenset(contributions)

    # -- the two halves ---------------------------------------------------

    def capability(
        self,
        segment_id: int,
        size: int,
        perms: int,
        *,
        fd: int | None = None,
        name: str | None = None,
        record: bytes | None = None,
    ) -> Installed | Failed | Released | None:
        """Apply the doorbell's ``M`` half.

        **Takes ownership of ``fd`` unconditionally.** Every path out of here
        closes it exactly once -- a rejection, a duplicate, and an exception
        alike -- which is why the close is a ``finally`` rather than a call on
        each branch (``shared_memory_protocol.md`` §9a step 2).

        ``record`` is the 112 bytes of the ``M``, and it is what makes a
        duplicate distinguishable from a conflicting grant (§8).  ``None`` means
        still pending: the other half has not arrived and there is no answer to
        give.
        """
        try:
            return self._capability(segment_id, size, perms, fd, name, record)
        finally:
            _close_fd(fd)

    def _capability(self, segment_id, size, perms, fd, name, record):
        entry = self._entry(segment_id)
        with entry.lock:
            if entry.state is SegmentState.RELEASED:
                # §9.4: answered with SegmentReleased again, never with silence.
                return Released(segment_id)
            if entry.state in (SegmentState.INSTALLED, SegmentState.FAILED):
                # §8: a duplicate after a terminal state never remaps and
                # replays the recorded answer -- but only if it IS a duplicate.
                # A second, DIFFERENT grant of an id that is never reused is a
                # conflicting grant, and re-acking it would hide the one kind
                # of defect that cannot be recovered from.
                if self._conflicts(entry, record):
                    return self._conflict(entry)
                return entry.answer
            if entry.have_capability:
                # §8: a duplicate M while PENDING closes the duplicate
                # descriptor, does not remap, and stays pending.
                if self._conflicts(entry, record):
                    return self._conflict(entry)
                return None
            try:
                mapping = self._map(size, perms, fd, name)
            except _GrantError as exc:
                return self._fail(entry, exc.failure_class, exc.detail)
            # The mapping owns the object now; the descriptor has done its job.
            entry.size, entry.perms, entry.mapping = size, perms, mapping
            entry.capability_bytes = record
            answer = self._settle(entry)
            pending = self._take_supersedes(entry, answer)
        if pending:
            self._retire_superseded(pending)
        return answer

    def reject_capability(
        self, segment_id: int, detail: str, *, record: bytes | None = None
    ) -> "Failed | Released | None":
        """The doorbell rejected an ``M`` record: fail the grant it named.

        A record-level violation on ``M`` is a **grant failure**, class
        ``malformed`` (``doorbell_M_memory.md`` §7: *"a vizi bug; re-granting
        the same thing fails again"*), and §8's *"report a failure the moment it
        is known"* applies to it exactly as it does to a mapping that would not
        map.  The transport has already closed the descriptor and hands up the
        reason (``Frame.grant_failure``); what is left is to record the failure
        **against the segment id** so it is replayable.

        **Recording it is what stops a loop.**  Without an entry the id stays
        *not present*, and §7's answer table makes the host re-send the same
        grant on a ``GrantStateQuery`` -- which the doorbell rejects again, and
        each pass puts another ``SCM_RIGHTS`` descriptor in flight against the
        host's user-wide ``RLIMIT_NOFILE`` (``doorbell_protocol.md`` §6 rule 6).
        A recorded ``failed`` answers the query with the truth and ends it.

        ``segment_id`` 0 is refused: `M` §7 routes ``zero_id`` to
        ``ContractViolation`` rather than to an acknowledgement, because there
        is no grant for the failure to be about.
        """
        if segment_id == 0:
            raise ValueError(
                "a zero segment id has no grant to fail; it is reported as "
                "ContractViolation{zero_id} (doorbell_M_memory.md §7)"
            )
        entry = self._entry(segment_id)
        with entry.lock:
            if entry.state is SegmentState.RELEASED:
                return Released(segment_id)  # §9.4
            if entry.state in (SegmentState.INSTALLED, SegmentState.FAILED):
                if self._conflicts(entry, record):
                    return self._conflict(entry)
                return entry.answer  # type: ignore[return-value]
            if entry.have_capability:
                # A record that mapped cannot also be the record that was
                # rejected, so this is a second, different `M`.
                return self._conflict(entry)
            entry.capability_bytes = record
            return self._fail(entry, GrantFailure.MALFORMED, detail)

    @staticmethod
    def _conflicts(entry: _Entry, record: bytes | None) -> bool:
        """§8: match on ``segment_id``, then compare the record's 112 bytes.

        A caller that supplies no bytes cannot be told apart from a duplicate,
        so it is treated as one -- the comparison is only as good as what the
        supervisor hands down.
        """
        return (
            record is not None
            and entry.capability_bytes is not None
            and entry.capability_bytes != record
        )

    @staticmethod
    def _take_supersedes(entry: _Entry, answer: object) -> int:
        """§9: ``supersedes`` fires when the replacement reaches *installed*.

        Not when its ``InstallSegment`` arrives: the two halves come on two
        channels, so retiring then gives up the old segment for a replacement
        that does not exist yet -- and if the other half fails, the plugin holds
        neither, at the one moment the host's answer is to grant again.
        """
        if entry.supersedes and isinstance(answer, Installed):
            pending, entry.supersedes = entry.supersedes, 0
            return pending
        return 0

    def install(
        self,
        segment_id: int,
        purpose: str,
        regions: list[Region],
        *,
        supersedes: int = 0,
        subject: str = "",
    ) -> Installed | Failed | Released | None:
        """Apply the control plane's ``InstallSegment`` half.

        A repeat is compared **field by field, never byte by byte** (§8):
        protobuf encoding is not canonical, so comparing bytes would report a
        conflicting grant for a message that says the same thing.

        ``subject`` is §3's *which contribution this segment serves*, and it is
        part of the identity a repeat is compared on: two grants agreeing on
        every region while naming different overlays are two different grants.
        """
        # §9: a grant that supersedes ITSELF asks the receiver to retire what
        # it is installing, which has nothing to carry out; acting on it
        # naively takes the same per-segment lock twice and hangs.  Decided
        # INSIDE the lock and AFTER the states an entry can already be in: a
        # repeat naming itself for a segment already installed is a
        # conflicting duplicate that must not tear the live segment down, and
        # for one released it is §9.4's replay.
        names_itself = bool(supersedes) and supersedes == segment_id
        key = (
            purpose,
            subject,
            tuple(
                sorted(
                    (
                        r.index,
                        r.offset,
                        r.extent,
                        r.discipline,
                        r.producer,
                        r.buffers,
                        r.slot_bytes,
                        r.id,
                        r.epoch_offset,
                    )
                    for r in regions
                )
            ),
        )
        entry = self._entry(segment_id)
        with entry.lock:
            if entry.state is SegmentState.RELEASED:
                return Released(segment_id)  # §9.4
            if entry.state in (SegmentState.INSTALLED, SegmentState.FAILED):
                if names_itself or entry.install_key not in (None, key):
                    return self._conflict(entry)
                return entry.answer  # type: ignore[return-value]
            if entry.have_install:
                if names_itself or entry.install_key != key:
                    return self._conflict(entry)
                return None
            if names_itself:
                return self._fail(
                    entry, GrantFailure.MALFORMED, "supersedes names the segment being installed"
                )
            try:
                table = self._region_table(purpose, regions, subject)
            except _GrantError as exc:
                # §8: report NOW, against the segment_id the message carries.
                # A later M for it closes its descriptor and replays this.  A
                # `supersedes` on a grant that failed has no effect at all.
                return self._fail(entry, exc.failure_class, exc.detail)
            entry.purpose, entry.regions, entry.install_key = purpose, table, key
            entry.subject = subject
            entry.supersedes = supersedes
            answer = self._settle(entry)
            pending = self._take_supersedes(entry, answer)
        # Outside the lock: retiring another segment takes that entry's lock,
        # and taking two per-segment locks at once is how an ordering bug gets in.
        if pending:
            self._retire_superseded(pending)
        return answer

    def _retire_superseded(self, segment_id: int) -> None:
        released = self.retire(segment_id)
        with self._lock:
            self._superseded.append((segment_id, released))

    # -- queries and lifecycle -------------------------------------------

    def state(self, segment_id: int) -> StateReport:
        """Answer ``GrantStateQuery``.  Idempotent, and ``pending`` names which
        half arrived -- the host's action differs between them, so ``pending``
        without that is not an answer."""
        with self._lock:
            entry = self._entries.get(segment_id)
        if entry is None:
            return StateReport(segment_id, SegmentState.NOT_PRESENT)
        with entry.lock:
            if entry.state is SegmentState.PENDING:
                return StateReport(
                    segment_id,
                    SegmentState.PENDING,
                    have_capability=entry.have_capability,
                    have_install=entry.have_install,
                )
            if entry.state is SegmentState.INSTALLED:
                return StateReport(
                    segment_id,
                    SegmentState.INSTALLED,
                    identity_proof=entry.answer.identity_proof,  # type: ignore[union-attr]
                )
            if entry.state is SegmentState.FAILED:
                answer = entry.answer
                return StateReport(
                    segment_id,
                    SegmentState.FAILED,
                    failure_class=answer.failure_class,  # type: ignore[union-attr]
                    detail=answer.detail,  # type: ignore[union-attr]
                )
            return StateReport(segment_id, SegmentState.RELEASED)

    def hint(
        self,
        segment_id: int,
        regions: Iterable[int],
        *,
        epoch: int = 0,
        installed_epoch: int | None = None,
    ) -> Hint:
        """Rule on a ``D`` record's mask against the region table.

        ``doorbell_D_notification.md`` §3 makes this the memory layer's check
        and not the doorbell's: condition (e) is about a region's *discipline*,
        and the transport cannot see one.  §3's table is the whole rule --
        Pulsed and Staged are what ``D`` is for, Owned is an error reported
        here, and Loaned and Constant are no-ops with a diagnostic for two
        different reasons.

        ``installed_epoch`` is the epoch this plugin acknowledged for the
        record's pane.  A hint older than it is ignored, and ignoring it is not
        a lost acceleration -- it is the hint doing its job (§5).

        It is also the whole of the staleness test **this module can make**.
        ``shared_memory_protocol.md`` §2 now declares where a segment's own
        ``(view_id, epoch)`` copy lives -- ``Region.epoch_offset``, an 8-byte
        ``u32 view_id``, ``u32 epoch`` header -- but :class:`Region` does not
        carry that field yet, so every region here is §2's **absent** case, for
        which the acknowledged epoch is expressly the whole of the test.  A
        check against an embedded pair this SDK was never given a location for
        would be checking something it invented.

        **Acting on the result is optional; the schedule is not.**  See
        :class:`ReadSchedule`.
        """
        wanted = list(dict.fromkeys(regions))  # de-duplicated, order preserved
        with self._lock:
            entry = self._entries.get(segment_id)
        if entry is None or entry.state is not SegmentState.INSTALLED:
            # An unknown segment id is counted and ignored, exactly like an
            # out-of-range mask bit (`doorbell_D_notification.md` §1).
            return Hint(segment_id, ignored=(("unknown_segment", -1),))
        segment = entry.segment
        if segment is None:
            return Hint(segment_id, ignored=(("unknown_segment", -1),))
        if installed_epoch is not None and 0 < epoch < installed_epoch:
            return Hint(segment_id, ignored=(("stale_epoch", -1),))
        if segment.retiring:
            # §9.1: while retiring, a bit naming a region in this segment is a
            # no-op with a diagnostic and nothing else.
            return Hint(segment_id, ignored=(("retiring_segment", -1),))
        scan: list[int] = []
        turns: list[int] = []
        violations: list[tuple[str, int]] = []
        ignored: list[tuple[str, int]] = []
        # `doorbell_D_notification.md` §3: a bit past the last granted region
        # means the sender knows about regions this receiver does not -- most
        # often a re-grant in flight -- so it sweeps.  The bit itself is still
        # counted and ignored, and it never discards the valid bits beside it.
        sweep = [i for i in wanted if i not in segment.regions]
        named = [i for i in wanted if i in segment.regions]
        if sweep:
            named += [
                i
                for i, r in segment.regions.items()
                if (
                    r.discipline is Discipline.PULSED
                    and r.producer is not Producer.PLUGIN
                    or r.discipline is Discipline.STAGED
                )
                and i not in named
            ]
        for index in sweep:
            ignored.append(("unknown_region", index))
        for index in named:
            region = segment.regions.get(index)
            if region is None:
                ignored.append(("unknown_region", index))
            elif segment.quarantined(index):
                ignored.append(("quarantined_region", index))
            elif region.discipline is Discipline.PULSED:
                if region.producer is Producer.PLUGIN:
                    # `D` is symmetric, so a hint is about the OTHER end's lane.
                    ignored.append(("own_pulsed_region", index))
                else:
                    scan.append(index)
            elif region.discipline is Discipline.STAGED:
                # `processing_stages.md` §6.2: loading the turn word claims
                # nothing, so a hint for a turn that is not ours is harmless and
                # the runner counts it.  `producer` is HOST on every slot and
                # says nothing about whose turn it is.
                turns.append(index)
            elif region.discipline is Discipline.OWNED:
                # §5: the read IS the handoff, so re-checking is unsafe.  It is
                # reported and the hint ignored; it is not a session fault.
                violations.append((WHERE_OWNED_REGION_HINT, index))
            elif region.discipline is Discipline.LOANED:
                # Safe to re-check, but a ring carries no in-band new-deposit
                # marker, so there is nothing for the hint to accelerate.
                ignored.append(("loaned_region", index))
            else:
                ignored.append(("constant_region", index))  # safe, and pointless
        return Hint(segment_id, tuple(scan), tuple(violations), tuple(ignored), tuple(turns))

    def segment(self, segment_id: int) -> Segment | None:
        with self._lock:
            entry = self._entries.get(segment_id)
        return entry.segment if entry is not None else None

    def _drained(self) -> None:
        callback = self.on_drained
        if callback is not None:
            callback()

    def installed(self) -> "list[Segment]":
        """Every segment currently in *installed*, oldest id first.

        A plugin that writes into a plugin-produced region has to find it, and
        the only name the documents give it is ``Region.id`` (§3) -- which is
        per region, not per segment, so the search is over all of them.  Ids are
        never reused and a released segment is gone from here, so the answer is
        a snapshot that can only shrink under a retirement.
        """
        with self._lock:
            entries = sorted(self._entries.items())
        out = []
        for _segment_id, entry in entries:
            with entry.lock:
                if entry.state is SegmentState.INSTALLED and entry.segment is not None:
                    out.append(entry.segment)
        return out

    def request_release(self, segment_id: int, reason: str = "") -> ReleaseRequest | None:
        """§9.6: produce one ``ReleaseRequest``, or ``None`` if there is none to send.

        ``None`` means one of the three cases §9.6 rules out rather than an
        error:

        - **already asked.**  Repeats are legal and idempotent, and a plugin
          should not schedule them -- re-sending on a timer converts a free
          hint into a per-session flood against §7's one-shot budget.  So this
          produces one per segment and then declines.
        - **not acknowledged.**  While a grant is *pending* the plugin owes an
          answer, not a request, and §8's state machine has no room for a third
          one racing the first two.  A *failed* segment is already unmapped and
          closed, so there is nothing to ask about.
        - **already released.**

        **Producing it changes nothing else.**  The segment stays fully
        operational: every discipline is honored exactly as before, a
        descriptor arriving afterwards is read rather than rejected, and
        ``RetireSegment`` remains the only thing that enters *retiring*.  A
        plugin that self-retired on send would have to reject a message the
        host sent correctly, for a race the protocol created.
        """
        with self._lock:
            entry = self._entries.get(segment_id)
        if entry is None:
            return None
        with entry.lock:
            if entry.state is not SegmentState.INSTALLED or entry.release_requested:
                return None
            entry.release_requested = True
            return ReleaseRequest(segment_id, reason)

    def shortfalls(self) -> list[SegmentShortfall]:
        """Drain every arena's pending ``SegmentShortfall`` (§5.6, §5.4).

        One per ``(segment_id, region)`` that came up short since the last
        drain, already merged -- largest ``wanted_bytes``, summed ``dropped``.
        The caller sends them on the coalescing queue; this module produces
        messages and never sends one.
        """
        with self._lock:
            entries = list(self._entries.values())
        out: list[SegmentShortfall] = []
        for entry in entries:
            segment = entry.segment
            if segment is None:
                continue
            for arena in segment._arena_list():
                pending = arena.take_shortfall()
                if pending is not None:
                    out.append(pending)
        return out

    def begin_retire(self, segment_id: int) -> bool:
        """``RetireSegment`` arrived: enter *retiring*.

        Returns ``True`` when :meth:`release` may be called now -- i.e. nothing
        the plugin started is still in flight.  A held Pulsed claim must be
        completed and released first; abandoning one wedges that lane for as
        long as the plugin lives (§9.1).
        """
        with self._lock:
            entry = self._entries.get(segment_id)
        if entry is None:
            return True  # §9.4: assert the end state; it is true and idempotent.
        with entry.lock:
            if entry.state is SegmentState.RELEASED:
                return True
            if entry.segment is not None:
                entry.segment._retiring = True
                # §5.4: a pending shortfall for a segment being retired is
                # DROPPED rather than kept -- a re-grant is what the plugin is
                # getting anyway, and the key leaves the coalescing space when
                # `SegmentReleased` settles it.
                for arena in entry.segment._arena_list():
                    arena.take_shortfall()
                return not entry.segment._has_held_claims()
            return True

    def release(self, segment_id: int) -> bool:
        """Unmap, close, and mark released.  The caller then sends
        ``SegmentReleased`` -- **after** this returns, never before (§9.2).

        ``True`` on the transition; ``True`` again on a duplicate, because
        §9.4 replays rather than re-releasing.
        """
        with self._lock:
            entry = self._entries.get(segment_id)
            if entry is None:
                # §9.4: an id that never arrived.  Ids are never reused, so
                # asserting the end state is both true and idempotent.
                entry = self._entries[segment_id] = _Entry(segment_id)
                entry.state = SegmentState.RELEASED
                return True
        with entry.lock:
            if entry.state is SegmentState.RELEASED:
                return True
            # §9.3: SegmentReleased supersedes every ReleaseDeposit still owed
            # inside the segment -- releasing a byte range in an object that no
            # longer exists is bookkeeping with no subject.  Dropping the
            # Segment drops its rings, which is that rule.
            if entry.segment is not None:
                entry.loans_out = {
                    (arena._r.index, arena._r.offset + inside, length)
                    for arena in entry.segment._arena_list()
                    for inside, length in list(arena._outstanding)
                }
            self._teardown(entry)
            entry.state = SegmentState.RELEASED
            entry.answer = None
            return True

    def take_crossed_loan(self, segment_id: int, region: int, offset: int, length: int) -> bool:
        """A ``ReleaseLoan`` for a segment already released (§9.3): True when
        it names a loan that was out at the release, which it now settles;
        False for a duplicate or a loan that never was -- the host's bug,
        which a crossing must not hide."""
        with self._lock:
            entry = self._entries.get(segment_id)
        if entry is None:
            return False
        with entry.lock:
            key = (int(region), int(offset), int(length))
            if key not in entry.loans_out:
                return False
            entry.loans_out.discard(key)
            return True

    def retire(self, segment_id: int) -> bool:
        """``begin_retire`` then, if nothing is in flight, ``release``."""
        if self.begin_retire(segment_id):
            return self.release(segment_id)
        return False

    def close(self) -> None:
        """Session teardown.  §9.4: a plugin owes no ``SegmentReleased`` here --
        process exit is the release, and the host is tearing down anyway."""
        with self._lock:
            entries = list(self._entries.values())
        for entry in entries:
            with entry.lock:
                self._teardown(entry)
                entry.state = SegmentState.RELEASED

    # -- internals --------------------------------------------------------

    def _entry(self, segment_id: int) -> _Entry:
        with self._lock:
            entry = self._entries.get(segment_id)
            if entry is None:
                entry = self._entries[segment_id] = _Entry(segment_id)
            return entry

    def _settle(self, entry: _Entry) -> Installed | Failed | None:
        """Emit the SUCCESS ack when the second half lands, and not before:
        only then is *mapped and preamble-validated and meaning-applied* true."""
        if not (entry.have_capability and entry.have_install):
            return None
        try:
            proof = self._validate(entry)
        except _GrantError as exc:
            return self._fail(entry, exc.failure_class, exc.detail)
        entry.segment = Segment(
            entry.segment_id,
            entry.purpose,  # type: ignore[arg-type]
            entry.size,  # type: ignore[arg-type]
            entry.perms,  # type: ignore[arg-type]
            entry.mapping,  # type: ignore[arg-type]
            entry.regions,  # type: ignore[arg-type]
            proof,
            entry.subject,
        )
        entry.segment._on_drained = self._drained
        entry.state = SegmentState.INSTALLED
        entry.answer = Installed(entry.segment_id, proof)
        return entry.answer

    @staticmethod
    def _conflict(entry: _Entry) -> Failed:
        """§8: a conflicting grant is *"reported and not allowed to replace the
        recorded one"*.  So the answer is a failure for the message, and the
        entry -- installed, failed or pending, mapping and all -- is left
        exactly as it was.  Tearing it down here unmapped a working segment
        whenever a host re-sent a grant under a fresh id.
        """
        return Failed(
            entry.segment_id, GrantFailure.MALFORMED, "conflicting grant for a segment id"
        )

    def _fail(self, entry: _Entry, cls: GrantFailure, detail: str) -> Failed:
        """§8: entering *failed* releases every resource the attempt acquired,
        BEFORE the failure is reported.  A mapping retained past a failed
        install is an invisible allocation, and the host's answer to a failure
        is to re-grant -- so without this each retry adds another."""
        self._teardown(entry)
        entry.state = SegmentState.FAILED
        entry.answer = Failed(entry.segment_id, cls, detail)
        return entry.answer

    def _teardown(self, entry: _Entry) -> None:
        if entry.segment is not None:
            entry.segment._close()
            entry.segment = None
            entry.mapping = None
        elif entry.mapping is not None:
            entry.mapping.close()
            entry.mapping = None

    def _map(self, size: int, perms: int, fd: int | None, name: str | None):
        if size < PREAMBLE_BYTES:
            raise _GrantError(GrantFailure.MALFORMED, f"size {size} is below the 32-byte preamble")
        if self._max_segment_bytes is not None and size > self._max_segment_bytes:
            # §2 and `doorbell_M_memory.md` §7: refused BEFORE mapping, so the
            # class is honestly a size-policy decision rather than a mapping
            # failure that happened to involve a large size -- which is the
            # distinction that makes `too_large` mean *re-grant smaller*.
            raise _GrantError(
                GrantFailure.TOO_LARGE,
                f"size {size} exceeds this peer's {self._max_segment_bytes}-byte mapping policy",
            )
        if perms & ~_PERM_KNOWN:
            # M §4: the one field where a reserved bit is NOT ignored, because
            # the acknowledgement asserts "installed, WITH THESE PERMISSIONS".
            raise _GrantError(GrantFailure.MALFORMED, "reserved perms bit set")
        if not perms & PERM_PLUGIN_READ:
            raise _GrantError(GrantFailure.MALFORMED, "PLUGIN_READ is required on every grant")
        if fd is not None:
            # §8: "the mapped object's size matches `size`".  Comparing
            # len(mapping) afterwards cannot do it, since mmap was asked for
            # exactly `size` -- a larger object passes silently and a smaller
            # one raises out of mmap instead of failing the grant.
            try:
                actual = os.fstat(fd).st_size
            except OSError as exc:
                raise _GrantError(GrantFailure.FAILED, f"fstat: {exc}") from exc
            if actual != size:
                raise _GrantError(
                    GrantFailure.MALFORMED,
                    f"the object is {actual} bytes and the grant says {size}",
                )
        access = mmap.ACCESS_WRITE if perms & PERM_PLUGIN_WRITE else mmap.ACCESS_READ
        try:
            if name is not None:
                return mmap.mmap(-1, size, tagname=name, access=access)
            if fd is None:
                raise _GrantError(
                    GrantFailure.MALFORMED,
                    "grant carried neither a descriptor nor a name",
                )
            return mmap.mmap(fd, size, access=access)
        except _GrantError:
            raise
        except Exception as exc:
            # doorbell_M_memory.md §7: RETRYABLE is a CLOSED list -- out of
            # address space, handles, or memory.  Everything else is permanent,
            # so that a class meaning "try again" is never reached by accident.
            # The catch is broad on purpose: §8 says every validation failure
            # maps to exactly one class, so no mapping failure may leave here as
            # an exception -- CPython raises ValueError, not OSError, when a
            # mapping is longer than its object.
            errno = getattr(exc, "errno", None)
            retryable = isinstance(exc, MemoryError) or errno in _RETRYABLE_ERRNOS
            cls = GrantFailure.RETRYABLE if retryable else GrantFailure.FAILED
            raise _GrantError(cls, f"{type(exc).__name__}: {exc}") from exc

    def _region_table(
        self, purpose: str, regions: list[Region], subject: str = ""
    ) -> dict[int, Region]:
        if purpose not in self._purposes:
            raise _GrantError(GrantFailure.UNSUPPORTED, f"unknown purpose {purpose!r}")
        self._check_subject(purpose, subject)
        table: dict[int, Region] = {}
        for region in regions:
            # §8's matrix: a ZERO required field is `malformed` (the sender
            # omitted it, so re-granting fails the same way); an UNKNOWN
            # non-zero one is `unsupported` (a peer older than the grant).
            for name, value, enum_type in (
                ("discipline", region.discipline, Discipline),
                ("producer", region.producer, Producer),
            ):
                if isinstance(value, enum_type):
                    continue
                if value in (0, None):
                    raise _GrantError(GrantFailure.MALFORMED, f"region {name} is unspecified")
                raise _GrantError(GrantFailure.UNSUPPORTED, f"unknown region {name}")
            if region.extent == 0:
                # A region naming no bytes has no reading, and every discipline
                # here needs at least one.
                raise _GrantError(GrantFailure.MALFORMED, f"region {region.index} is empty")
            if region.index in table:
                raise _GrantError(GrantFailure.MALFORMED, f"duplicate region {region.index}")
            table[region.index] = region
        if sorted(table) != list(range(len(table))):
            # §3: dense and 0-based, because it is what a D mask bit names by
            # position -- no translation table anywhere.
            raise _GrantError(GrantFailure.MALFORMED, "region indices are not dense from 0")
        arenas = [
            r.index
            for r in table.values()
            if r.discipline is Discipline.LOANED and r.producer is Producer.PLUGIN
        ]
        lanes = [
            r.index
            for r in table.values()
            if r.discipline is Discipline.PULSED and r.producer is Producer.PLUGIN
        ]
        if len(lanes) > 1:
            # §3.1 at `1.0`, and the arena rule's own argument applied to the
            # discipline beside it: a plugin publishing into a live lane must
            # know which lane is its own, and every available key is weaker
            # than the question.  **A plugin with two overlays is granted two
            # `O` segments**, told apart by `subject`.
            raise _GrantError(
                GrantFailure.MALFORMED,
                f"regions {lanes} are all plugin-produced PULSED; a segment carries "
                "at most one live lane (§3.1)",
            )
        if len(arenas) > 1:
            # §3: **at most one plugin-produced LOANED region per segment**, and
            # that single region is the plugin's arena -- everything it produces
            # into this segment reserves from it.  Two make the arena
            # unfindable: a plugin producing bytes cannot tell which is its own,
            # and every available key is weaker than the question (`id` is one
            # §8 says to IGNORE, `purpose` is per segment, index order is not a
            # contract).  Refusing the grant removes the lookup instead of
            # strengthening it, and fails where a host bug is cheapest to find.
            raise _GrantError(
                GrantFailure.MALFORMED,
                f"regions {arenas} are all plugin-produced LOANED; a segment carries "
                "at most one arena (§3)",
            )
        staged = [r.index for r in table.values() if r.discipline is Discipline.STAGED]
        if purpose == PURPOSE_STAGE:
            # `processing_stages.md` §4: the ring IS its slots.  Anything else in
            # it would be a region every stage in the chain maps read-write with
            # no rule for who writes it.
            if len(staged) != len(table):
                raise _GrantError(
                    GrantFailure.MALFORMED,
                    f"an {PURPOSE_STAGE!r} segment holds only STAGED slots",
                )
            if len(table) > STAGED_MAX_SLOTS:
                raise _GrantError(
                    GrantFailure.MALFORMED,
                    f"{len(table)} slots; a ring holds at most {STAGED_MAX_SLOTS}, the "
                    "regions one D mask can name",
                )
        elif staged:
            # The turn word is the whole handoff, and only a stage armed on the
            # ring knows whose turn a position names.
            raise _GrantError(
                GrantFailure.MALFORMED,
                f"regions {staged} are STAGED outside an {PURPOSE_STAGE!r} segment",
            )
        return table

    def _check_subject(self, purpose: str, subject: str) -> None:
        """§3's table, **both ways**.

        Whether a `subject` is required is a property of the PURPOSE and not of
        how many contributions this plugin declared:

        - ``O`` and ``P`` -- **required, non-empty.**  The segment IS the
          contribution's, and two of them are otherwise indistinguishable.
        - ``G``, ``V``, ``X``, ``S`` -- **required to be EMPTY.**  The segment
          serves an exchange that already carries its own identity: a reader
          handle, a ``ReadDataVolume`` reply, an export handle, the
          ``StageArm``s naming a stage ring.

        A non-empty subject where none is defined is refused rather than
        ignored, because it is a sender asserting a binding this version does
        not have -- and a receiver that ignored it would be the one guessing
        when a later version gives it a meaning.

        ``contributions`` is the second half of §9's row, *"check it names a
        contribution this plugin declared"*, and it is only checkable by a
        receiver that has been told what it declared.  Left unset it stays
        ``None`` and only the shape is checked -- which is the same place
        vizi's own ``set_contributions`` hook sits.
        """
        wants = purpose in _SUBJECT_PURPOSES
        if wants and not subject:
            raise _GrantError(
                GrantFailure.MALFORMED,
                f"purpose {purpose!r} requires a subject naming the contribution it serves (§3)",
            )
        if not wants and subject:
            raise _GrantError(
                GrantFailure.MALFORMED,
                f"purpose {purpose!r} serves an exchange that names itself; subject "
                f"{subject!r} asserts a binding this version does not have (§3)",
            )
        if wants and self._contributions is not None and subject not in self._contributions:
            raise _GrantError(
                GrantFailure.MALFORMED,
                f"subject {subject!r} names no contribution this plugin declared",
            )

    def _validate(self, entry: _Entry) -> bytes:
        """§8's cross-checks, in order, before anything is acknowledged."""
        size, mapping, regions = entry.size, entry.mapping, entry.regions
        assert size is not None and mapping is not None and regions is not None
        if len(mapping) != size:
            raise _GrantError(
                GrantFailure.MALFORMED, f"mapped object is {len(mapping)} bytes, grant says {size}"
            )
        # [8..12) is reserved (M §5): senders write zero and a receiver does
        # not check it, so a later minor can grow into it.  The CRC covers it.
        segment_id, _reserved, proof, crc = _PREAMBLE.unpack(mapping[:PREAMBLE_BYTES])
        if zlib.crc32(mapping[:28]) != crc:
            # M §5: crc32 of 28 zero bytes is 0x807077e9, so a fresh Windows
            # create-or-open section fails here without any round trip.
            raise _GrantError(GrantFailure.MALFORMED, "preamble checksum does not match")
        if segment_id != entry.segment_id:
            raise _GrantError(
                GrantFailure.MALFORMED,
                f"preamble names segment {segment_id}, grant names {entry.segment_id}",
            )
        total = PREAMBLE_BYTES
        spans: list[tuple[int, int]] = []
        for region in regions.values():
            end = region.offset + region.extent
            if region.offset < PREAMBLE_BYTES or end > size:
                raise _GrantError(GrantFailure.MALFORMED, f"region {region.index} out of bounds")
            spans.append((region.offset, end))
            total += region.extent
            self._check_shape(region, entry.perms or 0)
        if total > size:
            raise _GrantError(GrantFailure.MALFORMED, "32 + sum(extent) exceeds size")
        spans.sort()
        for (_, prev_end), (start, _) in zip(spans, spans[1:]):
            if start < prev_end:
                raise _GrantError(GrantFailure.MALFORMED, "regions overlap")
        return proof

    def _check_shape(self, region: Region, perms: int) -> None:
        if region.producer is Producer.PLUGIN and not perms & PERM_PLUGIN_WRITE:
            # §3.1: a region the receiver produces must be mapped writable, and
            # finding that at install beats finding it at the first write.
            raise _GrantError(
                GrantFailure.MALFORMED,
                f"region {region.index} is plugin-produced but the mapping is read-only",
            )
        if region.discipline is Discipline.PULSED:
            if not perms & PERM_PLUGIN_WRITE:
                # §3.1: a consumer claims by CAS (§6.5 step 3), which WRITES the
                # state word -- so a Pulsed lane needs write access from both
                # ends, and `producer` says who writes the payload rather than
                # who touches the region.  Accepting this maps read-only and
                # faults on the first claim, at drag rate.
                raise _GrantError(
                    GrantFailure.MALFORMED,
                    f"region {region.index}: PULSED needs PLUGIN_WRITE at either end",
                )
            if region.buffers < 3:
                # §6.4 step 3: newest-READY held back, one a reader may hold,
                # one free to write.
                raise _GrantError(
                    GrantFailure.MALFORMED,
                    f"region {region.index}: PULSED needs at least 3 buffers",
                )
            if region.slot_bytes <= 0:
                raise _GrantError(
                    GrantFailure.MALFORMED, f"region {region.index}: PULSED needs slot_bytes"
                )
            if region.offset % 8:
                raise _GrantError(
                    GrantFailure.MALFORMED,
                    f"region {region.index}: PULSED offset is not u64-aligned",
                )
            # §6.1: the extent identity is `N*16 + N*S` -- the state and length
            # words are SIXTEEN bytes per buffer, not part of the slot.
            want = region.buffers * (_PULSED_HEADER_BYTES + region.slot_bytes)
            if region.extent != want:
                raise _GrantError(
                    GrantFailure.MALFORMED,
                    f"region {region.index}: extent {region.extent} disagrees with "
                    f"{region.buffers} buffers of {region.slot_bytes}",
                )
        elif region.buffers or region.slot_bytes and region.discipline is not Discipline.LOANED:
            raise _GrantError(
                GrantFailure.MALFORMED,
                f"region {region.index}: buffers/slot_bytes are PULSED fields",
            )
        if region.discipline is Discipline.STAGED:
            self._check_staged(region, perms)
        self._check_pane_header(region)

    def _check_staged(self, region: Region, perms: int) -> None:
        """``processing_stages.md`` §4-§6's grant-time half of a slot.

        The rest of a slot's validity is its header, which changes every turn
        and is the stage runner's to check before each one (§7).
        """
        if region.producer is not Producer.HOST:
            # §4: the host writes each volume in first.
            raise _GrantError(
                GrantFailure.MALFORMED,
                f"region {region.index}: a STAGED slot is host-produced",
            )
        if not perms & PERM_PLUGIN_WRITE:
            # §4: every stage writes, and the turn word is stored by the holder.
            raise _GrantError(
                GrantFailure.MALFORMED,
                f"region {region.index}: STAGED needs PLUGIN_WRITE",
            )
        if region.offset % STAGED_ALIGN:
            raise _GrantError(
                GrantFailure.MALFORMED,
                f"region {region.index}: a STAGED slot begins at a multiple of {STAGED_ALIGN}",
            )
        if region.extent < STAGED_FIXED_HEADER_BYTES:
            raise _GrantError(
                GrantFailure.MALFORMED,
                f"region {region.index}: extent {region.extent} cannot hold the "
                f"{STAGED_FIXED_HEADER_BYTES}-byte slot header",
            )
        if region.epoch_offset is not None:
            # A slot's volume is identified in its own header (t,
            # open_generation), and a pane pair beside it would be a second
            # answer to the same question.
            raise _GrantError(
                GrantFailure.MALFORMED,
                f"region {region.index}: a STAGED slot carries no pane header",
            )

    def _check_pane_header(self, region: Region) -> None:
        """§2 and §3.2's bound on ``epoch_offset``, **only where it is present**.

        The bound is part of the GRANT, not a runtime check, and a violation is
        `malformed` -- the same footing as the ``offset >= 32`` floor.  The two
        disciplines differ because the unit does:

        - **PULSED**: §3.2 FIXES the offset at ``slot_bytes - 8``, so there is
          nothing to bound and everything to compare.  A value anywhere else
          would move the payload off slot byte 0 and make §6.5 step 5's copy
          origin depend on the region, which is the branch that section exists
          to remove.  ``slot_bytes <= 8`` leaves no payload beside the header
          and is refused for that reason rather than for arithmetic.
        - **CONSTANT, OWNED, LOANED**: the header sits inside the region or
          inside a deposit, so ``extent`` is the only bound available here and
          :class:`DepositRing` checks each deposit itself.  ``slot_bytes`` is
          ignored entirely, including zero -- it is alignment on LOANED, not a
          size, and a ring legitimately declares 0.
        """
        if region.epoch_offset is None:
            return  # §2: absent means the region is not pane-associated.
        if region.epoch_offset < 0:
            raise _GrantError(
                GrantFailure.MALFORMED, f"region {region.index}: negative epoch_offset"
            )
        if region.discipline is Discipline.PULSED:
            if region.slot_bytes <= PANE_HEADER_BYTES:
                raise _GrantError(
                    GrantFailure.MALFORMED,
                    f"region {region.index}: slot_bytes {region.slot_bytes} leaves no payload "
                    "beside the 8-byte pane header (§3.2)",
                )
            if region.epoch_offset != region.slot_bytes - PANE_HEADER_BYTES:
                raise _GrantError(
                    GrantFailure.MALFORMED,
                    f"region {region.index}: a PULSED epoch_offset is slot_bytes - 8 = "
                    f"{region.slot_bytes - PANE_HEADER_BYTES}, not {region.epoch_offset} (§3.2)",
                )
        elif region.epoch_offset + PANE_HEADER_BYTES > region.extent:
            raise _GrantError(
                GrantFailure.MALFORMED,
                f"region {region.index}: epoch_offset {region.epoch_offset} + 8 does not "
                f"fit in {region.extent}",
            )


_RETRYABLE_ERRNOS = frozenset(
    v
    for v in (
        getattr(__import__("errno"), n, None) for n in ("ENOMEM", "EMFILE", "ENFILE", "EAGAIN")
    )
    if v is not None
)


def _close_fd(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass
