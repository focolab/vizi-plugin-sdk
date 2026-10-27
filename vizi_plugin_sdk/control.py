"""The plugin side of the 1.0 control plane — ``control_plane_protocol.md``.

**It classifies, routes, applies arrangements and produces acknowledgements; it
does not own the stream.**  Messages come in as decoded ``HostMsg``\\ s and go
out through an :class:`Outbox` the caller drains, which is the same boundary
``memory.py`` and ``violations.py`` keep and for the same reason: everything
here is testable without gRPC, a socket or a host.

Four rules shape the whole module, and each is the one an implementation gets
wrong first:

- **Classify by KIND, before matching any id** (§4 rule 3).  A push arriving
  while a request is pending has an id that matches nothing, and an
  implementation that matches first drops it as a stray.
- **A coalescing queue holds one item per replacement key** (§5.4), and
  drop-oldest is not that: it evicts the oldest *message* whatever key it
  carried, so a burst on one widget loses another widget's only update and
  nothing knows one is missing.  **Coalescing and one-shot may not share a
  queue**, which is why there are two here.  And how two items for one key
  combine is the message's own rule -- a widget that has been sent a
  ``set_options`` is order-sensitive and its ops are held whole, because no
  ordering of a merged patch preserves a dropdown.
- **Install locally, THEN acknowledge** (§5.5 rule 3).  Acknowledge first and a
  fast record under the new arrangement reaches the doorbell's checks before the
  value is installed there, is ruled invalid, and is dropped — usually the
  ``PRESS``, so the whole stroke is lost and the plugin reports a host bug it
  caused itself.
- **A reply that names a resource has an owner from the moment it arrives**
  (§4's resource rule), whether or not anyone was waiting: an abandoned
  ``DataVolume`` is released with ``ReleaseDeposit`` and then dropped, because
  Loaned reclamation is FIFO and one abandoned deposit at the head pins every
  release behind it.

What this module deliberately does **not** build is §11a's list: per-session
fairness, the drain cap, the host's deadlines, the grant state machine's host
half, or any escalation ladder.  A plugin serves one session and has nothing to
be fair between.
"""

from __future__ import annotations

import collections
import enum
import hashlib
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from . import memory as _memory
from . import tables as _tables
from . import violations as _violations
from ._lazy import LazyProto as _LazyProto
from .geometry import EdgeBox

pb2 = _LazyProto("pb2")

__all__ = [
    "Class",
    "Action",
    "Routing",
    "Outbox",
    "SessionFault",
    "Refused",
    "ControlPlane",
    "IdLedger",
    "ID_WINDOW",
    "PaneState",
    "HOST_CLASSES",
    "PLUGIN_CLASSES",
    "REPLY_KINDS",
    "REQUEST_REPLIES",
    "ALTERNATE_REPLIES",
    "ONE_SHOT_CAPACITY",
    "HELD_OPS_CEILING",
    "MAX_MESSAGE_BYTES",
    "duplicate_reservation",
]


# --------------------------------------------------------------------------
# §5: the classes, and what each kind is
# --------------------------------------------------------------------------


class Class(enum.Enum):
    """§5.  The class decides delivery, ordering and what overload does."""

    HOST_REQUEST = "host_request"  #: §5.1 — a reply is required
    HOST_ONE_WAY = "host_one_way"  #: §5.2 — a reply to us, or a host push
    #: §5.7 — a host push that is STATE keyed per overlay: the host merges on
    #: its side (one send per change) and we keep the latest.  No receive
    #: queue: applied on arrival like a one-way, named apart because §5.4's
    #: replace-per-key is what the sender does with it and a receiver that
    #: treated a stale one as a transition would be wrong.
    HOST_COALESCING = "host_coalescing"
    PLUGIN_REQUEST = "plugin_request"  #: §5.3 — we block a thread on the answer
    ARRANGEMENT = "arrangement"  #: §5.5 — apply, then acknowledge
    ONE_SHOT = "one_shot"  #: §5.4 — never drop; overflow is a session fault
    COALESCING = "coalescing"  #: §5.4 — one pending item per replacement key
    REPLY = "reply"  #: our answer to a host request


#: Every ``HostMsg`` arm, by class (§8's tables).  A kind absent from this map
#: is one this SDK predates: §11 says forward compatibility is not a violation,
#: so it is counted as a local diagnostic and never reported.
HOST_CLASSES: dict[str, Class] = {
    "hello": Class.HOST_ONE_WAY,
    "shutdown": Class.HOST_ONE_WAY,
    "open_reader": Class.HOST_REQUEST,
    "read_volume": Class.HOST_REQUEST,
    "read_plane": Class.HOST_REQUEST,
    "metadata": Class.HOST_REQUEST,
    "close_reader": Class.HOST_REQUEST,
    "invoke_command": Class.HOST_REQUEST,
    "open_export": Class.HOST_REQUEST,
    "export_chunk": Class.HOST_REQUEST,
    "finish_export": Class.HOST_REQUEST,
    "abort_export": Class.HOST_REQUEST,
    "view_state": Class.HOST_ONE_WAY,
    "data_volume": Class.HOST_ONE_WAY,
    "host_error": Class.HOST_ONE_WAY,
    "rois": Class.HOST_ONE_WAY,
    "rois_contributed": Class.HOST_ONE_WAY,
    "roi_poses_extended": Class.HOST_ONE_WAY,
    "label_volume_compiled": Class.HOST_ONE_WAY,
    "mask_voxels": Class.HOST_ONE_WAY,
    "roi_inclusion": Class.HOST_ONE_WAY,
    "tool_activation_changed": Class.HOST_ONE_WAY,
    "asset_result": Class.HOST_ONE_WAY,
    "save_path_result": Class.HOST_ONE_WAY,
    "colormap_table": Class.HOST_ONE_WAY,
    "view_request_result": Class.HOST_ONE_WAY,
    "follow_started": Class.HOST_ONE_WAY,
    "follow_ended": Class.HOST_ONE_WAY,
    "request_status": Class.HOST_ONE_WAY,
    "panel_opened": Class.HOST_ONE_WAY,
    "doorbell_shutdown": Class.HOST_ONE_WAY,
    "release_loan": Class.HOST_ONE_WAY,
    "contract_violation": Class.HOST_ONE_WAY,
    "grant_state_query": Class.HOST_ONE_WAY,
    "retire_segment": Class.HOST_ONE_WAY,
    # §8.16: outside the in-flight slot, which the request it names holds.
    "cancel_work": Class.HOST_ONE_WAY,
    "chunks_wanted": Class.HOST_COALESCING,
    "view_changed": Class.HOST_COALESCING,
    "regions_changed": Class.HOST_COALESCING,
    # An `image` widget's drawn size (§8.4), keyed by panel and widget.
    "widget_geometry": Class.HOST_COALESCING,
    # The host's theme and colors (§8.4), keyed by the session: the newest wins.
    "appearance": Class.HOST_COALESCING,
    # Region types a plugin shapes (§8.8): the drag is state, keyed by
    # drag; the commit and the end are transitions, outside the in-flight slot.
    "shape_drag": Class.HOST_COALESCING,
    "shape_commit": Class.HOST_ONE_WAY,
    "shape_edit_ended": Class.HOST_ONE_WAY,
    "install_segment": Class.ARRANGEMENT,
    "tool_binding": Class.ARRANGEMENT,
    "epoch_change": Class.ARRANGEMENT,
    "time_mapping": Class.ARRANGEMENT,
    # Processing stages (processing_stages.md).
    "plan_stage": Class.HOST_REQUEST,
    "stage_arm": Class.ARRANGEMENT,
}

#: Every ``PluginMsg`` arm, by class.  What it decides is which outbound queue
#: the message goes on, which is §5.4's whole point.
PLUGIN_CLASSES: dict[str, Class] = {
    "ready": Class.ONE_SHOT,
    "reader_opened": Class.REPLY,
    "series_list": Class.REPLY,
    "segment": Class.REPLY,
    "metadata": Class.REPLY,
    "closed": Class.REPLY,
    "error": Class.REPLY,
    "command_done": Class.REPLY,
    "stage_planned": Class.REPLY,
    "export_opened": Class.REPLY,
    "chunk_written": Class.REPLY,
    "export_done": Class.REPLY,
    "panel_patch": Class.COALESCING,
    "overlay_update": Class.COALESCING,
    "overlay_remove": Class.ONE_SHOT,
    "overlay_chunk": Class.COALESCING,
    "overlay_label_table": Class.COALESCING,
    "segment_shortfall": Class.COALESCING,
    "offer_follow": Class.COALESCING,
    "follow_state": Class.COALESCING,
    "work_progress": Class.COALESCING,
    "shape_proposal": Class.COALESCING,
    "shape_answer": Class.ONE_SHOT,
    "end_follow": Class.ONE_SHOT,
    "get_view_state": Class.PLUGIN_REQUEST,
    "read_data_volume": Class.PLUGIN_REQUEST,
    "read_mask_voxels": Class.PLUGIN_REQUEST,
    "read_roi_inclusion": Class.PLUGIN_REQUEST,
    "contribute_rois": Class.PLUGIN_REQUEST,
    "get_rois": Class.PLUGIN_REQUEST,
    "compile_label_volume": Class.PLUGIN_REQUEST,
    "activate_tool": Class.PLUGIN_REQUEST,
    "request_asset": Class.PLUGIN_REQUEST,
    "request_save_path": Class.PLUGIN_REQUEST,
    "get_colormap": Class.PLUGIN_REQUEST,
    "view_request": Class.PLUGIN_REQUEST,
    "extend_roi_poses": Class.PLUGIN_REQUEST,
    "request_status_query": Class.PLUGIN_REQUEST,
    "release_deposit": Class.ONE_SHOT,
    "grant_ack": Class.ONE_SHOT,
    "grant_state": Class.ONE_SHOT,
    "segment_released": Class.ONE_SHOT,
    "release_request": Class.ONE_SHOT,
    "tool_binding_ack": Class.ONE_SHOT,
    "epoch_ack": Class.ONE_SHOT,
    "time_mapping_ack": Class.ONE_SHOT,
    "stage_arm_ack": Class.ONE_SHOT,
    "doorbell_fault": Class.ONE_SHOT,
    "doorbell_final_report": Class.ONE_SHOT,
    "contract_violation": Class.ONE_SHOT,
    # §3.3: the plugin's own account of its shutdown, sent BEFORE it closes the
    # stream.  One-shot for the reason a fault report is: nothing restates it,
    # and the close that follows cannot say why.
    "shutdown_report": Class.ONE_SHOT,
}

#: §8's request/reply pairing, host→plugin.  A handler that answers with the
#: wrong arm is a protocol error this SDK would otherwise emit: the host matches
#: the id, finds a reply of a kind its call cannot decode, and the failure is
#: attributed to the message rather than to the handler.
REQUEST_REPLIES: dict[str, str] = {
    "open_reader": "reader_opened",
    "read_volume": "segment",
    "read_plane": "segment",
    "metadata": "metadata",
    "close_reader": "closed",
    "invoke_command": "command_done",
    "open_export": "export_opened",
    "export_chunk": "chunk_written",
    "finish_export": "export_done",
    "abort_export": "export_done",
    "plan_stage": "stage_planned",
}

#: The requests with a second reply besides ``Error``: an ``OpenReader`` with
#: no series set is answered ``SeriesList`` when its file holds several.
ALTERNATE_REPLIES: dict[str, str] = {
    "open_reader": "series_list",
}

#: §4 rule 8's pairing, plugin→host: the one reply each plugin request takes,
#: besides ``host_error``.  A pending request's id belongs to its reply alone,
#: so :meth:`HostClient.route_reply` faults the
#: session on any other known reply kind under it.
PLUGIN_REQUEST_REPLIES: dict[str, str] = {
    "get_view_state": "view_state",
    "read_data_volume": "data_volume",
    "read_mask_voxels": "mask_voxels",
    "read_roi_inclusion": "roi_inclusion",
    "contribute_rois": "rois_contributed",
    "get_rois": "rois",
    "extend_roi_poses": "roi_poses_extended",
    "compile_label_volume": "label_volume_compiled",
    "activate_tool": "tool_activation_changed",
    "request_asset": "asset_result",
    "request_status_query": "request_status",
    "request_save_path": "save_path_result",
    "get_colormap": "colormap_table",
    "view_request": "view_request_result",
}

#: The host→plugin arms that ANSWER a plugin request (§5.3), routed by id.
#: Everything else in ``HOST_ONE_WAY`` is an unsolicited push and carries an
#: empty id, which a receiver MUST NOT reject it for (§4 rule 4).
REPLY_KINDS: frozenset[str] = frozenset(
    {
        "view_state",
        "data_volume",
        "rois",
        "rois_contributed",
        "roi_poses_extended",
        "label_volume_compiled",
        "mask_voxels",
        "roi_inclusion",
        "tool_activation_changed",
        "asset_result",
        "save_path_result",
        "colormap_table",
        "view_request_result",
        "request_status",
        "host_error",
    }
)

#: §9's bound for the one-shot queue.  The table names it as the *host's*, and
#: the plugin's own outbound queue is given the same number for the same
#: reason: it is sized for session-rate traffic, and a peer that outruns it is
#: not one the session can recover with.
#: `DoorbellFault.transport_class` on the wire, per the IDL.
_WIRE_TRANSPORT_CLASS = {"stream": "tcp"}

ONE_SHOT_CAPACITY = 256

#: §9: the largest decoded message, and the largest inline byte field.  Both
#: are the host's bounds, and a plugin adopts them for the same reason it
#: adopts the queue capacity: they size the same traffic from the other side.
#: The message ceiling is 32 MiB, sized against the ROI pose --
#: 96 bytes per posed ROI-frame, so about 350 000 of them in one `RoiList`
#: reply -- and it binds in BOTH directions here: `max_receive_message_length`
#: is what a plugin can accept, and the host's `GetRois` reply is the message
#: that grows.  The inline field bound stays at 8 MiB.
MAX_MESSAGE_BYTES = 32 << 20
MAX_INLINE_BYTES = 8 << 20

#: §9's repeated-field bounds.  A field over its bound is refused rather than
#: decoded -- which for a protobuf receiver means refused rather than *applied*,
#: since decoding already happened in the stub.
MAX_REPEATED = 65_536
MAX_TIME_MAPPING = 1_048_576
MAX_LEVELS = 64
#: `SeriesList.entries`: bounded by the message ceiling alone.  A file can
#: hold tens of thousands of images, and a reader saying so honestly is the
#: point; the smallest entry is over 8 bytes, so 32 MiB holds fewer than this.
MAX_SERIES = 1 << 22

#: How many of the host's `ContractViolation`s about us are RETAINED as
#: exemplars; every one is counted per `where` regardless.  §12: repeated
#: failures aggregate rather than flood, and the reporting path must not
#: become the failure path -- a host reporting once a tick for a session's
#: lifetime costs a dict entry, not memory.
MAX_VIOLATIONS_RETAINED = 64

#: Retired tool handles whose tool id is remembered, so a cancellation the
#: retirement queued still reaches its tool.  Only the newest can have one in
#: flight; the rest are headroom for a burst of switches.
RETIRED_TOOLS_KEPT = 64


#: ``processing_stages.md`` §4 and §6.1: a pipeline is a u16; a position is
#: stored as ``position + 1`` and ``-position`` in a signed 16-bit field; a
#: channel index is a u16 in the slot.
STAGE_MAX_PIPELINE = 0xFFFF
STAGE_MAX_POSITION = 32766
STAGE_MAX_CHANNEL = 0xFFFF


#: Built on first use rather than at module scope: touching a pb2 enum forces
#: the protobuf import, which `_lazy.py` exists to defer.
_BRIDGES: dict = {}


def _bridges() -> dict:
    if not _BRIDGES:
        _BRIDGES["discipline"] = {
            pb2.RegionDiscipline.REGION_DISCIPLINE_CONSTANT: _memory.Discipline.CONSTANT,
            pb2.RegionDiscipline.REGION_DISCIPLINE_OWNED: _memory.Discipline.OWNED,
            pb2.RegionDiscipline.REGION_DISCIPLINE_PULSED: _memory.Discipline.PULSED,
            pb2.RegionDiscipline.REGION_DISCIPLINE_LOANED: _memory.Discipline.LOANED,
            pb2.RegionDiscipline.REGION_DISCIPLINE_STAGED: _memory.Discipline.STAGED,
        }
        _BRIDGES["producer"] = {
            pb2.RegionProducer.REGION_PRODUCER_HOST: _memory.Producer.HOST,
            pb2.RegionProducer.REGION_PRODUCER_PLUGIN: _memory.Producer.PLUGIN,
        }
        _BRIDGES["failure"] = {
            _memory.GrantFailure.RETRYABLE: pb2.GrantFailureClass.GRANT_FAILURE_RETRYABLE,
            _memory.GrantFailure.TOO_LARGE: pb2.GrantFailureClass.GRANT_FAILURE_TOO_LARGE,
            _memory.GrantFailure.MALFORMED: pb2.GrantFailureClass.GRANT_FAILURE_MALFORMED,
            _memory.GrantFailure.UNSUPPORTED: pb2.GrantFailureClass.GRANT_FAILURE_UNSUPPORTED,
            _memory.GrantFailure.FAILED: pb2.GrantFailureClass.GRANT_FAILURE_FAILED,
        }
        _BRIDGES["state"] = {
            _memory.SegmentState.NOT_PRESENT: pb2.GrantStateKind.GRANT_STATE_NOT_PRESENT,
            _memory.SegmentState.PENDING: pb2.GrantStateKind.GRANT_STATE_PENDING,
            _memory.SegmentState.INSTALLED: pb2.GrantStateKind.GRANT_STATE_INSTALLED,
            _memory.SegmentState.FAILED: pb2.GrantStateKind.GRANT_STATE_FAILED,
            _memory.SegmentState.RELEASED: pb2.GrantStateKind.GRANT_STATE_RELEASED,
        }
    return _BRIDGES


def _region(region) -> "_memory.Region":
    """One wire ``Region`` as the memory layer's.

    **An unrecognized enum value is passed through as its number**, on purpose:
    `shared_memory_protocol.md` §8's matrix says a ZERO required field is
    `malformed` and an unknown NON-ZERO one is `unsupported`, and that
    classification is the memory layer's to make -- translating here would have
    to invent one of the two.
    """
    bridges = _bridges()
    return _memory.Region(
        index=region.index,
        offset=region.offset,
        extent=region.extent,
        discipline=bridges["discipline"].get(region.discipline, region.discipline),
        producer=bridges["producer"].get(region.producer, region.producer),
        buffers=region.buffers,
        slot_bytes=region.slot_bytes,
        id=region.id,
        epoch_offset=region.epoch_offset if region.HasField("epoch_offset") else None,
    )


def classify(kind: str, *, inbound: bool = True) -> "Class | None":
    """The class of one message kind, by KIND and never by id (§4 rule 3)."""
    return (HOST_CLASSES if inbound else PLUGIN_CLASSES).get(kind)


class Refused(RuntimeError):
    """A coalescing push this queue could not admit, and did not take.

    §5.4's table gives a coalescing kind *"count, report"* under overload and
    reserves the session fault for one-shot — so a push that cannot be merged
    because it names a loan (§12) is refused rather than fatal, and refusing
    leaves the bytes with the caller, which is the end that can still reclaim
    them.  A caller that gets this has sent nothing.
    """


class SessionFault(RuntimeError):
    """A protocol error that ends the session: §7's one-shot queue at
    capacity, §4's id reused with different content, or §4's known reply of
    another kind under a pending request's id.

    There is no honest alternative to faulting a full queue: dropping an
    acknowledgement makes an arrangement permanently pending, dropping a
    release leaks a reservation for the life of the session, and dropping a
    fault report loses the reason a session died.  A peer that cannot drain
    session-rate traffic is already broken, and saying so is the proportionate
    response.  A reused id is the same judgement on correlation: two different
    messages under one id leave the receiver unable to say which one an answer
    belongs to, and guessing is worse than stopping.  The message names the
    id, quoted, so the log says which.
    """


# --------------------------------------------------------------------------
# §5.4 + §7: the outbound queues
# --------------------------------------------------------------------------


def coalescing_key(msg) -> "tuple | None":
    """§5.4's replacement key for one outbound message, or ``None``.

    The keys are ``(panel, widget, row)``, ``(overlay id)``, ``(segment id,
    region)``, for a chunked overlay ``(overlay id, level, t)`` for a chunk
    and its own ``(overlay id)`` for the label table (§5.7), and for Follow
    ``(panel)`` for an offer and ``(follow id)`` for a state (§8.15).  A ``PanelPatch``
    carries ops across *several* widgets, so it is split rather than keyed
    whole -- the key cannot be the message, which is exactly what §5.4 says.
    """
    kind = msg.WhichOneof("msg")
    if kind == "overlay_update":
        return ("overlay", msg.overlay_update.overlay_id)
    if kind == "overlay_chunk":
        chunk = msg.overlay_chunk
        t = int(chunk.t) if chunk.HasField("t") else None
        return ("chunk", chunk.overlay_id, int(chunk.level), t)
    if kind == "overlay_label_table":
        return ("label_table", msg.overlay_label_table.overlay_id)
    if kind == "segment_shortfall":
        short = msg.segment_shortfall
        return ("segment", short.segment_id, short.region)
    if kind == "offer_follow":
        return ("follow_offer", msg.offer_follow.panel_id)
    if kind == "follow_state":
        return ("follow", int(msg.follow_state.follow_id))
    if kind == "work_progress":
        work = msg.work_progress
        subject = work.WhichOneof("subject")
        return ("work", subject == "task_id", getattr(work, subject) if subject else "")
    if kind == "shape_proposal":
        return ("shape", int(msg.shape_proposal.drag_id))
    return None  # `panel_patch` has one key per op; see Outbox.send


def names_a_loan(msg) -> bool:
    """Whether a coalescing push carries a descriptor into our own arena.

    §12's carve-out: a replacement is the class working and nothing is lost --
    *"unless the superseded message named a loan.  Then something IS lost until
    the receiver returns it […] A receiver that cannot release at replacement
    time must not coalesce."*  On this side the bytes were never handed over,
    so what a supersession strands is the sender's own arena, and the rule
    generalizes: **an end that cannot release at replacement time must not
    coalesce.**

    A ``PanelPatch`` carries one through ``set_image``; an ``OverlayUpdate``
    through any item geometry served from a segment rather than inline.
    """
    return _names_bytes(msg)


def _names_bytes(msg) -> bool:
    """Any ``SegmentRef`` or ``ImageRef`` with a length, anywhere in the tree."""
    for _ in _reservations(msg):
        return True
    return False


def _known_only(msg):
    """A copy of ``msg`` without the fields this build does not know, at every
    depth.  An arrangement's content and a plan's identity are the fields the
    receiver knows (§5.5 rule 4); the operand keeps its own."""
    known = type(msg)()
    known.CopyFrom(msg)
    known.DiscardUnknownFields()
    return known


def _known_enum(enum, number: int) -> bool:
    """Whether ``number`` is a value of this build's ``enum``."""
    return number in enum.DESCRIPTOR.values_by_number


def _message_children(descriptor, value) -> list:
    """The message values under one field -- one, the repeated ones, or a
    ``map<k, Message>``'s values.  A map's ``ListFields`` value iterates its
    KEYS, so treating it as a repeated field hands scalars to a walker that
    expects messages; ``OverlayLabelVolume.label_names`` is the map in this
    vocabulary whose values are scalars, and ``InvokeCommand.args`` the one
    whose values are messages."""
    if descriptor.message_type is not None and descriptor.message_type.GetOptions().map_entry:
        entry = descriptor.message_type.fields_by_name["value"]
        return list(value.values()) if entry.type == entry.TYPE_MESSAGE else []
    repeated = getattr(descriptor, "is_repeated", None)
    if repeated is None:
        repeated = descriptor.label == descriptor.LABEL_REPEATED
    return list(value) if repeated else [value]


def _unknown_arms(msg, where: str) -> list[str]:
    """Each oneof in ``msg``'s tree that is unset while its message carries a
    field this build does not know: a later minor's arm, which decodes to an
    empty oneof and an unknown field.  Named by that field's number, the only
    name this end has for the kind."""
    found = []
    if msg.DESCRIPTOR.oneofs:
        from google.protobuf import unknown_fields

        numbers = sorted({f.field_number for f in unknown_fields.UnknownFieldSet(msg)})
        for oneof in msg.DESCRIPTOR.oneofs:
            if numbers and msg.WhichOneof(oneof.name) is None:
                found.append(
                    f"{where} is a {msg.DESCRIPTOR.name}.{oneof.name} this build does not "
                    f"know (field {', '.join(map(str, numbers))})"
                )
    for descriptor, value in msg.ListFields():
        if descriptor.type == descriptor.TYPE_MESSAGE:
            for item in _message_children(descriptor, value):
                found += _unknown_arms(item, f"{where}.{descriptor.name}")
    return found


def _reservations(msg):
    """Every ``(segment_id, offset, len)`` the message names, in tree order.

    Walked rather than enumerated: an item geometry reaches one through
    ``GeomBuffer.segment``, a widget through ``set_image``, and a message kind
    added later would reach one somewhere this function has never heard of.
    A predicate that has to be updated for each is a predicate that will not be.
    """
    for descriptor, value in msg.ListFields():
        if descriptor.type != descriptor.TYPE_MESSAGE:
            continue
        for item in _message_children(descriptor, value):
            if descriptor.message_type.name in ("SegmentRef", "ImageRef"):
                if item.len:
                    yield (item.segment_id, item.offset, item.len)
            else:
                yield from _reservations(item)


def duplicate_reservation(msg):
    """The reservation this message names twice, or ``None`` (§5.6).

    **Within one message, no two operations may name the same reservation.**
    The receiver may split a message for queueing -- a ``PanelPatch`` coalesces
    per widget -- and a split carries each part to its own disposition, so a
    reservation named twice is disposed twice while ``uses`` counted the
    message once.  The first release then frees bytes the second part still
    needs.

    **It is reachable by an author, not by the allocator**, which is why it is
    checked rather than reasoned away: ``LoanedArena`` is a bump cursor that
    cannot hand out an outstanding offset twice, but nothing stops a plugin
    from putting one ``OverlayItem`` into a draw list twice, or two items on
    one geometry buffer.  The Go SDK checks this and this one did not, on
    exactly that reasoning.
    """
    seen = set()
    for key in _reservations(msg):
        if key in seen:
            return key
        seen.add(key)
    return None


#: §5.4's bound for a widget whose ops are held whole: **256 per PENDING batch**,
#: which is §9's row and the one-shot queue's number for the one-shot queue's
#: reason -- these ops can no more be dropped than an acknowledgement can.
#:
#: **Reaching it REFUSES the push to its caller; it does not fault the session.**
#: §9's *"session fault"* and §5.4's *"refused to its caller"* are not two
#: answers to one event; they are a sender rule and a receiver backstop:
#: ``PanelPatch`` travels plugin->host only, so an SDK is always the sender here
#: and the host always the receiver.  §5.4 is what this end owes its own caller
#: when its outbound queue is full -- the caller still holds the bytes, so tell
#: it the push did not go -- and §9 is what the host does about a peer that
#: ignored that and put 257 on the wire anyway.  **A conforming SDK makes §9's
#: row unreachable**, which is what a sender rule and a receiver backstop are
#: supposed to look like.  This SDK applied the host's rule to itself and
#: faulted its own session over its own caller's overflow.
HELD_OPS_CEILING = ONE_SHOT_CAPACITY

#: What one drained widget's table ops may encode to, under §9's 1 MiB for a
#: `PanelPatch` carrying a table op, with room for its ids.
TABLE_PATCH_BUDGET = (1 << 20) - 1024
#: And what a merged table op's repeated fields may count, under §9's 65,536.
TABLE_ROWS_BUDGET = 32_768


@dataclass
class _Panel:
    """One ``(panel, widget, row)`` key's pending ops, in one of §5.4's two regimes.

    - **Value-shaped**, which is every widget in the vocabulary but one: *"a
      slider's range comes from the manifest, a checkbox, a label and an image
      each take what they are given"* -- so an op replaces the pending op for
      its property, and a property not yet pending is appended.  That is what
      coalescing is for.
    - **Order-sensitive**: *"a widget that has been sent a `set_options` is
      order-sensitive, and its pending ops are held whole: nothing is
      superseded, nothing is reordered."*  A dropdown's two properties each
      depend on the state the other establishes -- ``set_options`` carries the
      selection across a list change *by text*, and ``set_value`` is
      bounds-checked against whatever list is there now -- so an intermediate
      ``set_options`` is a step the widget's state went through rather than a
      superseded value, and no ordering of a merged patch preserves it.  Two
      rules failed before this one, and both passed tests that asserted
      operation ORDER; **the test for a merge is the state it produces.**

    **Order-sensitivity lasts as long as a ``set_options`` is PENDING, and the
    difference is a drain** (§5.4 at `1.0`).  Once the queue has been drained
    the receiver has applied the options, so two ``set_value``s arriving
    afterwards may merge: the condition a merge has to satisfy is *"no
    ``set_options`` sits between the ops being merged"*, and after a drain none
    does.  This SDK read the rule as sticky-for-the-session and was
    conservative rather than wrong; the host has always been per-batch.  The
    sticky reading costs patch size and buys nothing, since a value applied
    against an options list the receiver has already installed is exactly the
    case coalescing is for.

    Which is also what makes :data:`HELD_OPS_CEILING` sufficient at a finite
    number: it bounds ONE PENDING BATCH, not a widget's lifetime.
    """

    panel_id: str
    order_sensitive: bool = False
    #: Value-shaped regime: property -> its one pending op, first-seen order.
    ops: dict = field(default_factory=dict)
    #: Order-sensitive regime: every op since the last drain, exactly as sent.
    held: list = field(default_factory=list)
    #: A table's: the last held op is one the host will apply, so a valid
    #: patch may merge into it.
    table_tail_valid: bool = False
    #: A table's merge record: the rows that stood before the run of table
    #: ops at the tail (after the latest selection), and those of them the run
    #: has removed; the same for the last held op alone.  Before a
    #: ``set_table`` has been sent the rows are unknown, an :class:`_AllBut`.
    run_base: set = field(default_factory=set)
    run_removed: set = field(default_factory=set)
    tail_base: set = field(default_factory=set)
    tail_removed: set = field(default_factory=set)

    def add(self, op) -> "object | None":
        """Take one op; return the pending op it superseded, if any."""
        prop = op.WhichOneof("op")
        rows = self.ops.get("set_rows") if not self.order_sensitive else None
        if (
            rows is not None
            and prop == "set_rows"
            and not set(op.set_rows.keys) <= set(rows.set_rows.keys)
        ):
            # A row the pending `set_rows` dropped comes back: merged, the
            # removal would vanish and the row would not start fresh.
            # Held whole from here, as a dropdown's ops are.
            self.order_sensitive = True
            self.held = list(self.ops.values())
            self.ops = {}
        if not self.order_sensitive and prop == "set_options":
            # From here on nothing merges.  What was already pending keeps its
            # order -- the order it was sent in, less the per-property merges
            # that were legitimate while the widget was not known to be one.
            self.order_sensitive = True
            self.held = list(self.ops.values())
            self.ops = {}
        if self.order_sensitive:
            if len(self.held) >= HELD_OPS_CEILING:
                # §5.4: refused to its caller, not a session fault.  Nothing may
                # be superseded and a drop is never correct, so the honest
                # outcome is to say the push did not go -- which is also what a
                # full arena already does, and the caller still holds its bytes.
                raise Refused(
                    f"{len(self.held)} ops are held whole for one order-sensitive "
                    f"widget and the ceiling is {HELD_OPS_CEILING}; nothing may be "
                    "superseded and a drop is never correct "
                    "(control_plane_protocol.md §5.4)"
                )
            self.held.append(op)
            return None
        previous = self.ops.get(prop)
        if prop == "set_value" and "set_rows" in self.ops:
            # A list's selection names a row: after the rows pending with it,
            # not in its older place ahead of them.
            self.ops.pop(prop, None)
        self.ops[prop] = op  # replaces in place; a new property appends
        return previous

    def hold(self) -> None:
        """Keep every op in arrival order from here (a table's regime): what
        was pending keeps its order."""
        if not self.order_sensitive:
            self.order_sensitive = True
            self.held = list(self.ops.values())
            self.ops = {}

    def pending_rows(self) -> int:
        """The most rows, or deleted keys, one of this slot's table ops holds."""
        ops = self.held if self.order_sensitive else self.ops.values()
        return max((_table_rows_of(op) for op in ops), default=0)

    def pending_bytes(self) -> int:
        """The encoded size of what this slot would drain as."""
        ops = self.held if self.order_sensitive else self.ops.values()
        return sum(op.ByteSize() + 8 for op in ops)

    def take(self) -> list:
        """The pending ops as one patch's worth, leaving nothing pending.

        The regime goes with them: nothing is pending, so nothing can sit
        between two later ops, and the next batch starts mergeable again.
        """
        if self.order_sensitive:
            out, self.held = self.held, []
        else:
            out, self.ops = list(self.ops.values()), {}
        return out


class Outbox:
    """The plugin's outbound queues: never-drop, requests, and coalescing.

    §5.4 forbids one queue for the mixture — *"a mixed queue with any eviction
    policy is unsound for the mixture even where it is sound for one member"* —
    and §7 fixes the drain order: **the never-drop queue first**, then
    requests, then coalescing pushes.  Serving one-shot first cannot invert the
    problem, because one-shot traffic is bounded by work already done and is
    the completion half of the requests it competes with.

    Replies to host requests share the never-drop queue.  §7 lists five queues
    and none of them holds one, but §5.1 makes a lost reply unrecoverable for
    the session — the in-flight slot never clears — so a reply is the one thing
    that must be dropped even less readily than an acknowledgement.
    """

    def __init__(
        self,
        *,
        one_shot_capacity: int = ONE_SHOT_CAPACITY,
        on_superseded: "Callable[[object], None] | None" = None,
    ) -> None:
        self._capacity = one_shot_capacity
        self._on_superseded = on_superseded
        self._lock = threading.Lock()
        self._one_shot: list = []
        self._requests: list = []
        self._coalescing: dict[tuple, object] = {}
        self._panels: dict[tuple, _Panel] = {}
        #: A table widget's key -> the table as the host will hold it, or None
        #: while this end does not know it (§8.4's merge is exact only on a
        #: known table).  Kept across drains: the host applies what is sent.
        self._tables: dict[tuple, "_tables.TableState | None"] = {}
        #: (panel id, widget id) -> the table's declared column count, or None.
        self.table_width: "Callable[[str, str], int | None] | None" = None
        self._order: list[tuple] = []  # coalescing keys, first-seen order
        #: §12: a replacement is the class working, so it is **not** a drop --
        #: but it is worth a number, because a session doing it continuously is
        #: a session whose peer is not draining.
        self.superseded = 0
        #: Coalescing pushes refused whole (§5.4's "count, report"), which is
        #: what a value leaving without a successor looks like on this side.
        self.refused = 0
        #: Messages sent, by class name; a developer-findable surface (6).
        self.sent: dict[str, int] = {}

    # -- sending ---------------------------------------------------------

    def send(self, msg) -> None:
        """Route one outbound message to the queue its CLASS names."""
        kind = msg.WhichOneof("msg")
        duplicate = duplicate_reservation(msg)
        if duplicate is not None:
            # §5.6: refused WHOLE and counted, before admission -- so nothing
            # is half-queued and the caller still owns every loan it made.
            # Releasing here instead would be this end guessing which of the
            # two operations the author meant to keep.
            self.refused += 1
            segment_id, offset, length = duplicate
            raise Refused(
                f"a {kind} names reservation ({segment_id}, {offset}, {length}) more than "
                "once; one message names a reservation once (§5.6)"
            )
        cls = PLUGIN_CLASSES.get(kind)
        if (
            cls in (Class.ONE_SHOT, Class.COALESCING)
            and msg.id
            and kind not in ("grant_state", "ready")
        ):
            # §4 rule 4: an ordinary unsolicited push carries an EMPTY id, and
            # §5.2 says why it matters -- a push that named its subject in `id`
            # lands in the receiver's reply matcher, which is the
            # classify-by-kind rule failing where it is most expensive.
            # `GrantState` is the one push the protocol correlates, and `Ready`
            # is the exception in the other direction: its id is the plugin's
            # identity string (§3.1), non-empty by rule and never matched.
            raise ValueError(f"an unsolicited {kind} push must carry an empty id")
        if cls is None:
            # An arm this SDK does not classify would otherwise silently take a
            # queue policy it was never given; refusing is the honest failure.
            raise ValueError(f"{kind!r} has no message class (control_plane_protocol.md §5)")
        with self._lock:
            self.sent[cls.value] = self.sent.get(cls.value, 0) + 1
            if cls is Class.COALESCING:
                self._coalesce(kind, msg)
                return
            if cls is Class.PLUGIN_REQUEST:
                self._requests.append(msg)
                return
            self._admit_one_shot(msg)

    def _admit_one_shot(self, msg) -> None:
        """Append to the never-drop queue, or fault.  Called under the lock."""
        if len(self._one_shot) >= self._capacity:
            raise SessionFault(
                f"the one-shot queue is at its {self._capacity}-message capacity; "
                "dropping one is never correct (control_plane_protocol.md §7)"
            )
        self._one_shot.append(msg)

    @staticmethod
    def _keys_of(kind: str, msg) -> "list[tuple]":
        """Every replacement key one message would touch."""
        if kind == "panel_patch":
            patch = msg.panel_patch
            return [("panel", patch.panel_id, op.widget_id, op.row_key) for op in patch.ops]
        key = coalescing_key(msg)
        return [key] if key is not None else []

    def _freeze(self, keys: "list[tuple]") -> None:
        """Flush pending items for these keys ahead of an ordered message.

        Ordering *within* a queue is contractual (§5.3), and for one widget or
        one overlay it is the whole meaning -- so a message that leaves the
        coalescing queue takes what was already pending for its keys with it,
        rather than arriving before an update it was supposed to follow.
        """
        for key in keys:
            if key not in self._order:
                continue
            self._order.remove(key)
            if key[0] == "panel":
                self._admit_one_shot(_patch_of(self._panels.pop(key)))
            else:
                self._admit_one_shot(self._coalescing.pop(key))

    def _coalesce(self, kind: str, msg) -> None:
        if self._on_superseded is None and names_a_loan(msg):
            # Nothing here can hand the bytes back, so this one is not merged:
            # it goes on the never-drop queue, behind whatever was already
            # pending for its keys so that nothing overtakes them.  One more
            # message on the wire costs less than an arena that never refills.
            if kind == "panel_patch":
                # Its own keys, and any list's pending `set_rows` in the panel:
                # a cell's image depends on its list's rows, which are another
                # key, and an image that overtook them was refused for a row
                # the host did not have yet (SDK report, region_quant).  Not
                # every key of the panel -- that froze hundreds of unrelated
                # cells into the never-drop queue and refused the image once
                # they did not fit.
                panel_id = msg.panel_patch.panel_id
                own = set(self._keys_of(kind, msg))

                def pending_rows(key) -> bool:
                    slot = self._panels.get(key)
                    if slot is None or key[3]:
                        return False
                    ops = slot.held if slot.order_sensitive else slot.ops.values()
                    return any(op.WhichOneof("op") == "set_rows" for op in ops)

                keys = [
                    k
                    for k in self._order
                    if k[0] == "panel" and k[1] == panel_id and (k in own or pending_rows(k))
                ]
            else:
                keys = [key for key in self._keys_of(kind, msg) if key in self._order]
            if len(self._one_shot) + len(keys) + 1 > self._capacity:
                # Refused whole rather than half-flushed, and NOT a fault: the
                # class is coalescing, so §5.4's answer to overload is "count,
                # report", and the caller still owns bytes it can reclaim.
                self.refused += 1
                raise Refused(
                    f"a {kind} naming a loan needs {len(keys) + 1} never-drop slots "
                    f"and {self._capacity - len(self._one_shot)} are free"
                )
            self._freeze(keys)
            self._admit_one_shot(msg)
            return
        if kind == "panel_patch":
            self._coalesce_panel(msg)
            return
        key = coalescing_key(msg)
        assert key is not None
        previous = self._coalescing.get(key)
        if previous is None:
            self._order.append(key)
        else:
            self.superseded += 1
            if self._on_superseded is not None:
                # §5.6: a coalescing push may carry a descriptor, and its
                # replacement inherits the obligation.  On this side the bytes
                # were never handed over, so what the caller owes is its own
                # arena, not a message -- but owing nobody is not one of the
                # options, and a supersession that skips it strands the arena.
                self._on_superseded(previous)
        if kind == "segment_shortfall" and previous is not None:
            msg = _merge_shortfall(previous, msg)
        self._coalescing[key] = msg

    def _coalesce_panel(self, msg) -> None:
        patch = msg.panel_patch
        for op in patch.ops:
            # A list row's cell is its own key: two rows'
            # cells share the template widget's id and are different state.
            key = ("panel", patch.panel_id, op.widget_id, op.row_key)
            if op.WhichOneof("op") in _TABLE_OPS or (
                op.WhichOneof("op") == "set_value" and self._is_table(key, op)
            ):
                self._coalesce_table(key, patch.panel_id, op)
                continue
            slot = self._panels.get(key)
            if slot is None:
                # §5.4 at `1.0`: a fresh batch is mergeable, whatever this
                # widget was in the last one.  A drain is what ends the
                # regime, and a drained slot is dropped from `_panels`.
                slot = self._panels[key] = _Panel(patch.panel_id)
                self._order.append(key)
            previous = slot.add(op)
            if previous is not None:
                self.superseded += 1
                if self._on_superseded is not None and _names_bytes(previous):
                    # The op is what was superseded here, not the patch: a
                    # per-property merge replaces one op, and the bytes that
                    # need handing back are that op's.
                    self._on_superseded(
                        pb2.PluginMsg(
                            panel_patch=pb2.PanelPatch(panel_id=patch.panel_id, ops=[previous])
                        )
                    )

    def _is_table(self, key: tuple, op) -> bool:
        """A selection is a table's when the table has had an op here, or its
        panel declares it: one sent before any rows must still stay in order,
        not supersede the selection before it as a value would."""
        if key in self._tables:
            return True
        return self.table_width is not None and self.table_width(key[1], op.widget_id) is not None

    def _coalesce_table(self, key: tuple, panel_id: str, op) -> None:
        """A table's ops, kept in arrival order -- a selection is never moved
        relative to the rows it names -- and merged only where the merge is
        exact (§8.4): a whole statement supersedes the
        table ops after the latest selection; a patch merges into the table op
        before it; and both only when this end knows the table they land on
        -- the declared columns, and a whole statement sent this session -- and
        the op is one the host will apply.  An op the host will refuse is sent
        alone, so it is refused alone; without the columns or the base nothing
        merges."""
        kind = op.WhichOneof("op")
        width = self.table_width(panel_id, op.widget_id) if self.table_width else None
        state = self._tables.get(key)
        slot = self._panels.get(key)
        if slot is not None and (
            slot.pending_bytes() + op.ByteSize() + 8 > TABLE_PATCH_BUDGET
            or slot.pending_rows() + _table_rows_of(op) > TABLE_ROWS_BUDGET
        ):
            # §9: a slot drains as one patch of at most 1 MiB -- so what would
            # merge past that goes ahead, in order, rather than into one
            # message the host must refuse.
            if len(self._one_shot) >= self._capacity:
                self.refused += 1
                raise Refused(
                    f"a table op for {op.widget_id!r} would pass one message's "
                    f"bound, and the never-drop queue is at its {self._capacity}"
                )
            self._order.remove(key)
            self._admit_one_shot(_patch_of(self._panels.pop(key)))
            slot = None
        if slot is None:
            slot = self._panels[key] = _Panel(panel_id)
            self._order.append(key)
        if slot is not None and len(slot.held) >= HELD_OPS_CEILING:
            # In order, so nothing is lost by sending what is pending ahead.
            if len(self._one_shot) >= self._capacity:
                self.refused += 1
                raise Refused(
                    f"table {op.widget_id!r} has {len(slot.held)} ops pending and the "
                    f"never-drop queue is at its {self._capacity}"
                )
            self._order.remove(key)
            self._admit_one_shot(_patch_of(self._panels.pop(key)))
            slot = self._panels[key] = _Panel(panel_id)
            self._order.append(key)
        slot.hold()
        held = slot.held
        last = held[-1].WhichOneof("op") if held else None
        valid = False
        if kind == "set_value":
            # A selection replaces the one before it only when the host will
            # apply it: a refused one leaves the earlier selection standing.
            value = op.set_value
            applies = state is not None and (
                value.WhichOneof("kind") == "s" and (not value.s or value.s in state.rows)
            )
            if last == "set_value" and applies:
                held[-1] = op
                self.superseded += 1
            else:
                held.append(op)
        elif kind == "set_table":
            trial = _tables.TableState()
            valid = width is not None and _tables.apply_op(trial, op, width, op.widget_id) is None
            run = last in _TABLE_OPS
            if valid and state is not None and run:
                removed = slot.run_removed | (slot.run_base & (set(state.rows) - set(trial.rows)))
            if valid and state is not None and run and not (removed & set(trial.rows)):
                while held and held[-1].WhichOneof("op") in _TABLE_OPS:
                    held.pop()
                    self.superseded += 1
                slot.run_removed = slot.tail_removed = removed
                slot.tail_base = slot.run_base
            else:
                self._start_table_op(slot, state, trial if valid else None, run, op)
            held.append(op)
            if valid:
                state = trial
        else:
            trial = state.copy() if state is not None and width is not None else None
            valid = trial is not None and _tables.apply_op(trial, op, width, op.widget_id) is None
            merge = valid and slot.table_tail_valid and last in _TABLE_OPS
            if merge:
                removed = slot.tail_removed | (slot.tail_base & _removed_by(op, state, trial))
                merge = not (removed & set(trial.rows))
            if merge and last == "set_table":
                held[-1] = _table_applied(held[-1], op)
            elif merge:
                held[-1] = _table_folded(held[-1], op)
            else:
                self._start_table_op(slot, state, trial if valid else None, last in _TABLE_OPS, op)
                held.append(op)
            if merge:
                slot.tail_removed = removed
                slot.run_removed |= slot.run_base & _removed_by(op, state, trial)
            if valid:
                state = trial
        if kind != "set_value":
            slot.table_tail_valid = valid
        self._tables[key] = state

    @staticmethod
    def _start_table_op(slot, state, trial, run: bool, op) -> None:
        """A table op held as its own: what a later merge into it, or a whole
        statement superseding the run it is in, must not undo -- a row that
        stood before them, removed and then present again.  In order, the
        person's selection of such a row is lost; merged, the host would keep
        it.  Any row may be selected, so any such row forbids the merge; and
        before this session has stated the table, any row may stand."""
        before = set(state.rows) if state is not None else _AllBut(())
        removed = _removed_by(op, state, trial) & before
        if not run:
            slot.run_base = before
            slot.run_removed = set()
        slot.run_removed |= removed & slot.run_base
        slot.tail_base = before
        slot.tail_removed = removed

    def drop_rows(self, panel_id: str, cells, keep) -> int:
        """Drop pending cell ops for rows a newer ``set_rows`` removes (§5.4).

        A superseding ``set_rows`` keeps the slot its predecessor held, so it
        drains AHEAD of cell ops queued after that predecessor; a cell op for a
        row the new list no longer holds would then reach the host after its
        row is gone and be refused.  ``cells`` are the list's template widget
        ids -- row keys are per list, so another list's row of the same key is
        left alone.  Returns the number of ops dropped."""
        cells, keep = frozenset(cells), frozenset(keep)
        dropped = 0
        with self._lock:
            for key in [
                k
                for k in self._order
                if k[0] == "panel"
                and k[1] == panel_id
                and k[2] in cells
                and k[3]
                and k[3] not in keep
            ]:
                self._order.remove(key)
                for op in self._panels.pop(key).take():
                    dropped += 1
                    self.superseded += 1
                    if self._on_superseded is not None and _names_bytes(op):
                        self._on_superseded(
                            pb2.PluginMsg(panel_patch=pb2.PanelPatch(panel_id=panel_id, ops=[op]))
                        )
        return dropped

    # -- draining --------------------------------------------------------

    def drain(self, budget: "int | None" = None) -> list:
        """Everything ready to send, in §7's order.

        ``budget`` caps one pass, which a plugin does not need — §11a says the
        drain cap is the host's, because it exists to keep one session from
        starving another and a plugin serves one.  It is here because a test
        and a host-side harness both want it.
        """
        out: list = []
        with self._lock:
            for queue in (self._one_shot, self._requests):
                while queue and (budget is None or len(out) < budget):
                    out.append(queue.pop(0))
            for key in list(self._order):
                if budget is not None and len(out) >= budget:
                    break
                self._order.remove(key)
                if key[0] == "panel":
                    out.append(_patch_of(self._panels.pop(key)))
                else:
                    out.append(self._coalescing.pop(key))
        return out

    @property
    def order_sensitive(self) -> "frozenset[tuple]":
        """The ``(panel, widget, row)`` keys whose PENDING ops are held whole (§5.4).

        A live view of the current batch rather than a memory of the session:
        a key leaves this set at the drain that empties it.
        """
        with self._lock:
            return frozenset(k for k, slot in self._panels.items() if slot.order_sensitive)

    # -- what is waiting -------------------------------------------------

    def drain_ready(self) -> bool:
        """Whether anything is waiting to go out."""
        with self._lock:
            return bool(self._one_shot or self._requests or self._order)

    @property
    def pending_one_shot(self) -> int:
        with self._lock:
            return len(self._one_shot)

    @property
    def pending_requests(self) -> int:
        with self._lock:
            return len(self._requests)

    @property
    def pending_coalescing(self) -> int:
        with self._lock:
            return len(self._order)

    def keys(self) -> "tuple[tuple, ...]":
        """The live replacement keys, in first-seen order."""
        with self._lock:
            return tuple(self._order)


#: §9: the three fields that scale with something other than the manifest and
#: get their own number, by fully-qualified name.  Everything else takes
#: :data:`MAX_REPEATED`, which §9 calls *"a backstop against a peer that
#: ignores its own manifest"* -- so it is the general rule and these are the
#: exceptions, not the other way round.
#:
#: **The three pose arrays were the fourth**, reported as a finding and answered
#: with ``TimeMapping.t``'s number for ``TimeMapping.t``'s reason: they are the
#: time axis again, via the span an ROI is posed over.  ``pose_stretch`` is
#: ``6*span`` doubles, so the general backstop fired at **10 922 frames** -- a
#: length a calcium-imaging timelapse reaches, on a reply the host sends.
#:
#: **``SeriesList.entries`` is the fifth**, and the only one sized to be
#: unreachable: a file's image count is the data's, not the manifest's, so the
#: message ceiling is its bound and :data:`MAX_SERIES` only says so.
#:
#: **The message ceiling bites before any of these does**, which §9 now says
#: under the table: one ROI posed over 174 762 frames is ~8 MiB of
#: ``pose_stretch`` alone, so a `GetRois` reply carrying two of them fails as a
#: MESSAGE whatever the field bound says.  That is vizi's to resolve and is
#: flagged in the text so it is not resolved by accident; nothing here should be
#: sized against these numbers as though they were reachable.
#:
#: Keyed on ``descriptor.full_name``, which carries the proto PACKAGE -- and the
#: package is the protocol major on the wire, so it moves exactly when the major
#: does.  A key that no longer resolves is not an error here:
#: :func:`_walk_bounds` falls back to ``MAX_REPEATED`` on a miss, which would
#: quietly re-bound ``TimeMapping.t`` at 65 536.  So
#: :func:`field_bounds_resolve` checks every key against the descriptor pool,
#: and `tests/test_control.py` runs it -- the check lives beside the table it
#: guards rather than in the test alone.
_FIELD_BOUNDS: dict[str, int] = {
    "vizi.plugin.v1.TimeMapping.t": MAX_TIME_MAPPING,
    "pois_math.v1.Roi.pose_translation": MAX_TIME_MAPPING,
    "pois_math.v1.Roi.pose_rotation": MAX_TIME_MAPPING,
    "pois_math.v1.Roi.pose_stretch": MAX_TIME_MAPPING,
    "vizi.plugin.v1.RoiPoseExtension.pose_translation": MAX_TIME_MAPPING,
    "vizi.plugin.v1.RoiPoseExtension.pose_rotation": MAX_TIME_MAPPING,
    "vizi.plugin.v1.RoiPoseExtension.pose_stretch": MAX_TIME_MAPPING,
    "vizi.plugin.v1.RoiList.rois": MAX_REPEATED,
    "vizi.plugin.v1.Geometry.levels": MAX_LEVELS,
    "vizi.plugin.v1.SeriesList.entries": MAX_SERIES,
}


def field_bounds_resolve() -> "list[str]":
    """The keys of :data:`_FIELD_BOUNDS` that name no field in the loaded
    descriptor pool -- empty when the table matches the stubs it is written
    for.  A key that misses would fall back to the general bound in
    :func:`_walk_bounds` and nothing would say so."""
    from google.protobuf import descriptor_pool

    pool = descriptor_pool.Default()
    pb2.HostMsg.DESCRIPTOR  # noqa: B018 -- resolve the lazy stubs into the pool
    missing = []
    for full_name in _FIELD_BOUNDS:
        message, _, field = full_name.rpartition(".")
        try:
            if field not in pool.FindMessageTypeByName(message).fields_by_name:
                missing.append(full_name)
        except KeyError:
            missing.append(full_name)
    return missing


def _over_bound(kind: str, hm) -> "tuple[str, str] | None":
    """§9's repeated-field bound, on **every** repeated field.

    Walked by reflection rather than enumerated, for the reason
    :func:`_iter_geom_buffers` gives about geometry: a hand-maintained list of
    the fields worth checking is a list that has to grow with the IDL and
    therefore will not.  This one enumerated three and missed the rest, which
    left `PanelPatch.ops`, `EpochChange.panes`, `Geometry.channels` and
    `InstallSegment.regions` with no backstop at all -- found by reading the Go
    SDK, which walks every message.

    ``kind`` is unused and kept because every caller has it and a bound that
    quietly became message-wide should not look like a per-kind check.
    """
    return _walk_bounds(hm)


def walk_bounds(message) -> "tuple[str, str] | None":
    """§9's two per-field bounds over a decoded message, by reflection:
    ``(rule, detail)`` for the first field over its bound -- ``"repeated"``
    for a repeated field or map past its count, ``"inline"`` for a ``bytes``
    or ``string`` past `MAX_INLINE_BYTES` -- or None.

    Both ends walk the same message the same way: the SDK on every host
    message, and the host on every plugin message.  A string is an inline byte field
    for this bound -- bytes with an encoding, and `MetadataResult.text` is the
    case that makes it matter.
    """
    return _walk_bounds(message)


def _walk_bounds(message) -> "tuple[str, str] | None":
    """Depth-first, and the first thing over its bound wins.

    A map counts like a repeated field -- ``InvokeCommand.args`` is the one in
    this vocabulary -- and its values are walked, since a `Value` is a message.
    """
    for descriptor, value in message.ListFields():
        # `server._field_is_repeated`'s reasoning, and it is not optional: a
        # bare `descriptor.label` reads absent on some protobuf builds and a bare
        # `is_repeated` on others.
        repeated = getattr(descriptor, "is_repeated", None)
        repeated = (
            descriptor.label == descriptor.LABEL_REPEATED if repeated is None else bool(repeated)
        )
        mapping = (
            repeated
            and descriptor.message_type is not None
            and (descriptor.message_type.GetOptions().map_entry)
        )
        if repeated:
            limit = _FIELD_BOUNDS.get(descriptor.full_name, MAX_REPEATED)
            if len(value) > limit:
                return ("repeated", f"{descriptor.full_name} carries {len(value)}, over {limit}")
        if descriptor.type in (descriptor.TYPE_BYTES, descriptor.TYPE_STRING):
            for item in value if repeated else (value,):
                if len(item) > MAX_INLINE_BYTES:
                    return (
                        "inline",
                        f"{descriptor.full_name} carries {len(item)} bytes inline, over "
                        f"{MAX_INLINE_BYTES}",
                    )
            continue
        if descriptor.type != descriptor.TYPE_MESSAGE:
            continue
        if mapping:
            entry = descriptor.message_type.fields_by_name["value"]
            children = list(value.values()) if entry.type == entry.TYPE_MESSAGE else ()
        else:
            children = list(value) if repeated else [value]
        for child in children:
            found = _walk_bounds(child)
            if found is not None:
                return found
    return None


def _doorbell_failure(name: str) -> int:
    """The transport's spelling as ``DoorbellFailureClass`` (§12).

    **There is no legal fallback**, and that is the IDL's own rule rather than
    a shortage of imagination: *"ENUM ZERO IS NEVER A VALUE.  Every enum here
    has an `_UNSPECIFIED = 0` that is a protocol error where it appears, not a
    default."*  So an unrecognized class cannot be sent as `UNSPECIFIED` -- that
    would be reporting a fault by committing one.  It raises instead, and
    :meth:`ControlPlane.doorbell_fault` keeps the whole fault as a local
    diagnostic so the reason is not lost where it still can be kept.

    It means a class this SDK does not know has **no way to reach the host**,
    which §7 says must never happen to a fault report.  The enum has no
    non-zero catch-all, and §12 does not say the transport's class set is
    closed, so a mismatch here is treated as an SDK defect.
    """
    try:
        return pb2.DoorbellFailureClass.Value(f"DOORBELL_FAILURE_{name.upper()}")
    except ValueError as exc:
        raise ValueError(f"no DoorbellFailureClass for {name!r}") from exc


class _AllBut:
    """Every row key but ``but``: the rows a table may hold before this
    session has stated it.  A relaunched panel keeps its rows, so until a
    ``set_table`` has been sent any row may be present, and selected."""

    __slots__ = ("but",)

    def __init__(self, but) -> None:
        self.but = frozenset(but)

    def __and__(self, other):
        if isinstance(other, _AllBut):
            return _AllBut(self.but | other.but)
        return set(other) - self.but

    __rand__ = __and__

    def __or__(self, other):
        if isinstance(other, _AllBut):
            return _AllBut(self.but & other.but)
        return _AllBut(self.but - set(other))

    __ror__ = __or__


def _removed_by(op, state, trial):
    """The rows an op takes away from ``state``: those ``trial`` no longer
    holds, and a patch's deletes -- a key deleted and upserted in one patch is
    a fresh row (§8.4).  On a table not yet stated (``state`` is ``None``) the
    op is taken as applied to rows unknown: a ``set_table`` removes every row
    but its own, a patch the rows it deletes."""
    if state is None:
        kind = op.WhichOneof("op")
        if kind == "set_table":
            return _AllBut(row.key for row in op.set_table.rows)
        if kind == "patch_table":
            return set(op.patch_table.delete)
        return set()
    if trial is None:
        return set()
    gone = set(state.rows) - set(trial.rows)
    if op.WhichOneof("op") == "patch_table":
        gone |= set(op.patch_table.delete) & set(state.rows)
    return gone


def _table_rows_of(op) -> int:
    """The larger repeated field of a table op: its rows, or its deletes."""
    kind = op.WhichOneof("op")
    if kind == "set_table":
        return len(op.set_table.rows)
    if kind == "patch_table":
        return max(len(op.patch_table.upsert), len(op.patch_table.delete))
    return 0


#: The ops that change a table's rows.
_TABLE_OPS = frozenset({"set_table", "patch_table"})


def _table_applied(whole, patch):
    """A pending ``set_table`` with a later ``patch_table`` applied to it: the
    rows the host would hold after both, as one statement."""
    gone = set(patch.patch_table.delete)
    rows = [r for r in whole.set_table.rows if r.key not in gone]
    at = {r.key: i for i, r in enumerate(rows)}
    for row in patch.patch_table.upsert:
        i = at.get(row.key)
        if i is None:
            at[row.key] = len(rows)
            rows.append(row)
        else:
            rows[i] = row
    return pb2.PatchOp(
        widget_id=whole.widget_id, row_key=whole.row_key, set_table=pb2.TableRows(rows=rows)
    )


def _table_folded(earlier, later):
    """Two ``patch_table``s as one, applied as the two in order would be: the
    deletes of both, then the upserts in order.  A key the later one deletes
    leaves the earlier one's upserts; one it upserts after a delete stays
    deleted AND upserted, so the host still appends it fresh."""
    deletes = list(dict.fromkeys(earlier.patch_table.delete))
    upserts = {r.key: r for r in earlier.patch_table.upsert}
    for key in later.patch_table.delete:
        upserts.pop(key, None)
        if key not in deletes:
            deletes.append(key)
    for row in later.patch_table.upsert:
        upserts[row.key] = row  # in place when already pending, else last
    return pb2.PatchOp(
        widget_id=earlier.widget_id,
        row_key=earlier.row_key,
        patch_table=pb2.TablePatch(delete=deletes, upsert=list(upserts.values())),
    )


def _patch_of(slot: _Panel):
    """One ``PanelPatch`` carrying everything pending for a widget."""
    return pb2.PluginMsg(panel_patch=pb2.PanelPatch(panel_id=slot.panel_id, ops=slot.take()))


def _merge_shortfall(previous, current):
    """§5.4/§5.6: largest ``wanted_bytes``, summed ``dropped``.

    A re-grant clearing the biggest shortfall clears the smaller ones, and the
    drop count is a total §12 needs -- a replace would discard exactly the
    number the message exists to carry.

    **``largest_free_bytes`` travels WITH the ``wanted_bytes`` it was measured
    beside**, and neither a maximum nor the newest value is right: the pair is
    one observation.  The host reads them together to tell exhaustion from
    fragmentation, so a want from one moment beside a free run from another
    describes an arena that never existed.  This SDK took the latest until
    `1.0` said so.
    """
    old, new = previous.segment_shortfall, current.segment_shortfall
    kept = new if new.wanted_bytes > old.wanted_bytes else old
    return pb2.PluginMsg(
        segment_shortfall=pb2.SegmentShortfall(
            segment_id=new.segment_id,
            region=new.region,
            wanted_bytes=kept.wanted_bytes,
            largest_free_bytes=kept.largest_free_bytes,
            dropped=old.dropped + new.dropped,
        )
    )


# --------------------------------------------------------------------------
# Inbound: what the caller must still do
# --------------------------------------------------------------------------


class Action(enum.Enum):
    """What :meth:`ControlPlane.receive` leaves for its caller."""

    HANDLED = "handled"  #: applied, acknowledged, routed — nothing further
    HOST_REQUEST = "host_request"  #: run the handler OFF the dispatch path (§6)
    HELLO = "hello"  #: the handshake is the caller's (§3.1)
    PANEL_OPENED = "panel_opened"  #: a lifecycle callback the caller owns
    WIDGET_GEOMETRY = "widget_geometry"  #: §8.4: an image widget's size; the caller's handler
    APPEARANCE = "appearance"  #: §8.4: the host's theme and colors; the caller's handler
    FOLLOW = "follow"  #: a Follow started or ended (§8.15); the caller drives it
    SHUTDOWN = "shutdown"  #: §3.3's window opens; the caller closes the stream
    CANCEL_WORK = "cancel_work"  #: §8.16: stop the named work; the caller's registry holds it
    DOORBELL_SHUTDOWN = "doorbell_shutdown"  #: §8.13's window opens; the caller stops the doorbell
    SHAPE = "shape"  #: §8.8: a reshape of a region whose type this plugin shapes
    UNKNOWN = "unknown"  #: a kind this SDK predates — counted, never reported
    RESENT = "resent"  #: §4 rule 7: a byte-identical re-send under a used id — not dispatched again


@dataclass(frozen=True)
class ChannelName:
    """One channel's name, and where it came from.

    ``name`` is never empty: a channel nothing named is shown as
    ``"Channel N"``, 1-based, the label the person sees -- and ``source`` is
    then ``"PLACEHOLDER"``, which a plugin MUST NOT write into an artifact as
    the file's own name.  The others are ``"FILE"`` (the file or its reader
    named it), ``"USER"`` (the person did) and ``"UNSPECIFIED"``; a source this
    SDK does not know is its number as a string."""

    index: int
    name: str
    source: str = "UNSPECIFIED"

    @property
    def declared(self) -> bool:
        """Whether a person or a file chose this name -- anything but a
        placeholder the host made up."""
        return self.source in ("FILE", "USER")

    @classmethod
    def from_wire(cls, info) -> "ChannelName":
        try:
            source = pb2.ChannelNameSource.Name(info.source).removeprefix("CHANNEL_NAME_SOURCE_")
        except ValueError:
            source = str(int(info.source))
        return cls(index=int(info.index), name=str(info.name), source=source)


@dataclass(frozen=True)
class PaneState:
    """What the host arranged for one pane, as this plugin acknowledged it
    (`control_plane_protocol.md` §8.10, §8.11): the dataset geometry the
    epoch stands for, the visible channels at index >= 15, and the time
    mapping.  Read-only and already installed -- the gate guarantees an ``E``
    record under this epoch arrives only after this state did, so a tool
    handler reads it with no round trip.

    ``geometry`` is the IDL's ``Geometry`` whole; the properties below are the
    questions a tool asks of it.
    """

    view_id: int
    epoch: int
    gen: int
    geometry: object  # pb2.Geometry
    channels_high: tuple[int, ...]
    time_mapping_revision: int
    #: One time per index, ascending, when a mapping was arranged for this
    #: generation; ``None`` means unknown, never "multiply by a constant".
    time_table: "tuple[float, ...] | None"

    @property
    def spacing_zyx(self) -> "tuple[float, float, float]":
        """Physical voxel size, ZYX -- real only when ``any_axis_known``."""
        g = self.geometry
        return (float(g.spacing_z), float(g.spacing_y), float(g.spacing_x))

    @property
    def spacing_unit(self) -> str:
        return str(self.geometry.spacing_unit)

    @property
    def any_axis_known(self) -> bool:
        """§8.11: the display fallback is not a measurement, and the unit
        being empty is the one thing that lets a plugin tell."""
        return bool(self.geometry.spacing_unit)

    @property
    def extent(self) -> "tuple[tuple[int, int], tuple[int, int], tuple[int, int]]":
        """Level-0 ``((z0, z_n), (y0, y_n), (x0, x_n))``."""
        e = self.geometry.extent
        return ((int(e.z0), int(e.z_n)), (int(e.y0), int(e.y_n)), (int(e.x0), int(e.x_n)))

    @property
    def crop(self) -> EdgeBox:
        """The clip box in level-0 edge coordinates.  :meth:`crop_at` is the
        conversion to a level -- it applies the factor the host declared,
        which ``EdgeBox.at_level`` does not."""
        c = self.geometry.crop
        return EdgeBox(c.z0, c.z1, c.y0, c.y1, c.x0, c.x1)

    def crop_at(self, level: int) -> EdgeBox:
        """The clip box at ``level``, converted with the factor the host
        declared for that level, per axis (§8.11's map, never an assumed
        ``2^L``).  Convert to the level FIRST, then to centers."""
        return self.crop.at_scale(self.level_scale(level))

    def level_scale(self, level: int) -> "tuple[float, float, float]":
        """The sampling factor of ``level`` relative to level 0, ZYX, as the
        host declared it -- the number §8.11's level map applies directly."""
        levels = self.geometry.levels
        if level < 0 or level >= len(levels):
            raise IndexError(f"level {level} is not one of the {len(levels)} declared")
        s = levels[level]
        return (float(s.z), float(s.y), float(s.x))

    def visible_channels(self, channels: int) -> frozenset[int]:
        """Resolve an ``E`` record's ``channels`` word to the set it names:
        bits 0-14 as themselves, bit 15 as this epoch's ``channels_high``."""
        low = {i for i in range(15) if channels >> i & 1}
        high = set(self.channels_high) if channels & 0x8000 else set()
        return frozenset(low | high)

    def channel_names(self) -> "dict[int, str]":
        return {int(ch.index): str(ch.name) for ch in self.geometry.channels}

    def channels(self) -> "tuple[ChannelName, ...]":
        """Every channel with its name and where the name came from."""
        return tuple(ChannelName.from_wire(ch) for ch in self.geometry.channels)


#: §4 rule 7's window: how many received ids a receiver remembers per session.
ID_WINDOW = 65_536


class IdLedger:
    """§4 rule 7: ids are never reused, and this end checks what it receives.

    Two tables, keyed by id, each holding the SHA-256 of the message's wire
    bytes: the host's messages that deserve a reply (its requests, a
    `GrantStateQuery`, a kind this SDK predates that carries an id), and the
    host's replies to this plugin's requests (`HostError` included).  They are
    apart because a reply echoes the requester's id: a host request minted
    ``r<n>`` and a reply echoing this plugin's ``q<n>`` live in different id
    spaces, and an unknown kind under ``q<n>`` -- answered `UNSUPPORTED`, never a
    reply (§11) -- does not use up the id the real reply arrives under.

    :meth:`observe` says ``"new"``, ``"resent"`` (byte-identical to the message
    already recorded under the id: an at-least-once re-send, not dispatched
    again) or ``"reused"`` (different bytes: a protocol error).  The window is
    the last :data:`ID_WINDOW` ids received across both tables; an older id is
    forgotten and not checked, which is the stated bound rather than a memory
    that grows for the life of the session.
    """

    def __init__(self, window: int = ID_WINDOW) -> None:
        self.window = window
        self.requests: dict[str, bytes] = {}
        self.replies: dict[str, bytes] = {}
        self._order: collections.deque = collections.deque()
        self._lock = threading.Lock()

    def observe(self, table: "dict[str, bytes]", hm) -> str:
        digest = hashlib.sha256(hm.SerializeToString(deterministic=True)).digest()
        with self._lock:
            seen = table.get(hm.id)
            if seen is not None:
                return "resent" if seen == digest else "reused"
            table[hm.id] = digest
            self._order.append((table, hm.id))
            if len(self._order) > self.window:
                old_table, old = self._order.popleft()
                old_table.pop(old, None)
            return "new"


@dataclass(frozen=True)
class Routing:
    """One classified message and what became of it."""

    kind: str
    cls: "Class | None"
    action: Action
    #: Local, aggregated notes: a stray reply, an unclassified kind, a refused
    #: bound.  Never a ``ContractViolation`` -- those go to the reporter.
    diagnostics: tuple[str, ...] = ()
    #: ``(where, detail)`` pairs reported as ``ContractViolation`` (§11).
    violations: tuple[tuple[str, str], ...] = ()
    #: True when a descriptor is held unread until its ``M`` lands (§8.12).
    deferred: bool = False


# --------------------------------------------------------------------------
# The binding
# --------------------------------------------------------------------------


class ControlPlane:
    """Classify, apply, acknowledge — the plugin half of §5 through §9.

    ``memory`` is a :class:`~vizi_plugin_sdk.memory.SegmentStore`, ``transport``
    is the doorbell (for §5.5's install-before-ack calls), ``reporter`` is a
    :class:`~vizi_plugin_sdk.violations.ViolationReporter`, and
    ``deliver_reply`` returns whether a waiter took a reply -- ``False`` puts
    §4's resource rule into play.  Every one of them is optional, because each
    is a different half of a session and a test wants one at a time.
    """

    def __init__(
        self,
        *,
        memory: "_memory.SegmentStore | None" = None,
        transport=None,
        reporter=None,
        outbox: "Outbox | None" = None,
        deliver_reply: "Callable[[object], bool] | None" = None,
        on_deferred_request: "Callable[[object], None] | None" = None,
        on_tool_binding: "Callable[[str, bool, object], None] | None" = None,
        on_chunks_wanted: "Callable[[str, list], None] | None" = None,
        on_subscribed: "Callable[[str, object], None] | None" = None,
        on_release_loan: "Callable[[int], None] | None" = None,
        on_stage_arm: "Callable[[object], None] | None" = None,
        on_installed: "Callable[[int], None] | None" = None,
    ) -> None:
        self.memory = memory
        self.transport = transport
        self.reporter = reporter
        self.outbox = outbox if outbox is not None else Outbox()
        self._deliver_reply = deliver_reply
        #: Where a request held for its capability goes when the mapping lands.
        self._on_deferred_request = on_deferred_request
        #: Told ``(tool_id, active)`` after a `ToolBinding` is installed or
        #: retired -- the author-facing half of tool state the wire carries.
        #: Called outside the lock, after this end's own
        #: handling; a hook that raises is the caller's to contain.
        self._on_tool_binding = on_tool_binding
        #: Told ``(overlay_id, wants)`` -- the WHOLE set, in the host's order --
        #: on every `ChunksWanted` (§5.7).  Same terms as the binding hook:
        #: outside the lock, after this end's own bookkeeping.
        self._on_chunks_wanted = on_chunks_wanted
        #: §8.7's subscribed pushes: ``(kind, message)``, the latest.
        self._on_subscribed = on_subscribed
        #: Told the segment id after a `ReleaseLoan` is applied: the arena has
        #: room it did not have, which is what a deferred chunk publish waits
        #: for (§5.7).
        self._on_release_loan = on_release_loan
        #: Told the `StageArm` after it is installed, so the stage runner looks
        #: at the ring at once.  Same terms as the binding hook.
        self._on_stage_arm = on_stage_arm
        #: Told the segment id once a grant is installed (both halves landed):
        #: a panel's images held until it had a segment go then.
        self._on_installed = on_installed
        self._lock = threading.RLock()
        #: overlay_id -> the last `ChunksWanted` list the host sent, as
        #: ``(level, t | None)`` pairs in its order.
        self.chunks_wanted: dict[str, list[tuple[int, "int | None"]]] = {}
        #: (panel id, widget id) -> the `WidgetGeometry` last heard (§8.4).
        self.widget_geometries: dict[tuple[str, str], object] = {}
        #: The `HostAppearance` last heard (§8.4), ``None`` before the first.
        self.appearance: "object | None" = None
        #: `ReleaseLoan`s that crossed this end's own `SegmentReleased` (§9.3):
        #: a normal retirement, so counted here and never reported -- when each
        #: names a loan that was out at the release, once.
        self.crossed_releases = 0
        #: view_id -> the epoch this plugin has acknowledged (§8.10).
        self.epochs: dict[int, int] = {}
        #: view_id -> the (epoch, gen, channels_high) it acknowledged, for the
        #: re-ack rule's "same identity, different content" check.
        self._pane_state: dict[int, tuple] = {}
        #: gen -> the acknowledged `time_mapping_revision`.
        self.mappings: dict[int, int] = {}
        #: gen -> the acknowledged mapping's table, one time per index (§8.11).
        #: Kept so a tool can read it off :meth:`pane_state` without a round
        #: trip; pruned with the dataset when a newer gen replaces it.
        self.time_tables: dict[int, tuple[float, ...]] = {}
        #: gen -> the `DatasetState` last acknowledged for it.
        self.datasets: dict[int, object] = {}
        #: tool handle -> the `ToolBinding` installed for it (§8.10).
        self.tools: dict[int, object] = {}
        #: tool handle -> the tool id a RETIRED handle named, newest last.  The
        #: retirement's cancellation of an open gesture is drained on the
        #: doorbell's listener after the binding has gone from `tools`, and
        #: the tool must still hear its `cancel` (`E` §2).
        self.retired_tool_ids: collections.OrderedDict[int, str] = collections.OrderedDict()
        #: ``(pipeline, position)`` -> the `StageArm` installed there
        #: (``processing_stages.md`` §4).  Kept after its ring retires: the arm's
        #: identity outlives the ring, and the host re-arms it at the next
        #: revision against the ring that supersedes it.
        self.stage_arms: dict[tuple[int, int], object] = {}
        #: Every arrangement identity a statement has NAMED -- a tool handle,
        #: a stage position -- whether it was installed or refused (§5.5 rule
        #: 4).  An identity's first statement is the first one RECEIVED, so a
        #: host that re-states at revision 2 after its revision 1 was refused
        #: (a later minor's mode or kind, an unusable arm) is not reported for
        #: starting in the wrong place: it did not.
        self.stated: set[tuple[str, object]] = set()
        #: The stage ids this plugin declared, or ``None`` when this end was not
        #: told -- in which case a `StageArm`'s `stage_id` is taken on trust, as
        #: a grant's `subject` is (`shared_memory_protocol.md` §8).
        self.declared_stages: "frozenset[str] | None" = None
        #: Why an arm's ``params`` do not match its stage's declaration, or
        #: ``None`` -- supplied by whoever holds the declarations.
        self.stage_arm_check: "Callable[[object], str | None] | None" = None
        #: Descriptors held unread until their capability lands (§8.12).
        self._deferred: dict[int, list] = {}
        #: Segments in *retiring* whose held claims have not finished (§9.1).
        self._retiring: set[int] = set()
        #: §8.13: exactly one `DoorbellFault` per terminal event, however many
        #: views of it the transport offers.
        self._fault_sent = False
        #: Messages refused for a repeated field over §9's bound.
        self.refused_bounds = 0
        #: ``(segment_id, offset, len)`` -> declared uses not yet disposed of.
        self._uses: dict[tuple[int, int, int], int] = {}
        #: Descriptors whose region this end could not resolve, so no release
        #: was sent.  Counted because a silent one would be a leak (§12).
        self.unreleasable = 0
        #: Local notes a developer can read, newest last, bounded at 256.
        self.diagnostics: list[str] = []
        #: Held descriptors dropped because their grant never completed (§12
        #: wants every value that left without a successor counted).
        self.dropped_descriptors = 0
        #: Kinds this SDK does not classify, counted (§11's forward rule).
        self.unknown_kinds: dict[str, int] = {}
        #: `ContractViolation`s the HOST reported about us — symmetric since
        #: 1.0, and report-and-continue at both ends.  The first
        #: `MAX_VIOLATIONS_RETAINED` are kept; `violations_received_counts`
        #: totals every one per `where`.
        self.violations_received: list = []
        self.violations_received_counts: dict[int, int] = {}
        #: The host's `DoorbellShutdown`, if one arrived (§8.13).
        self.doorbell_shutdown = None
        #: §3.1's two identity strings: the host's `Hello.id`, once the
        #: servicer has checked its form, and this plugin's `Ready.id`.  A
        #: session log names exactly which two builds spoke; neither is ever
        #: matched against anything.
        self.host_identity: str | None = None
        #: ``(major, minor)`` from the host's `Hello`, once it has arrived.
        self.host_protocol: tuple[int, int] | None = None
        #: ``(major, minor)`` this plugin's `Ready` announced, once it has: the
        #: host's minor clamped to this SDK's, which is the minor the session
        #: runs at (§3.1).  Features are gated on it, never on `host_protocol`.
        self.session_protocol: tuple[int, int] | None = None
        self.plugin_identity: str | None = None
        #: §3.3: the session's `Shutdown` has arrived and the window is open.
        #: New work is refused at this end from then on.
        self.shutting_down = False
        #: Host requests answered with an `Error` inside the window, unrun.
        self.refused_in_teardown = 0
        #: §4 rule 7: the ids this end received, and the re-sends it dropped.
        self.ids = IdLedger()
        self.resent = 0

    # -- the entry point --------------------------------------------------

    def receive(self, hm) -> Routing:
        """Classify one ``HostMsg``, act on what is ours, and say what is not.

        **Classification happens before any id matching** (§4 rule 3): a push
        arriving while a request is pending has an id that matches nothing, and
        matching first drops it as a stray.

        Whatever the routing noted goes through :meth:`diagnose` as well, so a
        note the caller does not read still reaches the surface §12 names.
        """
        routing = self._route(hm)
        for note in routing.diagnostics:
            self.diagnose(note)
        return routing

    def _id_table(self, kind, cls) -> "dict[str, bytes] | None":
        """Which of §4 rule 7's tables a message with an id is recorded in:
        one that deserves a reply, a reply, or neither (``None``)."""
        if cls is None or cls is Class.HOST_REQUEST or kind == "grant_state_query":
            return self.ids.requests
        if kind in REPLY_KINDS:
            return self.ids.replies
        return None

    def _route(self, hm) -> Routing:
        kind = hm.WhichOneof("msg")
        cls = HOST_CLASSES.get(kind) if kind else None
        table = self._id_table(kind, cls) if hm.id else None
        if table is not None:
            # §4 rule 7, ahead of everything else this end does with the
            # message: a re-send is not dispatched twice, and a reused id is
            # not dispatched at all.
            seen = self.ids.observe(table, hm)
            if seen == "resent":
                with self._lock:
                    self.resent += 1
                return Routing(
                    kind or "",
                    cls,
                    Action.RESENT,
                    diagnostics=(
                        f"{kind or 'an unknown kind'} {hm.id!r} re-sent byte for byte; "
                        "not dispatched again",
                    ),
                )
            if seen == "reused":
                raise SessionFault(
                    f"the host reused id {hm.id!r} with different content "
                    f"({kind or 'a kind this SDK predates'}); ids are never reused "
                    "(control_plane_protocol.md §4)"
                )
        if cls is None:
            with self._lock:
                self.unknown_kinds[kind or ""] = self.unknown_kinds.get(kind or "", 0) + 1
            if hm.id and self._deliver_reply is not None:
                # §4 rule 8: an unknown kind is NEVER a reply, and under the id
                # of one of this plugin's pending requests it faults the session
                # -- the waiter is woken with PROTOCOL, never left to its timeout.
                # The id tables are apart, so had it not faulted, the real reply
                # would not have been taken for a reuse.
                self._deliver_reply(hm)
            if hm.id:
                # §11: under any other id, a kind this SDK predates that carries
                # an id is a request whose sender is blocked on it: refused by
                # name, never left to its timeout.  A push of one stays a
                # diagnostic.
                self.outbox.send(
                    pb2.PluginMsg(
                        id=hm.id,
                        error=pb2.Error(
                            kind=pb2.Error.UNSUPPORTED,
                            message="this plugin does not know that message kind; "
                            "it speaks protocol 1.0",
                        ),
                    )
                )
            return Routing(
                kind or "",
                None,
                Action.UNKNOWN,
                diagnostics=(f"unclassified message kind {kind!r}",),
            )
        found = _over_bound(kind, hm)
        if found is not None:
            rule, over = found
            # §9/§13: a repeated field over its bound is refused rather than
            # **applied** -- "refused rather than decoded" was the earlier
            # wording and asks a protobuf receiver for something it cannot do,
            # since decoding is what produces the count.  So: decode, refuse to
            # act, and report.  A refused reply leaves its requester to time
            # out, which is the honest outcome: the host sent what the contract
            # does not allow.
            with self._lock:
                self.refused_bounds = getattr(self, "refused_bounds", 0) + 1
            # `repeated_field_overflow` is site 21: refusing to apply and
            # keeping only a local diagnostic would leave the host learning
            # nothing.
            if rule == "repeated":
                self._report([("repeated_field_overflow", over)])
            else:
                # An inline field over 8 MiB has no site of its own; the refusal
                # is answered where there is a reply and diagnosed locally.
                self.diagnose(f"refused: {over} (control_plane_protocol.md §9)")
            # **Refusing to apply it is not a license to say nothing.**  §11a's
            # *answer every host request* is a plugin obligation and an `Error`
            # is a legal answer to any request (§8), while §5.1 makes silence
            # expensive in a way this end can see and the host cannot: the
            # in-flight slot stays occupied and the caller waits out a deadline
            # for an answer that already exists.  A reply we refuse is the same
            # argument from the other side -- the waiter is ours, and leaving it
            # to time out costs a session the same way.
            #
            # This SDK let both time out, reasoning that a request the contract
            # does not allow deserves no answer.  The Go SDK answered both, and
            # it is right: refusing to *act* is what §13 asks for, and refusing
            # to *speak* is a second failure added to the peer's first.
            if cls is Class.HOST_REQUEST and hm.id:
                self.outbox.send(
                    pb2.PluginMsg(
                        id=hm.id,
                        error=pb2.Error(
                            kind=pb2.Error.PROTOCOL,
                            message=f"refused: {over} (control_plane_protocol.md §9)",
                        ),
                    )
                )
            elif kind in REPLY_KINDS and hm.id and self._deliver_reply is not None:
                self._deliver_reply(
                    pb2.HostMsg(
                        id=hm.id,
                        host_error=pb2.Error(kind=pb2.Error.PROTOCOL, message=over),
                    )
                )
            return Routing(kind, cls, Action.HANDLED, diagnostics=(over,))
        if cls is Class.HOST_REQUEST:
            if not hm.id:
                # §4 rule 1: a request carries a non-empty id, never reused.
                # A reply echoes it, so a request with none can only be
                # answered by a message that reads as an unsolicited push --
                # the host's call would never complete.
                self._report([("zero_id", f"a {kind} request carries no id")])
                return Routing(
                    kind,
                    cls,
                    Action.HANDLED,
                    diagnostics=(f"{kind} request with an empty id, not dispatched",),
                    violations=(("zero_id", f"a {kind} request carries no id"),),
                )
            return self._request(kind, hm)
        if cls is Class.ARRANGEMENT:
            return self._arrangement(kind, hm)
        if kind in ("shape_drag", "shape_commit", "shape_edit_ended"):
            return Routing(kind, cls, Action.SHAPE)
        if cls is Class.HOST_COALESCING:
            return self._host_coalescing(kind, hm)
        if kind == "hello":
            return Routing(kind, cls, Action.HELLO)
        if kind == "shutdown":
            with self._lock:
                first = not self.shutting_down
                self.shutting_down = True
            if not first:
                # §3.3: one window per session.  A second `Shutdown` inside it
                # neither re-runs the hook nor restarts the clock -- the
                # deadline is the host's, measured from its FIRST send.
                note = "a second Shutdown inside the window; the first stands"
                self.diagnose(note)
                return Routing(kind, cls, Action.HANDLED, diagnostics=(note,))
            return Routing(kind, cls, Action.SHUTDOWN)
        if kind == "panel_opened":
            return Routing(kind, cls, Action.PANEL_OPENED)
        if kind in ("follow_started", "follow_ended"):
            return Routing(kind, cls, Action.FOLLOW)
        if kind == "cancel_work":
            return Routing(kind, cls, Action.CANCEL_WORK)
        if kind in REPLY_KINDS:
            return self._reply(kind, hm)
        return self._push(kind, hm)

    def _request(self, kind: str, hm) -> Routing:
        """A host request, after any arrangement riding on it is applied.

        `ExportChunk` is the request that carries both (§8.12), so the
        ride-along's *"apply, then interpret"* order is settled here rather
        than left to a handler that would read the descriptor first.
        """
        if self.shutting_down:
            # §3.3: **new work is refused at both ends.**  Once the window is
            # open the host sends no request it has not already sent, so one
            # that arrives is answered with an `Error` rather than run --
            # otherwise cleanup runs beside a command the plugin never asked
            # for, and the hook's "may not start new work" cannot be kept.
            # The `Error` is a disposition (§5.6), so whatever the request
            # named is released; its ride-along, if any, is not applied,
            # because no grant is issued inside the window.
            note = f"{kind} {hm.id!r} refused: the shutdown window is open (§3.3)"
            with self._lock:
                self.refused_in_teardown += 1
            self.outbox.send(
                pb2.PluginMsg(
                    id=hm.id,
                    error=pb2.Error(
                        kind=pb2.Error.PROTOCOL,
                        message="refused: the session is shutting down "
                        "(control_plane_protocol.md §3.3)",
                    ),
                )
            )
            self.dispose(hm)
            self.diagnose(note)
            return Routing(kind, Class.HOST_REQUEST, Action.HANDLED, diagnostics=(note,))
        found, notes, _ = self._ride_along(kind, hm)
        self._report(found)
        outcome = self._hold_if_unreadable(kind, hm, notes)
        if outcome is not None:
            # §8.12: the descriptor names a segment this end cannot read yet,
            # so it is held unread -- and the request with it, since a
            # handler that ran now would read bytes it cannot see.  The
            # request's deadline is suspended by the same rule.
            return Routing(
                kind,
                Class.HOST_REQUEST,
                Action.HANDLED,
                deferred=outcome == "held",
                diagnostics=tuple(notes),
                violations=tuple(found),
            )
        return Routing(
            kind,
            Class.HOST_REQUEST,
            Action.HOST_REQUEST,
            diagnostics=tuple(notes),
            violations=tuple(found),
        )

    def _hold_if_unreadable(self, kind: str, hm, notes: "list[str]") -> "str | None":
        """§8.12's receiver rule: **a descriptor into a segment not yet
        installed is held unread, whichever half is missing**, and read when
        the installation completes.  ``None`` means the message is readable
        now; ``"held"`` or ``"dropped"`` means the caller is done with it.

        The hold does not key on the ride-along: a bare descriptor into a
        segment whose install was sent standalone a moment earlier is held
        too.  vizi's export path sends exactly that shape (install, then
        chunk, no ack in between), and it must not pass only because a
        local-socket `M` beats a gRPC chunk.  So the state of the
        DESCRIPTOR's segment decides:

        - *pending* (one half here) or *not present* (neither, or a segment
          never heard of -- indistinguishable from "install not yet
          arrived"): held, in arrival order, until the segment is installed.
          The grant's own deadline is the backstop, at the host.
        - *failed* or *released*: dropped and counted, as a descriptor into a
          released segment always was; a held REQUEST is answered with an
          `Error` when that happens (§11a), never forgotten.
        """
        ref, _ = self._descriptor(kind, hm)
        if ref is None or self.memory is None:
            return None
        # The state read and the enqueue are ONE step under the lock the
        # release pops under.  Read outside it, the doorbell thread could
        # complete the installation and pop the still-empty list between
        # this thread's read of *pending* and its append -- leaving the
        # request in a list nobody pops again, on a segment installed and
        # acknowledged.
        # `_release_deferred` and `_drop_deferred`
        # run only after the memory layer's state has moved and pop under
        # this lock, so a read of *pending* here is followed by the append
        # before any pop that installation could cause, and a read after
        # the pop sees *installed* and dispatches now.  The memory layer's
        # own lock nests inside this one and never takes it the other way.
        with self._lock:
            state = self._state_of(ref.segment_id)
            if state is _memory.SegmentState.INSTALLED:
                return None
            if state in (_memory.SegmentState.PENDING, _memory.SegmentState.NOT_PRESENT):
                self._deferred.setdefault(ref.segment_id, []).append(hm)
                return "held"
            # FAILED or RELEASED: nothing to read the descriptor out of and
            # nothing to release -- the bytes went with the mapping.
            self.dropped_descriptors += 1
        notes.append(f"a descriptor into segment {ref.segment_id}, which is {state.value}")
        if HOST_CLASSES.get(kind) is Class.HOST_REQUEST:
            self._answer_unreadable(hm, state)
        return "dropped"

    def _answer_unreadable(self, hm, state: "_memory.SegmentState") -> None:
        """§11a: every host request is answered, including one whose
        descriptor names a segment that failed or is gone."""
        self.outbox.send(
            pb2.PluginMsg(
                id=hm.id,
                error=pb2.Error(
                    kind=pb2.Error.NOT_FOUND,
                    message=(
                        f"the descriptor names segment "
                        f"{self._descriptor(hm.WhichOneof('msg'), hm)[0].segment_id}, "
                        f"which is {state.value} (control_plane_protocol.md §8.12)"
                    ),
                ),
            )
        )

    def _ride_along(self, kind: str, hm):
        """§5.5's exception: apply an embedded ``InstallSegment`` FIRST.

        Returns ``(violations, diagnostics, segment_id)``, where the id is the
        one an install was applied for -- the caller decides whether its
        mapping has landed yet.
        """
        ref, install = self._descriptor(kind, hm)
        if install is None:
            return [], [], None
        if ref is None:
            # §8.12: `ExportChunk` carrying `inline` names no segment, so an
            # install beside it has nothing to install into.  It is a
            # ContractViolation rather than a grant, and the inline payload is
            # still perfectly good -- so the message goes on being handled.
            return (
                [
                    (
                        "install_without_segment",
                        f"an install for segment {install.segment_id} rides a "
                        f"{kind} that names no segment",
                    )
                ],
                [],
                None,
            )
        if install.segment_id != ref.segment_id:
            # The exception exists so a message can install the segment its own
            # descriptor names.  Two different ids is a message about neither.
            return (
                [],
                [
                    f"a ride-along installs segment {install.segment_id} while the "
                    f"descriptor names {ref.segment_id}; not applied"
                ],
                None,
            )
        self._install_segment(install, source=f"install_segment.{kind}")
        return [], [], install.segment_id

    # -- §5.5: arrangements ----------------------------------------------

    def _arrangement(self, kind: str, hm) -> Routing:
        if kind == "install_segment":
            return self._install_segment(hm)
        if kind == "epoch_change":
            return self._epoch_change(hm)
        if kind == "tool_binding":
            return self._tool_binding(hm)
        if kind == "time_mapping":
            return self._time_mapping(hm)
        if kind == "stage_arm":
            return self._stage_arm(hm)
        raise AssertionError(f"unhandled arrangement {kind!r}")  # pragma: no cover

    def _epoch_change(self, hm) -> Routing:
        """§8.10.  Install what can be installed, acknowledge exactly that.

        A plugin **may acknowledge some panes and not others**, and each pane
        resumes on its own -- so one unacknowledgeable pane costs that pane and
        nothing else.
        """
        change = hm.epoch_change
        notes: list[str] = []
        found: list[tuple[str, str]] = []
        states = {d.gen: d for d in change.datasets}
        acked: list = []
        with self._lock:
            for pane in change.panes:
                dataset = states.get(pane.gen)
                if pane.epoch == 0:
                    found.append(("zero_id", f"pane {pane.view_id} carries epoch 0"))
                    continue
                if dataset is None:
                    found.append(
                        (
                            "missing_arrangement",
                            f"pane {pane.view_id} names gen {pane.gen}, which the "
                            "message does not carry",
                        )
                    )
                    continue
                revision = dataset.time_mapping_revision
                if revision and self.mappings.get(pane.gen) != revision:
                    # §8.10, stated outright: the host has emitted an
                    # arrangement referencing one this plugin has not
                    # acknowledged, which is §5.5's gate rule failing.  Its
                    # remedy is to send the mapping, wait, and re-send.
                    found.append(
                        (
                            "missing_arrangement",
                            f"gen {pane.gen} names time mapping revision {revision}, "
                            "which was never arranged",
                        )
                    )
                    continue
                # The WHOLE content, not the three fields that come off
                # `PaneEpoch`: the geometry lives in `DatasetState`, and a host
                # re-sending one epoch under a crop change, a voxel-size
                # override or a channel rename -- none of which advance `gen`
                # -- is exactly the host bug §5.5 rule 4 exists to catch.
                # vizi's reading of this SDK found the tuple stopped short of
                # it; vizi itself allocates a new
                # epoch on any change, so the check is for the host that does
                # not.
                #
                # Content is the fields this build KNOWS (§5.5 rule 4): a field
                # a later minor adds to `DatasetState` takes no part, so a
                # re-send differing only by one is the same content.
                identity = (
                    pane.epoch,
                    pane.gen,
                    tuple(pane.channels_high),
                    _known_only(dataset).SerializeToString(deterministic=True),
                )
                installed = self._pane_state.get(pane.view_id)
                if installed is not None and installed[0] == pane.epoch and installed != identity:
                    # §5.5 rule 4: a re-send is acknowledged again, but the same
                    # identity carrying DIFFERENT content is a host bug (§11).
                    found.append(
                        (
                            "arrangement_content_mismatch",
                            f"pane {pane.view_id} epoch {pane.epoch} was re-sent with "
                            "different state",
                        )
                    )
                    continue
                # §5.5 rule 3: the transport installs FIRST.  It refuses an
                # epoch that would rewind, and a refusal is not acknowledged --
                # acknowledging one would claim an install that did not happen.
                if self.transport is not None and not self.transport.epoch_acknowledged(
                    pane.view_id, pane.epoch
                ):
                    found.append(
                        (
                            # §11 at `1.0`: an arrangement whose IDENTITY went
                            # backwards is its OWN site, and it is not the case
                            # above -- that is one identity carrying different
                            # content, this is the identity moving the wrong
                            # way, which a receiver refuses rather than
                            # installs.  Reported as the nearest named rule
                            # until 20 existed, which filed it under a rule it
                            # was not.
                            "arrangement_regressed",
                            f"pane {pane.view_id} epoch {pane.epoch} does not advance "
                            f"the installed epoch {self.epochs.get(pane.view_id)}",
                        )
                    )
                    continue
                self.epochs[pane.view_id] = pane.epoch
                self._pane_state[pane.view_id] = identity
                self.datasets[pane.gen] = dataset
                acked.append(pb2.PaneEpoch(view_id=pane.view_id, epoch=pane.epoch, gen=pane.gen))
            self._prune_generations()
        if acked:
            self.outbox.send(pb2.PluginMsg(epoch_ack=pb2.EpochAck(panes=acked)))
        self._report(found)
        if not acked and not found:
            notes.append("epoch_change carried no panes")
        return Routing(
            "epoch_change",
            Class.ARRANGEMENT,
            Action.HANDLED,
            diagnostics=tuple(notes),
            violations=tuple(found),
        )

    def _prune_generations(self) -> None:
        """Drop the state of every generation older than the oldest a pane
        still names.  ``gen`` is monotonic, so a mapping that arrived for a gen
        no pane names YET is newer than every live one and survives."""
        live = [state[1] for state in self._pane_state.values()]
        if not live:
            return
        floor = min(live)
        for table in (self.datasets, self.mappings, self.time_tables):
            for gen in [g for g in table if g < floor]:
                del table[gen]

    def pane_ids(self) -> tuple[int, ...]:
        """The view ids this plugin holds an acknowledged epoch for."""
        with self._lock:
            return tuple(sorted(int(v) for v in self._pane_state))

    def pane_state(self, view_id: int, epoch: "int | None" = None) -> "PaneState | None":
        """What this plugin acknowledged for ``view_id`` -- the dataset
        geometry, the visible high channels, the time mapping -- or ``None``
        when it holds no epoch for that pane, or ``epoch`` names one it has
        moved past.  No round trip: it is the arrangement already installed.
        """
        with self._lock:
            installed = self._pane_state.get(view_id)
            if installed is None:
                return None
            pane_epoch, gen, channels_high, _serialized = installed
            if epoch is not None and int(epoch) != pane_epoch:
                return None
            dataset = self.datasets.get(gen)
            if dataset is None:
                return None
            return PaneState(
                view_id=int(view_id),
                epoch=int(pane_epoch),
                gen=int(gen),
                geometry=dataset.geometry,
                channels_high=tuple(int(c) for c in channels_high),
                time_mapping_revision=int(dataset.time_mapping_revision),
                time_table=self.time_tables.get(gen),
            )

    def _tool_binding(self, hm) -> Routing:
        """§8.10: handles are per-plugin, start at 1, increase, never reuse.

        A binding for a handle the receiver already knows **updates in place
        and does not advance the high-water mark** -- that is the message a
        conflict's recourse needs, since a second plugin starting must be able
        to take a binding away from one already running.

        **So one identity carries a sequence of contents, and `revision` is
        which of them this is** (§5.5 rule 4).  A handle alone cannot say what
        an acknowledgement confirms: a retry of the old content is still
        legitimately in flight when new content goes out, its ack lands
        afterwards, and the sender reads it as confirmation of an arrangement
        the peer never installed -- opening the gate on something not in force.
        So the ack echoes **what this end installed**, never what the message
        asked for, and the sender ignores anything else.
        """
        binding = hm.tool_binding
        found: list[tuple[str, str]] = []
        notes: list[str] = []
        changed: list[tuple[str, bool]] = []
        with self._lock:
            handle = binding.tool
            revision = binding.revision
            # Received, whatever becomes of it below (§5.5 rule 4).  A handle or
            # revision of 0 names no statement.
            first = handle != 0 and revision != 0 and ("tool", handle) not in self.stated
            if handle != 0 and revision != 0:
                self.stated.add(("tool", handle))
            if handle == 0:
                found.append(("zero_id", "ToolBinding carries handle 0"))
            elif revision == 0:
                # §5.5 rule 4: revisions begin at 1 and **a revision of 0 names
                # no statement**.  Acknowledging one would confirm nothing while
                # looking exactly like confirmation, which is the failure the
                # field was added to prevent -- so it is refused rather than
                # installed under a name that cannot be echoed.
                found.append(("zero_id", f"ToolBinding for handle {handle} carries revision 0"))
            elif not binding.retired and not _known_enum(pb2.ToolMode, binding.mode):
                # §8.10: a mode this build does not know is a later minor's,
                # not a host bug.  The binding is refused alone -- neither
                # activated nor acknowledged, since `ToolBindingAck` carries no
                # reason -- and the mode is diagnosed here.  Never a violation
                # (§11), and a retirement is not refused for its mode: it
                # activates nothing.
                notes.append(
                    f"ToolBinding for handle {handle} revision {revision} refused, not "
                    f"acknowledged: mode {binding.mode} is not a ToolMode this build knows"
                )
            elif binding.retired:
                if self.transport is not None and not self.transport.tool_retired(handle):
                    found.append(("unknown_tool", f"retiring handle {handle}, never activated"))
                else:
                    retired = self.tools.pop(handle, None)
                    self._ack_tool(handle, revision)
                    if retired is not None:
                        self.retired_tool_ids[handle] = str(retired.tool_id)
                        while len(self.retired_tool_ids) > RETIRED_TOOLS_KEPT:
                            self.retired_tool_ids.popitem(last=False)
                        changed.append((str(retired.tool_id), False, retired))
            else:
                installed = self.tools.get(handle)
                conflict = self._restated(installed, binding)
                if conflict is not None:
                    # §5.5 rule 4, and each half has its own site since `1.0`:
                    # one revision naming two contents is
                    # `arrangement_content_mismatch`, and a revision moving
                    # BACKWARDS is `arrangement_regressed`.  Neither is
                    # `interaction_mismatch`, which is an `E`-record replay
                    # whose bytes changed and merely has the same shape (§11,
                    # §12's per-`where` aggregation).
                    found.append(conflict)
                elif self.transport is not None and not self.transport.tool_activated(handle):
                    # The transport keeps §5's high-water mark and refuses an
                    # out-of-order handle: 12 before 11 would move the mark past
                    # 11, which is then rejected forever as retired.
                    found.append(
                        ("unknown_tool", f"handle {handle} arrived out of increasing order")
                    )
                else:
                    if first and revision != 1:
                        # §5.5 rule 4: **each IDENTITY'S sequence begins at 1**,
                        # so a sender allocating from a per-session counter --
                        # handle 7's first statement arriving as revision 12 --
                        # is non-conforming.  That binds the SENDER.
                        #
                        # **A receiver INSTALLS it and reports**, which is the
                        # rule's other half and settles a split two SDKs
                        # shipped.  Nothing downstream depends on the number: a
                        # receiver's only uses of a revision are echoing it in
                        # the acknowledgement and checking the identity has not
                        # regressed, so 12 echoes as 12, matches the sender's
                        # ledger, and opens the gate on exactly the content
                        # installed.  The rule a receiver genuinely needs is the
                        # `0` refusal above.  Refusing would cost a session over
                        # arithmetic: the host re-sends the same revision and
                        # §9's budget escalates.
                        #
                        # Site 26 and not `arrangement_regressed` (an identity
                        # moving the wrong way) or `arrangement_content_mismatch`
                        # (one identity carrying two contents): this identity is
                        # starting in the wrong PLACE, and §12 aggregates per
                        # `where`.
                        found.append(
                            (
                                "arrangement_first_revision",
                                f"handle {handle}'s first ToolBinding carries revision "
                                f"{revision}; each identity's sequence begins at 1",
                            )
                        )
                    # A re-statement is legal, is applied in place, and is
                    # acknowledged again (§5.5 rule 4) -- the host re-sends on a
                    # lost ack, so suppressing the repeat would leave a
                    # correctly installed arrangement escalated.
                    self.tools[handle] = binding
                    self._ack_tool(handle, revision)
                    if installed is None:
                        changed.append((str(binding.tool_id), True, binding))
        self._report(found)
        # The notes travel in the routing, and `receive` diagnoses them: once.
        if self._on_tool_binding is not None:
            # The binding travels with the notice: at
            # activation it is the tool's arrangement, at retirement the one
            # just withdrawn, which `tools` no longer holds.
            for tool_id, active, binding in changed:
                self._on_tool_binding(tool_id, active, binding)
        return Routing(
            "tool_binding",
            Class.ARRANGEMENT,
            Action.HANDLED,
            diagnostics=tuple(notes),
            violations=tuple(found),
        )

    def _restated(self, installed, binding, name: str = "") -> "tuple[str, str] | None":
        """Which rule this statement breaks and why, if it breaks one.

        **Monotonic per handle**: a re-send of the same content carries the
        SAME revision -- that is what makes a retry idempotent -- and a change
        increments it.  So two things are host bugs and one is ordinary:

        - the same revision with different content: the sender changed a
          statement without renaming it, and the two ends now disagree about
          what one number means;
        - a revision going backwards: a statement older than the one installed,
          which cannot be a retry of it and cannot be a successor to it;
        - a higher revision with any content: the ordinary in-place update, and
          §8.10 requires it, since a ban on changed content under one identity
          would block the conflict recourse.
        """
        if installed is None:
            return None
        name = name or f"handle {binding.tool}"
        if binding.revision == installed.revision:
            # Compared WHOLE rather than field by field.  `revision` and `tool`
            # are equal in this branch by construction, so message equality is
            # content equality -- and an enumerated list of the content fields
            # is a list that has to grow with the IDL and therefore will not,
            # which is the argument `_names_bytes` makes about walking a tree.
            #
            # Whole means every field this build KNOWS (§5.5 rule 4): a field a
            # later minor adds takes no part, so a restatement differing only
            # by one is the same content and is acknowledged again.
            binding, installed = _known_only(binding), _known_only(installed)
            if binding == installed:
                return None
            changed = sorted(
                {f.name for f, _ in binding.ListFields()}
                ^ {f.name for f, _ in installed.ListFields()}
                | {
                    f.name
                    for f, value in binding.ListFields()
                    if getattr(installed, f.name) != value
                }
            )
            return (
                "arrangement_content_mismatch",
                f"{name} revision {binding.revision} was re-sent with a "
                f"different {', '.join(changed)}",
            )
        if binding.revision < installed.revision:
            return (
                "arrangement_regressed",
                f"{name} revision {binding.revision} is older than the "
                f"installed revision {installed.revision}",
            )
        return None

    def _stage_arm(self, hm) -> Routing:
        """``processing_stages.md`` §4: where a stage sits, identified by
        ``(pipeline, position)`` with ``revision`` counting within that pair.

        One stage may be armed at several positions, and each is its own
        arrangement.  An arm this end cannot use is not acknowledged and is
        reported at ``stage_arm_refused``: the host arms only after the ring's
        grant is installed, so there is nothing to wait for, and the
        arrangement's deadline is how the host sees the silence.  An arm
        naming a dtype outside this build's ``DType``, or carrying a oneof
        arm this build does not know under ``input`` or ``params``, is not
        acknowledged either, but is a local diagnostic: a later minor's value
        is not a host bug.
        """
        arm = hm.stage_arm
        name = f"stage {arm.stage_id!r} at ({arm.pipeline}, {arm.position})"
        found: list[tuple[str, str]] = []
        notes: list[str] = []
        installed_now = False
        with self._lock:
            key = (arm.pipeline, arm.position)
            # Received, whether installed or refused below (§5.5 rule 4).
            first = arm.revision != 0 and ("stage", key) not in self.stated
            if arm.revision != 0:
                self.stated.add(("stage", key))
            unknown = [
                f"{side} dtype {shape.dtype} is not a DType this build knows"
                for side, shape in (("input", arm.input), ("output", arm.output))
                if not _known_enum(pb2.DType, shape.dtype)
            ]
            # The same for a oneof arm under `input` or `params`: a later
            # minor's kind, not a host bug, and the stage cannot run on a
            # value it cannot read.
            if arm.HasField("input"):
                unknown += _unknown_arms(arm.input, "input")
            for param in sorted(arm.params):
                unknown += _unknown_arms(arm.params[param], f"params {param!r}")
            refusal = None if unknown else self._unusable_arm(arm)
            if arm.revision == 0:
                found.append(("zero_id", f"StageArm for {name} carries revision 0"))
            elif unknown:
                # §4: a dtype or a oneof arm this build does not know is a
                # later minor's, not a host bug.  This arm alone is refused --
                # not acknowledged, so the arrangement's deadline is how the
                # host sees it -- and the reason is a local diagnostic, never
                # `stage_arm_refused`.  The plan behind it stands, so a re-arm
                # with a kind this build knows is acknowledged.
                notes.append(
                    f"StageArm for {name} revision {arm.revision} refused, not "
                    f"acknowledged: {'; '.join(unknown)}"
                )
            elif refusal is not None:
                found.append(("stage_arm_refused", f"StageArm for {name}: {refusal}"))
            else:
                installed = self.stage_arms.get(key)
                conflict = self._restated(installed, arm, name)
                if conflict is not None:
                    found.append(conflict)
                else:
                    if first and arm.revision != 1:
                        # §5.5 rule 4: installed and reported, as a binding is.
                        found.append(
                            (
                                "arrangement_first_revision",
                                f"the first StageArm for {name} carries revision "
                                f"{arm.revision}; each identity's sequence begins at 1",
                            )
                        )
                    self.stage_arms[key] = arm
                    installed_now = True
                    self.outbox.send(
                        pb2.PluginMsg(
                            stage_arm_ack=pb2.StageArmAck(
                                stage_id=arm.stage_id,
                                revision=arm.revision,
                                pipeline=arm.pipeline,
                                position=arm.position,
                            )
                        )
                    )
        self._report(found)
        # The notes travel in the routing, and `receive` diagnoses them: once.
        if installed_now and self._on_stage_arm is not None:
            self._on_stage_arm(arm)
        return Routing(
            "stage_arm",
            Class.ARRANGEMENT,
            Action.HANDLED,
            diagnostics=tuple(notes),
            violations=tuple(found),
        )

    def stage_arm_at(self, pipeline: int, position: int):
        """The arm installed at ``(pipeline, position)`` now, or ``None``."""
        with self._lock:
            return self.stage_arms.get((pipeline, position))

    def stage_arm_snapshot(self) -> "dict[tuple[int, int], object]":
        """The installed arms, copied under the lock for the stage runner."""
        with self._lock:
            return dict(self.stage_arms)

    def _unusable_arm(self, arm) -> "str | None":
        """Why this end cannot use ``arm``, or ``None`` (§4's refusal list)."""
        if not 1 <= arm.pipeline <= STAGE_MAX_PIPELINE:
            return f"pipeline {arm.pipeline} is outside 1..{STAGE_MAX_PIPELINE}"
        if not 1 <= arm.position <= STAGE_MAX_POSITION:
            return f"position {arm.position} is outside 1..{STAGE_MAX_POSITION}"
        if self.declared_stages is not None and arm.stage_id not in self.declared_stages:
            return "names no stage this plugin declared"
        if not arm.HasField("input") or not arm.HasField("output"):
            return "carries no input or no output shape to hold the stage to"
        channels = [*arm.input.channels, *arm.new_channels.values()]
        if any(c > STAGE_MAX_CHANNEL for c in channels):
            return f"names a channel index above {STAGE_MAX_CHANNEL}"
        if len(set(channels)) != len(channels):
            return "names one channel index twice among its input and new channels"
        if self.stage_arm_check is not None:
            problem = self.stage_arm_check(arm)
            if problem is not None:
                return problem
        segment = self.memory.segment(arm.segment_id) if self.memory is not None else None
        if segment is None or segment.purpose != _memory.PURPOSE_STAGE:
            return f"segment {arm.segment_id} is no installed {_memory.PURPOSE_STAGE!r} ring"
        if segment.retiring:
            return f"segment {arm.segment_id} is retiring"
        return None

    def _ack_tool(self, handle: int, revision: int) -> None:
        """§5.5 rule 4: a receiver echoes the revision it INSTALLED."""
        self.outbox.send(
            pb2.PluginMsg(tool_binding_ack=pb2.ToolBindingAck(tool=handle, revision=revision))
        )

    def _time_mapping(self, hm) -> Routing:
        """§8.11: an ordinary arrangement, per ``gen``, monotonic ``revision``.

        Absent one a plugin may not assume uniform spacing: silence means
        unknown, not "multiply by a constant".
        """
        mapping = hm.time_mapping
        found: list[tuple[str, str]] = []
        notes: list[str] = []
        # The bound itself is checked before dispatch (`_over_bound`), because
        # it applies to every kind the table names rather than to this one.
        with self._lock:
            installed = self.mappings.get(mapping.gen)
            table = tuple(float(v) for v in mapping.t)
            if mapping.revision == 0:
                found.append(("zero_id", f"time mapping for gen {mapping.gen} carries revision 0"))
            elif installed is not None and mapping.revision < installed:
                # §5.5 rule 4, as every other arrangement receiver here applies
                # it: a statement older than the one installed
                # is neither a retry nor a successor.  Refused, not
                # acknowledged.
                found.append(
                    (
                        "arrangement_regressed",
                        f"gen {mapping.gen}'s time mapping revision {mapping.revision} is "
                        f"older than the installed {installed}",
                    )
                )
            elif installed == mapping.revision and self.time_tables.get(mapping.gen) != table:
                found.append(
                    (
                        "arrangement_content_mismatch",
                        f"gen {mapping.gen}'s time mapping revision {mapping.revision} was "
                        "re-sent with a different table",
                    )
                )
            else:
                if installed is None and mapping.revision != 1:
                    # §5.5 rule 4 binds the ARRANGEMENT CLASS, not tool
                    # bindings: §8.11 gives `TimeMapping` a `revision` per
                    # `gen` and §8.12 lists it as an arrangement, so `gen` is
                    # an identity and its sequence begins at 1 like any other.
                    # Installed and reported, as a binding is.  This SDK
                    # applied the rule to one tenant of the class and the Go
                    # SDK applied it to two.
                    found.append(
                        (
                            "arrangement_first_revision",
                            f"gen {mapping.gen}'s first time mapping carries revision "
                            f"{mapping.revision}; each identity's sequence begins at 1",
                        )
                    )
                self.mappings[mapping.gen] = mapping.revision
                self.time_tables[mapping.gen] = table
                self.outbox.send(
                    pb2.PluginMsg(
                        time_mapping_ack=pb2.TimeMappingAck(
                            gen=mapping.gen, revision=mapping.revision
                        )
                    )
                )
        self._report(found)
        return Routing(
            "time_mapping",
            Class.ARRANGEMENT,
            Action.HANDLED,
            diagnostics=tuple(notes),
            violations=tuple(found),
        )

    # -- §8.12: the grant path -------------------------------------------

    def _install_segment(self, hm, *, source: str = "install_segment") -> Routing:
        install = hm.install_segment if source == "install_segment" else hm
        if self.memory is None:
            return Routing(
                source,
                Class.ARRANGEMENT,
                Action.HANDLED,
                diagnostics=("no memory layer is wired; the install was ignored",),
            )
        regions = [_region(r) for r in install.regions]
        answer = self.memory.install(
            install.segment_id,
            install.purpose,
            regions,
            supersedes=install.supersedes,
            # §3: WHICH contribution the segment serves.  Host-set and
            # load-bearing, unlike `Region.id` -- it is what tells a plugin's
            # two `O` segments apart, and the memory layer checks it against
            # the purpose both ways.
            subject=install.subject,
        )
        self._answer_grant(install.segment_id, answer)
        if isinstance(answer, _memory.Installed):
            self._release_deferred(install.segment_id)
            self._settle_superseded()
        return Routing(source, Class.ARRANGEMENT, Action.HANDLED)

    def capability(
        self,
        segment_id: int,
        size: int,
        perms: int,
        *,
        fd: "int | None" = None,
        name: "str | None" = None,
        record: "bytes | None" = None,
    ) -> Routing:
        """Apply the doorbell's ``M`` half and acknowledge if it completes one.

        The two halves arrive on two channels and either may be first (§8.12),
        so this and :meth:`receive`'s ``install_segment`` are the same event
        seen from opposite sides: whichever lands second emits the ack.
        """
        if self.memory is None:
            return Routing("capability", None, Action.HANDLED, diagnostics=("no memory layer",))
        answer = self.memory.capability(segment_id, size, perms, fd=fd, name=name, record=record)
        self._answer_grant(segment_id, answer)
        if isinstance(answer, _memory.Installed):
            self._release_deferred(segment_id)
            self._settle_superseded()
        return Routing("capability", None, Action.HANDLED)

    def _settle_superseded(self) -> None:
        """§9: a `supersedes` retirement is a `RetireSegment` in every respect,
        so the segment the store just retired behind a replacement is
        announced with `SegmentReleased` -- or, while a claim is still held in
        it, parked with the retiring ones `settle_retirements` finishes.
        Unannounced, a host re-granting a chunked overlay's arena would wait
        on a release that never comes."""
        for segment_id, released in self.memory.take_superseded():
            if released:
                self.outbox.send(
                    pb2.PluginMsg(segment_released=pb2.SegmentReleased(segment_id=segment_id))
                )
            else:
                with self._lock:
                    self._retiring.add(segment_id)

    def reject_capability(
        self, segment_id: int, detail: str, *, record: "bytes | None" = None
    ) -> Routing:
        """The doorbell rejected an ``M``: fail its grant and acknowledge that.

        The transport reports the reason and closes the descriptor
        (``Frame.grant_failure``); this is the other half, and without it the
        id stays *not present* and §7's query table sends the host back around
        the same rejected grant.
        """
        if self.memory is None:
            return Routing("capability", None, Action.HANDLED, diagnostics=("no memory layer",))
        answer = self.memory.reject_capability(segment_id, detail, record=record)
        self._answer_grant(segment_id, answer)
        return Routing("capability", None, Action.HANDLED)

    def _answer_grant(self, segment_id: int, answer) -> None:
        """§8.12: the success ack carries the proof READ OUT OF the mapping."""
        if answer is None:
            return  # still pending; the other half has not landed
        if isinstance(answer, _memory.Installed):
            self.outbox.send(
                pb2.PluginMsg(
                    grant_ack=pb2.GrantAck(
                        segment_id=segment_id,
                        installed=pb2.GrantInstalled(identity_proof=answer.identity_proof),
                    )
                )
            )
            if self._on_installed is not None:
                self._on_installed(segment_id)
        elif isinstance(answer, _memory.Failed):
            self.outbox.send(
                pb2.PluginMsg(
                    grant_ack=pb2.GrantAck(
                        segment_id=segment_id,
                        failed=pb2.GrantFailed(
                            failure_class=_bridges()["failure"][answer.failure_class],
                            detail=answer.detail,
                        ),
                    )
                )
            )
            self._drop_deferred(segment_id, "the grant failed")
        elif isinstance(answer, _memory.Released):
            # §9.4: a half arriving for a released segment is answered with
            # `SegmentReleased` again, never with silence.
            self.outbox.send(
                pb2.PluginMsg(segment_released=pb2.SegmentReleased(segment_id=segment_id))
            )
            self._drop_deferred(segment_id, "the segment was released")

    def _drop_deferred(self, segment_id: int, why: str) -> None:
        """A descriptor held for a capability that will never land.

        §8.12 holds a ride-along's descriptor unread until `M` arrives, which
        assumes the grant completes.  When it fails or the segment is released
        instead, there is nothing to read it out of and nothing to release --
        the bytes went with the mapping -- so the hold is dropped and counted
        rather than kept for a `M` that is not coming.
        """
        with self._lock:
            waiting = self._deferred.pop(segment_id, [])
            if waiting:
                self.dropped_descriptors += len(waiting)
        if not waiting:
            return
        for hm in waiting:
            # §11a: a held request whose wait just ended in failure is answered,
            # not forgotten -- its deadline was suspended for a half the host
            # owed, and the host has now been told the grant is gone.
            if HOST_CLASSES.get(hm.WhichOneof("msg")) is Class.HOST_REQUEST:
                self._answer_unreadable(hm, self._state_of(segment_id))
        self.diagnose(f"{len(waiting)} held descriptor(s) for segment {segment_id} dropped: {why}")

    def _push(self, kind: str, hm) -> Routing:
        if kind == "retire_segment":
            return self._retire(hm.retire_segment.segment_id)
        if kind == "grant_state_query":
            return self._grant_state(hm)
        if kind == "release_loan":
            return self._release_loan(hm.release_loan)
        if kind == "contract_violation":
            violation = hm.contract_violation
            with self._lock:
                where = int(violation.where)
                counts = self.violations_received_counts
                counts[where] = counts.get(where, 0) + max(int(violation.count), 1)
                if len(self.violations_received) < MAX_VIOLATIONS_RETAINED:
                    self.violations_received.append(violation)
            return Routing(
                kind,
                Class.HOST_ONE_WAY,
                Action.HANDLED,
                diagnostics=(
                    f"the host reported a contract violation at site {violation.where}: "
                    f"{violation.detail} (x{max(int(violation.count), 1)})",
                ),
            )
        if kind == "doorbell_shutdown":
            # §8.13: ONE channel's end, not the session's -- the caller stops
            # the doorbell and may answer with the one-shot final report.  A
            # second notice changes nothing: the window opened at the first
            # and the host does not restart it either, so it is counted rather than acted on twice.
            with self._lock:
                repeat = self.doorbell_shutdown is not None
                if not repeat:
                    self.doorbell_shutdown = hm.doorbell_shutdown
            if repeat:
                self.diagnose("a second DoorbellShutdown; the window is already open")
                return Routing(kind, Class.HOST_ONE_WAY, Action.HANDLED)
            return Routing(kind, Class.HOST_ONE_WAY, Action.DOORBELL_SHUTDOWN)
        return Routing(kind, Class.HOST_ONE_WAY, Action.HANDLED)

    def _retire(self, segment_id: int) -> Routing:
        """§9.1: *retiring* permits finishing and forbids starting.

        ``SegmentReleased`` goes out only once the mapping is closed, so a
        segment whose Pulsed claim this endpoint still holds stays in
        *retiring* until :meth:`settle_retirements` finds it done.
        """
        if self.memory is None:
            return Routing("retire_segment", Class.HOST_ONE_WAY, Action.HANDLED)
        ready = self.memory.begin_retire(segment_id)
        if ready and self.memory.release(segment_id):
            self.outbox.send(
                pb2.PluginMsg(segment_released=pb2.SegmentReleased(segment_id=segment_id))
            )
            return Routing("retire_segment", Class.HOST_ONE_WAY, Action.HANDLED)
        with self._lock:
            self._retiring.add(segment_id)
        # The last claim may have dropped between the check above and the add:
        # its drain found nothing retiring and settled nothing, and nothing
        # would until unrelated traffic.  Look once more.
        if self.memory.begin_retire(segment_id):
            self.settle_retirements()
        return Routing(
            "retire_segment",
            Class.HOST_ONE_WAY,
            Action.HANDLED,
            diagnostics=(f"segment {segment_id} is retiring with a claim still held",),
        )

    def settle_retirements(self) -> list[int]:
        """Release every retiring segment whose held claims have finished.

        §9.1 makes completing a held claim an obligation, so the release cannot
        happen inside `RetireSegment` -- it happens when the claim does, and
        this is where a supervisor asks.
        """
        released: list[int] = []
        if self.memory is None:
            return released
        with self._lock:
            pending = list(self._retiring)
        retried: set[int] = set()
        while pending:
            segment_id = pending.pop()
            # Taken off the set before the release, so two passes -- the loop's
            # and the one a drained claim schedules -- cannot both send
            # `SegmentReleased` for one segment.  Put back if not ready.
            with self._lock:
                if segment_id not in self._retiring:
                    continue
                self._retiring.discard(segment_id)
            if self.memory.begin_retire(segment_id) and self.memory.release(segment_id):
                self.outbox.send(
                    pb2.PluginMsg(segment_released=pb2.SegmentReleased(segment_id=segment_id))
                )
                released.append(segment_id)
            else:
                with self._lock:
                    self._retiring.add(segment_id)
                # The same window as in `_retire` -- once, so a
                # segment the store will not release cannot spin here.
                if segment_id not in retried and self.memory.begin_retire(segment_id):
                    retried.add(segment_id)
                    pending.append(segment_id)
        return released

    def _grant_state(self, hm) -> Routing:
        """§8.12: a one-way whose answer is a push echoing the query's id."""
        segment_id = hm.grant_state_query.segment_id
        if self.memory is None:
            return Routing("grant_state_query", Class.HOST_ONE_WAY, Action.HANDLED)
        report = self.memory.state(segment_id)
        state = pb2.GrantState(segment_id=segment_id, state=_bridges()["state"][report.state])
        if report.state is _memory.SegmentState.PENDING:
            state.have_capability = bool(report.have_capability)
            state.have_install = bool(report.have_install)
        elif report.state is _memory.SegmentState.INSTALLED:
            state.identity_proof = report.identity_proof or b""
        elif report.state is _memory.SegmentState.FAILED:
            if report.failure_class is not None:
                state.failure_class = _bridges()["failure"][report.failure_class]
            state.detail = report.detail or ""
        self.outbox.send(pb2.PluginMsg(id=hm.id, grant_state=state))
        return Routing("grant_state_query", Class.HOST_ONE_WAY, Action.HANDLED)

    def _release_loan(self, loan) -> Routing:
        """§8.7's mirror: the host is done with bytes the PLUGIN allocated."""
        found: list[tuple[str, str]] = []
        segment = self.memory.segment(loan.segment_id) if self.memory is not None else None
        if segment is None and self._released_here(loan.segment_id):
            # §9.3: the host released a loan while this end's
            # `SegmentReleased` was on its way -- the two cross, and a push the
            # host dropped into a retiring segment has its loan returned after
            # the claim that held the release back has ended.  Ids are never
            # reused, so released is told apart from never-installed, which is
            # the only `unknown_release` -- other than a crossing naming a loan
            # that was not out at the release, or one already returned: that
            # is the host's bug, and counting it silently would hide it.
            if self.memory.take_crossed_loan(loan.segment_id, loan.region, loan.offset, loan.len):
                with self._lock:
                    self.crossed_releases += 1
                return Routing("release_loan", Class.HOST_ONE_WAY, Action.HANDLED)
            found.append(
                (
                    "unknown_release",
                    f"ReleaseLoan ({loan.offset}, {loan.len}) in region {loan.region} of "
                    f"released segment {loan.segment_id}: no such loan was out at the release",
                )
            )
            self._report(found)
            return Routing(
                "release_loan", Class.HOST_ONE_WAY, Action.HANDLED, violations=tuple(found)
            )
        if segment is None:
            found.append(
                ("unknown_release", f"ReleaseLoan names segment {loan.segment_id}, not installed")
            )
        else:
            try:
                segment.release_loan(loan.region, loan.offset, loan.len)
            except _memory.Quarantined as exc:
                found.append((exc.where, exc.detail))
            except (KeyError, ValueError) as exc:
                found.append(("unknown_release", str(exc)))
        self._report(found)
        if not found and self._on_release_loan is not None:
            self._on_release_loan(int(loan.segment_id))
        return Routing("release_loan", Class.HOST_ONE_WAY, Action.HANDLED, violations=tuple(found))

    def _released_here(self, segment_id: int) -> bool:
        if self.memory is None:
            return False
        return self.memory.state(int(segment_id)).state is _memory.SegmentState.RELEASED

    # -- §5.7: chunked state the host asks for ----------------------------

    def _host_coalescing(self, kind: str, hm) -> Routing:
        if kind == "appearance":
            with self._lock:
                self.appearance = hm.appearance
            return Routing(kind, Class.HOST_COALESCING, Action.APPEARANCE)
        if kind == "widget_geometry":
            geometry = hm.widget_geometry
            with self._lock:
                self.widget_geometries[(geometry.panel_id, geometry.widget_id)] = geometry
            return Routing(kind, Class.HOST_COALESCING, Action.WIDGET_GEOMETRY)
        if kind in ("view_changed", "regions_changed"):
            if self._on_subscribed is not None:
                self._on_subscribed(kind, getattr(hm, kind))
            return Routing(kind, Class.HOST_COALESCING, Action.HANDLED)
        if kind != "chunks_wanted":
            return Routing(kind, Class.HOST_COALESCING, Action.HANDLED)
        wanted = hm.chunks_wanted
        wants = [(int(w.level), int(w.t) if w.HasField("t") else None) for w in wanted.wants]
        with self._lock:
            self.chunks_wanted[str(wanted.overlay_id)] = wants
        if self._on_chunks_wanted is not None:
            self._on_chunks_wanted(str(wanted.overlay_id), list(wants))
        return Routing(kind, Class.HOST_COALESCING, Action.HANDLED)

    # -- §4: replies, and what an abandoned one still owns ----------------

    def _reply(self, kind: str, hm) -> Routing:
        # §5.5's ride-along: apply the arrangement, THEN interpret the rest of
        # the message.  Within one message the order is the receiver's own, and
        # it is fixed here rather than in whoever reads the descriptor.
        found, notes, _ = self._ride_along(kind, hm)
        self._report(found)
        # §8.12: applying an installation is not the same as being able to
        # READ, and a descriptor into a segment this end cannot read yet --
        # whichever half is missing -- is held unread rather than failed.
        outcome = self._hold_if_unreadable(kind, hm, notes)
        if outcome is not None:
            return Routing(
                kind,
                Class.HOST_ONE_WAY,
                Action.HANDLED,
                deferred=outcome == "held",
                diagnostics=tuple(notes),
                violations=tuple(found),
            )
        try:
            delivered = self._deliver_reply(hm) if self._deliver_reply is not None else False
        except SessionFault:
            # §4 rule 8: a reply of a kind its pending request does not take
            # faults the session -- and rule 6 still holds, so what it names
            # is released before the fault goes on.
            self._release_abandoned(kind, hm)
            raise
        if not delivered:
            # §4 rule 5: an unmatched id is never silently discarded, and rule 6:
            # a dropped reply is still CONSUMED for the resources it carries.
            notes.append(f"reply {hm.id!r} ({kind}) matched no waiter")
            found.extend(self._release_abandoned(kind, hm))
        return Routing(
            kind,
            Class.HOST_ONE_WAY,
            Action.HANDLED,
            diagnostics=tuple(notes),
            violations=tuple(found),
        )

    # -- §5.6: a descriptor is a loan, and every disposition counts --------

    @staticmethod
    def _descriptor(kind: str, hm):
        """The ``(SegmentRef, InstallSegment)`` a message carries, if any.

        Four kinds do: a ``DataVolume`` reply, a ``MaskVoxels`` reply, a
        ``RoiInclusion`` reply, and an ``ExportChunk`` request -- which is a
        *request*, so its ride-along and its release are on the request path
        rather than the reply one.
        """
        if kind == "data_volume":
            volume = hm.data_volume
            return (
                volume.segment if volume.HasField("segment") else None,
                volume.install if volume.HasField("install") else None,
            )
        if kind == "mask_voxels":
            voxels = hm.mask_voxels
            return (voxels.segment if voxels.HasField("segment") else None, None)
        if kind == "roi_inclusion":
            inclusion = hm.roi_inclusion
            return (inclusion.segment if inclusion.HasField("segment") else None, None)
        if kind == "export_chunk":
            chunk = hm.export_chunk
            ref = chunk.segment if chunk.WhichOneof("source") == "segment" else None
            return (ref, chunk.install if chunk.HasField("install") else None)
        return (None, None)

    def dispose(self, hm) -> bool:
        """One disposition of whatever descriptor ``hm`` carried (§5.6).

        ``True`` when this was the last declared use and a ``ReleaseDeposit``
        went out.  **Every** disposition counts -- applied, superseded,
        rejected, and an abandoned reply nobody was waiting for -- because the
        receiver is done with the bytes whatever it did with them, and the
        release is owed at zero rather than at the first finish.
        """
        kind = hm.WhichOneof("msg")
        ref, _ = self._descriptor(kind, hm)
        if ref is None or not ref.len:
            return False
        key = (ref.segment_id, ref.offset, ref.len)
        with self._lock:
            remaining = self._uses.get(key)
            if remaining is None:
                remaining = max(1, ref.uses)
            remaining -= 1
            if remaining > 0:
                self._uses[key] = remaining
                return False
            self._uses.pop(key, None)
        return self._release_deposit(ref)

    def _release_deposit(self, ref) -> bool:
        """Send the release, with the region resolved from our own table.

        ``SegmentRef`` carries no region and ``ReleaseDeposit`` requires one,
        so the region is ours to resolve (§8.12's redundancy is between the
        release's own two fields).  Naming region 0 blindly is how a correct
        peer earns a `release_region_mismatch` against itself, so a range we
        cannot place is diagnosed rather than released -- and §5.5's gate rule
        makes that unreachable for a descriptor into an installed segment.
        """
        segment = self.memory.segment(ref.segment_id) if self.memory is not None else None
        if segment is None:
            with self._lock:
                self.unreleasable += 1
            return False
        try:
            region = segment.resolve_deposit(ref.offset, ref.len)
        except (ValueError, KeyError):
            with self._lock:
                self.unreleasable += 1
            return False
        # The ring's own record of the deposit, cleared because the release is
        # going out now.  `settle` rather than `release_deposit`: the declared
        # uses were counted HERE (an abandoned reply is a disposition that never
        # reaches the ring), so the ring's count is not the authoritative one.
        try:
            segment.settle_deposit(region, ref.offset, ref.len)
        except (_memory.Quarantined, _memory.Retiring, KeyError, ValueError):
            pass  # the region is gone or isolated; the release is still owed
        self.outbox.send(
            pb2.PluginMsg(
                release_deposit=pb2.ReleaseDeposit(
                    segment_id=ref.segment_id,
                    region=region,
                    offset=ref.offset,
                    len=ref.len,
                )
            )
        )
        return True

    def finish_request(self, hm) -> bool:
        """The handler for a host request is done: dispose of its descriptor.

        `ExportChunk` is the case -- it carries a descriptor into a host
        segment, and §5.6's *"every disposition releases"* covers a request's
        completion exactly as it covers a reply's.  Call it once per request,
        whatever the handler returned.
        """
        return self.dispose(hm)

    def _release_abandoned(self, kind: str, hm) -> list[tuple[str, str]]:
        """§4: the receiving dispatcher releases the resource, without
        delivering the payload to anyone.

        The damage a leak does here is out of proportion to the event, because
        Loaned reclamation is FIFO: one abandoned deposit at the head pins every
        later released deposit behind it, so a timed-out read costs the ring
        rather than one slot.
        """
        self.dispose(hm)
        return []

    def _state_of(self, segment_id: int) -> "_memory.SegmentState":
        if self.memory is None:
            return _memory.SegmentState.NOT_PRESENT
        return self.memory.state(segment_id).state

    def _release_deferred(self, segment_id: int) -> None:
        """The capability landed: the held descriptors can be read now.

        A held *reply* is delivered to its waiter; a held *request* goes back
        to the caller through ``on_deferred_request``, because dispatching it
        is the caller's job and this is the moment it became possible.
        """
        with self._lock:
            waiting = self._deferred.pop(segment_id, [])
        for hm in waiting:
            kind = hm.WhichOneof("msg")
            if HOST_CLASSES.get(kind) is Class.HOST_REQUEST:
                if self._on_deferred_request is not None:
                    self._on_deferred_request(hm)
                else:
                    with self._lock:
                        self.dropped_descriptors += 1
                continue
            self._reply(kind, hm)

    @property
    def deferred(self) -> dict:
        """Segment id -> descriptors held unread until their ``M`` lands."""
        with self._lock:
            return {k: list(v) for k, v in self._deferred.items()}

    # -- §8.13: the doorbell's faults, composed here ----------------------

    def doorbell_fault(self, fault, **session) -> "object | None":
        """Compose and queue one ``DoorbellFault`` (§8.13).

        **The transport supplies what it observed; the supervisor adds the
        session facts and sends the message** -- neither half is optional and
        neither can produce the other's.  The doorbell's boundary excludes
        supervising a session, so it cannot know a manifest source or this
        stream's health, and an implementation that asks it for one gets an
        empty field or an invented one.

        **A field this end cannot establish is ABSENT, not zero-filled**, which
        is why every keyword defaults to ``None`` rather than to a plausible
        value.  ``process_healthy`` and ``stream_healthy`` are snapshots with a
        known blind spot -- §9 records that both stay green for a peer whose
        doorbell listener has died -- so what they say is *"these two checks
        passed at that moment"*, which is the evidence a reader needs and not a
        claim that the peer was fine.

        **Exactly one is sent per terminal event.**  A transport may surface
        that event through more than one view -- a terminal mailbox and the
        ordered outcome stream -- and reporting both gives Plugin Manager two
        diagnostics for one fault.  Returns ``None`` when this fault has
        already been reported, which is how that rule is kept here.
        """
        with self._lock:
            if self._fault_sent:
                return None
        try:
            failure_class = _doorbell_failure(fault.failure_class)
        except ValueError:
            # Unsendable, so the diagnostic is where the reason survives -- and
            # the one-per-event latch is NOT spent, because nothing was sent.
            self.diagnose(
                f"doorbell fault {fault.failure_class!r} has no wire class: {fault.detail}"
            )
            return None
        with self._lock:
            self._fault_sent = True
        message = pb2.DoorbellFault(
            failure_class=failure_class,
            direction=fault.direction,
            # The IDL's spelling (`seqpacket | dgram | tcp`), not the
            # transport's own name for its socket type: the Windows doorbell
            # is a TCP stream, and "stream" matched nothing the host or the
            # other SDKs send.
            transport_class=_WIRE_TRANSPORT_CLASS.get(fault.transport_class, fault.transport_class),
            detail=fault.detail,
        )
        for name in (
            "capture",
            "observed_len",
            "msg_ctrunc",
            "ordinal",
            "os_error",
            "expected_crc",
            "observed_crc",
        ):
            value = getattr(fault, name, None)
            if value is not None:
                setattr(message, name, value)
        for name in (
            "plugin_id",
            "manifest_source",
            "sdk_version",
            "peer_protocol_major",
            "peer_protocol_minor",
            "process_healthy_at_detection",
            "stream_healthy_at_detection",
        ):
            value = session.get(name)
            if value is not None:
                setattr(message, name, value)
        self.outbox.send(pb2.PluginMsg(doorbell_fault=message))
        return message

    def doorbell_final_report(self, detail: str) -> None:
        """§8.13: the peer's diagnostic inside the grace window, one-shot.

        Sent on THIS channel while it is still usable, which is the entire
        point of reporting a doorbell failure on the other one.
        """
        self.outbox.send(
            pb2.PluginMsg(doorbell_final_report=pb2.DoorbellFinalReport(detail=detail))
        )

    # -- reporting --------------------------------------------------------

    def diagnose(self, note: str) -> None:
        """Record one local diagnostic (§12: a counter nobody can read is not
        a surface).  Bounded, because a diagnostic path that grows without
        limit is the failure path becoming the failure.

        **Written to stderr as well**, because the list is read by nothing in
        production: the host drains a plugin's stderr continuously and keeps
        a redacted 64 KiB tail it shows in Plugin Manager (§12), which is the
        surface a developer can find.  Without it, a tool handler raising on
        every event leaves a live listener doing nothing, with nobody told.
        """
        with self._lock:
            self.diagnostics.append(note)
            if len(self.diagnostics) > 256:
                del self.diagnostics[:-256]
        try:
            print(f"vizi plugin sdk: {note}", file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass  # a closed or broken stderr must not become the second failure

    def _report(self, found: "list[tuple[str, str]]") -> None:
        if self.reporter is None:
            return
        for where, detail in found:
            self.emit_violation(self.reporter.report(where, detail))

    def emit_violation(self, violation) -> bool:
        """Put one ``ContractViolation`` on the outbox (§7, §11).

        ``None`` is the ordinary case and not a failure: the reporter folded
        this occurrence into a count :meth:`report_due` will emit.  A ``where``
        the wire enum has no value for stays a local diagnostic -- sending it
        as ``UNSPECIFIED`` would report a violation by committing one, which
        the IDL's own zero-is-never-a-value convention forbids.
        """
        if violation is None:
            return False
        site = _violations.site_number(violation.where)
        if site is None:
            self.diagnose(f"violation {violation.where!r} has no ViolationSite: {violation.detail}")
            return False
        message = pb2.ContractViolation(
            where=site, detail=violation.detail, count=max(1, violation.count)
        )
        if violation.record:
            message.record = violation.record
        self.outbox.send(pb2.PluginMsg(contract_violation=message))
        return True

    def report(self, where: str, detail: str) -> None:
        """Report one violation the SUPERVISOR found, not this plane (§11).

        The layers above own conditions the plane never sees -- a `Shutdown`
        with no deadline, a `uses` count that disagrees with itself -- and they
        are §11 reports like any other, subject to §7's rate bound.  Public so
        a supervisor does not reach into ``_report``.
        """
        self._report([(where, detail)])

    def report_records(self, violations) -> int:
        """Emit the reports one received record produced (§2's unit)."""
        return sum(1 for v in violations if self.emit_violation(v))

    def report_due(self) -> int:
        """Emit every report the reporter's rate and interval now permit.

        §2's aggregation is a *rate*, not a loss: a repeat it defers stays
        pending until some loop asks again, so a supervisor calls this from
        whatever loop owns the stream.  Returns how many went out.
        """
        if self.reporter is None:
            return 0
        return sum(1 for v in self.reporter.due() if self.emit_violation(v))

    def next_report_due(self) -> "float | None":
        """Seconds until a deferred report may go out, or ``None``."""
        return None if self.reporter is None else self.reporter.next_due()

    def report_remaining(self) -> int:
        """Teardown's version: everything the rate permits right now (§2).

        The next interval will never arrive, so :meth:`ViolationReporter.drain`
        replaces :meth:`ViolationReporter.due` -- the rate still applies, which
        is why this is called until it returns 0 or the window closes.
        """
        if self.reporter is None:
            return 0
        return sum(1 for v in self.reporter.drain() if self.emit_violation(v))
