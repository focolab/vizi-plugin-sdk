"""Processing stages (``processing_stages.md``): the slot, and the turns.

A stage edits a volume **in place**, in a ring of STAGED slots one mapping of
which every stage in a chain shares.  This module is the plugin's half of §5-§7:

- the header codec at §5.1's offsets, and §7's validation of it;
- :class:`StageSlot`, what an author's ``process`` is handed: the planes as
  numpy views of the shared memory, and the header fields a stage may change;
- :class:`StageRunner`, the one thread that loads turn words, runs ``process``
  and hands each slot on.

The runner is woken by a ``D`` naming a slot and re-checks every armed ring on
its own schedule as well (§6.2), so a lost hint delays a turn and never loses
one.
"""

from __future__ import annotations

import gc
import math
import struct
import threading
import traceback
import weakref
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

import numpy as np

from . import memory as _memory

#: §5.1: the fixed part of the header; the channel array follows it.
HEADER_FIXED_BYTES = 256
#: §5.1: the data begins at the header's end rounded up to this.
HEADER_ALIGN = 64

#: §6.3's outcome codes.
OUTCOME_NONE, OUTCOME_PROCESSED, OUTCOME_REFUSED, OUTCOME_FAILED = 0, 1, 2, 3
#: §6.3's reasons for *refused*.
REFUSE_CAUSAL, REFUSE_MISSING_INPUT, REFUSE_OTHER = 1, 2, 3
#: §6.3's reasons for *failed*.
FAIL_INVALID_HEADER, FAIL_INTERNAL, FAIL_OTHER = 1, 2, 3

#: The wire ``DType`` numbers, spelled here so a slot can be read without the
#: generated stubs (``_lazy.py``).
DTYPES: dict[int, np.dtype] = {
    1: np.dtype(np.uint8),
    2: np.dtype(np.uint16),
    3: np.dtype(np.float32),
    4: np.dtype(np.float64),
}
_WIRE_DTYPE = {dt: code for code, dt in DTYPES.items()}

# Offsets 8..256: outcome, a reserved u32, t, done_through, reason, dtype,
# planes, plane_cap, the allocation, a reserved u32, capacity, the extent,
# origin, pose_r, pose_t, open_generation, 40 reserved bytes.
_FIXED = struct.Struct("<12IQ6I3d9d3dQ40s")
assert _FIXED.size == HEADER_FIXED_BYTES - 8
_OUTCOME = struct.Struct("<I")
_OFF_OUTCOME, _OFF_REASON = 8, 24
_IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


def header_bytes(plane_cap: int) -> int:
    """§5.1: 256 fixed bytes and one ``u16`` per allocated plane, rounded up."""
    raw = HEADER_FIXED_BYTES + 2 * plane_cap
    return -(-raw // HEADER_ALIGN) * HEADER_ALIGN


# -- the turn word (§6.1) ------------------------------------------------------


@dataclass(frozen=True)
class Turn:
    """One decoded turn word.  ``position`` is SIGNED: ``0`` the host, ``k > 0``
    position ``k``, ``-k`` the host after the stage at ``k`` refused or failed."""

    iteration: int
    pipeline: int
    position: int


def encode_turn(iteration: int, pipeline: int, position: int) -> int:
    if not 0 <= iteration <= 0xFFFFFFFF:
        raise ValueError(f"iteration {iteration} is not a u32")
    if not 0 <= pipeline <= 0xFFFF:
        raise ValueError(f"pipeline {pipeline} is not a u16")
    if not -0x8000 <= position <= 0x7FFF:
        raise ValueError(f"position {position} is not an i16")
    return iteration | pipeline << 32 | (position & 0xFFFF) << 48


def decode_turn(word: int) -> Turn:
    position = (word >> 48) & 0xFFFF
    if position >= 0x8000:
        position -= 0x10000
    return Turn(word & 0xFFFFFFFF, (word >> 32) & 0xFFFF, position)


# -- the header (§5.1) ----------------------------------------------------------


@dataclass(frozen=True)
class SlotHeader:
    """Everything in a slot but the turn word and the data."""

    outcome: int = OUTCOME_NONE
    t: int = 0
    done_through: int = 0
    reason: int = 0
    dtype: int = 0
    planes: int = 0
    plane_cap: int = 0
    alloc: tuple[int, int, int] = (0, 0, 0)
    capacity: int = 0
    #: ``(z0, zN, y0, yN, x0, xN)``, §5.4.
    extent: tuple[int, int, int, int, int, int] = (0, 0, 0, 0, 0, 0)
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    pose_r: tuple[float, ...] = _IDENTITY
    pose_t: tuple[float, float, float] = (0.0, 0.0, 0.0)
    open_generation: int = 0
    #: The channel index of each plane in use: ``planes`` entries, §5.2.
    channels: tuple[int, ...] = ()
    #: The reserved fields as found; §5.1 makes a non-zero one invalid.
    reserved_zero: bool = True

    @property
    def header_bytes(self) -> int:
        return header_bytes(self.plane_cap)

    @property
    def plane_bytes(self) -> int:
        z, y, x = self.alloc
        dtype = DTYPES.get(self.dtype)
        return z * y * x * (dtype.itemsize if dtype is not None else 0)


def read_header(buf) -> SlotHeader:
    """Decode a slot's header.  Reads ``planes`` channel entries only where the
    header's own numbers put them inside ``buf``; :func:`validate` says the rest."""
    f = _FIXED.unpack_from(buf, 8)
    outcome, r12, t, done, reason, dtype, planes, plane_cap, z, y, x, r52, capacity = f[:13]
    extent = tuple(f[13:19])
    origin, pose_r, pose_t = tuple(f[19:22]), tuple(f[22:31]), tuple(f[31:34])
    open_generation, tail = f[34], f[35]
    channels: tuple[int, ...] = ()
    if planes <= plane_cap and HEADER_FIXED_BYTES + 2 * planes <= len(buf):
        channels = struct.unpack_from(f"<{planes}H", buf, HEADER_FIXED_BYTES)
    return SlotHeader(
        outcome=outcome,
        t=t,
        done_through=done,
        reason=reason,
        dtype=dtype,
        planes=planes,
        plane_cap=plane_cap,
        alloc=(z, y, x),
        capacity=capacity,
        extent=extent,  # type: ignore[arg-type]
        origin=origin,  # type: ignore[arg-type]
        pose_r=pose_r,
        pose_t=pose_t,  # type: ignore[arg-type]
        open_generation=open_generation,
        channels=tuple(channels),
        reserved_zero=r12 == 0 and r52 == 0 and not any(tail),
    )


def write_header(buf, header: SlotHeader) -> None:
    """Encode everything but the turn word, reserved fields zero (§5.1)."""
    _FIXED.pack_into(
        buf,
        8,
        header.outcome,
        0,
        header.t,
        header.done_through,
        header.reason,
        header.dtype,
        header.planes,
        header.plane_cap,
        *header.alloc,
        0,
        header.capacity,
        *header.extent,
        *header.origin,
        *header.pose_r,
        *header.pose_t,
        header.open_generation,
        bytes(40),
    )
    struct.pack_into(f"<{len(header.channels)}H", buf, HEADER_FIXED_BYTES, *header.channels)


def _write_outcome(buf, outcome: int, reason: int) -> None:
    """§6.3 for *refused* and *failed*: two fields and nothing else, since the
    rest of the header may be what was wrong."""
    _OUTCOME.pack_into(buf, _OFF_OUTCOME, outcome)
    _OUTCOME.pack_into(buf, _OFF_REASON, reason)


def validate(header: SlotHeader, extent: int, armed_input=None) -> list[str]:
    """§7's minimum.  Empty means the slot may be used.

    ``armed_input`` is the arm's ``StageShape``: the slot's ``dtype`` must be
    its dtype, and each dimension of the allocation at most its own, since a
    plan states the largest output and a slot may be smaller.
    """
    problems: list[str] = []
    if not header.reserved_zero:
        problems.append("a reserved field is not zero")
    dtype = DTYPES.get(header.dtype)
    if dtype is None:
        problems.append(f"dtype {header.dtype} is not a known DType")
    if header.planes > header.plane_cap:
        problems.append(f"planes {header.planes} exceeds plane_cap {header.plane_cap}")
    if header.plane_cap > MAX_PLANE_CAP:
        problems.append(f"plane_cap {header.plane_cap} exceeds {MAX_PLANE_CAP} (§5.1)")
    present = header.channels[: header.planes]
    if len(set(present)) != len(present):
        problems.append(f"channels {list(present)} name one channel twice (§5.2)")
    if header.header_bytes + header.capacity > extent:
        problems.append(
            f"header ({header.header_bytes}) + capacity ({header.capacity}) exceeds the "
            f"slot's {extent} bytes"
        )
    if dtype is not None and header.planes * header.plane_bytes > header.capacity:
        problems.append(
            f"{header.planes} planes of {header.plane_bytes} bytes exceed capacity "
            f"{header.capacity}"
        )
    z0, zn, y0, yn, x0, xn = header.extent
    for axis, lo, hi, size in zip("zyx", (z0, y0, x0), (zn, yn, xn), header.alloc):
        if not lo <= hi <= size:
            problems.append(f"extent {axis} [{lo}, {hi}) is not within the allocation {size}")
    if not all(map(math.isfinite, (*header.origin, *header.pose_r, *header.pose_t))):
        problems.append("origin or pose is not finite")
    if armed_input is not None:
        if header.dtype != armed_input.dtype:
            problems.append(f"dtype {header.dtype} is not the armed input's {armed_input.dtype}")
        armed = (armed_input.z, armed_input.y, armed_input.x)
        if any(have > most for have, most in zip(header.alloc, armed)):
            problems.append(f"allocation {header.alloc} exceeds the armed input's {armed}")
    return problems


# -- planning (§3) ----------------------------------------------------------------

#: §4: channel indices are u16 in the slot.
MAX_CHANNEL = 0xFFFF
#: §5.1: the largest legal ``plane_cap`` -- one plane per u16 channel index.
MAX_PLANE_CAP = MAX_CHANNEL + 1


class ChannelIndexTooLarge(ValueError):
    """A plan names a channel index above 65 535: ``Error{PROTOCOL}`` (§4),
    not the ``INTERNAL`` of any other broken plan."""


@dataclass(frozen=True)
class StageInput:
    """What a ``PlanStage`` offers: the channels present in plane order, the
    allocation each plane is laid out in, and the one dtype (§3)."""

    channels: tuple[int, ...]
    z: int
    y: int
    x: int
    dtype: np.dtype

    @property
    def alloc(self) -> tuple[int, int, int]:
        return (self.z, self.y, self.x)


@dataclass(frozen=True)
class NewChannel:
    """A channel the stage appends (§3 ``adds``).  ``key`` is plugin-local; the
    host assigns the index and the arm says which (``StageSlot.add_channel``)."""

    key: str
    name: str = ""
    needs: tuple[int, ...] = ()


@dataclass(frozen=True)
class StagePlan:
    """A stage's answer to ``PlanStage``: the LARGEST output it produces (§3).

    ``modifies`` maps each channel rewritten in place to the input channels
    it needs.  ``reads`` names input channels the stage reads and does not
    write -- a stabilizer computing its pose from channel 0 (§3).  ``alloc``
    and ``dtype`` default to the input's.  Empty ``modifies``, ``adds`` and
    ``reads`` are legal: a stage that only reports extent, origin or pose
    from nothing needs no channel at all.
    """

    modifies: "dict[int, Iterable[int]]" = field(default_factory=dict)
    adds: "tuple[NewChannel, ...] | list[NewChannel]" = ()
    alloc: "tuple[int, int, int] | None" = None
    dtype: object = None
    reads: "Iterable[int]" = ()

    def needs(self) -> frozenset[int]:
        """Every input channel this plan cannot run without."""
        needed: set[int] = set(self.modifies) | {int(ch) for ch in self.reads}
        for needs in self.modifies.values():
            needed.update(needs)
        for add in self.adds:
            needed.update(add.needs)
        return frozenset(needed)


#: §2: the ``Value`` kind each declared parameter ``type`` travels as.
_PARAM_KIND = {"float": "f", "int": "i", "bool": "b", "choice": "s", "string": "s"}


def param_values(params) -> dict:
    """A wire ``map<string, Value>`` as plain Python values."""
    return {key: getattr(value, value.WhichOneof("kind") or "s") for key, value in params.items()}


def check_params(declared, params) -> dict:
    """``params`` against the manifest's declaration (§2), as plain values.

    Every declared key and no other, each of its type's ``Value`` kind and in
    its range or options.  ``ValueError`` names the first that is not."""
    by_key = {p.key: p for p in declared}
    undeclared = sorted(set(params) - set(by_key))
    if undeclared:
        raise ValueError(f"params {undeclared} are not declared")
    missing = sorted(set(by_key) - set(params))
    if missing:
        raise ValueError(f"declared params {missing} are missing")
    values = {}
    for key, value in params.items():
        spec = by_key[key]
        kind = value.WhichOneof("kind")
        if kind != _PARAM_KIND[spec.type]:
            raise ValueError(f"param {key!r} is a {spec.type}, sent as Value.{kind}")
        v = getattr(value, kind)
        if spec.type == "choice" and v not in spec.options:
            raise ValueError(f"param {key!r} = {v!r} is not one of {list(spec.options)}")
        if spec.type == "string" and len(v.encode("utf-8")) > spec.max_length:
            raise ValueError(
                f"param {key!r} is longer than its max_length ({spec.max_length} UTF-8 bytes)"
            )
        if spec.type in ("float", "int"):
            if not math.isfinite(v):
                raise ValueError(f"param {key!r} is not finite")
            if (spec.min is not None and v < spec.min) or (spec.max is not None and v > spec.max):
                raise ValueError(f"param {key!r} = {v} is outside [{spec.min}, {spec.max}]")
        values[key] = v
    return values


def stage_input(shape) -> StageInput:
    """A wire ``StageShape`` as an author sees it.  ``ValueError`` names what
    makes it unusable (§4: an index above 65 535 is a protocol error)."""
    if any(c > MAX_CHANNEL for c in shape.channels):
        raise ValueError(f"a channel index exceeds {MAX_CHANNEL}")
    dtype = DTYPES.get(shape.dtype)
    if dtype is None:
        raise ValueError(f"dtype {shape.dtype} is not a slot dtype")
    return StageInput(tuple(shape.channels), shape.z, shape.y, shape.x, dtype)


def plan_reply(plan: StagePlan, inp: StageInput, pb2):
    """``plan`` as a ``StagePlanned``, after checking it against §3's rules.

    ``ValueError`` names a rule the author's plan breaks.  ``output.channels``
    is left empty: the host computes it (§3)."""
    alloc = inp.alloc if plan.alloc is None else tuple(int(v) for v in plan.alloc)
    if len(alloc) != 3 or any(not 1 <= out <= have for out, have in zip(alloc, inp.alloc)):
        raise ValueError(f"output allocation {alloc} is not within the input's {inp.alloc}")
    dtype = inp.dtype if plan.dtype is None else np.dtype(plan.dtype)
    if dtype not in _WIRE_DTYPE:
        raise ValueError(f"{dtype} is not a slot dtype")
    present = set(inp.channels)
    modifies = {int(ch): tuple(int(n) for n in needs) for ch, needs in plan.modifies.items()}
    reads = sorted({int(ch) for ch in plan.reads})
    named = [*modifies, *(n for needs in modifies.values() for n in needs), *reads]
    named += [int(n) for add in plan.adds for n in add.needs]
    if any(not 0 <= n <= MAX_CHANNEL for n in named):
        raise ChannelIndexTooLarge(f"the plan names a channel index above {MAX_CHANNEL}")
    if not present.issuperset(reads):
        raise ValueError(f"reads {sorted(set(reads) - present)}, not in the input")
    for ch, needs in modifies.items():
        if ch not in present:
            raise ValueError(f"modifies channel {ch}, which the input does not hold")
        if not present.issuperset(needs):
            raise ValueError(f"channel {ch} needs {sorted(set(needs) - present)}, not in the input")
    keys = [add.key for add in plan.adds]
    if any(not key for key in keys) or len(set(keys)) != len(keys):
        raise ValueError(f"adds keys {keys} must be non-empty and unique")
    for add in plan.adds:
        if not present.issuperset(add.needs):
            raise ValueError(
                f"{add.key!r} needs {sorted(set(add.needs) - present)}, not in the input"
            )
    return pb2.StagePlanned(
        output=pb2.StageShape(z=alloc[0], y=alloc[1], x=alloc[2], dtype=_WIRE_DTYPE[dtype]),
        modifies=[
            pb2.StageChannelNeed(channel=ch, needs=list(needs)) for ch, needs in modifies.items()
        ],
        adds=[
            pb2.StageNewChannel(key=add.key, name=add.name or add.key, needs=list(add.needs))
            for add in plan.adds
        ],
        reads=reads,
    )


# -- what an author is handed (§5) ----------------------------------------------


class RefuseVolume(Exception):
    """Raise from ``process`` to refuse a volume (§6.3).  **Before writing
    anything**: a refused volume is used as far as the previous stage got, so
    its data must still be that stage's output."""

    def __init__(self, reason: int = REFUSE_OTHER, detail: str = "") -> None:
        super().__init__(detail or f"refused, reason {reason}")
        self.reason = reason if reason in (REFUSE_CAUSAL, REFUSE_MISSING_INPUT) else REFUSE_OTHER


class StageSlot:
    """One slot during this stage's turn: the planes, and the header fields a
    stage may change.

    The arrays are views of the shared memory, so writing one IS the edit.
    **Keep none of them past** ``process``: the next holder may be writing the
    same bytes, and a live view keeps the mapping from ever closing.
    """

    def __init__(self, buf, header: SlotHeader, arm, turn: Turn) -> None:
        self._buf = buf
        self._h = header
        self._arm = arm
        self._turn = turn
        #: This position's parameters (§2), checked when it was armed.
        self.params: dict = param_values(arm.params)
        #: Every array handed out, so the runner can tell an author who kept
        #: one: the array exports the mapping, not the slot's memoryview.
        self._handed: list[weakref.ref] = []

    # -- identity ---------------------------------------------------------

    @property
    def t(self) -> int:
        return self._h.t

    @property
    def open_generation(self) -> int:
        return self._h.open_generation

    @property
    def iteration(self) -> int:
        return self._turn.iteration

    @property
    def pipeline(self) -> int:
        return self._turn.pipeline

    @property
    def position(self) -> int:
        return self._turn.position

    @property
    def stage_id(self) -> str:
        return self._arm.stage_id

    # -- layout -----------------------------------------------------------

    @property
    def dtype(self) -> np.dtype:
        return DTYPES[self._h.dtype]

    @property
    def alloc(self) -> tuple[int, int, int]:
        return self._h.alloc

    @property
    def capacity(self) -> int:
        return self._h.capacity

    @property
    def channels(self) -> tuple[int, ...]:
        """The channel index of each plane, in plane order (§5.2)."""
        return self._h.channels

    @property
    def planes(self) -> np.ndarray:
        """``(planes, z, y, x)`` over the shared memory, at the current layout."""
        z, y, x = self._h.alloc
        n = self._h.planes
        array = np.frombuffer(
            self._buf, self.dtype, count=n * z * y * x, offset=self._h.header_bytes
        ).reshape(n, z, y, x)
        self._handed.append(weakref.ref(array))
        return array

    def _kept(self) -> bool:
        """Whether any array handed out is still alive."""
        if any(ref() is not None for ref in self._handed):
            gc.collect()  # a traceback's frames may be all that holds one
        return any(ref() is not None for ref in self._handed)

    def plane(self, channel: int) -> np.ndarray:
        """The plane holding channel index ``channel``."""
        try:
            p = self._h.channels.index(channel)
        except ValueError:
            raise KeyError(f"channel {channel} is not in this slot {self._h.channels}") from None
        return self.planes[p]

    def add_channel(self, key: str) -> np.ndarray:
        """Append the plane for this plan's ``adds`` entry ``key``, zeroed, at
        the index the host assigned (§4, §5.2), and return it."""
        try:
            index = self._arm.new_channels[key]
        except KeyError:
            raise KeyError(f"the arm assigns no channel to {key!r}") from None
        h = self._h
        if index in h.channels:
            raise ValueError(f"channel {index} ({key!r}) is already in this slot")
        if h.planes >= h.plane_cap:
            raise ValueError(f"the slot has room for {h.plane_cap} planes and holds them all")
        if (h.planes + 1) * h.plane_bytes > h.capacity:
            raise ValueError("another plane at this layout exceeds the slot's capacity")
        self._h = replace(h, planes=h.planes + 1, channels=h.channels + (index,))
        added = self.planes[-1]
        added[...] = 0
        return added

    def relayout(
        self,
        dtype=None,
        alloc: "tuple[int, int, int] | None" = None,
        offset: tuple[int, int, int] = (0, 0, 0),
    ) -> np.ndarray:
        """Repack every plane into a new ``dtype`` and allocation (§5.3).

        The new plane is the old one's ``[oz:oz+nz, oy:oy+ny, ox:ox+nx]``
        window cast to ``dtype`` (a plain ``astype``: scaling is the stage's).
        The extent is carried into the new layout and the origin moved so every
        voxel stays where it was placed (§5.4).  Returns the new ``planes``.
        """
        h = self._h
        new_dtype = self.dtype if dtype is None else np.dtype(dtype)
        if new_dtype not in _WIRE_DTYPE:
            raise ValueError(f"{new_dtype} is not a slot dtype")
        nz, ny, nx = h.alloc if alloc is None else alloc
        oz, oy, ox = offset
        for axis, o, n, size in zip("zyx", (oz, oy, ox), (nz, ny, nx), h.alloc):
            if o < 0 or n < 0 or o + n > size:
                raise ValueError(f"window {axis} [{o}, {o + n}) is not within {size}")
        if h.planes * nz * ny * nx * new_dtype.itemsize > h.capacity:
            raise ValueError("the new layout exceeds the slot's capacity")
        scratch = self.planes[:, oz : oz + nz, oy : oy + ny, ox : ox + nx].astype(new_dtype)
        z0, zn, y0, yn, x0, xn = h.extent
        lo = [min(max(v - o, 0), n) for v, o, n in zip((z0, y0, x0), offset, (nz, ny, nx))]
        hi = [min(max(v - o, 0), n) for v, o, n in zip((zn, yn, xn), offset, (nz, ny, nx))]
        origin = tuple(
            orig + (new_lo + o - old_lo)
            for orig, new_lo, o, old_lo in zip(h.origin, lo, offset, (z0, y0, x0))
        )
        self._h = replace(
            h,
            dtype=_WIRE_DTYPE[new_dtype],
            alloc=(nz, ny, nx),
            extent=(lo[0], hi[0], lo[1], hi[1], lo[2], hi[2]),
            origin=origin,  # type: ignore[arg-type]
        )
        planes = self.planes
        planes[...] = scratch
        return planes

    # -- placement (§5.4, §5.5) --------------------------------------------

    @property
    def extent(self) -> tuple[int, int, int, int, int, int]:
        """``(z0, zN, y0, yN, x0, xN)``: the valid rectangle of every plane."""
        return self._h.extent

    @extent.setter
    def extent(self, value) -> None:
        z0, zn, y0, yn, x0, xn = (int(v) for v in value)
        old = self._h.extent
        for lo, hi, old_lo, old_hi in (
            (z0, zn, *old[0:2]),
            (y0, yn, *old[2:4]),
            (x0, xn, *old[4:6]),
        ):
            if not old_lo <= lo <= hi <= old_hi:
                # §5.4: a stage never makes valid what it was not given.
                raise ValueError(f"extent {tuple(value)} is not within {old}")
        self._h = replace(self._h, extent=(z0, zn, y0, yn, x0, xn))

    @property
    def origin(self) -> tuple[float, float, float]:
        return self._h.origin

    @origin.setter
    def origin(self, value) -> None:
        origin = tuple(float(v) for v in value)
        if len(origin) != 3 or not all(map(math.isfinite, origin)):
            raise ValueError(f"origin {value} is not three finite numbers")
        self._h = replace(self._h, origin=origin)  # type: ignore[arg-type]

    @property
    def pose(self) -> tuple[np.ndarray, np.ndarray]:
        """``(R, T)``: this slot's data to real space, absolute (§5.5)."""
        return np.array(self._h.pose_r).reshape(3, 3), np.array(self._h.pose_t)

    @pose.setter
    def pose(self, value) -> None:
        r, t = value
        r = np.asarray(r, dtype=np.float64).reshape(3, 3)
        t = np.asarray(t, dtype=np.float64).reshape(3)
        if not (np.isfinite(r).all() and np.isfinite(t).all()):
            raise ValueError("a pose is finite")
        self._h = replace(
            self._h, pose_r=tuple(r.ravel().tolist()), pose_t=tuple(t.tolist())  # type: ignore[arg-type]
        )

    def compose_pose(self, r_k, t_k) -> None:
        """Compose this stage's motion, ``s_in = R_k · s_out + T_k``, into the
        absolute pose: ``(R, T) <- (R·R_k, R·T_k + T)`` with the R and T from
        before this stage (§5.5)."""
        r, t = self.pose
        r_k = np.asarray(r_k, dtype=np.float64).reshape(3, 3)
        t_k = np.asarray(t_k, dtype=np.float64).reshape(3)
        self.pose = (r @ r_k, r @ t_k + t)


# -- the runner (§6) -------------------------------------------------------------


def _beyond_armed_output(header: SlotHeader, arm) -> "str | None":
    """How a processed slot exceeds its arm's ``output``, or ``None``: the
    dtype must be the armed one and each allocation at most the armed one's
    (a plan states the largest output).  An arm without an output holds the
    stage to nothing here."""
    if not arm.HasField("output"):
        return None
    out = arm.output
    if header.dtype != out.dtype:
        return f"finished as dtype {header.dtype}; the arm's output is {out.dtype}"
    armed = (out.z, out.y, out.x)
    if any(have > most for have, most in zip(header.alloc, armed)):
        return f"finished with allocation {header.alloc}; the arm's output is at most {armed}"
    return None


class StageRunner:
    """One thread: load turn words, run ``process``, hand each slot on.

    ``stage_for(stage_id)`` answers the stage instance, or ``None``.
    ``needs_for(arm)`` answers the input channels the stage declared it needs
    at that arm, or ``None`` if unknown; a missing one is refused before the
    author's code runs (§7).  ``notify(segment_id, [index])`` sends the ``D``.
    """

    #: §6.2's own schedule: how long a lost ``D`` can delay a turn.
    RECHECK_S = 0.04

    def __init__(
        self,
        *,
        memory: "_memory.SegmentStore",
        control,
        stage_for: Callable[[str], object],
        needs_for: "Callable[[object], Iterable[int] | None]" = lambda arm: None,
        notify: "Callable[[int, list[int]], object] | None" = None,
    ) -> None:
        self._memory = memory
        self._control = control
        self._stage_for = stage_for
        self._needs_for = needs_for
        self._notify = notify
        self._wake = threading.Event()
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        #: The last failed pass reported, so a lasting one is said once.
        self._last_failure: str | None = None
        #: ``(segment_id, slot, pipeline, position)`` -> the last iteration
        #: acted on there (§6.2: compared for equality only).
        self._last: dict[tuple[int, int, int, int], int] = {}
        #: Turns taken, by outcome, for a developer to read.
        self.counts = {OUTCOME_PROCESSED: 0, OUTCOME_REFUSED: 0, OUTCOME_FAILED: 0}

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="vizi-sdk-stage", daemon=True)
            self._thread.start()

    def wake(self, *_: object) -> None:
        self._wake.set()

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stopping = True
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _run(self) -> None:
        while not self._stopping:
            self._wake.wait(self.RECHECK_S)
            self._wake.clear()
            if self._stopping:
                return
            try:
                self.pass_once()
            except ImportError as exc:
                # A missing package will not appear between passes: the runner
                # stops, and says so once.
                self._control.diagnose(f"stage runner stopped: {exc}")
                return
            except Exception as exc:  # the schedule must outlive one bad pass
                # Passes run every RECHECK_S, so a failure that does not go away
                # is reported once, until it changes or a pass succeeds (83c).
                report = repr(exc)
                if report != self._last_failure:
                    self._last_failure = report
                    self._control.diagnose(f"stage runner pass raised: {report}")
            else:
                self._last_failure = None

    def pass_once(self) -> int:
        """Load every armed slot's turn word once and take each turn that is
        ours.  Returns how many were taken."""
        rings = {arm.segment_id for arm in self._control.stage_arm_snapshot().values()}
        taken = 0
        for segment_id in sorted(rings):
            segment = self._memory.segment(segment_id)
            if segment is None or segment.purpose != _memory.PURPOSE_STAGE:
                continue
            try:
                if not segment.hold():
                    continue  # retiring: finishing is permitted, starting is not
                for index in sorted(segment.regions):
                    if segment.quarantined(index):
                        continue
                    turn = decode_turn(segment.load_turn(index))
                    if turn.iteration == 0 or turn.position <= 0:
                        continue  # §6.1: never filled, or the host's
                    # Read per slot, not per pass: a re-arm acknowledged
                    # while an earlier slot ran is what this slot is held to.
                    # A turn already started finishes
                    # under the revision it began with.
                    arm = self._control.stage_arm_at(turn.pipeline, turn.position)
                    if arm is None or arm.segment_id != segment_id:
                        continue
                    key = (segment_id, index, turn.pipeline, turn.position)
                    if self._last.get(key) == turn.iteration:
                        continue
                    self._take(segment, index, arm, turn)
                    self._last[key] = turn.iteration
                    taken += 1
            finally:
                segment.unhold()
        return taken

    def _take(self, segment, index: int, arm, turn: Turn) -> None:
        buf = segment.slot_buffer(index)
        name = f"stage {arm.stage_id!r} at ({turn.pipeline}, {turn.position}), slot {index}"
        processed = False
        try:
            header = read_header(buf)
            problems = validate(header, len(buf), arm.input)
            stage = self._stage_for(arm.stage_id)
            needs = self._needs_for(arm)
            missing = sorted(set(needs) - set(header.channels)) if needs is not None else []
            if problems:
                outcome, reason = OUTCOME_FAILED, FAIL_INVALID_HEADER
                self._control.report("stage_slot_invalid", f"{name}: {'; '.join(problems)}")
            elif stage is None:
                outcome, reason = OUTCOME_FAILED, FAIL_OTHER
                self._control.diagnose(f"{name}: armed, but this plugin serves no such stage")
            elif missing:
                # §7: refused, reason 2, and not a violation.
                outcome, reason = OUTCOME_REFUSED, REFUSE_MISSING_INPUT
            else:
                outcome, reason, header = self._process(stage, buf, header, arm, turn, name)
                beyond = _beyond_armed_output(header, arm) if outcome == OUTCOME_PROCESSED else None
                if beyond is not None:
                    # §4: the arm is what the stage is held to, and a stage
                    # that outgrew it broke its own plan -- the author's fault,
                    # so failed/2, and the data is undefined.
                    outcome, reason = OUTCOME_FAILED, FAIL_INTERNAL
                    self._control.diagnose(f"{name}: {beyond}")
            if outcome == OUTCOME_PROCESSED:
                write_header(
                    buf, replace(header, outcome=outcome, reason=0, done_through=turn.position)
                )
                processed = True
            else:
                _write_outcome(buf, outcome, reason)
            self.counts[outcome] += 1
        finally:
            self._release(buf, name)
        nxt = turn.position + 1 if processed else -turn.position
        segment.store_turn(index, encode_turn(turn.iteration, turn.pipeline, nxt))
        if self._notify is not None:
            self._notify(segment.segment_id, [index])

    def _process(self, stage, buf, header, arm, turn, name):
        slot = StageSlot(buf, header, arm, turn)
        try:
            stage.process(slot)
        except RefuseVolume as refusal:
            result = OUTCOME_REFUSED, refusal.reason, header
        except Exception:
            self._control.diagnose(f"{name}: process raised\n{traceback.format_exc()}")
            result = OUTCOME_FAILED, FAIL_INTERNAL, header
        else:
            result = OUTCOME_PROCESSED, 0, slot._h
        if slot._kept():
            self._control.diagnose(
                f"{name}: an array over the slot outlived process(); the next holder "
                "may be writing those bytes, and the ring's mapping cannot close while "
                "it lives"
            )
        return result

    @staticmethod
    def _release(buf: memoryview, name: str) -> None:
        """Drop the slot's view before handing it on."""
        try:
            buf.release()
        except BufferError:  # pragma: no cover - arrays take their own export
            pass
