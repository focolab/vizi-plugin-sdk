"""v2 doorbell transport — the plugin side.

Implements, from the vizi documents alone, the parts of the fixed-size fast
path that bind a plugin (``doorbell_protocol.md`` §11a):

* framing, CRC, header, fixed padding, ancillary descriptors, socket errors
  and their classification (transport §4–§8);
* record ``id`` gap / regression diagnostics (transport §4);
* typed decode of ``M``, ``E`` and ``D`` with the raw 112 bytes attached;
* the record-local checks each op document assigns to the transport
  (``M`` §7, ``D`` §4) and the whole of ``E`` §2's ordered algorithm — the
  open watermark, per-interaction ``seq``, the interaction index, the
  per-tool open gesture, the active-tool set and high-water mark, and the
  per-pane installed epoch;
* the five lifecycle inputs the supervisor feeds it
  (``doorbell_implementation_details.md`` §6);
* one ordered sequence of observable outcomes, delivered on the listener
  thread, with a thread-safe ``stop()``;
* a dedicated fault sink — ``.faults`` — that the socket release waits on, so
  a fault reaches the supervisor even when the outcome consumer is wedged;
* a ``D`` sender — disposable, never retried, dropped on refusal.

What it deliberately is **not**: it sends no gRPC, maps no memory, validates
no arrangement, polls no region and supervises no session.  A fault is handed
up as an outcome; the supervisor is what puts ``DoorbellFault`` on the control
plane.  A ``ContractViolation`` or grant acknowledgement is likewise the
supervisor's to send from the ``Frame`` it receives.

Threading.  All outcomes reach the consumer on the transport's own listener
thread, in one total order.  Lifecycle inputs and ``send_notification`` may be
called from any thread; they mutate state synchronously under the transport's
serialization and their outcomes are queued onto the listener's sequence and
drained there (transport §8).  The consumer's callback is never invoked while
the serialization is held, and a callback may itself call any transport method.

Stdlib only, on purpose: this file is meant to be readable as a second,
independent statement of the protocol.
"""

from __future__ import annotations

import collections
import enum
import errno
import hmac
import math
import os
import queue
import selectors
import socket
import struct
import sys
import threading
import zlib
from dataclasses import dataclass
from typing import Callable, Iterable, Literal, Union

# ---------------------------------------------------------------------------
# The record (transport §4)
# ---------------------------------------------------------------------------

RECORD_SIZE = 112
PAYLOAD_SIZE = 100
RECORD_VERSION = 2
#: This record format is platform protocol 1.0, and the **major** is what must
#: match — one version implies one doorbell format, and
#: `control_plane_protocol.md` §3.2 makes the format changeable only on a major
#: bump.  So there is no version in which two framings are legal (transport
#: §3.1a), and a minor bump may not touch the framing at all: a receiver of one
#: format reading a record of another sees a nonsense length and
#: desynchronizes silently, so the change cannot be made compatible.
PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
#: Receive into a buffer strictly larger than a record (transport §7 rule 1).
RECV_BUFFER = 120


def compatible(major: int, minor: int) -> bool:
    """Whether a peer at ``major.minor`` may share a session with this build.

    `control_plane_protocol.md` §3.2: **a major bump breaks and a minor bump
    does not**, so a `1.1` host serves a `1.0` plugin and a `2.0` host serves
    neither.  Matching on both numbers would make every version a breaking
    version and the minor a serial number — a third-party author cannot build
    against that.

    **Within ``0.x`` the match is exact**: a `0.x` minor was allowed to change
    behavior, so the promise does not apply backwards to those peers.

    Written as a pair comparison on purpose.  A test on the minor alone — the
    obvious `MINOR >= N` — becomes permanently false at the next major,
    whose minor is `0`.
    """
    if major != PROTOCOL_MAJOR:
        return False
    return major != 0 or minor == PROTOCOL_MINOR


OP_MEMORY = 0x4D  # 'M' grant shared memory, host->plugin
OP_INPUT = 0x45  # 'E' tool input event, host->plugin
OP_NOTIFY = 0x44  # 'D' advisory notification, both directions
OP_RESERVED = 0xFF  # reserved recovery marker; never emitted, never legal

_HEADER = struct.Struct("<BBBBI")  # op, ver, flags, reserved, id
_CRC = struct.Struct("<I")
_U32_MASK = 0xFFFFFFFF
_ID_HALF = 1 << 31

#: CRC-32/ISO-HDLC check value over ``b"123456789"`` (transport §4).
CRC_CHECK_VALUE = 0xCBF43926

TransportClass = Literal["seqpacket", "dgram", "stream"]


def crc32(data: bytes) -> int:
    """CRC-32/ISO-HDLC exactly as ``zlib.crc32`` computes it, all 32 bits."""
    return zlib.crc32(data) & _U32_MASK


class Header(collections.namedtuple("Header", "op ver flags reserved id")):
    """The four header bytes and the record ``id`` (transport §4)."""

    __slots__ = ()


def encode_record(op: int, record_id: int, payload: bytes) -> bytes:
    """Build one 112-byte record.  ``payload`` must be exactly 100 bytes with
    its padding already zero — the typed encoders below guarantee that."""
    if not 0 <= op <= 0xFE:
        raise ValueError(f"op {op:#x} is not a legal opcode")
    if len(payload) != PAYLOAD_SIZE:
        raise ValueError(f"payload must be {PAYLOAD_SIZE} bytes, got {len(payload)}")
    body = _HEADER.pack(op, RECORD_VERSION, 0, 0, record_id & _U32_MASK) + payload
    return body + _CRC.pack(crc32(body))


def platform_transport_class() -> TransportClass:
    """The one socket type each platform has (transport §3)."""
    if os.name == "nt":
        return "stream"
    if sys.platform == "darwin":
        return "dgram"
    return "seqpacket"


_SOCK_TYPE_FOR_CLASS = {
    "seqpacket": getattr(socket, "SOCK_SEQPACKET", None),
    "dgram": socket.SOCK_DGRAM,
    "stream": socket.SOCK_STREAM,
}


# ---------------------------------------------------------------------------
# M — shared memory grant (doorbell_M_memory.md §4)
# ---------------------------------------------------------------------------

PERM_PLUGIN_READ = 0x01
PERM_PLUGIN_WRITE = 0x02
PERM_HOST_READ = 0x04
PERM_HOST_WRITE = 0x08
_PERM_RESERVED = 0xF0
#: The preamble is the first 32 bytes of every mapping; ``size`` includes it.
PREAMBLE_SIZE = 32
MAX_NAME_LEN = 80

_M_HEAD = struct.Struct("<QQBBB")  # segment_id, size, perms, padding, name_len
_M_NAME_OFFSET = 19


@dataclass(frozen=True)
class MemoryGrant:
    """A decoded ``M`` payload — the capability and nothing about its meaning.

    ``name`` is the decoded section name on a stream (Windows) transport and
    ``None`` on unix, where the field is ignored whole (``M`` §7).  ``name_len``
    is the raw byte either way, for diagnostics."""

    segment_id: int
    size: int
    perms: int
    name_len: int
    name: str | None

    @property
    def plugin_may_write(self) -> bool:
        return bool(self.perms & PERM_PLUGIN_WRITE)

    @property
    def host_may_write(self) -> bool:
        return bool(self.perms & PERM_HOST_WRITE)


def _decode_memory(
    payload: bytes, transport_class: str
) -> tuple[MemoryGrant, str | None, list[str]]:
    """Returns ``(grant, rejection, diagnostics)``.  A rejection is a grant
    failure the transport reports without handing the descriptor up; every
    reason but ``zero_id`` maps to the acknowledgement class ``malformed``."""
    segment_id, size, perms, _pad, name_len = _M_HEAD.unpack_from(payload, 0)
    diagnostics: list[str] = []
    name: str | None = None
    rejection: str | None = None
    if transport_class == "stream":
        # The name IS the capability on Windows, so it is validated in full —
        # length first, then the terminator, and only then any name byte.
        if name_len == 0:
            rejection = "empty_name"
        elif name_len > MAX_NAME_LEN:
            rejection = "name_too_long"
        elif payload[_M_NAME_OFFSET + name_len] != 0:
            rejection = "name_unterminated"
        else:
            raw = payload[_M_NAME_OFFSET : _M_NAME_OFFSET + name_len]
            if 0 in raw:
                rejection = "nul_in_name"
            elif any(payload[_M_NAME_OFFSET + name_len + 1 :]):
                rejection = "name_tail_not_zero"
            else:
                try:
                    name = raw.decode("utf-8", "strict")
                except UnicodeDecodeError:
                    rejection = "name_not_utf8"
    elif name_len != 0 or any(payload[_M_NAME_OFFSET:]):
        # unix: the descriptor is the capability; the field is read for the
        # diagnostic and for nothing else, and the grant proceeds.
        diagnostics.append(f"name_field_on_unix:{name_len}")
    grant = MemoryGrant(segment_id, size, perms, name_len, name)
    if segment_id == 0:
        rejection = "zero_id"  # reported on ContractViolation, not the ack
    elif rejection is None:
        if size < PREAMBLE_SIZE:
            rejection = "size_below_preamble"
        elif not perms & PERM_PLUGIN_READ:
            rejection = "no_plugin_read"
        elif perms & _PERM_RESERVED:
            rejection = "reserved_perms_bit"
    return grant, rejection, diagnostics


# ---------------------------------------------------------------------------
# D — advisory notification (doorbell_D_notification.md §1)
# ---------------------------------------------------------------------------

MASK_BITS = 512
_D_TAIL = struct.Struct("<IB")  # epoch, view_id at payload [72..77)
_D_MASK_OFFSET, _D_MASK_END = 8, 72
_D_TAIL_OFFSET, _D_PAD_OFFSET = 72, 77


@dataclass(frozen=True)
class Notification:
    """A decoded ``D``.  ``mask`` is the 512-bit region mask as one Python int,
    bit ``r`` naming region ``r`` — which is exactly the record's little-endian
    ``[8..72)`` read as a number, and therefore both of ``D`` §1's
    formulations at once."""

    segment_id: int
    mask: int
    epoch: int
    view_id: int

    def names(self, region: int) -> bool:
        return bool(self.mask >> region & 1)

    def regions(self) -> tuple[int, ...]:
        """Region indices set in the mask, ascending.  A lower bound on what
        changed — never the exact set (``D`` §1)."""
        out = []
        m = self.mask
        while m:
            low = m & -m
            out.append(low.bit_length() - 1)
            m ^= low
        return tuple(out)

    @property
    def pane_associated(self) -> bool:
        return self.epoch != 0


def encode_notification_payload(
    segment_id: int, regions: Iterable[int] | int, epoch: int = 0, view_id: int = 0
) -> bytes:
    """Build a ``D`` payload.  ``regions`` is either an iterable of region
    indices or a ready-made mask int.  Raises ``ValueError`` rather than ever
    producing a record the receiver would reject or ignore."""
    if not 0 < segment_id < 1 << 64:
        raise ValueError("segment_id must be a non-zero u64")
    if isinstance(regions, int):
        mask = regions
        if not 0 <= mask < 1 << MASK_BITS:
            raise ValueError("mask must fit in 512 bits")
    else:
        mask = 0
        for r in regions:
            if not 0 <= r < MASK_BITS:
                raise ValueError(f"region index {r} outside the {MASK_BITS}-bit mask")
            mask |= 1 << r
    if not 0 <= epoch < 1 << 32:
        raise ValueError("epoch must be a u32")
    if not 0 <= view_id <= 0xFF:
        raise ValueError("view_id must be a u8")
    if epoch == 0 and view_id != 0:
        raise ValueError("view_id means nothing without an epoch and must be 0")
    return (
        struct.pack("<Q", segment_id)
        + mask.to_bytes(64, "little")
        + _D_TAIL.pack(epoch, view_id)
        + bytes(PAYLOAD_SIZE - _D_PAD_OFFSET)
    )


def _decode_notification(payload: bytes) -> tuple[Notification, str | None, list[str]]:
    segment_id = struct.unpack_from("<Q", payload, 0)[0]
    mask = int.from_bytes(payload[_D_MASK_OFFSET:_D_MASK_END], "little")
    epoch, view_id = _D_TAIL.unpack_from(payload, _D_TAIL_OFFSET)
    diagnostics = []
    if epoch == 0 and view_id != 0:
        diagnostics.append("view_id_without_epoch")
    rejection = "zero_id" if segment_id == 0 else None
    return Notification(segment_id, mask, epoch, view_id), rejection, diagnostics


# ---------------------------------------------------------------------------
# E — tool input event (doorbell_E_input.md §1, §3, §4)
# ---------------------------------------------------------------------------

PHASE_PRESS, PHASE_DRAG, PHASE_RELEASE, PHASE_CANCEL, PHASE_FIRE = 0, 1, 2, 3, 4
#: vizi's two panes, by ``view_id``.  NOT a protocol constant: ``E`` §1 makes
#: ``view_id`` host-assigned and names no values, and the transport validates
#: it as a u8 and nothing more.  These are the numbers vizi's host uses --
#: it has exactly these two panes and they never close -- and they live here
#: so the host and a plugin written against vizi share one spelling.  A plugin that wants a pane's
#: KIND has a better source than its number: a 3D pane sends ``KIND_RAY`` /
#: ``KIND_RAY_AND_PICKED`` and a 2D pane ``KIND_VOXEL`` or a column, which
#: holds for any host.
VIEW_2D, VIEW_3D = 0, 1
_OPENING_PHASES = frozenset({PHASE_PRESS, PHASE_FIRE})
_TERMINAL_PHASES = frozenset({PHASE_RELEASE, PHASE_CANCEL})
_KNOWN_PHASES = frozenset({PHASE_PRESS, PHASE_DRAG, PHASE_RELEASE, PHASE_CANCEL, PHASE_FIRE})

KIND_NONE, KIND_VOXEL, KIND_PICKED = 0, 1, 2
KIND_COLUMN_YX, KIND_COLUMN_ZX, KIND_COLUMN_ZY = 3, 4, 5
KIND_RAY, KIND_RAY_AND_PICKED = 6, 7
_KIND_LENGTH = {
    KIND_NONE: 0,
    KIND_VOXEL: 12,
    KIND_PICKED: 12,
    KIND_COLUMN_YX: 16,
    KIND_COLUMN_ZX: 16,
    KIND_COLUMN_ZY: 16,
    KIND_RAY: 32,
    KIND_RAY_AND_PICKED: 48,
}
_PICKED_KINDS = frozenset({KIND_PICKED, KIND_RAY_AND_PICKED})

TARGET_NONE, TARGET_ANNOTATION, TARGET_ROI, TARGET_HANDLE = 0, 1, 2, 3
_KNOWN_TARGET_KINDS = frozenset({TARGET_NONE, TARGET_ANNOTATION, TARGET_ROI, TARGET_HANDLE})

FLAG_MISS = 0x01
FLAG_ROLL_REF_Y = 0x02
_FLAGS_RESERVED = 0xFC

MOD_SHIFT, MOD_CTRL, MOD_ALT, MOD_META = 0x01, 0x02, 0x04, 0x08
_MODS_RESERVED = 0xF0

CHANNEL_HIGH_BIT = 0x8000  # a channel at index >= 15 is also visible

_E_HEAD = struct.Struct("<IIIIIIIIHBBBBBBBB")  # 42 bytes
#: `E` §1 ``action`` at [42..44): 0 on every phase but a FIRE that names one of
#: the tool's declared actions (1-based, `ToolBinding.actions`).  They are how
#: a key reaches the engaged tool mid-drag.
_E_ACTION = struct.Struct("<H")
_E_ACTION_OFFSET = 42
_E_POSITION_OFFSET, _E_POSITION_END = 44, 92
_E_PAD2 = slice(92, 100)
_F3 = struct.Struct("<fff")
_F4 = struct.Struct("<ffff")
_RAY = struct.Struct("<ffffffff")


@dataclass(frozen=True)
class Voxel:
    z: float
    y: float
    x: float


@dataclass(frozen=True)
class Column:
    """A projection column.  ``kind`` says which axis was collapsed: for
    ``KIND_COLUMN_YX`` the coordinates are ``(y, x)`` and the range is ``z``,
    and so on (``KIND_COLUMN_ZX``, ``KIND_COLUMN_ZY``).  The range is
    half-open."""

    kind: int
    a: float
    b: float
    lo: float
    hi: float


@dataclass(frozen=True)
class Ray:
    camera: Voxel
    far: Voxel
    entry_frac: float
    roll: float

    @property
    def entry(self) -> Voxel:
        f = self.entry_frac
        return Voxel(
            self.camera.z + f * (self.far.z - self.camera.z),
            self.camera.y + f * (self.far.y - self.camera.y),
            self.camera.x + f * (self.far.x - self.camera.x),
        )


@dataclass(frozen=True)
class RayAndPicked:
    """The ray, and the host's pick along it (`E` §3).  ``picked`` is the front of
    the structure the host found under the cursor.  ``back_frac`` says how far
    that structure runs BEHIND its front, as a fraction of the ray that is left
    there: the back point is ``front + back_frac * (far - front)``, with
    ``front`` the picked voxel's center projected onto the ray.

    ``0.0`` is a structure of no depth -- a real pick whose back is its front.
    NaN is the other thing entirely: nothing along the ray stood out, the host
    took a likely voxel, and there is no structure to have a back
    (:attr:`is_point`).  A host that predates the field sends zeros, which read
    as the first.
    """

    ray: Ray
    picked: Voxel
    back_frac: float = math.nan

    @property
    def is_point(self) -> bool:
        """Nothing stood out along the ray: ``picked`` is a likely voxel with no
        structure around it.  Not the same as a zero-depth pick (0.0)."""
        return math.isnan(self.back_frac)

    @property
    def front_frac(self) -> float:
        """Where the picked voxel's center projects onto ``camera -> far``, as a
        fraction of it, clamped to the segment."""
        c, e, p = self.ray.camera, self.ray.far, self.picked
        d = (e.z - c.z, e.y - c.y, e.x - c.x)
        dd = d[0] * d[0] + d[1] * d[1] + d[2] * d[2]
        if dd <= 0.0:
            return 0.0
        t = ((p.z - c.z) * d[0] + (p.y - c.y) * d[1] + (p.x - c.x) * d[2]) / dd
        return min(1.0, max(0.0, t))

    @property
    def front(self) -> Voxel:
        """The picked front ON the ray: ``picked``'s center projected onto it."""
        return self._on_ray(self.front_frac)

    @property
    def back(self) -> Voxel | None:
        """Where the picked structure ends, on the ray: ``front + back_frac *
        (far - front)``.  None for a point pick; the front itself for a
        structure of no depth."""
        if self.is_point:
            return None
        t = self.front_frac
        return self._on_ray(t + self.back_frac * (1.0 - t))

    def _on_ray(self, t: float) -> Voxel:
        c, e = self.ray.camera, self.ray.far
        return Voxel(c.z + t * (e.z - c.z), c.y + t * (e.y - c.y), c.x + t * (e.x - c.x))


Position = Union[Voxel, Column, Ray, RayAndPicked]


@dataclass(frozen=True)
class InputEvent:
    #: The dataset open generation -- the LOW 32 BITS of the control plane's
    #: ``uint64`` ``open_generation`` (``E`` §1), which is how the host
    #: composes it; a receiver correlating the two compares those bits.
    gen: int
    t: int
    interaction: int
    seq: int
    epoch: int
    target_id: int
    target_part: int
    tool: int
    channels: int
    phase: int
    view_id: int
    level: int
    mods: int
    click_count: int
    target_kind: int
    payload_kind: int
    position_flags: int
    #: Which of the tool's declared actions a ``FIRE`` names, 1-based in the
    #: manifest's order; 0 is the primary gesture.  A ``FIRE`` naming an action
    #: lands INSIDE whatever gesture is open and neither opens nor closes one.
    action: int
    #: ``None`` for ``payload_kind == NONE`` and for a kind this version does
    #: not define (which is processed as if it were ``NONE``).
    position: Position | None
    #: ``False`` when the SUPERVISOR found the record's context irreconcilable
    #: with arranged state (`E` §3's third terminal row): a ``level`` the
    #: epoch does not declare, a non-``MISS`` voxel outside the clip box, a
    #: ``t`` past the time table.  The record still closes what it closes and
    #: is delivered -- a terminal may not be rejected -- but its coordinate was
    #: not checked and must not be acted on; the host has been told
    #: (``unusable_context``).  Local to this end, never on the wire.
    usable: bool = True

    @property
    def ray(self) -> "Ray | None":
        """The 3D ray this record carries, whether or not a pick came with it
        (``RAY`` or ``RAY_AND_PICKED``); ``None`` for every other kind.  A tool
        reads the ray here and does not branch on which of the two arrived."""
        if isinstance(self.position, RayAndPicked):
            return self.position.ray
        return self.position if isinstance(self.position, Ray) else None

    @property
    def pick(self) -> "RayAndPicked | None":
        """The host's pick along the ray, when one came: ``None`` on a bare
        ``RAY`` -- the tool declared ``wants_pick=False``, or the host found
        nothing to send -- and on every non-ray kind."""
        return self.position if isinstance(self.position, RayAndPicked) else None

    @property
    def is_opening(self) -> bool:
        return self.phase in _OPENING_PHASES

    @property
    def is_terminal(self) -> bool:
        return self.phase in _TERMINAL_PHASES

    @property
    def miss(self) -> bool:
        """The coordinate is real but is not a RELEVANT place.

        ``doorbell_E_input.md`` §3: not the same as not existing.  A ``MISS``
        may lie outside the dataset entirely, or merely outside the crop -- and
        in the second case the voxels are there and a read returns data, since
        the crop bounds what an interaction *means* and never what a plugin may
        read (``control_plane_protocol.md`` §8.11).  A tool painting or sampling
        the current volume skips it exactly as it skips ``NONE``; a tool
        building a selection uses it; a tool deliberately working outside the
        crop is entitled to.  Meaningless -- and reported ``False`` -- when
        there is no coordinate at all.
        """
        return bool(self.position_flags & FLAG_MISS) and self.position is not None

    @property
    def roll_ref_y(self) -> bool:
        return bool(self.position_flags & FLAG_ROLL_REF_Y)

    @property
    def target_known(self) -> bool:
        return self.target_kind in _KNOWN_TARGET_KINDS

    def channel_visible(self, index: int) -> bool:
        """Visibility of channels 0–14 from the record; a channel at index
        >= 15 is epoch state, and this returns only bit 15's summary."""
        if index < 15:
            return bool(self.channels >> index & 1)
        return bool(self.channels & CHANNEL_HIGH_BIT)


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


def _decode_position(kind: int, flags: int, buf: bytes) -> tuple[Position | None, str | None]:
    """Decode the union at ``buf`` (48 bytes).  Returns ``(position, error)``
    where error is ``position_tail`` or ``nonfinite_position``.  An unknown kind
    yields ``(None, None)`` — its extent is unknowable, so nothing is checked."""
    length = _KIND_LENGTH.get(kind)
    if length is None:
        return None, None
    if any(buf[length:]):
        return None, "position_tail"
    if kind == KIND_NONE:
        return None, None
    if kind in (KIND_VOXEL, KIND_PICKED):
        z, y, x = _F3.unpack_from(buf, 0)
        return (Voxel(z, y, x), None) if _finite(z, y, x) else (None, "nonfinite_position")
    if kind in (KIND_COLUMN_YX, KIND_COLUMN_ZX, KIND_COLUMN_ZY):
        a, b, lo, hi = _F4.unpack_from(buf, 0)
        return (
            (Column(kind, a, b, lo, hi), None)
            if _finite(a, b, lo, hi)
            else (None, "nonfinite_position")
        )
    values = _RAY.unpack_from(buf, 0)
    # camera == far destroys the direction and leaves roll uninterpretable, so
    # it is rejected with the range and finiteness cases (E §2 step 5, §3).
    if not _finite(*values) or not 0.0 <= values[6] <= 1.0 or values[0:3] == values[3:6]:
        return None, "nonfinite_position"
    ray = Ray(Voxel(*values[0:3]), Voxel(*values[3:6]), values[6], values[7])
    if kind == KIND_RAY:
        return ray, None
    z, y, x = _F3.unpack_from(buf, 32)
    (back_frac,) = struct.unpack_from("<f", buf, 44)
    # The one NaN with a meaning: nothing stood out, so there is no back.
    # 0.0 is a real pick of no depth -- and what a pre-field sender's zeros read as.
    if not _finite(z, y, x) or not (math.isnan(back_frac) or 0.0 <= back_frac <= 1.0):
        return None, "nonfinite_position"
    return RayAndPicked(ray, Voxel(z, y, x), back_frac), None


def _decode_input(payload: bytes) -> tuple[InputEvent, str | None, list[str]]:
    fields = _E_HEAD.unpack_from(payload, 0)
    kind, flags = fields[15], fields[16]
    (action,) = _E_ACTION.unpack_from(payload, _E_ACTION_OFFSET)
    position, error = _decode_position(kind, flags, payload[_E_POSITION_OFFSET:_E_POSITION_END])
    ev = InputEvent(*fields, action, position)
    diagnostics = []
    if ev.mods & _MODS_RESERVED:
        diagnostics.append("reserved_bit:mods")
    if ev.position_flags & _FLAGS_RESERVED:
        diagnostics.append("reserved_bit:position_flags")
    if kind not in _KIND_LENGTH:
        diagnostics.append(f"unknown_payload_kind:{kind}")
    if ev.target_kind not in _KNOWN_TARGET_KINDS:
        diagnostics.append(f"unknown_target_kind:{ev.target_kind}")
    return ev, error, diagnostics


# ---------------------------------------------------------------------------
# Outcomes (implementation details §6)
# ---------------------------------------------------------------------------


class Disposition(enum.Enum):
    DECODED = "decoded"  # a record the consumer acts on
    UNKNOWN_OP = "unknown_op"  # dropped and counted, never a fault (transport §5)
    SEMANTIC_REJECTION = "semantic_rejection"  # well-formed, content wrong: report and drop
    IGNORED = "ignored"  # counted locally and not acted on (a duplicate, a stale epoch, ...)
    FRAMING_REJECTION = "framing_rejection"  # the frame that revealed a terminal fault


class Descriptor:
    """A descriptor attached to a handed-up ``M``.

    **Claim it inside the callback that receives it.**  The transport closes it
    if and only if it is still unclaimed when that callback returns, so a
    consumer that hands the ``Frame`` to another thread, stores it for later, or
    awaits anything before claiming will find the descriptor already closed —
    and the fd number reused by then, which is why claiming late is worse than
    losing the grant.  There is exactly one close and it is never performed by
    two parties (transport §8).

    If the work must happen elsewhere, call :meth:`claim` first and hand the
    resulting descriptor over; ownership transfers with it, including the
    obligation to close it on every path.
    """

    __slots__ = ("_fd", "_claimed")

    def __init__(self, fd: int):
        self._fd = fd
        self._claimed = False

    @property
    def fd(self) -> int:
        """Peek without taking ownership."""
        return self._fd

    @property
    def claimed(self) -> bool:
        return self._claimed

    def claim(self) -> int:
        """Take ownership.  The caller now owns exactly one close.

        Raises ``RuntimeError`` once the emit has returned, rather than handing
        back a number the transport has already closed and the kernel may have
        reused."""
        if self._claimed:
            raise RuntimeError("descriptor already claimed")
        self._claimed = True
        return self._fd

    def _close_if_unclaimed(self) -> None:
        if not self._claimed:
            self._claimed = True
            _close_quietly(self._fd)


@dataclass
class Frame:
    """One received frame's disposition, with everything the supervisor needs
    to act on it.  ``raw`` is always the bytes examined — the whole record, or
    what was read when the length was wrong."""

    raw: bytes
    ordinal: int
    disposition: Disposition
    header: Header | None = None
    record: MemoryGrant | InputEvent | Notification | None = None
    #: The ``where`` of a ``ContractViolation`` (``E`` §2, ``D`` §4, ``M`` §7's
    #: ``zero_id``) or the reason for a grant failure (``M``, class
    #: ``malformed``): see ``grant_failure``.
    violation: str | None = None
    #: True when ``violation`` rides the grant acknowledgement rather than
    #: ``ContractViolation`` (``M`` §7: everything but ``zero_id``).
    grant_failure: bool = False
    #: Why an ``IGNORED`` frame was ignored, or the failure class of a
    #: ``FRAMING_REJECTION``.
    reason: str | None = None
    diagnostics: tuple[str, ...] = ()
    #: The granted descriptor, on a unix message transport.  Claim it *inside*
    #: the outcome callback or it is closed when that callback returns —
    #: see :class:`Descriptor`.
    descriptor: Descriptor | None = None
    #: An accepted ``PRESS`` names the interaction it implicitly closed
    #: (``E`` §6); no cancellation outcome accompanies it.
    implicitly_closed: int | None = None
    #: An accepted terminal closes its interaction as a completion.
    closed_interaction: int | None = None
    #: Samples of this interaction lost before this record (``E`` §6).
    seq_gap: int = 0


@dataclass(frozen=True)
class Cancellation:
    """An interaction ended with neither its own terminal nor a successor
    announcing it (``E`` §6)."""

    interaction: int
    tool: int
    gen: int
    view_id: int
    reason: str


@dataclass(frozen=True)
class Fault:
    """A terminal transport fault (transport §8, §12).  Every field the plugin
    cannot supply for this class is ``None``, never a plausible zero."""

    failure_class: str
    direction: str
    transport_class: str
    detail: str
    capture: bytes | None = None
    observed_len: int | None = None
    msg_ctrunc: bool | None = None
    ordinal: int | None = None
    os_error: int | None = None
    expected_crc: int | None = None
    observed_crc: int | None = None


@dataclass(frozen=True)
class Teardown:
    """The doorbell ended without a fault: the peer closed, the peer is gone,
    or ``stop()`` was called.  Exactly one of ``Teardown`` or ``Fault`` is
    ever delivered."""

    reason: str  # peer_closed | peer_gone | locally_closed | stopped
    detail: str = ""
    os_error: int | None = None
    #: Bytes buffered when a stream ended mid-record (transport §8).
    partial: bytes | None = None


Outcome = Union[Frame, Cancellation, Fault, Teardown]


class SendResult(enum.Enum):
    SENT = "sent"  # accepted for transmission — never evidence of delivery
    REFUSED = "refused"  # backpressure; dropped and counted (the record is disposable)
    SUPPRESSED = "suppressed"  # an all-zero mask, not worth a record
    CLOSED = "closed"  # the transport has ended; nothing is sent after that
    NOT_READY = "not_ready"  # the handshake has not settled the contract yet (§3.1a)


class DoorbellSetupError(RuntimeError):
    """A launch failure: the doorbell could not be adopted or connected."""


class ProtocolMismatch(DoorbellSetupError):
    """The peer's platform protocol version is not ours.  A mismatch fails the
    launch whole, naming both versions — it is never partially accepted
    (``plugin_principles.md``, Version skew)."""


# ---------------------------------------------------------------------------
# errno classification (transport §6 rule 6/7, §7 rule 5)
# ---------------------------------------------------------------------------


def _errnos(*names: str) -> frozenset[int]:
    return frozenset(v for v in (getattr(errno, n, None) for n in names) if v is not None)


# Both spellings of every Winsock condition, because which one arrives depends
# on the interpreter rather than the protocol.  CPython raises socket errors on
# Windows through `PyErr_SetExcFromWindowsErr`, which puts the raw `WSAE*` code
# on `winerror` and its POSIX translation on `errno` — so `WSAECONNABORTED`
# reaches us as `ECONNABORTED`, not as 10053.  Matching only the 100xx literals
# would classify an ordinary Windows teardown, and our own `shutdown()`, as
# faults; §6 rule 7 names both as the mistakes to avoid.
_WSAEWOULDBLOCK, _WSAENOBUFS, _WSAESHUTDOWN = 10035, 10055, 10058
_WSAECONNABORTED, _WSAECONNRESET = 10053, 10054

_BACKPRESSURE = _errnos("EAGAIN", "EWOULDBLOCK", "ENOBUFS", "ETOOMANYREFS") | {
    _WSAEWOULDBLOCK,
    _WSAENOBUFS,
}
_WOULD_BLOCK = _errnos("EAGAIN", "EWOULDBLOCK") | {_WSAEWOULDBLOCK}
_PEER_GONE = _errnos(
    "EPIPE", "ECONNREFUSED", "ECONNRESET", "ECONNABORTED", "EDESTADDRREQ", "ENOTCONN"
) | {_WSAECONNRESET, _WSAECONNABORTED}
#: Our own `shutdown()` — §10's escape hatch doing its job.  Never attributed
#: to the peer: a classifier keyed on it reports a dead host for a state we
#: caused.
_LOCALLY_CLOSED = _errnos("ESHUTDOWN") | {_WSAESHUTDOWN}
_EINTR = _errnos("EINTR")


def _codes(exc: OSError) -> tuple[int | None, int | None]:
    """The error's POSIX errno and its raw Windows code, either of which may be
    the one a given interpreter supplies."""
    return exc.errno, getattr(exc, "winerror", None)


def _is(exc: OSError, table: frozenset[int]) -> bool:
    return any(code in table for code in _codes(exc) if code is not None)


def _raw_code(exc: OSError) -> int:
    """The code a diagnostic should carry.  §12 asks for the **raw** errno or
    WSA code, so the Winsock value wins where there is one: its POSIX
    translation is what CPython chose, not what the OS reported.  ``0`` where
    there is no OS error, which is the field's stated "none"."""
    win = getattr(exc, "winerror", None)
    if win is not None:
        return win
    return exc.errno if exc.errno is not None else 0


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------


@dataclass
class _Interaction:
    tool: int
    gen: int
    view_id: int
    #: The 112 bytes of the record that opened it.  A duplicate is an *exact
    #: replay*, not merely a familiar id (``E`` §2), and a retry reuses its
    #: ``id`` unchanged (transport §4) — so the whole record compares equal.
    opening_raw: bytes
    last_seq: int = 0


class DoorbellTransport:
    """One plugin session's doorbell.

    ``on_outcome`` is called on the listener thread with every ``Frame``,
    ``Cancellation``, and finally exactly one ``Fault`` or ``Teardown``.  It
    should return within a bound the SDK documents; a slow callback stops the
    socket draining, which is the designed backpressure (``doorbell_principles``
    D1), but it also delays every grant queued behind it.

    Faults reach the supervisor by a second, dedicated route.  ``.faults`` is a
    non-blocking queue the supervisor drains to put ``DoorbellFault`` on the
    control plane, and the socket is released once the fault has been *accepted
    there* — never waiting on ``on_outcome``, which may be blocked in a slow
    handler, and never on gRPC delivery (transport §8).  The same fault is also
    delivered to ``on_outcome`` as the session's one terminal outcome, so a
    consumer that only watches outcomes still sees it.

    **The two are views of one fault, not two faults**, and the guarantee is
    identity: the object taken from ``.faults`` *is* the one the outcome stream
    carries.  Exactly one ``DoorbellFault`` goes on the control plane per
    terminal event, so a supervisor watching both routes must report the fault
    once — two reports would give the Plugin Manager two diagnostics for one
    event and invite a reader to treat the second as a new defect.

    ``fault_sink``, if given, is called with the fault the moment it is raised,
    on whichever thread raised it.  It **must not block**: it is the hand-off
    the socket release waits on.  ``handup_deadline`` is the escape hatch for a
    sink that blocks anyway, and is SDK policy rather than protocol.
    """

    def __init__(
        self,
        sock: socket.socket,
        transport_class: TransportClass,
        on_outcome: Callable[[Outcome], None],
        *,
        fault_sink: Callable[[Fault], None] | None = None,
        handup_deadline: float = 5.0,
        session_token: str | None = None,
        name: str = "doorbell",
    ):
        if transport_class not in _SOCK_TYPE_FOR_CLASS:
            raise ValueError(f"unknown transport class {transport_class!r}")
        self._sock = sock
        self._class: TransportClass = transport_class
        self._message = transport_class != "stream"
        self._on_outcome = on_outcome
        self._fault_sink = fault_sink
        self._handup_deadline = handup_deadline
        #: The token this connection authenticated with, on transports that use
        #: one.  The host echoes it in ``Hello`` and a plugin seeing a different
        #: one fails the session (§3.2 step 8).
        self._session_token = session_token
        self._name = name

        #: The dedicated fault hand-off (transport §8).  Never blocks a put.
        self.faults: "queue.SimpleQueue[Fault]" = queue.SimpleQueue()

        sock.setblocking(False)  # set once, at setup, on every platform (§6 rule 3)

        self._lock = threading.Lock()  # the one serialization for state + outcome order
        # One serialized writer per direction (§6 rule 1).  Reentrant because a
        # send that faults raises the terminal from inside its own critical
        # section; the terminal latch takes this lock so that no record can
        # reach a channel a fault has already closed.
        self._send_lock = threading.RLock()
        self._outcomes: collections.deque[Outcome] = collections.deque()
        self._terminal: Fault | Teardown | None = None
        #: Set once the terminal outcome has been queued.  Nothing joins the
        #: sequence behind it: the terminal is the last thing a consumer sees.
        self._sealed = False
        self._stop_requested = False
        #: No record may be sent before the handshake completes (§3.1a).  The
        #: version is what selects the framing, so sending beforehand would be
        #: guessing which format the peer reads.
        self._ready = False
        self._finished = threading.Event()
        self._thread: threading.Thread | None = None
        self._deadline_timer: threading.Timer | None = None

        # A loopback socket pair is the wake endpoint on every platform: it is
        # what select() can wait on beside the doorbell (§6 rule 3).
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)

        # receive side
        self._ordinal = 0
        self._expected_next = 1
        self._stream_buf = b""
        self._recv_flags = getattr(socket, "MSG_CMSG_CLOEXEC", 0)
        self._anc_space = socket.CMSG_SPACE(2 * 4) if hasattr(socket, "CMSG_SPACE") else 0

        # send side
        self._next_send_id = 1
        self._send_flags = getattr(socket, "MSG_DONTWAIT", 0) | getattr(socket, "MSG_NOSIGNAL", 0)

        # E state (doorbell_E_input.md §2, §6)
        self._watermark = 0
        self._index: dict[int, _Interaction] = {}
        self._tool_open: dict[int, int] = {}
        self._active_tools: set[int] = set()
        self._high_water = 0
        self._epochs: dict[int, int] = {}

        self.counters: collections.Counter[str] = collections.Counter()

    # -- lifecycle ---------------------------------------------------------

    @property
    def transport_class(self) -> TransportClass:
        return self._class

    def start(self) -> None:
        """Start the listener thread.  Do this before signaling readiness to
        the host — grants ride session setup and can arrive immediately."""
        if self._thread is not None:
            raise RuntimeError("already started")
        if self._terminal is not None:
            raise RuntimeError("cannot start a transport that has already ended")
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Request cancellation.  Returns as soon as the request is recorded
        **and the socket released** (transport §6 rule 3) — it is not a join,
        and it does not wait on a listener that may be inside a slow consumer
        callback.  A ``Teardown('stopped')`` follows unless a terminal event
        already latched.

        Releasing here rather than on the listener thread is what makes the
        bound real: a listener blocked in a callback would otherwise leave the
        socket open and sends flowing for as long as that callback ran."""
        with self._lock:
            self._stop_requested = True
            started = self._thread is not None
        self._publish_terminal(Teardown("stopped"))
        if not started:
            # Nothing will ever drain or close on our behalf, so do both here.
            # open_doorbell() hands back an unstarted transport on purpose, and
            # a caller that abandons it still needs its descriptors released.
            self._drain()
            self._close_resources()
            self._finished.set()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the listener to finish.  Bounded only by the consumer."""
        return self._finished.wait(timeout)

    @property
    def terminal(self) -> Fault | Teardown | None:
        """The terminal outcome once one has latched, whether or not it has
        been delivered yet."""
        with self._lock:
            return self._terminal

    # -- sending -----------------------------------------------------------

    def handshake_complete(self, major: int, minor: int, session_token: str | None = None) -> None:
        """Record that ``Hello``/``Ready`` settled the contract, unblocking
        sends.  This is not negotiation: **the major must match**, and a
        difference fails the launch whole rather than degrading (§3.1a).  Call
        it before publishing readiness.

        A differing *minor* is accepted from ``1.0`` on, because
        `control_plane_protocol.md` §3.2 makes a minor bump backward-compatible
        and a major bump the only breaking one — and a minor may therefore not
        change the framing, which is the constraint that makes this safe rather
        than an assumption about the peer.  **Within ``0.x`` the match stays
        exact**: twelve of those minors were allowed to change behavior and
        did, so the promise cannot be applied backwards to peers that genuinely
        disagree.

        On a token-authenticated transport, pass the token the session stream
        proved (§3.2 step 8): the stream is authenticated before it is
        accepted, so a stream with a wrong token never gets here, and a
        mismatch at this point means the two channels disagree about the
        session -- a session failure rather than a launch failure, because the
        doorbell is already open by then."""
        if not compatible(major, minor):
            raise ProtocolMismatch(
                f"peer speaks protocol {major}.{minor}; this doorbell is "
                f"{PROTOCOL_MAJOR}.{PROTOCOL_MINOR} (v{RECORD_VERSION} framing)"
            )
        if self._session_token is None:
            if session_token is not None:
                raise DoorbellSetupError("this transport authenticated with no token")
        else:
            # Constant time: the token is the capability that gates the
            # connection, so a comparison must not leak it a byte at a time.
            if session_token is None or not hmac.compare_digest(session_token, self._session_token):
                self._publish_terminal(
                    Teardown("handshake_mismatch", "Hello echoed a different session token")
                )
                raise DoorbellSetupError(
                    "Hello.session_token does not match the token this connection used"
                )
        with self._lock:
            if self._terminal is not None:
                raise DoorbellSetupError("the doorbell has already ended")
            self._ready = True

    @property
    def ready(self) -> bool:
        """Whether the handshake has settled the contract."""
        with self._lock:
            return self._ready

    def send_notification(
        self,
        segment_id: int,
        regions: Iterable[int] = (),
        epoch: int = 0,
        view_id: int = 0,
        *,
        mask: int | None = None,
    ) -> SendResult:
        """Send a ``D``: look at these regions of this segment now.  Disposable —
        refused means dropped and counted, never retried (transport §11a).

        ``regions`` is an iterable of region INDICES and never a mask: a bare
        ``int`` is a ``TypeError``.  Were it either, a caller passing
        ``region.index`` would have it read as a ready-made mask -- so region 0
        would ring nothing (``SUPPRESSED``) and region 1 would ring region 0.
        The value still arrives, because the host's scan needs no hint; only
        the latency the hint buys is lost.  A ready-made mask goes behind
        ``mask=``, where it cannot be mistaken for an index.

        Refused as ``NOT_READY`` until :meth:`handshake_complete`, since no
        record may be sent before the handshake (§3.1a)."""
        if isinstance(regions, int):
            raise TypeError("regions is an iterable of region indices; a ready-made mask is mask=")
        if mask is not None:
            if tuple(regions):
                raise TypeError("pass region indices or mask=, not both")
            regions = mask
        payload = encode_notification_payload(segment_id, regions, epoch, view_id)
        if not any(payload[_D_MASK_OFFSET:_D_MASK_END]):
            self.counters["send_suppressed_empty_mask"] += 1
            return SendResult.SUPPRESSED
        return self._send(OP_NOTIFY, payload)

    def _send(self, op: int, payload: bytes) -> SendResult:
        with self._send_lock:
            with self._lock:
                if self._terminal is not None:
                    return SendResult.CLOSED
                if not self._ready:
                    # Nothing is admitted, so nothing burns an id: the record
                    # was never offered to the writer (§4).
                    self.counters["send_before_handshake"] += 1
                    return SendResult.NOT_READY
                # Allocated on admission, inside the writer's serialization, so
                # ids reach the wire in allocation order and every admitted
                # record burns one — sent or refused (§4).
                record_id = self._next_send_id
                self._next_send_id = (record_id + 1) & _U32_MASK
            record = encode_record(op, record_id, payload)
            while True:
                try:
                    n = self._sock.send(record, self._send_flags)
                except InterruptedError:
                    continue  # nothing was transferred; re-issuing is exact
                except OSError as exc:
                    return self._classify_send_error(exc)
                break
            if n != RECORD_SIZE:
                self._publish_terminal(
                    Fault(
                        "short_write",
                        "plugin_to_host",
                        self._class,
                        f"send accepted {n} of {RECORD_SIZE} bytes",
                        observed_len=n,
                        os_error=0,  # a short write is not an OS error; §12's "0 if none"
                    )
                )
                return SendResult.CLOSED
            self.counters["send_sent"] += 1
            return SendResult.SENT

    def _classify_send_error(self, exc: OSError) -> SendResult:
        code = _raw_code(exc)
        if _is(exc, _BACKPRESSURE):
            self.counters["send_refused"] += 1
            return SendResult.REFUSED
        if _is(exc, _LOCALLY_CLOSED):
            # Checked before peer-gone: this is our own shutdown, and the
            # released sender must not report its own rescue as the peer dying.
            self._publish_terminal(Teardown("locally_closed", f"send: {exc}", os_error=code))
        elif _is(exc, _PEER_GONE):
            self._publish_terminal(Teardown("peer_gone", f"send: {exc}", os_error=code))
        else:
            self._publish_terminal(
                Fault(
                    "ambiguous_send", "plugin_to_host", self._class, f"send: {exc}", os_error=code
                )
            )
        return SendResult.CLOSED

    # -- lifecycle inputs (implementation details §6) ----------------------

    def tool_activated(self, handle: int) -> bool:
        """Register an activation.  MUST be called before the activation is
        acknowledged over gRPC, and in increasing-handle order."""
        with self._lock:
            if self._terminal is not None or handle == 0:
                return False
            if handle > self._high_water:
                self._high_water = handle
                self._active_tools.add(handle)
                return True
            return handle in self._active_tools  # a live repeat is a no-op

    def tool_retired(self, handle: int) -> bool:
        """Purge the tool and cancel its open gesture, if any.

        Idempotent in the direction that matters: a handle at or below the
        high-water mark but not active — already retired, or a gap that was
        never registered — succeeds, because the state it asks for already
        holds.  ``handle == 0`` and a handle above the mark fail without
        mutating.  Retirement never advances the mark."""
        with self._lock:
            if self._terminal is not None or handle == 0 or handle > self._high_water:
                return False
            if handle not in self._active_tools:
                return True
            self._active_tools.discard(handle)
            n = self._tool_open.get(handle)
            outs: list[Outcome] = []
            if n is not None:
                self._cancel(n, "tool_retired", outs)
            self._append(outs)
        self._wake()
        return True

    def epoch_acknowledged(self, view_id: int, epoch: int) -> bool:
        """Install a pane epoch.  MUST be called before the gRPC acknowledgement
        is sent.  Newer installs and closes every gesture open in that pane;
        equal is a no-op; older is rejected — it never rewinds.  Epoch ``0`` is
        the "no epoch" sentinel and is refused: there is no such epoch to have
        acknowledged, and installing it would rewind every pane to a state no
        record can legally carry.

        Note for the layer above: installing is synchronous while the resulting
        cancellations are drained later, so a plugin that lets a tool *commit* a
        straddling stroke must retain the superseded geometry until those
        cancellations have been delivered (``E`` §1a).  Otherwise two plugins
        commit the same stroke against different geometry."""
        if epoch == 0:
            return False
        with self._lock:
            if self._terminal is not None:
                return False
            installed = self._epochs.get(view_id)
            if installed is not None and epoch <= installed:
                return epoch == installed
            self._epochs[view_id] = epoch
            outs: list[Outcome] = []
            for n, entry in list(self._index.items()):
                if entry.view_id == view_id:
                    self._cancel(n, "epoch_installed", outs)
            self._append(outs)
        self._wake()
        return True

    def cancel_interaction(self, interaction: int) -> bool:
        """Retract an interaction the supervisor rejected upstream.  Returns
        ``False`` when there was nothing to cancel."""
        with self._lock:
            if self._terminal is not None or interaction not in self._index:
                return False
            outs: list[Outcome] = []
            self._cancel(interaction, "cancel_interaction", outs)
            self._append(outs)
        self._wake()
        return True

    def cancel_interactions(self, view_id: int) -> int:
        """Cancel every interaction open in a pane that is going away."""
        with self._lock:
            if self._terminal is not None:
                return 0
            outs: list[Outcome] = []
            for n, entry in list(self._index.items()):
                if entry.view_id == view_id:
                    self._cancel(n, "pane_cancelled", outs)
            self._append(outs)
        if outs:
            self._wake()
        return len(outs)

    # -- state helpers (call with self._lock held) -------------------------

    def _cancel(self, interaction: int, reason: str, outs: list[Outcome]) -> None:
        entry = self._index.pop(interaction, None)
        if entry is None:
            return
        if self._tool_open.get(entry.tool) == interaction:
            del self._tool_open[entry.tool]
        outs.append(Cancellation(interaction, entry.tool, entry.gen, entry.view_id, reason))

    def _append(self, outs: Iterable[Outcome], *, final: bool = False) -> None:
        """Queue outcomes.  The terminal seals the sequence: a frame the
        listener was already decoding when a fault latched on another thread
        must not be delivered behind it, since the terminal is by definition
        the last thing a consumer sees (transport §8)."""
        batch = list(outs)
        if self._sealed:
            self.counters["outcomes_after_terminal"] += len(batch)
            return
        self._outcomes.extend(batch)
        if final:
            self._sealed = True

    def _publish_terminal(self, outcome: Fault | Teardown) -> bool:
        """The first terminal event latches: both directions close to new
        traffic and exactly one terminal outcome is queued (transport §8).
        Open interactions are canceled first so no gesture is stranded.

        The order is the one §8 fixes: close the channel to further traffic,
        then report, then release the socket — and the middle step cannot undo
        the first.  Taking the writer's lock is what closes the channel: a send
        already past its own check holds it, so it completes before the latch
        rather than putting a record onto a channel this fault has closed."""
        with self._send_lock:
            with self._lock:
                if self._terminal is not None:
                    return False
                self._terminal = outcome
                outs: list[Outcome] = []
                for n in list(self._index):
                    self._cancel(n, "transport_ended", outs)
                outs.append(outcome)
                self._append(outs, final=True)
                self.counters[
                    "terminal:"
                    + (outcome.failure_class if isinstance(outcome, Fault) else outcome.reason)
                ] += 1
        # The escape hatch, armed before the hand-off so that a sink which
        # blocks in defiance of its contract still releases the socket.
        timer = threading.Timer(self._handup_deadline, self._deadline_expired)
        timer.daemon = True
        self._deadline_timer = timer
        timer.start()
        if isinstance(outcome, Fault):
            self._hand_up_fault(outcome)
        # Released once the report has been accepted — never behind a consumer
        # callback, which may be blocked in a slow handler.  The descriptor
        # rules make this safe: anything still inside an emit is abandoned
        # rather than closed, since the consumer may already have claimed it.
        self._release_socket()
        timer.cancel()
        self._wake()
        return True

    def _hand_up_fault(self, fault: Fault) -> None:
        """Publish the fault to the dedicated sink.  The queue put cannot
        block; a supplied callback is contractually non-blocking and its
        failure must not stop the release."""
        self.faults.put(fault)
        if self._fault_sink is not None:
            try:
                self._fault_sink(fault)
            except Exception:  # noqa: BLE001 — a broken sink must not wedge teardown
                self.counters["fault_sink_errors"] += 1

    def _release_socket(self) -> None:
        """Release the socket to the peer.  ``shutdown`` rather than ``close``:
        the listener may still be selecting on this descriptor, and closing an
        fd another thread waits on is what the wake endpoint exists to avoid.
        The close itself happens when the listener exits."""
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # the peer is already gone, or we are not connected

    def _deadline_expired(self) -> None:
        if not self._finished.is_set():
            self.counters["handup_deadline_expired"] += 1
            self._release_socket()

    def _wake(self) -> None:
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass  # a wake is already pending, or we are shutting down

    # -- the listener ------------------------------------------------------

    def _run(self) -> None:
        # A selector, not select(): select() takes descriptor NUMBERS below
        # FD_SETSIZE (1024), and a plugin holding many files open -- or a host
        # with many sessions -- gets a doorbell socket above that, which
        # select() refuses, ending the session.
        selector = selectors.DefaultSelector()
        try:
            try:
                selector.register(self._sock, selectors.EVENT_READ)
                selector.register(self._wake_r, selectors.EVENT_READ)
            except (OSError, ValueError) as exc:
                self._publish_terminal(
                    Fault(
                        "recv_error",
                        "host_to_plugin",
                        self._class,
                        f"select: {exc}",
                        os_error=_raw_code(exc),
                    )
                )
                self._drain()
                return
            while True:
                with self._lock:
                    ended = self._terminal is not None or self._stop_requested
                if ended:
                    # stop() publishes and releases on the caller's thread, so
                    # by here there is nothing left to do but drain.
                    self._publish_terminal(Teardown("stopped"))
                    break
                try:
                    readable = [key.fileobj for key, _ in selector.select()]
                except (OSError, ValueError) as exc:
                    self._publish_terminal(
                        Fault(
                            "recv_error",
                            "host_to_plugin",
                            self._class,
                            f"select: {exc}",
                            os_error=_raw_code(exc),
                        )
                    )
                    break
                if self._wake_r in readable:
                    try:
                        self._wake_r.recv(4096)
                    except OSError:
                        pass
                if self._sock in readable:
                    self._receive_once()
                self._drain()  # the per-frame drains happen inside _process
            self._drain()  # deliver the terminal outcome, bounded by the deadline timer
        finally:
            selector.close()
            self._close_resources()
            self._finished.set()

    def _close_resources(self) -> None:
        timer = self._deadline_timer
        if timer is not None:
            timer.cancel()
        self._stream_buf = b""
        for sock in (self._sock, self._wake_r, self._wake_w):
            try:
                sock.close()
            except OSError:
                pass

    def _drain(self) -> None:
        """Deliver queued outcomes, outside the serialization, in order."""
        while True:
            with self._lock:
                if not self._outcomes:
                    return
                outcome = self._outcomes.popleft()
            try:
                self._on_outcome(outcome)
            except Exception:  # noqa: BLE001 — a raising consumer must not kill the listener
                self.counters["consumer_errors"] += 1
            finally:
                if isinstance(outcome, Frame) and outcome.descriptor is not None:
                    outcome.descriptor._close_if_unclaimed()

    def _receive_once(self) -> None:
        if self._message:
            self._receive_message()
        else:
            self._receive_stream()

    def _classify_recv_error(self, exc: OSError) -> None:
        code = _raw_code(exc)
        if _is(exc, _EINTR) or _is(exc, _WOULD_BLOCK):
            return  # nothing consumed; resume polling
        if _is(exc, _LOCALLY_CLOSED):
            self._publish_terminal(Teardown("locally_closed", f"recv: {exc}", os_error=code))
        elif _is(exc, _PEER_GONE):
            self._publish_terminal(Teardown("peer_gone", f"recv: {exc}", os_error=code))
        else:
            self._publish_terminal(
                Fault(
                    "recv_error",
                    "host_to_plugin",
                    self._class,
                    f"recv: {exc}",
                    ordinal=self._ordinal,
                    os_error=code,
                )
            )

    def _receive_message(self) -> None:
        try:
            data, ancdata, msg_flags, _ = self._sock.recvmsg(
                RECV_BUFFER, self._anc_space, self._recv_flags
            )
        except OSError as exc:
            self._classify_recv_error(exc)
            return
        fds: list[int] = []
        for level, ctype, cdata in ancdata:
            if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
                usable = len(cdata) - len(cdata) % 4
                fds.extend(struct.unpack(f"{usable // 4}i", cdata[:usable]))
        if not self._recv_flags:
            for fd in fds:  # Darwin: no atomic form exists; close the window as fast as we can
                os.set_inheritable(fd, False)
        ctrunc = bool(msg_flags & socket.MSG_CTRUNC)
        n = len(data)
        if n == 0 and not fds and not ctrunc:
            if self._class == "seqpacket":
                self._publish_terminal(Teardown("peer_closed", "0-byte read on SOCK_SEQPACKET"))
                return
            # SOCK_DGRAM: a closed peer never reads as 0, so this is an empty
            # datagram from a live peer — the guarantee broken (§8).
        self._process(data, fds, ctrunc)

    def _receive_stream(self) -> None:
        try:
            chunk = self._sock.recv(4096)
        except OSError as exc:
            self._classify_recv_error(exc)
            return
        if not chunk:
            partial = self._stream_buf or None
            self._stream_buf = b""
            self._publish_terminal(Teardown("peer_closed", "end of stream", partial=partial))
            return
        self._stream_buf += chunk
        while len(self._stream_buf) >= RECORD_SIZE:
            with self._lock:
                if self._terminal is not None:
                    return  # a fault stops both directions; nothing after it is read
            frame, self._stream_buf = self._stream_buf[:RECORD_SIZE], self._stream_buf[RECORD_SIZE:]
            self._process(frame, [], False)

    # -- validation (transport §7.5) ---------------------------------------

    def _fault(
        self, failure_class: str, raw: bytes, fds: list[int], ctrunc: bool, detail: str, **extra
    ) -> None:
        for fd in fds:
            _close_quietly(fd)
        crc_fields = {}
        if len(raw) >= RECORD_SIZE:
            crc_fields = dict(
                expected_crc=crc32(raw[: RECORD_SIZE - 4]),
                observed_crc=_CRC.unpack_from(raw, RECORD_SIZE - 4)[0],
            )
        fault = Fault(
            failure_class,
            "host_to_plugin",
            self._class,
            detail,
            capture=bytes(raw),
            observed_len=len(raw),
            msg_ctrunc=ctrunc,
            ordinal=self._ordinal,
            **crc_fields,
            **extra,
        )
        with self._lock:
            header = None
            if len(raw) >= _HEADER.size and failure_class not in ("bad_crc", "bad_length"):
                header = Header(*_HEADER.unpack_from(raw, 0))
            self._append(
                [
                    Frame(
                        bytes(raw),
                        self._ordinal,
                        Disposition.FRAMING_REJECTION,
                        header,
                        reason=failure_class,
                    )
                ]
            )
        self._publish_terminal(fault)

    def _process(self, raw: bytes, fds: list[int], ctrunc: bool) -> None:
        # Drain before each frame rather than once per readiness edge: a
        # cancellation a lifecycle input queued mid-burst would otherwise sit
        # behind every frame still buffered, and the consumer would meanwhile
        # see records for an interaction it has not been told was canceled —
        # which reads exactly like the loss §6 makes visible, and is not.
        self._drain()
        with self._lock:
            if self._terminal is not None:
                # A fault stops both directions before anyone is told, so a
                # frame decoded after the latch reaches neither the consumer
                # nor E's state.
                self.counters["frames_after_terminal"] += 1
                for fd in fds:
                    _close_quietly(fd)
                return
        self._ordinal += 1
        n = len(raw)
        # Step 1: the frame, its length, and its ancillary data.
        if ctrunc:
            return self._fault(
                "ctrunc", raw, fds, ctrunc, "control message truncated: descriptors were lost"
            )
        if len(fds) > 1:
            return self._fault(
                "wrong_fd_count",
                raw,
                fds,
                ctrunc,
                f"{len(fds)} descriptors; the protocol-wide cap is one",
            )
        if n != RECORD_SIZE:
            return self._fault(
                "bad_length", raw, fds, ctrunc, f"datagram of {n} bytes is not a record"
            )
        # Step 2: CRC before any field is trusted.
        expected = crc32(raw[: RECORD_SIZE - 4])
        observed = _CRC.unpack_from(raw, RECORD_SIZE - 4)[0]
        if expected != observed:
            return self._fault(
                "bad_crc", raw, fds, ctrunc, f"crc {observed:#010x} != {expected:#010x}"
            )
        header = Header(*_HEADER.unpack_from(raw, 0))
        # Step 3: op-independent header fields.
        if header.ver != RECORD_VERSION:
            return self._fault("bad_ver", raw, fds, ctrunc, f"ver {header.ver} != {RECORD_VERSION}")
        if header.flags != 0:
            return self._fault("bad_flags", raw, fds, ctrunc, f"flags {header.flags:#04x}")
        if header.reserved != 0:
            return self._fault("bad_reserved", raw, fds, ctrunc, f"reserved {header.reserved:#04x}")
        diagnostics = self._track_id(header.id)
        # Step 4: the opcode.  A plugin has no illegal inbound direction.
        if header.op == OP_RESERVED:
            return self._fault(
                "reserved_op", raw, fds, ctrunc, "op 0xFF outside a negotiated recovery"
            )
        decoder = _DECODERS.get(header.op)
        if decoder is None:
            for fd in fds:
                _close_quietly(fd)
            self.counters["unknown_op"] += 1
            self._emit(
                Frame(
                    raw,
                    self._ordinal,
                    Disposition.UNKNOWN_OP,
                    header,
                    reason=f"op:{header.op:#04x}",
                    diagnostics=tuple(diagnostics),
                )
            )
            return
        # Step 5: the op's declared descriptor count on this transport class.
        declared = 1 if (header.op == OP_MEMORY and self._message) else 0
        if len(fds) != declared:
            return self._fault(
                "wrong_fd_count",
                raw,
                fds,
                ctrunc,
                f"op {chr(header.op)} carried {len(fds)} descriptors, declared {declared}",
            )
        # Step 6: the op's fixed padding.
        payload = raw[_HEADER.size : _HEADER.size + PAYLOAD_SIZE]
        for lo, hi in _FIXED_PADDING[header.op]:
            if any(payload[lo:hi]):
                return self._fault(
                    "bad_padding",
                    raw,
                    fds,
                    ctrunc,
                    f"non-zero byte in fixed padding [{lo}..{hi}) of {chr(header.op)}",
                )
        # Step 7: the op's own rules.  Never a fault from here on.
        decoder(self, raw, header, payload, fds, diagnostics)

    def _track_id(self, record_id: int) -> list[str]:
        """Diagnostic only; no receiver drops a record on ``id`` (§4)."""
        delta = (record_id - self._expected_next) & _U32_MASK
        if delta == 0:
            self._expected_next = (record_id + 1) & _U32_MASK
            return []
        if delta < _ID_HALF:
            self._expected_next = (record_id + 1) & _U32_MASK
            self.counters["id_gap_records"] += delta
            self.counters["id_gaps"] += 1
            return [f"id_gap:{delta}"]
        self.counters["id_regressions"] += 1
        return ["id_regression"]

    def _emit(self, *outs: Outcome) -> None:
        with self._lock:
            self._append(outs)

    # -- op decoders (step 7) ----------------------------------------------

    def _decode_m(
        self, raw: bytes, header: Header, payload: bytes, fds: list[int], diagnostics: list[str]
    ) -> None:
        grant, rejection, more = _decode_memory(payload, self._class)
        diagnostics += more
        for d in more:
            self.counters["m_" + d.split(":")[0]] += 1
        if rejection is not None:
            # A record-locally rejected M is never handed up claimable: its
            # descriptor is closed before the rejection is returned (M §7).
            for fd in fds:
                _close_quietly(fd)
            self.counters["violations:" + rejection] += 1
            self._emit(
                Frame(
                    raw,
                    self._ordinal,
                    Disposition.SEMANTIC_REJECTION,
                    header,
                    grant,
                    violation=rejection,
                    grant_failure=rejection != "zero_id",
                    diagnostics=tuple(diagnostics),
                )
            )
            return
        descriptor = Descriptor(fds[0]) if fds else None
        self._emit(
            Frame(
                raw,
                self._ordinal,
                Disposition.DECODED,
                header,
                grant,
                diagnostics=tuple(diagnostics),
                descriptor=descriptor,
            )
        )

    def _decode_d(
        self, raw: bytes, header: Header, payload: bytes, fds: list[int], diagnostics: list[str]
    ) -> None:
        note, rejection, more = _decode_notification(payload)
        diagnostics += more
        for d in more:
            self.counters["d_" + d] += 1
        if rejection is not None:
            self.counters["violations:" + rejection] += 1
            self._emit(
                Frame(
                    raw,
                    self._ordinal,
                    Disposition.SEMANTIC_REJECTION,
                    header,
                    note,
                    violation=rejection,
                    diagnostics=tuple(diagnostics),
                )
            )
        elif note.mask == 0:
            self.counters["d_empty_mask"] += 1
            self._emit(
                Frame(
                    raw,
                    self._ordinal,
                    Disposition.IGNORED,
                    header,
                    note,
                    reason="empty_mask",
                    diagnostics=tuple(diagnostics),
                )
            )
        else:
            self._emit(
                Frame(
                    raw,
                    self._ordinal,
                    Disposition.DECODED,
                    header,
                    note,
                    diagnostics=tuple(diagnostics),
                )
            )

    def _decode_e(
        self, raw: bytes, header: Header, payload: bytes, fds: list[int], diagnostics: list[str]
    ) -> None:
        ev, position_error, more = _decode_input(payload)
        diagnostics += more
        for d in more:
            self.counters["e_" + d.split(":")[0]] += 1
        with self._lock:
            outs: list[Outcome] = []
            self._process_input(raw, header, ev, position_error, diagnostics, outs)
            self._append(outs)

    # -- E §2's ordered algorithm (call with self._lock held) --------------

    def _process_input(
        self,
        raw: bytes,
        header: Header,
        ev: InputEvent,
        position_error: str | None,
        diagnostics: list[str],
        outs: list[Outcome],
    ) -> None:
        def ignore(reason: str) -> None:
            self.counters["input_ignored:" + reason] += 1
            outs.append(
                Frame(
                    raw,
                    self._ordinal,
                    Disposition.IGNORED,
                    header,
                    ev,
                    reason=reason,
                    diagnostics=tuple(diagnostics),
                )
            )

        def reject(where: str) -> None:
            self.counters["violations:" + where] += 1
            frame = Frame(
                raw,
                self._ordinal,
                Disposition.SEMANTIC_REJECTION,
                header,
                ev,
                violation=where,
                diagnostics=tuple(diagnostics),
            )
            outs.append(frame)
            if ev.is_opening:
                # A rejected PRESS still performs the implicit close its
                # acceptance would have.  Duplicates and stale-epoch records
                # never reach here — they are ignored, not rejected, which is
                # what keeps a live gesture safe from a record carrying no
                # news.  FIRE never opens or closes anything.
                if ev.phase == PHASE_PRESS:
                    self._implicit_close(ev.tool, "implicit_close_rejected_press", outs)
            else:
                # Cancel the interaction the record identifies; if none, the
                # open gesture of the tool it identifies; if neither, nothing.
                if ev.interaction != 0:
                    self._cancel(ev.interaction, "rejected_record", outs)
                elif ev.tool != 0 and ev.tool in self._active_tools:
                    self._implicit_close(ev.tool, "rejected_record", outs)

        # 1. phase — every check below depends on whether this record opens.
        if ev.phase not in _KNOWN_PHASES:
            return ignore("unknown_phase")
        opening = ev.is_opening
        # 2. Opening records only: the open watermark, on a NON-ZERO
        # interaction.  It runs before the semantic checks because a record can
        # be both a duplicate and malformed, and a malformed opening record
        # closes the tool's prior gesture — which a duplicate must never do.
        # The `0 <` guard is what lets it precede the zero checks.
        if opening and 0 < ev.interaction <= self._watermark:
            entry = self._index.get(ev.interaction)
            if entry is None:
                # Already closed: nothing to compare and nothing to merge with.
                return ignore("duplicate_opening")
            if raw == entry.opening_raw:
                return ignore("duplicate_opening")  # the exact replay §6 describes
            # A reused id carrying a different PRESS would otherwise be dropped
            # while its DRAGs continued the old gesture — host and plugin
            # silently disagreeing about which gesture is live.
            return reject("interaction_mismatch")
        # 3. the structural zero checks.
        if ev.interaction == 0 or ev.tool == 0 or ev.gen == 0 or ev.epoch == 0:
            return reject("zero_id")
        # 4.
        if opening and ev.seq != 0:
            return reject("seq_on_open")
        # 5. the position field (skipped entirely for an unknown kind).
        if position_error is not None:
            return reject(position_error)
        # 6. MISS with a kind that carries PICKED.
        if ev.payload_kind in _PICKED_KINDS and ev.position_flags & FLAG_MISS:
            return reject("miss_with_picked")
        # 7. the target invariant, only when the kind is known.
        if ev.target_known:
            if (ev.target_kind == TARGET_NONE) != (ev.target_id == 0) or (
                ev.target_kind == TARGET_NONE and ev.target_part != 0
            ):
                return reject("invalid_target")
        # 8. the pane epoch, against what epoch_acknowledged last supplied.
        installed = self._epochs.get(ev.view_id)
        if installed is None or ev.epoch > installed:
            return reject("epoch_mismatch")
        if ev.epoch < installed:
            return ignore("stale_epoch")
        # 9. tool status: never seen / retired / active.
        if ev.tool > self._high_water:
            return reject("unknown_tool")
        if ev.tool not in self._active_tools:
            return ignore("retired_tool")
        # 10–11. non-opening records: the index, then seq.
        seq_gap = 0
        entry: _Interaction | None = None
        if not opening:
            entry = self._index.get(ev.interaction)
            if entry is None:
                return ignore("unknown_interaction")
            if (entry.tool, entry.gen, entry.view_id) != (ev.tool, ev.gen, ev.view_id):
                return reject("interaction_mismatch")
            if ev.seq <= entry.last_seq:
                return ignore("duplicate_seq")
            seq_gap = ev.seq - entry.last_seq - 1
            if seq_gap:
                self.counters["seq_gap_samples"] += seq_gap
                diagnostics.append(f"seq_gap:{seq_gap}")
        # Accepted.
        frame = Frame(
            raw,
            self._ordinal,
            Disposition.DECODED,
            header,
            ev,
            diagnostics=tuple(diagnostics),
            seq_gap=seq_gap,
        )
        if opening:
            self._watermark = ev.interaction
            if ev.phase == PHASE_PRESS:
                prior = self._tool_open.get(ev.tool)
                if prior is not None:
                    self._index.pop(prior, None)
                    frame.implicitly_closed = prior
                self._index[ev.interaction] = _Interaction(ev.tool, ev.gen, ev.view_id, raw)
                self._tool_open[ev.tool] = ev.interaction
        else:
            assert entry is not None
            entry.last_seq = ev.seq
            if ev.is_terminal:
                del self._index[ev.interaction]
                if self._tool_open.get(ev.tool) == ev.interaction:
                    del self._tool_open[ev.tool]
                frame.closed_interaction = ev.interaction
        outs.append(frame)

    def _implicit_close(self, tool: int, reason: str, outs: list[Outcome]) -> None:
        if tool in self._active_tools:
            n = self._tool_open.get(tool)
            if n is not None:
                self._cancel(n, reason, outs)


_DECODERS = {
    OP_MEMORY: DoorbellTransport._decode_m,
    OP_INPUT: DoorbellTransport._decode_e,
    OP_NOTIFY: DoorbellTransport._decode_d,
}
#: Payload-relative ranges framing owns: fixed offset, fixed extent (§8).
_FIXED_PADDING = {
    OP_MEMORY: ((17, 18),),
    OP_INPUT: ((_E_PAD2.start, _E_PAD2.stop),),
    OP_NOTIFY: ((_D_PAD_OFFSET, PAYLOAD_SIZE),),
}


# ---------------------------------------------------------------------------
# Launch environment (transport §3.1, §3.2)
# ---------------------------------------------------------------------------

_SO_NOSIGPIPE = 0x1022  # not exposed by CPython's socket module


def adopt_inherited_doorbell(fd: int | None = None) -> socket.socket:
    """unix: adopt ``VIZI_PLUGIN_DOORBELL_FD`` with its own family and type,
    verify it is the platform's socket type, clear its inheritable flag and
    set it non-blocking.  Any failure is a launch failure."""
    if os.name == "nt":
        raise DoorbellSetupError("inherited doorbell descriptors exist only on unix")
    if fd is None:
        raw = os.environ.get("VIZI_PLUGIN_DOORBELL_FD")
        if raw is None:
            raise DoorbellSetupError(
                "VIZI_PLUGIN_DOORBELL_FD is not set; running without a doorbell is not legal"
            )
        try:
            fd = int(raw)
        except ValueError as exc:
            raise DoorbellSetupError(
                f"VIZI_PLUGIN_DOORBELL_FD={raw!r} is not a descriptor"
            ) from exc
    try:
        sock = socket.socket(fileno=fd)
    except OSError as exc:
        # The number came through the environment and the descriptor did
        # not: a launcher in front of the plugin spawned it as a child with
        # the runtime's default descriptor policy (`manifest_schema.md`
        # §2.2).  Said here, because a bare errno on a descriptor number is
        # a silent, misattributed failure.
        hint = ""
        if exc.errno in (errno.EBADF, errno.ENOTSOCK):
            hint = (
                " -- the doorbell fd was not inherited: a launcher between the host and "
                "this process closed non-stdio descriptors in its child (manifest_schema.md "
                "§2.2); exec the plugin, or pass the descriptor through"
            )
        raise DoorbellSetupError(f"cannot adopt doorbell fd {fd}: {exc}{hint}") from exc
    os.set_inheritable(fd, False)
    sock.setblocking(False)
    expected = _SOCK_TYPE_FOR_CLASS[platform_transport_class()]
    actual = sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
    if actual != expected:
        sock.detach()
        raise DoorbellSetupError(
            f"doorbell fd {fd} has SO_TYPE {actual}, expected {expected} on this platform"
        )
    if sys.platform == "darwin":
        sock.setsockopt(socket.SOL_SOCKET, _SO_NOSIGPIPE, 1)
    return sock


def connect_windows_doorbell(
    addr: str | None = None, token: str | None = None, *, timeout: float = 10.0
) -> tuple[socket.socket, str]:
    """Windows: connect to the host's loopback listener, write the token as the
    first bytes of the stream, then go non-blocking.  Record framing begins at
    the first byte after the token."""
    addr = addr if addr is not None else os.environ.get("VIZI_PLUGIN_DOORBELL_ADDR")
    token = token if token is not None else os.environ.get("VIZI_PLUGIN_TOKEN")
    if not addr or not token:
        raise DoorbellSetupError("VIZI_PLUGIN_DOORBELL_ADDR and VIZI_PLUGIN_TOKEN are required")
    host, _, port = addr.rpartition(":")
    host = host.strip("[]")
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
    except (OSError, ValueError) as exc:
        raise DoorbellSetupError(f"cannot connect doorbell to {addr}: {exc}") from exc
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        data = token.encode("ascii")
        sock.settimeout(timeout)  # the authentication phase is bounded locally
        while data:  # the token is not a record: a short write here is ordinary
            n = sock.send(data)
            data = data[n:]
    except (OSError, UnicodeEncodeError) as exc:
        sock.close()
        raise DoorbellSetupError(f"doorbell token write failed: {exc}") from exc
    sock.setblocking(False)
    return sock, token


def open_doorbell(on_outcome: Callable[[Outcome], None], **kwargs) -> DoorbellTransport:
    """Adopt or connect the doorbell from the launch environment and wrap it.
    The transport is returned unstarted so the caller can order ``start()``
    against its readiness signal.

    On Windows the connection's token is carried into the transport, so
    :meth:`DoorbellTransport.handshake_complete` can check the echo the host
    puts in ``Hello.session_token``."""
    cls = platform_transport_class()
    if cls == "stream":
        sock, token = connect_windows_doorbell()
        kwargs.setdefault("session_token", token)
    else:
        sock = adopt_inherited_doorbell()
    return DoorbellTransport(sock, cls, on_outcome, **kwargs)
