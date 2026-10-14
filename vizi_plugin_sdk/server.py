"""Plugin-side SDK: decorators + a server runner that hides the protocol.

A plugin author writes plain Python:

- a reader class with ``open(uri)`` and ``read_volume(t, c)``, decorated
  ``@reader("id")``;
- command handlers ``def run(ctx): ...`` decorated ``@command("plugin.cmd")``,
  where ``ctx.args`` carries the typed invocation arguments,
  ``ctx.panel("panel_id")`` returns a :class:`PanelHandle` for pushing
  declarative-panel state patches (``set_value`` / ``set_enabled`` /
  ``set_visible`` / ``set_options`` / ``push_image``), and ``ctx.host``
  asks the HOST for the active view state, dataset voxels, and annotations
  (manifest permission ``data_access``), or contributes native annotations
  and compiles resident label volumes into native masks (permission
  ``contribute_rois``);
- panel-open handlers decorated ``@panel_opened("panel_id")`` receive the
  same context with empty args and push initial widget state through
  ``ctx.panel("panel_id")``;

then calls :func:`serve`.  The SDK runs the gRPC ``Session`` bidi stream,
receives the host's memory grants on the v2 doorbell (``M`` records carrying
one ``SCM_RIGHTS`` descriptor each, met by the ``InstallSegment`` that says
what the mapping means), and moves bulk bytes through those segments — voxels
and PNG plots travel the zero-copy data plane, never gRPC.  The author never
touches gRPC, fds, or mmap.

**This module is the supervisor and nothing else.**  The four layers under it
each own one document and none of them owns a session: :mod:`.doorbell` frames
and validates records, :mod:`.memory` maps and acknowledges grants,
:mod:`.control` classifies every gRPC message and produces the
acknowledgements, and :mod:`.violations` bounds what gets reported.  What is
left here — and it is the only thing that cannot live in any of them — is
ordering the two channels against each other and running the plugin's code off
the dispatch path.

Phase 2 stream shape: the ``Session`` generator is fed by the control plane's
``Outbox``.  A dispatcher thread consumes the host's requests and enqueues
replies; ``PanelHandle`` enqueues unsolicited ``panel_patch`` pushes from any
plugin thread.

Runs in the plugin's own process + venv (grpcio + numpy live there, not in
vizi).  The doorbell is ``SCM_RIGHTS`` over a datagram-preserving unix socket
(SEQPACKET on Linux, DGRAM on macOS — the host picks; the SDK adopts the
inherited fd as-is), or a token-authenticated loopback stream on Windows.

Plugins run as ordinary user processes. Installing a plugin therefore grants
it the same filesystem, network, and device access as the user running vizi.
"""

from __future__ import annotations

import dataclasses
import functools
import inspect
import itertools
import json
import math
import os
import sys
import threading
import time
import unicodedata
from concurrent import futures
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from . import control as _control
from . import declare as _declare
from . import doorbell as _doorbell
from . import manifest as _manifest
from . import memory as _memory
from . import stages as _stages
from . import violations as _violations
from . import work as _work
from ._lazy import LazyProto as _LazyProto
from .control import ChannelName, PaneState  # noqa: F401 -- re-exported for authors
from .declare import UNSET

# What a plugin author needs from the fast path is the doorbell's vocabulary,
# re-exported from here so `from vizi_plugin_sdk import ...` keeps being the
# one import.
from .doorbell import (  # noqa: F401 — re-exported for plugin authors
    KIND_COLUMN_YX,
    KIND_COLUMN_ZX,
    KIND_COLUMN_ZY,
    PHASE_CANCEL,
    PHASE_DRAG,
    PHASE_FIRE,
    PHASE_PRESS,
    PHASE_RELEASE,
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    TARGET_ANNOTATION,
    TARGET_HANDLE,
    TARGET_NONE,
    TARGET_ROI,
    VIEW_2D,
    VIEW_3D,
    Column,
    InputEvent,
    Notification,
    Ray,
    RayAndPicked,
    Voxel,
)
from .geometry import EdgeBox
from .work import Cancelled, TaskHandle, cancelled, report_progress  # noqa: F401 -- re-exported

# Protocol 1.0: the doorbell (112-byte `M`/`E`/`D`), the two-halves memory
# grant with `InstallSegment`, symmetric `ContractViolation`, the shutdown
# window and its `ShutdownReport`, and panel-open as a control-plane message.
# ONE VERSION IMPLIES ONE OF EACH (`docs/spec/README.md`): §3.2 makes a MAJOR
# bump the breaking one and a minor bump backward-compatible, and the
# doorbell's framing cannot be made compatible, so a framing change is a major
# bump by construction.
#
# Defined ONCE, in the doorbell module, and imported (above) here and by the
# manifest parser, so the copies cannot drift.

#: §3.1's identity strings.  The handshake's two ``id``s are each end's
#: canonical build identity, not correlation ids: a session log then names
#: exactly which two builds spoke, for one string per session.  Each is a
#: fixed prefix followed by a non-empty, free-form version string; the whole
#: is at most 2048 bytes of UTF-8 with no control character, because both are
#: written to logs and a newline in one would forge a log line.
HELLO_PREFIX = "Hello from "
READY_PREFIX = "Ready; I am "
IDENTITY_MAX_BYTES = 2048
#: Where the composed identity draws its natural content: the manifest's
#: ``id`` and ``version``, read from the working directory the host launches
#: a plugin in (`docs/manifest.md`).
MANIFEST_FILE = "vizi-plugin.json"


def check_identity(identity: str, prefix: str) -> "str | None":
    """§3.1's form check, and nothing else: the reason it is wrong, or
    ``None``.  Never matches the string, never parses the version string,
    never compares it to a manifest -- composing one fixed string correctly is
    the least a peer is asked to do, and one that cannot is not one to trust
    with the rest."""
    if not identity.startswith(prefix):
        return f"does not start with {prefix!r}"
    if len(identity) == len(prefix):
        return "carries no version string after the prefix"
    if len(identity.encode("utf-8")) > IDENTITY_MAX_BYTES:
        return f"exceeds {IDENTITY_MAX_BYTES} bytes"
    if any(unicodedata.category(ch) == "Cc" for ch in identity):
        return "contains a control character"
    return None


def _shown(identity: str, limit: int = 120) -> str:
    """An identity string as a diagnostic shows it: escaped, so a control
    character cannot forge a log line, and truncated, so an oversized one is
    refused without being echoed whole (§3.1)."""
    shown = repr(identity[:limit])
    return shown + f"... ({len(identity)} chars)" if len(identity) > limit else shown


def _manifest_identity() -> "tuple[str, str] | None":
    """The ``(id, version)`` of the manifest in the working directory, or
    ``None``.  Best effort by design: the identity string is diagnostic, and a
    plugin whose manifest is elsewhere still has ``plugin_id`` to name."""
    try:
        with open(MANIFEST_FILE, encoding="utf-8") as f:
            manifest = json.load(f)
        plugin_id, version = manifest.get("id"), manifest.get("version")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(plugin_id, str) and isinstance(version, str) and plugin_id and version:
        return plugin_id, version
    return None


def _sdk_version() -> str:
    """The version of THIS package, from the source that was loaded.

    Never from ``importlib.metadata``: that names whichever distribution
    called ``vizi-plugin-sdk`` is installed in the environment, which is not
    necessarily the copy running -- vizi's in-tree copy (``vizi.plugins.sdk``),
    a checkout on ``PYTHONPATH``, or an older install beside a namesake all
    load different files, and the string would claim an unrelated
    distribution's version.  The constant in ``__init__`` is
    the one thing that travels with the file, and when the package was
    imported under another name that name is worth a session log too,
    because it says WHICH copy spoke."""
    from . import __version__

    package = __name__.rpartition(".")[0]
    if package and package != "vizi_plugin_sdk":
        return f"{__version__} as {package}"
    return __version__


def compose_identity(
    plugin_id: str, version: "str | None" = None, build: "str | None" = None
) -> str:
    """This plugin's ``Ready.id``: ``Ready; I am <version string>`` (§3.1).

    **Publishing a good one should cost the author nothing, and the SDK must
    not guess**, because the string exists for reproducibility and a
    confident wrong claim is worse than none.  So the version string is
    composed from what is KNOWN: the manifest id and version (``version`` if
    passed, else the manifest's from the working directory the host launches
    a plugin in, and only when its ``id`` is this ``plugin_id``); ``build``
    when given; then the SDK's version, because the SDK is what carries the
    conversation and its build is part of what spoke.  Not the protocol
    version: that is an implementation detail the ``Ready`` fields already
    state, and it says nothing about the build.

    ``build`` is the author's own tag -- a release hash, a CI build number --
    and it is the ONLY route for a revision, on purpose.  An earlier cut read
    the nearest ``.git`` up from the working directory, which named the
    wrong project (this SDK's own checkout, for the example plugin inside it;
    a dotfiles repository, for a plugin under a home that is one) and named
    ``HEAD`` for a working tree that was not at ``HEAD``.  Only the build
    step knows which revision it built and whether the tree was clean; a
    run-time reading of the filesystem does not.  Checked by the same rule
    the host applies, so a plugin that could not compose its own string
    fails HERE, at start-up, with the reason, rather than at the handshake
    with the host's."""
    if version is None:
        found = _manifest_identity()
        if found is not None and found[0] == plugin_id:
            version = found[1]
    parts = [f"{plugin_id} {version}" if version else plugin_id]
    if build:
        parts.append(f"build {build}")
    parts.append(f"vizi-plugin-sdk {_sdk_version()}")
    identity = READY_PREFIX + "; ".join(parts)
    problem = check_identity(identity, READY_PREFIX)
    if problem is not None:
        raise ValueError(f"the plugin's identity string {problem}: {_shown(identity)}")
    return identity


_MISSING_DEPS = (
    "the vizi plugin SDK needs its plugin-side dependencies; "
    "install grpcio, protobuf and atomicshm in the plugin environment "
    "(pip install grpcio protobuf atomicshm)"
)


#: The stubs resolve on FIRST USE, not at import: grpcio and protobuf are
#: plugin-environment deps a vizi wheel install does not carry, and the rule is
#: that no module a host might import may pull them in at import time
#: (`_lazy.py`).  The host imports `doorbell.py` for its phase and view codes;
#: this module keeps the discipline because `control.py` and `memory.py` do,
#: and because a host reaching for `PROTOCOL_MINOR` should not have to install
#: grpcio to read it.
pb2 = _LazyProto("pb2")
pb2_grpc = _LazyProto("pb2_grpc")
roi_pb2 = _LazyProto("roi_pb2")


# Built per call, not at module scope: pb2 enum values force the protobuf
# import, which must stay deferred (see _LazyProto).  Human-rate paths only.
def _np_to_dtype() -> dict:
    return {
        np.dtype(np.uint8): pb2.DType.DTYPE_U8,
        np.dtype(np.uint16): pb2.DType.DTYPE_U16,
        np.dtype(np.float32): pb2.DType.DTYPE_F32,
    }


def _dtype_to_np() -> dict:
    return {v: k for k, v in _np_to_dtype().items()}


@dataclass
class ReaderInfo:
    """What a reader's ``open(uri)`` returns: the dataset's shape + dtype, and
    whatever calibration the reader KNOWS.

    ``spacing_zyx`` is the voxel pitch in ``spacing_unit`` (``"um"``), and
    ``time_increment`` the interval between consecutive timepoints in
    ``time_unit`` (``"s"``); ``None`` means unknown, which is an honest and
    common answer -- never invent 1.0.  An axis the reader does not know can be
    ``None`` inside the tuple.  These ride ``ReaderOpened`` (§8.2) and win over
    a metadata document that disagrees.

    ``axis_inference`` says how the reader decided which axis is which --
    ``"ome"`` (the file names them), ``"fallback"`` (guessed), or anything
    plugin-specific.  The reference host records it on the dataset and
    branches on nothing: a plugin dataset is never re-shaped by vizi, so a
    guess here does not open the re-shape dialog the way a native reader's
    ``"fallback"`` does.  It is a log line, and worth one honest word."""

    shape_tczyx: tuple[int, int, int, int, int]
    dtype: str  # "u8" | "u16" | "f32"
    spacing_zyx: "tuple[float | None, float | None, float | None] | None" = None
    spacing_unit: str = "um"
    time_increment: "float | None" = None
    time_unit: str = "s"
    axis_inference: str = "plugin"
    #: The channels' names by index; ``""`` for one the reader does not
    #: name, and it may stop short of C.  They win over a metadata document's.
    channel_names: "tuple[str, ...]" = ()


@dataclass
class SeriesEntry:
    """One image of a file that holds several, as a reader lists it.

    ``id`` is the reader's own and opaque to the host, which sends it back as
    ``open(uri, series=id)``: an HDF5 path, an index, a member name -- whatever
    finds the image again.  ``label`` is what a user is shown (the id when
    empty).  ``shape_tczyx`` and ``dtype`` are for the picker and may be left
    ``None`` when knowing them would mean reading more than the listing
    should."""

    id: str
    label: str = ""
    shape_tczyx: "tuple[int, int, int, int, int] | None" = None
    dtype: "str | None" = None  # "u8" | "u16" | "f32"
    description: str = ""


@dataclass
class SeriesChoice:
    """What ``open(uri)`` returns instead of a :class:`ReaderInfo` when the file
    holds several images and none is honestly the one a user means.

    The SDK sends the list; the host asks the user and calls ``open(uri,
    series=<id>)`` **on the same instance** when the answer comes back
    promptly, so whatever the listing opened can stay open.  A list longer than
    one message carries is sent as a prefix and marked truncated -- say every
    image there is; the host is the one that decides how to show them."""

    entries: "list[SeriesEntry]"


@dataclass(frozen=True)
class AppliedFilter:
    """The processing stage voxels passed through, when they are not the
    file's own -- Apply Filter's stage, as the host applied it.

    It is on every array the host hands a plugin (:attr:`HostDataArray.filter`,
    ``None`` for the file's own voxels), on :attr:`HostViewState.filter` for
    what the views show, and on :attr:`ExportDescriptor.filter` for an export.
    It says what produced THOSE voxels: a channel the stage does not rewrite, a
    pyramid level above 0, or a timepoint it refused carries ``None`` even
    with a filter applied.  Compare :attr:`plugin_id` with your own to refuse
    to analyse your own stage's output."""

    plugin_id: str
    #: The manifest's ``version`` of the plugin that ran the stage.
    plugin_version: str
    stage_id: str
    #: Every declared key, as the stage received it, as plain Python values.
    params: dict
    #: The channels the stage rewrites, ascending.
    channels: tuple[int, ...]

    @classmethod
    def from_wire(cls, parent, field: str = "filter") -> "AppliedFilter | None":
        """``parent``'s ``AppliedFilter`` field, or ``None`` when it is absent."""
        if not parent.HasField(field):
            return None
        wire = getattr(parent, field)
        return cls(
            plugin_id=wire.plugin_id,
            plugin_version=wire.plugin_version,
            stage_id=wire.stage_id,
            params=_stages.param_values(wire.params),
            channels=tuple(int(c) for c in wire.channels),
        )


@dataclass
class ExportDescriptor:
    """What an exporter's ``begin`` receives: the whole-artifact geometry the
    host declared in ``OpenExport``, the output target, and what the artifact
    was derived from.

    **What the artifact IS** is the structured fields: ``shape_tczyx``, the
    calibration, and ``channels`` -- write a channel's name only when it is
    :attr:`ChannelName.declared`.  **What it came from** is
    ``source_metadata_*`` (the SOURCE's document, which describes the source's
    sizes and channels and so must never be written as the artifact's own) and
    ``provenance`` (vizi's own record of the export); embed either as provenance where the
    format keeps extra metadata."""

    locator: str
    shape_tczyx: tuple[int, int, int, int, int]
    dtype: "np.dtype"
    little_endian: bool
    source_metadata_form: str  # "ome-xml" | "ome-ngff" | "" (none)
    source_metadata_text: str
    #: The calibration the host knows (§8.3), ``None`` where it does not -- an
    #: exporter writes no measurement it was not given.
    spacing_zyx: "tuple[float | None, float | None, float | None] | None" = None
    spacing_unit: str = ""
    time_increment: "float | None" = None
    time_unit: str = ""
    #: This plugin's folder for the SOURCE dataset, as
    #: :attr:`HostViewState.file_state_dir` -- not for ``locator``.  Named, not
    #: created; ``None`` when the source has no local path.
    file_state_dir: "Path | None" = None
    #: The filter this export reads through, ``None`` when it writes
    #: the file's own voxels.  Which volumes are its output is each ``write``
    #: volume's :attr:`HostDataArray.filter`.  By default the host aborts the
    #: export at a timepoint the stage did not process; only when the
    #: user allowed it is that timepoint exported as the file has it.
    filter: "AppliedFilter | None" = None
    #: The artifact's channels in its C order, each as its SOURCE index with
    #: the name the person sees and where that came from.
    channels: "tuple[ChannelName, ...]" = ()
    #: vizi's provenance record for this export, parsed; ``None`` when the host sent
    #: none.  The same record vizi embeds in its own exports.
    provenance: "dict | None" = None


def _state_dir(raw: str) -> "Path | None":
    return Path(raw) if raw else None


def _provenance(raw: str) -> "dict | None":
    """``OpenExport.provenance``, parsed; a record that is not a JSON object
    is none at all -- provenance never fails an export."""
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _enum_name(enum, value: int, prefix: str = "") -> str:
    """``enum``'s name for ``value`` without ``prefix``; a value this build
    does not know -- a newer host's -- is its number as a string, not a
    ``ValueError`` out of a read or a shutdown handler."""
    try:
        return enum.Name(value).removeprefix(prefix)
    except ValueError:
        return str(int(value))


_REGISTERED: dict[str, type] = {}
_COMMANDS: dict[str, Callable] = {}
_EXPORTERS: dict[str, type] = {}
_TOOLS: dict[str, type] = {}
_PANEL_OPENED: dict[str, Callable] = {}
_SHUTDOWN_HOOK: list[Callable] = []
_CHUNK_SOURCES: dict[str, Callable] = {}
_STAGES: dict[str, type] = {}
#: ``"view_changed"`` / ``"regions_changed"`` -> the registered handler.
_SUBSCRIBED: dict[str, Callable] = {}
_FOLLOWS: dict[str, Callable] = {}
#: stage id -> its declared parameters, parsed at declaration.
_STAGE_PARAMS: dict[str, tuple] = {}

#: ``control_plane_protocol.md`` §9: the session shutdown grace window, used
#: when the host names no ``deadline_ms``.  It is an UPPER BOUND
#: and not a budget: the host tears down at its own deadline either way.
DEFAULT_SHUTDOWN_GRACE_S = 5.0
#: §9's 2 s doorbell grace, then the 5 s shutdown window, with room to spare:
#: the host's `Shutdown` after a lost doorbell always lands inside it.
DOORBELL_LOSS_BACKSTOP_S = 2.0 + DEFAULT_SHUTDOWN_GRACE_S + 3.0

_SHUTDOWN_REASONS = {
    0: "unspecified",
    1: "host_exit",
    2: "user_disabled",
    3: "session_fault",
    4: "restart",
    5: "not_needed",
    6: "user_stopped",
}


@dataclass(frozen=True)
class ShutdownNotice:
    """Why the host is ending the SESSION (``control_plane_protocol.md`` §3.3).

    **Every reaction is PERMITTED, never required.**  A plugin that ignores
    this and just returns is conforming; the field exists so that one which can
    do better is able to.  The axis is *will I be started again*, because that
    is the only thing a plugin can usefully act on inside the window:

    ==================  ================================================
    ``reason``          what it permits
    ==================  ================================================
    ``host_exit``       not coming back this run -- discard warm caches
                        rather than spend the window persisting them
    ``user_disabled``   not until a person acts -- as above
    ``not_needed``      may be started again without a person; cheap
                        state may be worth keeping
    ``user_stopped``    the person stopped it from work that would not
                        stop (§8.16); started again when next used
    ``restart``         **yes, imminently** -- the one reason to persist
                        expensive derived state
    ``session_fault``   something was wrong; a final diagnostic is worth
                        more here than anywhere else
    ==================  ================================================
    """

    #: One of the table above, or ``"unspecified"`` -- which is what an empty
    #: ``Shutdown`` reads as, and is not an error.
    reason: str = "unspecified"
    #: Milliseconds from the host's SEND, never an absolute time: the host
    #: cannot know when the message arrived, so a duration measured at the
    #: receiver would promise time the host does not intend to give.
    #:
    #: **``None`` means the host named none, and ``0`` means it named zero.**
    #: They are different answers and were the same one until the field became
    #: `optional` at `1.0` round two -- as a plain ``uint32`` an absent value
    #: and a zero were identical bytes.  A host that sets 0 is asking for no
    #: grace at all and is entitled to be obeyed; only ABSENCE takes §9's 5 s.
    deadline_ms: int | None = None
    #: For the plugin's own log; never parsed.
    detail: str = ""

    @property
    def grace_seconds(self) -> float:
        """What is left to work with, in seconds -- the host's figure, or §9's.

        §9's default is for an ABSENT deadline only.  Reading a present ``0``
        as "unset" would give a host five seconds it deliberately did not
        offer, which is the reading the presence bit exists to remove.
        """
        if self.deadline_ms is None:
            return DEFAULT_SHUTDOWN_GRACE_S
        return self.deadline_ms / 1000.0

    @property
    def starting_again(self) -> bool | None:
        """``True`` for ``restart``, ``False`` where a person must act, and
        ``None`` where the host has not said."""
        if self.reason == "restart":
            return True
        if self.reason in ("host_exit", "user_disabled"):
            return False
        return None


def reader(
    reader_id: str,
    *,
    extensions: "list[str] | str" = UNSET,
    name: str = UNSET,
    fallback_extensions: "list[str] | str" = UNSET,
) -> Callable[[type], type]:
    """Register a reader implementation class under ``reader_id``.

    The class must define ``open(self, uri) -> ReaderInfo`` and
    ``read_volume(self, t, c) -> np.ndarray`` (a ``(Z, Y, X)`` array).  A
    reader whose files can hold several images defines ``open(self, uri,
    series=None)`` instead: with ``series`` ``None`` it returns a
    :class:`SeriesChoice` when there is more than one, and the host calls it
    again with the id the user chose.

    The keyword arguments are the manifest's (`manifest_schema.md` §3.1), for
    a plugin that declares its interface in code (:func:`plugin`).
    """
    if isinstance(extensions, str):
        extensions = [extensions]
    if isinstance(fallback_extensions, str):
        fallback_extensions = [fallback_extensions]

    def deco(cls: type) -> type:
        _REGISTERED[reader_id] = cls
        _declare._record(
            "readers",
            reader_id,
            _declare._given(
                extensions=extensions, name=name, fallback_extensions=fallback_extensions
            ),
        )
        return cls

    return deco


def exporter(
    exporter_id: str, *, extension: str = UNSET, name: str = UNSET
) -> Callable[[type], type]:
    """Register an exporter implementation class under ``exporter_id``.

    The class must define ``begin(self, desc: ExportDescriptor)``,
    ``write(self, t, c, volume_zyx: np.ndarray)`` (called once per delivered
    ``(t, c)`` volume, in chunk order), ``finish(self)``, and ``abort(self)``
    (clean up the partial artifact — the host never finalizes an abort).
    Calls arrive serialized, in stream order, on the SDK's export worker —
    off the dispatch loop (§6), so a slow write blocks neither the loop nor a
    concurrent read.

    The keyword arguments are the manifest's (§3.4), for a declared plugin."""

    def deco(cls: type) -> type:
        _EXPORTERS[exporter_id] = cls
        _declare._record("exporters", exporter_id, _declare._given(extension=extension, name=name))
        return cls

    return deco


def stage(
    stage_id: str, *, name: str = UNSET, params: "list[dict]" = UNSET
) -> Callable[[type], type]:
    """Register a processing stage class under ``stage_id`` (``processing_stages.md``).

    The class defines ``plan(self, inp: StageInput, params: dict) -> StagePlan``,
    the largest output it produces from ``inp`` with these parameter values
    (§3), and ``process(self, slot: StageSlot)``, which edits the slot in place
    (§5) and finds its position's values in ``slot.params``.

    ``params`` is the manifest's declaration (``manifest_schema.md`` §3.7), a
    flat list such as ``[{"key": "sigma", "type": "float", "value": 2.0,
    "min": 0.1}]``; it is checked here, and vizi sends a value for every key.

    ``process`` returning is *processed*; raising :class:`RefuseVolume` --
    before writing anything -- is *refused*; any other exception is *failed*.
    ``plan`` raising :class:`Refusal` answers ``Error`` of its kind, which
    leaves the stage out of the chain.

    One instance per stage id, constructed with a :class:`PluginContext` when
    its signature asks for one.  ``plan`` runs on the command worker and
    ``process`` on the stage worker, so the two may overlap.  Where the stage
    runs, and how often in a chain, is the host's (§4)."""

    try:
        declared = _manifest.parse_stage_params(
            [] if params is UNSET else params, f"stage {stage_id!r}"
        )
    except _manifest.ManifestError as exc:
        raise _declare.DeclarationError(str(exc)) from None

    def deco(cls: type) -> type:
        _STAGES[stage_id] = cls
        _STAGE_PARAMS[stage_id] = declared
        _declare._record("stages", stage_id, _declare._given(name=name, params=params))
        return cls

    return deco


def tool(
    tool_id: str,
    *,
    name: str = UNSET,
    views: "list[str]" = UNSET,
    menu: "str | None" = UNSET,
    mode: str = UNSET,
    cancel_on_camera_move: bool = UNSET,
    wants_pick: bool = UNSET,
    triggers: "list[dict]" = UNSET,
    actions: "list[dict]" = UNSET,
    pane: dict = UNSET,
    view_targets: "list[str]" = UNSET,
) -> Callable[[type], type]:
    """Register a tool implementation class under ``tool_id`` (Phase 3).

    The class defines any of ``press`` / ``drag`` / ``release`` / ``cancel`` /
    ``fire`` / ``action``, each ``(self, ctx, ev)`` — a :class:`ToolContext`
    and the :class:`~vizi_plugin_sdk.doorbell.InputEvent` the host forwarded
    (``doorbell_E_input.md`` §1) — and optionally ``activated`` /
    ``deactivated``, each ``(self, ctx)`` or ``(self, ctx, binding)``, called
    after the host installs or retires the tool's activation (the menu entry
    checked, a trigger, `Esc`, another plugin's tool taken up); a hook with
    the second parameter is handed the ``ToolBinding`` just installed (or,
    at ``deactivated``, just withdrawn), and ``ctx.arrangement(tool_id)``
    finds a live one by id, so a tool can say *P toggles, U undoes* the
    moment it is engaged.  ``action`` receives a ``FIRE`` naming one
    of the manifest's ``actions`` (a key or a pane button fired into the
    engaged tool, mid-drag if that is where the user was; ``ctx.action(ev)``
    is its id) and falls back to ``fire`` when the class has no ``action``.
    ``ctx.pane(ev)`` is the geometry, spacing and
    visible high channels an event was captured under, already acknowledged
    and read without a round trip.  ``fire`` is a whole interaction in one
    record and has no drag or release; ``cancel`` also arrives *without* a
    record, when an interaction ended with neither its own terminal nor a
    successor announcing it (§6), so a tool holding gesture state is always
    told the gesture is over.

    Handlers run on the doorbell listener thread at event rate: do cheap work
    only, and push live feedback with ``ctx.overlay(id).set_items_live(...)``
    (the Pulsed lane and its ``D`` hint — never gRPC in this loop).

    ``wants_pick=False`` tells the host not to compute its surface pick for
    this tool's 3D presses (`E` §3): for a tool that resolves its own anchor
    from the ray, the pick is a trace per press nobody reads.  The default
    wants it.  Either way a handler must take what arrives -- a ``Ray`` or a
    ``RayAndPicked`` -- because the declaration is a request about what the
    host computes, not a filter on the wire: a host that predates the field
    always picks, and a host that picks may still find nothing and send the
    bare ray.  ``ev.ray`` is the ray under either, and ``ev.pick`` the pick
    or ``None``.

    The keyword arguments are the manifest's (§3.6), for a declared plugin;
    :func:`trigger`, :func:`action` and :func:`pane` build the nested ones."""
    fields = _declare._given(
        name=name,
        views=views,
        menu=menu,
        mode=mode,
        cancel_on_camera_move=cancel_on_camera_move,
        wants_pick=wants_pick,
        triggers=triggers,
        actions=actions,
        pane=pane,
        view_targets=view_targets,
    )

    def deco(cls: type) -> type:
        _TOOLS[tool_id] = cls
        _declare._record("tools", tool_id, fields)
        return cls

    return deco


def command(
    command_id: str,
    *,
    title: str = UNSET,
    description: str = UNSET,
    default_keybinding: "str | None" = UNSET,
    menu: "str | None" = UNSET,
    pane: dict = UNSET,
) -> Callable[[Callable], Callable]:
    """Register a command handler for a manifest-declared command id.

    The handler is called as ``fn(ctx)`` with a :class:`CommandContext`:
    ``ctx.args`` is the typed argument dict the host sent (slider floats,
    checkbox bools, dropdown ``index``/``value``…), ``ctx.panel(panel_id)``
    returns the :class:`PanelHandle` for pushing state patches.  Handlers run
    serialized on one command worker, off the session-dispatch loop, so a
    blocking ``ctx.host`` request can receive its reply.

    **Return a string to report success** — the host shows it (``CommandDone``
    ``ok=True, message=...``), which is the only command-scoped textual channel
    a command invoked from the Tools menu has.  Return ``None`` (or ``""``) to
    stay silent; raise to report failure (``ok=False`` with ``str(exc)``).

    ``True`` and any ``int`` are a silent success and ``False`` is a failure,
    so a handler whose trailing expression is a ``ctx.host`` call -- a boolean
    ``activate_tool``, a count from ``compile_label_volume`` or
    ``contribute_rois`` -- reads correctly.  Any other type is reported as a
    failure naming the type, rather than dropped.

    The keyword arguments are the manifest's (§3.2), for a declared plugin;
    ``title`` defaults to the id there.  A panel widget may bind the decorated
    function itself (``widgets.button("go", command=run)``)."""
    fields = _declare._given(
        title=command_id if title is UNSET else title,
        description=description,
        default_keybinding=default_keybinding,
        menu=menu,
        pane=pane,
    )

    def deco(fn: Callable) -> Callable:
        _COMMANDS[command_id] = fn
        _declare._record("commands", command_id, fields)
        _declare._record_command_handler(command_id, fn)
        return fn

    return deco


def panel_opened(panel_id: str) -> Callable[[Callable], Callable]:
    """Register a handler for a manifest-declared panel's open lifecycle.

    The handler receives a :class:`CommandContext` with empty ``args`` and
    can initialize panel widgets through ``ctx.panel(panel_id)``. It runs on
    every open, including when an existing dock is re-shown.  Unlike a
    :func:`command` handler it has no reply to ride: the return value goes
    nowhere, so report through the panel.
    """

    def deco(fn: Callable) -> Callable:
        _PANEL_OPENED[panel_id] = fn
        _declare._record_panel_handler(panel_id)
        return fn

    return deco


@dataclasses.dataclass(frozen=True)
class ViewChange:
    """Subscription ``"view"``: where the person is looking.
    ``open_generation`` 0 is no dataset open; a new value is a new dataset."""

    open_generation: int
    t: int
    z: int
    visible_channels: tuple[int, ...]
    #: The region the person has selected, ``""`` for none -- which region a
    #: plugin showing region data should show.
    selected_region_id: str = ""
    #: The filter applied to what the views show, ``None`` for the file's own
    #: voxels -- :attr:`HostViewState.filter`'s, as a push.
    filter: "AppliedFilter | None" = None


@dataclasses.dataclass(frozen=True)
class RegionChange:
    """One region created, edited or deleted.  ``revision`` is the one
    ``get_rois`` records carry (0 when deleted); ``origin`` is ``"user"`` or
    ``"plugin:<id>"`` -- a plugin's own regions are reported too."""

    region_id: str
    revision: int
    origin: str
    deleted: bool


@dataclasses.dataclass(frozen=True)
class RegionsChange:
    """Subscription ``"regions"``: which regions changed, never their
    geometry -- read those with ``ctx.host.get_rois(region_ids=...)``.
    ``reset`` means everything held before is void (the session's first push,
    or a new dataset) and ``changes`` names every region there is."""

    open_generation: int
    reset: bool
    changes: tuple[RegionChange, ...]


def on_view_changed(fn: Callable) -> Callable:
    """Register ``fn(ctx, change)``, called with a :class:`ViewChange` when the
    person moves ``t``/``z``, changes the visible channels, selects a region,
    or opens or closes a dataset -- and once when the session starts.  Needs
    ``subscriptions=["view"]`` and ``data_access``.

    It runs on a worker of its own, so it may call ``ctx.host``.  Changes that
    arrive while it runs are folded: the next call gets the newest, never a
    backlog -- a scrub costs one call per change the plugin can keep up with."""
    _SUBSCRIBED["view_changed"] = fn
    return fn


def follow(panel_id: str) -> Callable[[Callable], Callable]:
    """Register what drives a Follow the person starts from ``panel_id``
    (§8.15): ``fn(ctx, follow)``, with a :class:`Follow`.

    The plugin offers a Follow with ``ctx.panel(panel_id).offer_follow(label)``;
    the person starts it.  ``fn`` runs on a thread of its own for as long as the
    Follow lasts, moving the view with ``follow.move(...)`` and pacing itself
    with ``follow.wait(seconds)``, which returns ``False`` the moment the Follow
    ends.  Returning ends the Follow (``PLUGIN_ENDED``), as ``follow.end()``
    does.  The panel must declare ``follow_targets``::

        @follow("quant")
        def tour(ctx, f):
            for region in flagged:
                f.move(frame_region=region)
                if not f.wait(2.0):
                    return
    """

    def deco(fn: Callable) -> Callable:
        _FOLLOWS[panel_id] = fn
        return fn

    return deco


class Follow:
    """One running Follow (§8.15), handed to a :func:`follow` handler.

    ``targets`` is what it may drive; ``resumed`` is set when the person
    pressed Resume after taking control.  Once it has ended, ``active`` is
    False and ``reason`` names why (``"USER_TOOK_CONTROL"``, ``"STOPPED"``,
    ``"PLUGIN_ENDED"``, ``"INVALID"`` ...) with ``detail``."""

    def __init__(self, follow_id: int, panel_id: str, targets, resumed: bool, emit) -> None:
        self.id = int(follow_id)
        self.panel_id = panel_id
        self.targets = tuple(targets)
        self.resumed = bool(resumed)
        self.reason = ""
        self.detail = ""
        self._emit = emit
        self._seq = 0
        self._lock = threading.Lock()
        self._ended = threading.Event()

    @property
    def active(self) -> bool:
        return not self._ended.is_set()

    def move(
        self,
        *,
        t: "int | None" = None,
        z: "int | None" = None,
        select_region: "str | None" = None,
        frame_region: "str | None" = None,
        look_at=None,
        look_fit: float = 0.0,
        look_direction=None,
        orbit_point=None,
    ) -> bool:
        """Where the view should be now: whole state, the latest wins.  Only
        this Follow's ``targets``; the host eases the camera there.  Returns
        ``False``, sending nothing, once the Follow has ended.  A state the
        host cannot apply ends the Follow ``INVALID``."""
        named = {
            name
            for name, value in (
                ("t", t),
                ("z", z),
                ("select_region", select_region),
                ("frame_region", frame_region),
                ("look_at", look_at),
                ("orbit_point", orbit_point),
            )
            if value is not None
        }
        if not named:
            raise ValueError("a Follow state names at least one target")
        undeclared = sorted(named - set(self.targets))
        if undeclared:
            raise ValueError(f"this Follow does not drive {', '.join(undeclared)}")
        with self._lock:
            if self._ended.is_set():
                return False
            self._seq += 1
            state = pb2.FollowState(follow_id=self.id, seq=self._seq)
        if t is not None:
            state.t = int(t)
        if z is not None:
            state.z = int(z)
        if select_region is not None:
            state.select_region = str(select_region)
        if frame_region is not None:
            state.frame_region = str(frame_region)
        if look_at is not None:
            state.look_at.target_zyx.extend(float(v) for v in look_at)
            state.look_at.fit_extent = float(look_fit)
            if look_direction is not None:
                state.look_at.direction_zyx.extend(float(v) for v in look_direction)
        if orbit_point is not None:
            state.orbit_point.zyx.extend(float(v) for v in orbit_point)
        self._emit(pb2.PluginMsg(follow_state=state))
        return True

    def wait(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; ``False`` as soon as the Follow ends."""
        return not self._ended.wait(max(0.0, float(seconds)))

    def end(self) -> None:
        """End this Follow ("the tour is done").  The host answers
        ``PLUGIN_ENDED``; ending one that has ended does nothing."""
        with self._lock:
            if self._ended.is_set():
                return
            self._ended.set()
            self.reason = "PLUGIN_ENDED"
        self._emit(pb2.PluginMsg(end_follow=pb2.EndFollow(follow_id=self.id)))

    def _ended_by_host(self, reason: str, detail: str) -> None:
        with self._lock:
            if not self._ended.is_set():
                self.reason, self.detail = reason, detail
            self._ended.set()


_SHAPERS: dict[str, Callable] = {}
_SHAPE_ENDED: list[Callable] = []


def region_shaper(type_name: str, *, name: str = UNSET) -> Callable[[Callable], Callable]:
    """Register what computes the shape of every region of ``type_name``, one of
    this plugin's manifest ``region_types`` (`control_plane_protocol.md`
    §8.8): ``fn(ctx, edit)``, with a :class:`ShapeEdit`.

    It returns where EVERY vertex of the region now is -- an ``(n, 3)`` ZYX array
    in ``edit``'s own rest coordinates -- or :class:`Shape` to say the handle was
    clamped, or :class:`Rejected` to keep the last shape (during a drag) or
    revert the edit (at the commit).  It answers each edit from the edit alone::

        @region_shaper("tetra.volume")
        def keep_volume(ctx, edit):
            pts = edit.positions.copy()
            pts[edit.handle] = edit.p1
            return rescale_others_to_keep_volume(pts, edit.handle)

    Drags run on a worker of their own and only the newest is computed, so a
    slow shaper lags rather than queues; ``edit.final`` is True for the commit,
    which is always answered -- ``REJECTED`` when nothing is registered.

    It also declares ``type_name`` in the manifest's ``region_types`` for a
    plugin declared in code (``name`` is its display name there); a type with
    no shaper is declared with :func:`.declare.region_type`."""

    def deco(fn: Callable) -> Callable:
        _SHAPERS[type_name] = fn
        _declare._record("region_types", type_name, _declare._given(name=name), merge=True)
        return fn

    return deco


def on_shape_edit_ended(fn: Callable) -> Callable:
    """Register ``fn(ctx, drag_id, outcome, detail)``, called when an edit of a
    region this plugin shapes ends: ``outcome`` is ``"COMMITTED"``,
    ``"REVERTED"`` or ``"CANCELLED"`` (§8.8)."""
    _SHAPE_ENDED.append(fn)
    return fn


@dataclasses.dataclass(frozen=True)
class ShapeEdit:
    """A person editing a region whose type this plugin shapes (§8.8).

    ``roi`` is the region's REST form as it stood when the edit began (the wire
    ``pois_math.v1.Roi``); ``positions`` its vertices as an ``(n, 3)`` ZYX array.
    The grabbed vertex is ``handle``, which started at ``p0`` and which the
    person has put at ``p1`` -- already on its rail, plane or free space, along
    ``axis`` for ``mode``, and within the handle's declared extents.  ``final``
    is True for the commit."""

    region_id: str
    contrib_id: str
    type_name: str
    drag_id: int
    seq: int
    final: bool
    roi: object
    positions: object
    handle: int
    mode: int
    axis: object
    p0: object
    p1: object


@dataclasses.dataclass(frozen=True)
class Shape:
    """A shaper's answer that says the handle did not land at ``p1``."""

    positions: object
    clamped: bool = True


@dataclasses.dataclass(frozen=True)
class Rejected:
    """A shaper's answer that there is no shape for this position: during a
    drag the last one stays on screen; at the commit the edit reverts."""

    message: str = ""


def on_regions_changed(fn: Callable) -> Callable:
    """Register ``fn(ctx, change)``, called with a :class:`RegionsChange`
    whenever regions are created, edited or deleted.  Needs
    ``subscriptions=["regions"]`` and ``data_access``.

    Runs as :func:`on_view_changed` does.  Pushes that arrive while it runs
    are MERGED rather than dropped, so no region's change is lost: the next
    call names every region changed since the last one, with its latest
    change -- a later deletion outranking an earlier edit."""
    _SUBSCRIBED["regions_changed"] = fn
    return fn


@dataclasses.dataclass(frozen=True)
class ChunkWant:
    """One (level, t) volume the host wants from a chunked overlay
    (`control_plane_protocol.md` §5.7).  ``t`` is ``None`` for a ``static``
    overlay, whose one volume per level serves every timepoint."""

    level: int
    t: int | None

    @property
    def key(self) -> tuple[int, int | None]:
        return (self.level, self.t)


@dataclasses.dataclass(frozen=True)
class Chunk:
    """A chunk source's answer: bbox-tight ``(Z, Y, X)`` u8/u16 labels on
    the WANTED level's lattice, and where their origin sits in that level's
    voxel indices.  Return a bare array for a volume at the origin, or
    ``None`` for an empty chunk (a state, not a removal)."""

    labels: np.ndarray
    position_zyx: tuple[int, int, int] = (0, 0, 0)


def chunk_source(
    overlay_id: str, *, name: str = UNSET, time: str = UNSET
) -> Callable[[Callable], Callable]:
    """Register the function that answers the host's wants for a CHUNKED
    overlay (`control_plane_protocol.md` §5.7; manifest ``chunked: true``).

    Called as ``fn(ctx, want)`` with a :class:`PluginContext` and a
    :class:`ChunkWant`, on a worker of its own, once per want in the host's
    priority order, and returns the labels (:class:`Chunk`, a bare array, or
    ``None`` for empty).  The SDK does the rest: it keeps the host's want set,
    serves a want that has not been answered, reserves the bytes in the
    overlay's arena and pushes ``OverlayChunk`` -- and when the arena has no
    room it WAITS for the host's next ``ReleaseLoan`` and retries rather than
    failing the push, because the host asked and will release.  A want that
    leaves the set before it is served is dropped; one that returns is
    served again.  ``ctx.overlay(id).invalidate_chunks()`` re-serves what is
    currently wanted after the plugin's results changed.

    A plugin may instead push by hand with ``ctx.overlay(id).publish_chunk``,
    reading ``ctx.overlay(id).wants``; then it owns the waiting too.

    For a declared plugin this also declares the overlay, chunked, with the
    manifest's ``name`` and ``time`` (§3.5).
    """

    def deco(fn: Callable) -> Callable:
        _CHUNK_SOURCES[overlay_id] = fn
        _declare._record(
            "overlays",
            overlay_id,
            {"chunked": True, **_declare._given(name=name, time=time)},
            merge=True,
        )
        return fn

    return deco


def on_shutdown(fn: Callable) -> Callable:
    """Register the hook that runs when the host ends the session (§3.3).

    Called as ``fn(notice)`` with a :class:`ShutdownNotice`, on a worker of
    its own (never the command pool, §11a), after the host's ``Shutdown`` and
    before the SDK closes the stream -- and **closing the stream is what
    "done" means**, which is why the SDK waits for this to return rather than
    acknowledging anything.  What it reports ahead of the close is a
    ``ShutdownReport``: ``completed``, ``raised`` (with the exception's text),
    ``overran``, or ``no_hook`` when nothing was registered.

    **The hook may not start new work** (§3.3): flush what exists, persist what
    a ``restart`` makes worth persisting, write a final diagnostic.  Its budget
    is ``notice.grace_seconds`` and that is an upper bound rather than a
    promise -- the host tears down at its own deadline either way, so a hook
    that overruns is not given more time, it is cut off.

    One hook per plugin; registering a second replaces the first.
    """
    _SHUTDOWN_HOOK[:] = [fn]
    return fn


# ---------------------------------------------------------------------------
# Finding the arena to write into
# ---------------------------------------------------------------------------
#
# A plugin never grants a segment (`shared_memory_protocol.md` §9a, "what a
# plugin does not build"), so every arena it writes into was granted TO it.
# What §3 settles is that there is nothing to look up: **a segment carries at
# most one plugin-produced LOANED region**, and a grant with two is
# `malformed`, so the arena and the segment coincide.
#
# The alternative -- a key per region -- was tried here and is wrong.  `id` is
# the only candidate the IDL offers and §8's table says a receiver **ignores**
# it, that it is not required to be unique, and that it is never load-bearing;
# a rule needing it present and authoritative is a change to `id`'s
# specification wearing the clothes of a convention.  `purpose` is per segment
# rather than per region.  Index order is not a contract.
#
# So the lookup is by `purpose`, which names a whole segment, and §3's table is
# the vocabulary: `P` a panel's image arena, `O` an overlay's, `G` a reader's
# deposit.  A plugin that genuinely needs two arenas is granted two segments.


def _emit_or_reclaim(emit, msg, store: "_memory.SegmentStore", diagnose=None) -> None:
    """Send one message, counting the reservations it names against what the
    sender declared, and take back what it never sent.

    **The sender's own §5.6 accounting.**  A reservation's ``uses`` is the
    count the host releases at; a message naming it past that count -- a push
    then a compile of one item built with the default ``uses=1`` -- is refused
    HERE with :class:`~vizi_plugin_sdk.memory.UsesExceeded`, before it leaves
    and in either order, where before it released the bytes under the message
    still in flight on every timing.  The fix is the count on the builder.

    A push that reserves arena bytes and then fails to be admitted owns bytes
    nothing will ever release: the host was never told about them, so no
    ``ReleaseLoan`` is coming, and the arena is permanently short by that much.
    §5.6's refusals are all reachable — a full never-drop queue, a coalescing
    push naming a loan with no room, a message naming one reservation twice —
    so the reserve and the admission have to be one step or the other.  But a
    reservation an EARLIER message already carried is the host's to release,
    and a later message failing does not take it back: it stays
    outstanding, counted, until the session ends.

    The Go SDK reaches the same place from the other side, by owning the
    reservation inside its publish helper; this is the same rule for an API
    whose reservations happen while the caller is still building the message.
    """
    arenas = []
    for segment_id, offset, length in dict.fromkeys(_control._reservations(msg)):
        segment = store.segment(segment_id)
        if segment is None:
            continue
        try:
            region = next(
                r.index
                for r in segment.regions.values()
                if r.discipline is _memory.Discipline.LOANED
                and r.producer is _memory.Producer.PLUGIN
            )
            arenas.append((segment.loaned_unchecked(region), offset, length))
        except (StopIteration, KeyError, ValueError, _memory.Quarantined):
            pass  # not ours to count, or a region that is already gone
    named = []
    try:
        for arena, offset, length in arenas:
            arena.name(offset, length)
            named.append((arena, offset, length))
        emit(msg)
        # Carried: a reservation whose every use has now been sent is no
        # longer in flight, and a retirement waiting on it may go (§9.1).
        for arena, offset, length in named:
            arena.sent(offset, length)
    except BaseException:
        for arena, offset, length in named:
            before = arena.stranded
            arena.unname(offset, length)
            if arena.stranded > before and diagnose is not None:
                diagnose(
                    f"reservation ({offset}, {length}) stays outstanding: an earlier message "
                    "named it and this one was not admitted; the host holds a use that will "
                    "not arrive (§5.6)"
                )
        raise


def _report_uses(plane, ring) -> None:
    """§5.6: a reservation's ``uses`` count is the sender's to state
    consistently, and a receiver seeing two applies the FIRST and reports.

    Site 24, appended after both SDKs applied the first count and kept the
    reason to themselves.  Drained from the ring rather than pushed by it: §11's
    rate bound and its outbox are the control plane's, and a reporter in the
    memory layer would be a second budget for one session.
    """
    detail = ring.take_disagreement()
    if detail is not None:
        plane.report("uses_disagreement", detail)


#: Consecutive coalesced live publishes before the first heads-up; then again at
#: each doubling, so a stall that lasts is not a stream of lines.
_LIVE_STALL_REPORT = 64


class NoRegion(RuntimeError):
    """No installed segment offers the arena a handle needs.

    Raised rather than silently dropping the push: a plugin that writes an
    image into an arena the host never granted has produced nothing the host
    can read, and saying so is the difference between a missing feature and a
    missing picture.
    """


class AmbiguousSegment(NoRegion):
    """Two installed segments answer one ``(purpose, subject)``.

    A host defect -- a replacement granted without retiring the incumbent,
    which ``supersedes`` exists to prevent -- and not a wait: unlike a missing
    or full arena, nothing this plugin does will resolve it.  A subclass of
    :class:`NoRegion`, so a caller that only knows the parent still refuses.
    """


def _segment_for(store: "_memory.SegmentStore", purpose: str, subject: str):
    """The one installed segment serving ``subject`` under ``purpose`` (§3).

    ``purpose`` says what KIND of segment; ``subject`` says WHICH one, and for
    `O` and `P` it is required non-empty precisely so this lookup exists.  For
    the exchange purposes it is empty and the purpose is the whole key, because
    a session holds one reader arena.

    Two segments answering one key is a host that granted a replacement without
    retiring the incumbent -- which `supersedes` exists to avoid -- and is
    refused rather than resolved, since picking either one is a guess.
    """
    # A RETIRING segment is no place to start anything (§9.1), so it is not a
    # candidate: with a successor installed the lookup finds the successor, and
    # without one it finds nothing -- the quiet outcome.
    segments = [
        s
        for s in store.installed()
        if s.purpose == purpose and s.subject == subject and not s._retiring
    ]
    named = f"purpose {purpose!r}" + (f" subject {subject!r}" if subject else "")
    if not segments:
        raise NoRegion(f"no segment with {named} is installed")
    if len(segments) > 1:
        raise AmbiguousSegment(f"{len(segments)} installed segments carry {named}; one names one")
    return segments[0]


def _arena(store: "_memory.SegmentStore", purpose: str, subject: str = ""):
    """The one plugin-produced LOANED region of that segment.

    Both halves of "the one" are §3's, and neither is a heuristic: the grant
    that declared two arenas was refused at install, and `subject` is what
    tells two contributions' segments apart.
    """
    segment = _segment_for(store, purpose, subject)
    for region in segment.regions.values():
        if (
            region.discipline is _memory.Discipline.LOANED
            and region.producer is _memory.Producer.PLUGIN
        ):
            return segment, region, segment.loaned(region.index)
    raise NoRegion(
        f"segment {segment.segment_id} ({purpose!r}) declares no plugin-produced "
        "LOANED region to reserve from"
    )


#: Every `HOST_REQUEST` kind onto the worker pool and handler that serve it,
#: by attribute name on the servicer (§6).  **A partition, checked below.**  A
#: request kind with no lane is not an error anyone sees: it is a diagnostic
#: and a host waiting on a reply that never comes -- which is what a kind
#: left off a list produces.  What is
#: checked is the list itself, against the control plane's classification.
_REQUEST_LANES: dict[str, tuple[str, str]] = {
    "open_reader": ("_reader_pool", "_on_open"),
    "read_volume": ("_reader_pool", "_on_read_volume"),
    "read_plane": ("_reader_pool", "_on_read_plane"),
    "metadata": ("_reader_pool", "_on_metadata"),
    "close_reader": ("_reader_pool", "_on_close_reader"),
    "open_export": ("_export_pool", "_on_open_export"),
    "export_chunk": ("_export_pool", "_on_export_chunk"),
    "finish_export": ("_export_pool", "_on_finish_export"),
    "abort_export": ("_export_pool", "_on_abort_export"),
    # Off the loop like the rest (see `_invoke_pool`): a handler blocking in
    # `ctx.host` must not stall the routing, and a command in flight when
    # `Shutdown` arrives is waited for and keeps its reply (§3.3).
    "invoke_command": ("_invoke_pool", "_on_invoke"),
    # Processing stages (`processing_stages.md`).
    "plan_stage": ("_invoke_pool", "_on_plan_stage"),
}


def _assert_request_lanes_exhaustive() -> None:
    """The lanes partition the control plane's `HOST_REQUEST` kinds: every
    request has a lane and every lane serves a request.  Import-time, because
    both sides are plain tables and nothing here needs the IDL."""
    requests = {
        kind for kind, cls in _control.HOST_CLASSES.items() if cls is _control.Class.HOST_REQUEST
    }
    lanes = set(_REQUEST_LANES)
    if requests != lanes:
        raise RuntimeError(
            "plugin SDK request lanes do not partition HOST_REQUEST: "
            f"requests with no lane {sorted(requests - lanes)}, "
            f"lanes serving no request {sorted(lanes - requests)}"
        )


_assert_request_lanes_exhaustive()


def _live_lane(store: "_memory.SegmentStore", purpose: str, subject: str = ""):
    """The plugin-produced PULSED lane of that segment.

    **§3.1 closed this at `1.0`, on the arena rule's own argument**: at most one
    plugin-produced `PULSED` region per segment, `malformed` otherwise, so the
    grant that declared two was refused at install -- and a plugin with two
    overlays is granted two `O` segments, told apart by `subject`.  Before
    that, the `O` row read *"plugin-published Pulsed live lanes"*, plural, with
    §8 forbidding `Region.id` as the key, and this SDK served a single lane and
    refused two rather than guessing.  Both halves of "the one" are now the
    document's.
    """
    segment = _segment_for(store, purpose, subject)
    for region in segment.regions.values():
        if (
            region.discipline is _memory.Discipline.PULSED
            and region.producer is _memory.Producer.PLUGIN
        ):
            return segment, region, segment.pulsed_writer(region.index)
    raise NoRegion(
        f"segment {segment.segment_id} ({purpose!r}) declares no plugin-produced "
        "PULSED lane to publish into"
    )


def _value_to_python(v: "pb2.Value"):
    kind = v.WhichOneof("kind")
    if kind is None:
        return None
    if kind == "ints":
        return tuple(int(i) for i in v.ints.values)
    return getattr(v, kind)


def _python_to_value(v) -> "pb2.Value":
    if isinstance(v, bool):  # before int — bool is an int subclass
        return pb2.Value(b=v)
    if isinstance(v, int):
        return pb2.Value(i=v)
    if isinstance(v, float):
        return pb2.Value(f=v)
    if isinstance(v, (list, tuple)):
        return pb2.Value(ints=pb2.IntList(values=[int(i) for i in v]))
    return pb2.Value(s=str(v))


def _declared_panel_root(panel_id: str):
    """One panel's declared root widget, from the declaration in code or else
    ``vizi-plugin.json`` in the working directory; ``None`` when neither
    declares it."""
    declared = _declare._STATE.contributions["panels"].get(panel_id)
    if declared is not None:
        return declared.get("root")
    try:
        with open(MANIFEST_FILE, encoding="utf-8") as f:
            panels = json.load(f).get("contributions", {}).get("panels", [])
        return next((p.get("root") for p in panels if p.get("id") == panel_id), None)
    except (OSError, ValueError, AttributeError):
        return None


def _declared_list_max_rows(panel_id: str) -> "dict[str, int]":
    """Each ``list`` widget's ``max_rows`` in one panel (the manifest default
    when unsaid); empty when the panel is not declared here."""
    out: dict[str, int] = {}

    def walk(widget) -> None:
        if not isinstance(widget, dict):
            return
        if widget.get("kind") == "list" and widget.get("id"):
            bound = widget.get("max_rows", _manifest.LIST_DEFAULT_MAX_ROWS)
            if isinstance(bound, int) and not isinstance(bound, bool):
                out[widget["id"]] = bound
        for child in widget.get("children", ()):
            walk(child)

    walk(_declared_panel_root(panel_id))
    return out


def _declared_list_cells(panel_id: str) -> "dict[str, frozenset[str]]":
    """Each ``list`` widget's template widget ids in one panel, from the
    declaration in code or else ``vizi-plugin.json`` in the working directory;
    empty when neither declares the panel.  Best effort: it only lets the
    outbox drop a removed row's pending cell ops."""
    root = _declared_panel_root(panel_id)
    out: dict[str, frozenset[str]] = {}

    def walk(widget) -> None:
        if not isinstance(widget, dict):
            return
        if widget.get("kind") == "list" and widget.get("id"):
            out[widget["id"]] = frozenset(
                c["id"] for c in widget.get("row", ()) if isinstance(c, dict) and c.get("id")
            )
        for child in widget.get("children", ()):
            walk(child)

    walk(root)
    return out


class PanelHandle:
    """Push state patches for one declared panel.  Thread-safe (patches ride
    the outbound queue); last-write-wins per widget on the host side.

    A ``list`` widget's rows are set with :meth:`set_rows`, and every other
    method takes ``row=`` to address one row's cell: ``widget_id`` is then the
    row template's widget and ``row`` the key :meth:`set_rows` gave it."""

    def __init__(
        self,
        panel_id: str,
        emit: Callable[["pb2.PluginMsg"], None],
        store: "_memory.SegmentStore",
        drop_rows: "Callable[[str, frozenset, frozenset], int] | None" = None,
    ):
        self._panel_id = panel_id
        self._emit = emit
        self._store = store
        self._drop_rows = drop_rows
        self._list_cells: "dict[str, frozenset[str]] | None" = None
        #: Images pushed before the panel's `P` segment is granted, which the
        #: host does when the panel OPENS: the latest per (widget, row), sent
        #: on `PanelOpened`.  The host keeps every other patch to an unopened
        #: panel; an image cannot be kept there, because the
        #: bytes it names have nowhere to live yet, so it is kept here.
        self._held_images: dict[tuple[str, str], bytes] = {}
        #: Guards the held set AND the hold-or-send decision, so an image is
        #: never both held and sent, nor an older one flushed over a newer
        #: one.  While a flush runs, new pushes join the held set.
        self._held_lock = threading.Lock()
        self._flushing = False

    def _patch(self, *ops: "pb2.PatchOp") -> None:
        _emit_or_reclaim(
            self._emit,
            pb2.PluginMsg(panel_patch=pb2.PanelPatch(panel_id=self._panel_id, ops=list(ops))),
            self._store,
        )

    def set_value(self, widget_id: str, value, *, row: str = "") -> None:
        """Set a widget's value -- for a selectable ``list``, the selected row's
        key, or ``""`` for none."""
        self._patch(
            pb2.PatchOp(widget_id=widget_id, set_value=_python_to_value(value), row_key=row)
        )

    def set_enabled(self, widget_id: str, enabled: bool, *, row: str = "") -> None:
        self._patch(pb2.PatchOp(widget_id=widget_id, set_enabled=bool(enabled), row_key=row))

    def set_visible(self, widget_id: str, visible: bool, *, row: str = "") -> None:
        self._patch(pb2.PatchOp(widget_id=widget_id, set_visible=bool(visible), row_key=row))

    def offer_follow(self, label: str) -> None:
        """Offer a Follow from this panel (§8.15): vizi shows a "Follow:
        ``label``" button in the panel's first row, and the person starts it,
        running the panel's :func:`follow` handler.  The panel must declare
        ``follow_targets``.  A newer offer replaces the last."""
        if not str(label):
            raise ValueError("an offer needs a label; withdraw_follow() withdraws")
        self._offer(str(label))

    def withdraw_follow(self) -> None:
        """Withdraw the offer; a Follow running from it ends ``WITHDRAWN``."""
        self._offer("")

    def _offer(self, label: str) -> None:
        self._emit(
            pb2.PluginMsg(offer_follow=pb2.OfferFollow(panel_id=self._panel_id, label=label))
        )

    def set_rows(self, list_id: str, keys) -> None:
        """A ``list`` widget's rows, in display order: the whole list,
        replacing the last.  A new key's row starts from the template; a key
        that leaves drops its row.  Keys are non-empty, distinct, at most 128
        characters, and at most the list's ``max_rows``; the host refuses a
        longer list whole."""
        keys = [str(k) for k in keys]
        if len(set(keys)) != len(keys) or not all(0 < len(k) <= 128 for k in keys):
            raise ValueError("row keys must be distinct, non-empty and at most 128 characters")
        # Refused here, not by the host: cell ops for more rows than the list
        # can hold can overrun the session's coalescing capacity, which is a
        # fault rather than a refusal.
        max_rows = _declared_list_max_rows(self._panel_id).get(list_id)
        if max_rows is not None and len(keys) > max_rows:
            raise ValueError(f"list {list_id!r} holds at most {max_rows} rows, not {len(keys)}")
        if self._list_cells is None:
            self._list_cells = _declared_list_cells(self._panel_id)
        cells = self._list_cells.get(list_id)
        if cells:
            if self._drop_rows is not None:
                self._drop_rows(self._panel_id, cells, frozenset(keys))
            kept = frozenset(keys)
            with self._held_lock:
                for held in [
                    k for k in self._held_images if k[0] in cells and k[1] and k[1] not in kept
                ]:
                    del self._held_images[held]
        self._patch(pb2.PatchOp(widget_id=list_id, set_rows=pb2.RowKeys(keys=keys)))

    def set_progress(
        self,
        widget_id: str,
        value: float = 0.0,
        *,
        indeterminate: bool = False,
        row: str = "",
    ) -> None:
        """Move a ``progress`` widget.  ``value`` is a fraction in
        ``[0, 1]`` and the host clamps it; ``indeterminate`` says the length is
        not known in advance and wins over ``value``.

        Not a command and never echoed: unlike a slider driven for the same
        purpose, a ``progress`` widget binds nothing, so setting it cannot come
        back as an invocation.
        """
        self._patch(
            pb2.PatchOp(
                widget_id=widget_id,
                set_progress=pb2.Progress(value=float(value), indeterminate=bool(indeterminate)),
                row_key=row,
            )
        )

    def set_options(self, widget_id: str, options: list[str], *, row: str = "") -> None:
        self._patch(
            pb2.PatchOp(
                widget_id=widget_id,
                set_options=pb2.Options(options=[str(o) for o in options]),
                row_key=row,
            )
        )

    def _has_segment(self) -> bool:
        if self._store is None:
            return False
        return any(
            s.purpose == _memory.PURPOSE_PANEL and s.subject == self._panel_id and not s._retiring
            for s in self._store.installed()
        )

    def _hold_image(self, widget_id: str, row: str, png_bytes: bytes) -> None:
        """Keep ``png_bytes`` until the panel can show it: the newest per
        widget and row, and no more than :data:`HELD_IMAGE_BYTES` in all --
        an unopened panel is no reason to grow without bound.
        Call with ``_held_lock`` held."""
        key = (widget_id, row)
        others = sum(len(b) for k, b in self._held_images.items() if k != key)
        if others + len(png_bytes) > HELD_IMAGE_BYTES:
            raise NoRegion(
                f"panel {self._panel_id!r} is not open and holds {others} bytes of images "
                f"already; {len(png_bytes)} more would pass {HELD_IMAGE_BYTES}"
            )
        self._held_images[key] = bytes(png_bytes)

    def _flush_held_images(self) -> None:
        """The panel can show images now (it opened, or its segment arrived):
        send what was held.  One that fails is reported and the rest still go;
        images pushed meanwhile are held and go in the next round,
        so nothing overtakes a newer image."""
        failures = []
        while True:
            with self._held_lock:
                if not self._held_images or not self._has_segment():
                    self._flushing = False
                    break
                self._flushing = True
                held, self._held_images = self._held_images, {}
            for (widget_id, row), png in held.items():
                try:
                    self._send_image(widget_id, row, png)
                except Exception as exc:  # noqa: BLE001 -- reported below, the rest still go
                    failures.append(f"{widget_id!r} row {row!r}: {exc}")
        if failures:
            raise NoRegion("held images could not be sent: " + "; ".join(failures))

    def push_image(self, widget_id: str, png_bytes: bytes, *, row: str = "") -> None:
        """Show ``png_bytes`` in an ``image`` widget.  The bytes go through a
        plugin-produced LOANED arena (never gRPC); the patch carries only the
        reference.

        **The bytes stay lent until the host sends ``ReleaseLoan``**
        (`control_plane_protocol.md` §5.6): the arena's cursor is what owns
        them, nothing here reuses or overwrites them, and the control plane
        applies the release when it arrives.  An arena with no room fails the
        push and counts it -- never blocks, never reuses -- and the
        ``SegmentShortfall`` that refusal merged tells the host by how much.

        **Before the panel has opened** there is no arena: the host grants the
        panel's segment when the person opens it.  The image is then kept --
        the latest per widget and row -- and sent when the panel opens, ahead
        of the plugin's own ``@panel_opened`` handler; it is not an error.
        """
        with self._held_lock:
            if self._flushing or not self._has_segment():
                self._hold_image(widget_id, row, png_bytes)
                return
            # Sent directly: an older held image for the same cell must not be
            # flushed over it later.
            self._held_images.pop((widget_id, row), None)
        self._send_image(widget_id, row, png_bytes)

    def _send_image(self, widget_id: str, row: str, png_bytes: bytes) -> None:
        # §3: the `P` grant's `subject` is this panel's manifest id.  A plugin
        # with two panels holds two `P` segments and the same question the
        # overlay rule answered arrives here, which is why `subject` is on the
        # segment rather than on one discipline's region.
        try:
            segment, _region, arena = _arena(self._store, _memory.PURPOSE_PANEL, self._panel_id)
            reserved = arena.reserve(bytes(png_bytes))
        except (_memory.Retiring, NoRegion) as exc:
            if isinstance(exc, NoRegion) and self._has_segment():
                raise
            # The segment began retiring between the lookup and the reserve:
            # an ordinary outcome, kept for the panel's next segment (§9.1).
            with self._held_lock:
                self._hold_image(widget_id, row, png_bytes)
            return
        if reserved is None:
            self._shortfall(arena)
            raise NoRegion(
                f"the panel image arena is full ({arena.largest_free} bytes free, "
                f"{len(png_bytes)} wanted)"
            )
        offset, length = reserved
        self._patch(
            pb2.PatchOp(
                widget_id=widget_id,
                set_image=pb2.ImageRef(segment_id=segment.segment_id, offset=offset, len=length),
                row_key=row,
            )
        )

    def _shortfall(self, arena) -> None:
        """§5.4: one merged ``SegmentShortfall`` per arena, sent when it exists."""
        pending = arena.take_shortfall()
        if pending is not None:
            self._emit(
                pb2.PluginMsg(
                    segment_shortfall=pb2.SegmentShortfall(
                        segment_id=pending.segment_id,
                        region=pending.region,
                        wanted_bytes=pending.wanted_bytes,
                        largest_free_bytes=pending.largest_free_bytes,
                        dropped=pending.dropped,
                    )
                )
            )


def _field_is_repeated(field) -> bool:
    """Is this field descriptor repeated, across every protobuf we support?

    `FieldDescriptor.label` was deprecated in protobuf 5.27 in favour of
    `is_repeated`, and the upb C extension has since DROPPED it — but only in
    some per-Python-version builds: with one `protobuf==6.33.6` install,
    `label` is present on 3.11 and absent on 3.13.

    Both spellings are load-bearing, in opposite directions.  `pyproject.toml`
    declares `protobuf>=4.25,<7` deliberately, so a plugin environment may
    carry protobuf 4.x (the TensorFlow-era runtimes the pin calls out by name),
    where `is_repeated` does not exist at all.  Modern first, `label` as the
    fallback, and neither may be assumed.

    The `is None` test is the subtle part and must not be "simplified" to a
    truthiness check: `is_repeated` is `False` for every singular field, so
    `getattr(...) or field.label == ...` would fall through and read `.label`
    on precisely the fields the new builds cannot answer for — reintroducing
    this bug while looking like a fix for it.
    """
    repeated = getattr(field, "is_repeated", None)
    if repeated is None:
        return field.label == field.LABEL_REPEATED
    return bool(repeated)


def _iter_geom_buffers(message):
    """Yield every populated GeomBuffer below a protobuf message.

    Reflection keeps the live-lane self-containment check complete when a new
    geometry kind or auxiliary buffer field is added; a hand-maintained list
    previously missed line colors and both label-volume buffers.
    """
    if message.DESCRIPTOR is pb2.GeomBuffer.DESCRIPTOR:
        yield message
        return
    for field, value in message.ListFields():
        if field.type != field.TYPE_MESSAGE:
            continue
        for child in _control._message_children(field, value):
            yield from _iter_geom_buffers(child)


def _pane_ref(pane) -> "_memory.PaneRef | None":
    """Normalise what a handler naturally has -- the ``InputEvent`` it is
    answering, a ``PaneRef``, or a ``(view_id, epoch)`` pair -- to §2's pair."""
    if pane is None:
        return None
    if isinstance(pane, _memory.PaneRef):
        return pane
    if isinstance(pane, tuple):
        view_id, epoch = pane
    else:
        view_id, epoch = pane.view_id, pane.epoch
    if not 0 <= view_id <= 0xFF or not 0 < epoch < 1 << 32:
        raise ValueError(f"pane ({view_id}, {epoch}): view_id is a u8 and epoch a non-zero u32")
    return _memory.PaneRef(view_id, epoch)


def _declared_uses(uses, inline: bool) -> int:
    """Validate a builder's ``uses`` (§5.6): a positive count of the messages
    that will name the reservation.  Inline geometry has no reservation, so a
    count above one there is a mistake about what the parameter is for."""
    if isinstance(uses, bool) or not isinstance(uses, int) or uses < 1:
        raise ValueError(f"uses must be a positive integer; got {uses!r}")
    if inline and uses > 1:
        raise ValueError(
            "uses declares the messages that will name a segment reservation; "
            "inline geometry has none"
        )
    return uses


class _ChunkServer(threading.Thread):
    """Answers a chunked overlay's wants with its `@chunk_source`, one at a
    time in the host's order, on its own thread (§5.7).

    The one rule that differs from every other push: **a publish the arena
    cannot hold is DEFERRED, not failed** -- the host asked for this volume
    and will release the one before it -- so on `NoRegion` the computed
    labels are kept and the push retried at the next `ReleaseLoan`, want
    change or, while a grant is still landing, a short tick.  A want that
    leaves the set is forgotten (its computed answer too); one that returns
    is served again."""

    #: How long to wait for a release before re-checking anyway -- a grant's
    #: two halves may land after the want, and neither wakes this thread.
    RETRY_S = 0.25

    def __init__(self, overlay: "OverlayHandle", source, context) -> None:
        super().__init__(name=f"vizi-sdk-chunks-{overlay._overlay_id}", daemon=True)
        # Not `_handle`: a `Thread` subclass owns no underscore name on the
        # base class, and 3.13 took that one (`Thread._handle` is the
        # `_ThreadHandle` `start()` type-checks) -- assigning it raised
        # `TypeError` from `start()` on every 3.13 and nothing on 3.11.
        self._overlay = overlay
        self._source = source
        self._context = context
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def _next_want(self) -> "ChunkWant | None":
        # The served-set is reconciled by the HANDLE as each set arrives
        # (`OverlayHandle._wanted`), never here: two sets landing before this
        # thread wakes -- `[]` then a returning key -- must both count.
        for want in self._overlay._wants:
            if want.key not in self._overlay._served:
                return want
        return None

    def run(self) -> None:
        cv = self._overlay._chunk_cv
        handle = self._overlay
        pending: "tuple[ChunkWant, object, int] | None" = None
        while True:
            with cv:
                while not self._stopped:
                    if pending is not None:
                        want, _answer, gen = pending
                        if gen == handle._generation and want in handle._wants:
                            break
                        # The results moved (`invalidate_chunks`) or the want
                        # left: a deferred answer is stale either way and is
                        # dropped, never published.
                        pending = None
                    want = self._next_want()
                    if want is not None:
                        break
                    handle._in_flight = None
                    cv.wait()
                if self._stopped:
                    handle._in_flight = None
                    return
                gen = handle._generation
                # Named while the source runs and while the answer waits for
                # room, so a set that drops this key retires the work
                # (`_wanted`); the generation read above is what it checks.
                handle._in_flight = want.key
            if pending is None:
                try:
                    answer = self._source(self._context(), want)
                except Exception as exc:  # noqa: BLE001 -- a source bug is not a fault
                    handle._diagnose(f"chunk source for {want} raised: {exc!r}")
                    with cv:
                        if gen == handle._generation:
                            handle._served.add(want.key)
                    continue
                pending = (want, answer, gen)
                # Back to the top, where a held answer is checked against the
                # set and the generation before it goes out: the key may have
                # left while the source ran, and a set that named it again
                # since is a new ask this answer does not serve.
                continue
            want, answer, gen = pending
            labels, position = None, (0, 0, 0)
            if isinstance(answer, Chunk):
                labels, position = answer.labels, tuple(answer.position_zyx)
            elif answer is not None:
                labels = answer
            try:
                handle.publish_chunk(want, labels, position_zyx=position)
            except AmbiguousSegment as exc:
                # Not a wait: retrying every 0.25 s forever would leave the
                # overlay empty for the session with nothing saying why.
                # Diagnosed, and the want is marked served so it is not asked
                # again until the want set changes.
                handle._diagnose(f"publishing chunk {want} failed: {exc}")
            except NoRegion:
                # Deferred: keep the answer, wait for room (a release) or for
                # the want to go away.
                with cv:
                    if not self._stopped:
                        cv.wait(timeout=self.RETRY_S)
                continue
            except Exception as exc:  # noqa: BLE001
                handle._diagnose(f"publishing chunk {want} failed: {exc!r}")
            with cv:
                if gen == handle._generation:
                    handle._served.add(want.key)
                handle._in_flight = None
            pending = None


class OverlayHandle:
    """Push typed overlay draw lists for one manifest-declared overlay id.

    ``set_items`` is a FULL replacement (last-write-wins, the panel-patch
    idiom); ``clear`` retracts the overlay.  Bulk geometry is reserved from
    the overlay's plugin-produced LOANED arena and stays lent until the host
    sends ``ReleaseLoan`` (`control_plane_protocol.md` §5.6).  ``set_items_live``
    publishes into the overlay's plugin-produced PULSED region and rings the
    doorbell with a ``D``.  Positions are **ZYX doubles** — the wire
    convention; the host owns the flip to its internal axis order.

    Both regions are found by this overlay id matching the **segment's**
    ``subject`` (`shared_memory_protocol.md` §3) -- never by ``Region.id``,
    which §8 says a receiver ignores -- and neither exists until the host has
    granted it."""

    def __init__(
        self,
        overlay_id: str,
        emit: Callable[["pb2.PluginMsg"], None],
        store: "_memory.SegmentStore",
        transport=None,
        diagnose: "Callable[[str], None] | None" = None,
    ) -> None:
        self._overlay_id = overlay_id
        self._emit = emit
        self._store = store
        #: Where a chunk server's trouble goes (§12): the control plane's
        #: diagnostics in a session, stderr when a handle stands alone.
        self._diagnose = diagnose or (lambda text: print(text, file=sys.stderr))
        #: The doorbell, for the ``D`` that hints at a live publication.  A
        #: hint is advisory (``doorbell_D_notification.md`` §5): a refused or
        #: absent one costs a reader nothing, because the reader re-checks on
        #: its own schedule regardless.
        self._transport = transport
        self._revision = 0
        #: Consecutive live publishes that found no buffer (§6.4 step 5).
        self._live_stalled = 0
        self._publish_lock = threading.Lock()
        #: §5.7: the host's current want set for a chunked overlay, in its
        #: order, and the server that answers it (started on the first want
        #: when a `@chunk_source` is registered).
        self._wants: list[ChunkWant] = []
        self._chunk_server: "_ChunkServer | None" = None
        self._chunk_cv = threading.Condition()
        #: Keys served under their CURRENT membership of the want set --
        #: intersected with every set as it arrives, so a key that leaves and
        #: returns is served again however quickly.
        self._served: set[tuple[int, int | None]] = set()
        #: Bumped by `invalidate_chunks`, and by a want set that drops the key
        #: the server is working on: an answer computed under an older
        #: generation is stale and is not published.
        self._generation = 0
        #: The key the chunk server is computing or holding for room, or None.
        self._in_flight: "tuple[int, int | None] | None" = None

    def _arena(self):
        return _arena(self._store, _memory.PURPOSE_OVERLAY, self._overlay_id)

    # -- chunked overlays (§5.7) ------------------------------------------------

    @property
    def wants(self) -> list[ChunkWant]:
        """The (level, t) volumes the host currently wants, in its order."""
        with self._chunk_cv:
            return list(self._wants)

    def publish_chunk(self, want: ChunkWant, labels, *, position_zyx=(0, 0, 0)) -> None:
        """Push one (level, t) volume: bbox-tight ``(Z, Y, X)`` u8/u16 labels
        on the want's level, ``position_zyx`` their origin in that level's
        indices; ``labels=None`` publishes an empty chunk.  Raises
        :class:`NoRegion` when the arena has no room -- the caller waits for
        a release and retries (`@chunk_source` does this for you)."""
        chunk = pb2.OverlayChunk(overlay_id=self._overlay_id, level=int(want.level))
        if want.t is not None:
            chunk.t = int(want.t)
        if labels is not None:
            a = np.ascontiguousarray(labels)
            if a.dtype not in (np.uint8, np.uint16):
                raise ValueError(f"chunk labels must be uint8 or uint16, got {a.dtype}")
            wire_dtype = pb2.DTYPE_U8 if a.dtype == np.uint8 else pb2.DTYPE_U16
            # A chunk the arena cannot hold is DEFERRED by its caller (§5.7):
            # the host asked and will release.  So the capacity is checked
            # ahead of the reservation, and a full arena is a `NoRegion`
            # without the shortfall a fixed-arena push would record and send.
            _segment, _region, arena = self._arena()
            padded = -(-a.nbytes // _memory._LOANED_ALIGNMENT) * _memory._LOANED_ALIGNMENT
            if arena.largest_free < padded:
                raise NoRegion(
                    f"the overlay arena has {arena.largest_free} bytes free and the chunk "
                    f"needs {padded}; waiting for a release"
                )
            chunk.labels.CopyFrom(self._geom(a, a.dtype, wire_dtype, ndim=3))
            chunk.position_zyx[:] = [int(v) for v in position_zyx]
        _emit_or_reclaim(self._emit, pb2.PluginMsg(overlay_chunk=chunk), self._store)

    def set_label_table(
        self,
        colormap,
        *,
        label_names: "dict[int, str] | None" = None,
        label_visible: "dict[int, bool] | None" = None,
        opacity: float = 1.0,
        hidden: bool = False,
        draw_priority: int = 0,
    ) -> None:
        """The chunked overlay's LUT, names, visibility and style -- what an
        ordinary label volume carries per item, once per overlay (§8.8's LUT
        rule).  Sent inline; a 256x4 LUT is a kilobyte."""
        lut = np.ascontiguousarray(colormap, dtype=np.uint8)
        if lut.shape != (256, 4):
            raise ValueError(f"colormap must be (256, 4) u8, got {lut.shape}")
        table = pb2.OverlayLabelTable(
            overlay_id=self._overlay_id,
            colormap=pb2.GeomBuffer(inline=lut.tobytes(), shape=[256, 4], dtype=pb2.DTYPE_U8),
            style=pb2.OverlayStyle(
                opacity=float(opacity), hidden=bool(hidden), draw_priority=int(draw_priority)
            ),
        )
        for label_id, name in (label_names or {}).items():
            table.label_names[int(label_id)] = str(name)
        for label_id, visible in (label_visible or {}).items():
            table.label_visible[int(label_id)] = bool(visible)
        self._emit(pb2.PluginMsg(overlay_label_table=table))

    def invalidate_chunks(self) -> None:
        """The plugin's results changed: serve every current want again, and
        drop any answer computed before this call that is still waiting for
        arena room -- it would publish the old results under a served key."""
        with self._chunk_cv:
            self._served.clear()
            self._generation += 1
            self._chunk_cv.notify_all()

    def _wanted(self, wants: list[ChunkWant], source, context) -> None:
        """The host restated its want set (`ChunksWanted`).  Reconciled HERE,
        under the lock, as each set arrives: a key absent from this set is no
        longer served, so `[]` followed by a set that names it again -- a
        dataset change, or t moving there and back within one tick -- serves
        it again even when the worker saw neither set land."""
        with self._chunk_cv:
            keys = {w.key for w in wants}
            if self._in_flight is not None and self._in_flight not in keys:
                # The key the server is working on has left: what it computes
                # is the OLD ask, and a set naming the key again is a new one
                # -- a dataset change restates `[]` first, then the same
                # (level, t) for the next dataset.  Retired here, as it
                # leaves, rather than at publication, where the reissued key
                # would look wanted and the stale answer would be served and
                # marked complete for the dataset it was never computed on.
                self._generation += 1
            self._wants = list(wants)
            self._served &= keys
            if self._chunk_server is None and source is not None:
                self._chunk_server = _ChunkServer(self, source, context)
                self._chunk_server.start()
            self._chunk_cv.notify_all()

    def _arena_freed(self) -> None:
        """A `ReleaseLoan` landed: a deferred publish may fit now."""
        with self._chunk_cv:
            self._chunk_cv.notify_all()

    def _stop_chunk_server(self) -> None:
        with self._chunk_cv:
            server = self._chunk_server
            self._chunk_server = None
            if server is not None:
                server.stop()
            self._chunk_cv.notify_all()
        if server is not None:
            server.join(timeout=2.0)

    def _live(self):
        return _live_lane(self._store, _memory.PURPOSE_OVERLAY, self._overlay_id)

    def _next_revision(self) -> int:
        self._revision += 1
        return self._revision

    def _geom(
        self, arr, dtype, wire_dtype, inline: bool = False, ndim: int = 2, uses: int = 1
    ) -> "pb2.GeomBuffer":
        a = np.ascontiguousarray(arr, dtype=dtype)
        if a.ndim != ndim:
            raise ValueError(f"geometry must be {ndim}-D; got shape {a.shape}")
        uses = _declared_uses(uses, inline)
        if inline:
            return pb2.GeomBuffer(inline=a.tobytes(), shape=list(a.shape), dtype=wire_dtype)
        try:
            segment, _region, arena = self._arena()
            reserved = arena.reserve(a.tobytes(), uses=uses)
        except _memory.Retiring:
            # Retired between the lookup and the reserve (§9.1): look again,
            # which finds the successor if the host granted one.
            segment, _region, arena = self._arena()
            reserved = arena.reserve(a.tobytes(), uses=uses)
        if reserved is None:
            self._shortfall(arena)
            raise NoRegion(
                f"the overlay geometry arena is full ({arena.largest_free} bytes free, "
                f"{a.nbytes} wanted)"
            )
        offset, length = reserved
        ref = pb2.SegmentRef(segment_id=segment.segment_id, offset=offset, len=length)
        if uses > 1:
            # §5.6: 0 and 1 both mean one, so the ordinary case stays off the
            # wire and a reservation named by more than one message carries
            # the count only the sender can know.
            ref.uses = uses
        return pb2.GeomBuffer(segment=ref, shape=list(a.shape), dtype=wire_dtype)

    def _reclaim(self, geom: "pb2.GeomBuffer") -> None:
        """Take back one reservation `_geom` made for an item that will not
        be built: no message names it, so nothing else ever releases it."""
        if geom.WhichOneof("source") != "segment":
            return
        try:
            _segment, _region, arena = self._arena()
        except NoRegion:
            return
        arena.reclaim(geom.segment.offset, geom.segment.len)

    def _shortfall(self, arena) -> None:
        """§5.4: one merged ``SegmentShortfall`` per arena, sent when it exists."""
        pending = arena.take_shortfall()
        if pending is not None:
            self._emit(
                pb2.PluginMsg(
                    segment_shortfall=pb2.SegmentShortfall(
                        segment_id=pending.segment_id,
                        region=pending.region,
                        wanted_bytes=pending.wanted_bytes,
                        largest_free_bytes=pending.largest_free_bytes,
                        dropped=pending.dropped,
                    )
                )
            )

    def marker_item(
        self,
        item_id: str,
        positions_zyx,
        *,
        color_rgba=None,
        size_px: float = 0.0,
        shape: str = "",
        frame: str = "voxel",
        inline: bool = False,
        uses: int = 1,
    ) -> "pb2.OverlayItem":
        """``uses``: how many messages will name this item's segment-backed
        geometry -- see :meth:`label_volume_item`."""
        return pb2.OverlayItem(
            item_id=item_id,
            transform=pb2.OverlayTransform(frame=frame),
            style=pb2.OverlayStyle(color_rgba=list(color_rgba or ())),
            marker=pb2.OverlayMarker(
                positions_zyx=self._geom(
                    positions_zyx, np.float64, pb2.DTYPE_F64, inline, uses=uses
                ),
                size_px=size_px,
                shape=shape,
            ),
        )

    def line_item(
        self,
        item_id: str,
        positions_zyx,
        *,
        topology: str = "",
        color_rgba=None,
        thickness: float = 0.0,
        frame: str = "voxel",
        inline: bool = False,
        uses: int = 1,
    ) -> "pb2.OverlayItem":
        """A polyline through ``positions_zyx`` (``topology="loop"`` closes
        it, ``"segments"`` draws independent pairs instead), ``thickness``
        screen pixels wide (0: the host's 1).  Drawn by the
        3D pane, and by the 2D panes PROJECTED into every slice: a segment
        with both endpoints in the slice is solid, any other is dashed --
        so a trace made in a 2D pane is solid there and a 3D path shows its
        in-plane run solid and the rest dashed.  In the 2D panes a pixel is a
        voxel, for this and for a marker's ``size_px`` alike
        (`control_plane_protocol.md` §8.5).  ``uses``: how many messages will
        name this item's segment-backed geometry -- see
        :meth:`label_volume_item`."""
        return pb2.OverlayItem(
            item_id=item_id,
            transform=pb2.OverlayTransform(frame=frame),
            style=pb2.OverlayStyle(color_rgba=list(color_rgba or ()), thickness=thickness),
            line_set=pb2.OverlayLineSet(
                positions_zyx=self._geom(
                    positions_zyx, np.float64, pb2.DTYPE_F64, inline, uses=uses
                ),
                topology=topology,
            ),
        )

    def text_item(
        self,
        item_id: str,
        text: str,
        anchor_zyx,
        *,
        size_px: float = 0.0,
        color_rgba=None,
        frame: str = "voxel",
    ) -> "pb2.OverlayItem":
        """A label at ``anchor_zyx`` -- a level-0 voxel under ``frame="voxel"``,
        as a marker's positions are -- drawn at ``size_px`` screen pixels (0:
        the host's 12).  Drawn by the 3D pane beside vizi's own annotation
        labels; the 2D panes draw no text at 1.0 (`control_plane_protocol.md`
        §8.5).  Inline: a label is bytes-small and has no loan to declare."""
        return pb2.OverlayItem(
            item_id=item_id,
            transform=pb2.OverlayTransform(frame=frame),
            style=pb2.OverlayStyle(color_rgba=list(color_rgba or ())),
            text=pb2.OverlayText(
                text=str(text),
                anchor_zyx=[float(v) for v in anchor_zyx],
                size_px=float(size_px),
            ),
        )

    def mesh_item(
        self,
        item_id: str,
        geometry: str,
        position_zyx,
        *,
        size: float = 1.0,
        color_rgba=None,
        frame: str = "voxel",
    ) -> "pb2.OverlayItem":
        """A unit ``"sphere"`` or ``"cube"`` centred at ``position_zyx`` (a
        level-0 voxel under ``frame="voxel"``) with ``size`` in WORLD units,
        isotropic -- a sphere's RADIUS, a cube's EDGE.  Drawn by the 3D pane
        with vizi's own gizmos; the 2D panes draw no mesh at 1.0.  Explicit
        vertices are not in the 1.0 vocabulary and the host refuses them."""
        if geometry not in ("sphere", "cube"):
            raise ValueError(f"mesh geometry must be 'sphere' or 'cube', got {geometry!r}")
        return pb2.OverlayItem(
            item_id=item_id,
            transform=pb2.OverlayTransform(
                frame=frame,
                position_zyx=[float(v) for v in position_zyx],
                scale_zyx=[float(size)] * 3,
            ),
            style=pb2.OverlayStyle(color_rgba=list(color_rgba or ())),
            mesh=pb2.OverlayMesh(geometry=geometry),
        )

    def label_volume_item(
        self,
        item_id: str,
        labels,
        colormap,
        *,
        label_names=None,
        label_visible=None,
        t: int | None = None,
        position_zyx=None,
        frame: str = "voxel",
        inline: bool = False,
        uses: int = 1,
    ) -> "pb2.OverlayItem":
        """A dense ``(Z, Y, X)`` u8/u16 label volume + ``(256, 4)`` u8 RGBA
        LUT — segmentation's natural output.  Labels are categorical
        (nearest-sampled, never interpolated).  **The LUT rule**: row 0 is
        label 0's alone and should be transparent (background); a foreground
        label ``id`` is drawn with row ``1 + (id - 1) % 255``, so ids 1..255
        use rows 1..255 directly and ids above 255 wrap over the foreground
        rows — never onto row 0.  A row's alpha is the ROW's, shared by every
        id that lands on it, and ``compile_label_volume`` persists it as the
        region's ``visible``.  ``label_visible`` is an optional ``{label id:
        bool}`` overriding that per LABEL, which the alpha cannot do above 255
        — zeroing row 1 to hide label 1 also hid label 256.  It governs
        the compiled REGION; the label voxels are painted from the LUT by the
        GPU, which has only the row.
        ``label_names`` is an optional ``{label id: name}`` mapping (or a
        sequence, index = id) used by ``compile_label_volume``; it is keyed by
        LABEL ID, so labels that share a LUT row keep their own names, and an
        unnamed label compiles to ``"Label <id>"``.  ``position_zyx`` places a
        bbox-tight volume at its origin; ``t`` scopes display to one timepoint
        (None => every t).  Composites in BOTH the 2D views and the native 3D
        pass; the 3D pass follows the renderer-selected pyramid level with
        nearest decimation, so labels finer than the decimation factor can
        drop out while zoomed out and return as the selected level changes.

        **``labels`` are LEVEL-0 voxels.**  ``frame="voxel"`` names the
        dataset's level-0 lattice whatever level the pane draws or the plugin
        read (`vizi_plugin.proto`, ``OverlayTransform``): compute on a
        decimated level and the volume must be converted before it is sent
        (`control_plane_protocol.md` §8.11), because ``scale_zyx`` — the one
        field that could say "these are level-L voxels" — is what the
        voxel-selection rule below rejects.

        **A label volume IS a voxel selection**: ``frame="voxel"`` (the
        default here), unit scale, identity rotation, an INTEGER
        ``position_zyx``, and the whole box -- position plus ``labels.shape``,
        zeros included -- inside the dataset.  The host applies that at the
        PUSH: an item that is not one refuses the whole ``set_items`` and the
        plugin is told which rule (``ContractViolation`` on
        ``overlay_item_refused``, in this process's log), and
        ``compile_label_volume`` refuses it again with a ``HostError``.  The
        one thing that displays and does not compile is inline geometry
        (``inline=True``): the compile's transport rule, not part of the
        definition -- an item that will be committed is sent segment-backed.
        The bytes ride the overlay arena the manifest sizes (``segment_mib``,
        32 MiB default, 512 max, never grown), so a whole-volume segmentation
        of a large dataset is bbox-tight or the manifest declares the arena it
        needs (`control_plane_protocol.md` §8.8).

        ``uses`` is **how many messages will name this item's bytes**: every
        ``set_items`` push that includes it, plus one for each
        ``compile_label_volume`` of it (which repeats the descriptor rather
        than the voxels, `control_plane_protocol.md` §8.8).  The labels and the
        LUT are loans (§5.6): the host releases each reservation once it has
        finished with that many messages, and **only the sender can know the
        number** -- a request and a push travel on channels with different
        drain priority, so the host cannot count what it has not yet seen.
        Under-declare and the release comes back while a later message still
        names the bytes, which the arena then hands out again; over-declare
        and the bytes stay pinned until the segment retires -- waste, never
        corruption.  The default is one push and nothing else; the common
        push-then-compile shape is ``uses=2`` **when the compile is
        certain**.  A use cannot be withdrawn once declared, so a push whose
        compile may never come -- per gesture, committed rarely -- declares
        one, and the compile reserves the item again when it happens (§8.8).
        Meaningless with ``inline=True``, which has no reservation, and
        refused there."""
        labels = np.ascontiguousarray(labels)
        if labels.dtype == np.uint8:
            wire_dtype = pb2.DTYPE_U8
        elif labels.dtype == np.uint16:
            wire_dtype = pb2.DTYPE_U16
        else:
            raise ValueError(f"labels must be uint8/uint16, got {labels.dtype}")
        # Everything that can refuse the item is checked BEFORE the arena is
        # touched, and the two reservations are one step: a builder that
        # raised between them, or after both, left bytes nothing would ever
        # release -- no message named them, so no `ReleaseLoan` was coming --
        # and a full arena is the ordinary way the second one fails.
        lv = pb2.OverlayLabelVolume()
        if label_names is not None:
            entries = (
                label_names.items() if hasattr(label_names, "items") else enumerate(label_names)
            )
            for label_id, name in entries:
                if isinstance(label_id, bool) or not isinstance(label_id, (int, np.integer)):
                    raise ValueError(f"label_names keys are label ids; got {label_id!r}")
                label_id = int(label_id)
                if label_id < 0 or label_id > 0xFFFF:
                    raise ValueError(f"label id {label_id} is outside u8/u16")
                if name:
                    lv.label_names[label_id] = str(name)
        if label_visible is not None:
            entries = (
                label_visible.items()
                if hasattr(label_visible, "items")
                else enumerate(label_visible)
            )
            for label_id, shown in entries:
                if isinstance(label_id, bool) or not isinstance(label_id, (int, np.integer)):
                    raise ValueError(f"label_visible keys are label ids; got {label_id!r}")
                label_id = int(label_id)
                if label_id < 0 or label_id > 0xFFFF:
                    raise ValueError(f"label id {label_id} is outside u8/u16")
                lv.label_visible[label_id] = bool(shown)
        if t is not None:
            lv.t = int(t)
        lv.labels.CopyFrom(self._geom(labels, labels.dtype, wire_dtype, inline, ndim=3, uses=uses))
        try:
            lv.colormap.CopyFrom(self._geom(colormap, np.uint8, pb2.DTYPE_U8, inline, uses=uses))
        except BaseException:
            self._reclaim(lv.labels)
            raise
        return pb2.OverlayItem(
            item_id=item_id,
            transform=pb2.OverlayTransform(
                frame=frame,
                position_zyx=[float(v) for v in (position_zyx or ())],
            ),
            label_volume=lv,
        )

    def set_items(self, items) -> None:
        with self._publish_lock:
            revision = self._next_revision()
            _emit_or_reclaim(
                self._emit,
                pb2.PluginMsg(
                    overlay_update=pb2.OverlayUpdate(
                        overlay_id=self._overlay_id,
                        items=list(items),
                        revision=revision,
                    )
                ),
                self._store,
            )

    def set_items_live(self, items, *, pane=None) -> None:
        """Drag-rate replacement (Phase 3 tools): publish the encoded update
        into this overlay's PULSED region and ring the doorbell — NEVER gRPC in
        a per-event loop.  Items must carry INLINE geometry (``inline=True`` on
        the builders): a Pulsed value is claimed and consumed whole, so each
        committed one has to be complete and self-contained.

        ``pane`` stamps §2's ``(view_id, epoch)`` onto the value -- the pair the
        geometry was computed under.  Pass the :class:`InputEvent` being
        answered (anything with ``view_id`` and ``epoch``), a
        :class:`~vizi_plugin_sdk.memory.PaneRef`, or a ``(view_id, epoch)``
        tuple.  **The host applies a value carrying a pair only while that pair
        names the pane's CURRENT epoch**: a stale
        one is consumed and counted, not drawn -- which is the point, since a
        crop or level the user has moved off is exactly where a gesture's
        geometry should not appear.  A value with no pair is applied as before.
        The ``D`` that follows carries the same pair.  Passing one for a lane
        the host granted without ``epoch_offset`` is a caller error.

        Returns nothing, and a coalesced publish is not an error
        (`shared_memory_protocol.md` §6.4 step 5): the newest READY value is
        untouched and still legible to a reader that has not arrived yet.  The
        ``D`` that follows is a hint and may be refused; a reader that polled
        only when told would stall on the first dropped one, which is why §5
        makes the re-check schedule unconditional at both ends."""
        pane = _pane_ref(pane)
        with self._publish_lock:
            revision = self._next_revision()
            update = pb2.OverlayUpdate(
                overlay_id=self._overlay_id,
                items=list(items),
                revision=revision,
            )
            segment, region, writer = self._live()
            # §3.3's two rules, checked by the SAME code a receiver runs
            # (:func:`check_live_overlay`) -- the sender refuses what the
            # receiver would refuse, rather than two readings of one section.
            broken = check_live_overlay(update, segment.subject)
            if broken:
                raise ValueError("; ".join(detail for _, detail in broken))
            payload = update.SerializeToString()
            # §3.2: capacity is `slot_bytes` less the pane header where one is
            # declared, because the header is the slot's last eight bytes and
            # the published length covers the payload only.
            capacity = region.slot_bytes - (
                _memory.PANE_HEADER_BYTES if region.epoch_offset is not None else 0
            )
            if len(payload) > capacity:
                # §3.3: the sender fails its own call rather than truncating or
                # splitting -- §6 has no continuation, and a partial
                # `OverlayUpdate` is not a smaller one.
                raise ValueError(
                    f"live update {len(payload)}B exceeds the {capacity}B payload capacity "
                    f"of a {region.slot_bytes}B slot"
                )
            if not writer.publish(payload, pane=pane):
                # Coalesced away; the durable newest value stands.  One drop is
                # ordinary; a long unbroken run is not -- the reader may be
                # holding its claims -- and it looked exactly like ordinary
                # coalescing from outside.  Reported as a
                # heads-up, never a fault: it is evidence of no progress, not a
                # claim the peer is wrong, and the host keeps scanning.
                self._live_stalled += 1
                n = self._live_stalled
                if n >= _LIVE_STALL_REPORT and n & (n - 1) == 0:
                    self._diagnose(
                        f"overlay {self._overlay_id!r}: {n} live publishes in a row found "
                        "no free buffer; the host may be holding its claims "
                        "(shared_memory_protocol.md §6.4)"
                    )
                return
            self._live_stalled = 0
            if self._transport is not None:
                # A list of INDICES, never the bare index: the transport once
                # read an int as a mask, so region 0 rang nothing -- the one
                # check of the 1.0 run that failed.
                self._transport.send_notification(
                    segment.segment_id,
                    [region.index],
                    epoch=pane.epoch if pane is not None else 0,
                    view_id=pane.view_id if pane is not None else 0,
                )

    def clear(self) -> None:
        with self._publish_lock:
            revision = self._next_revision()
            self._emit(
                pb2.PluginMsg(
                    overlay_remove=pb2.OverlayRemove(overlay_id=self._overlay_id, revision=revision)
                )
            )


def check_live_overlay(update: "pb2.OverlayUpdate", subject: str) -> list[tuple[str, str]]:
    """`shared_memory_protocol.md` §3.3's two rules, as ``(where, detail)``.

    Empty means the update may be applied.  This is the RECEIVER's check and
    the SENDER's refusal in one function, because §3.3 is one rule read from
    two ends and two implementations of it is how the ends come to disagree.

    - **Every ``GeomBuffer`` must be ``inline``** (`live_geometry_not_inline`).
      A Pulsed value is consumed at a moment neither end chooses, so a
      descriptor inside one names a loan the sender has no way to keep alive
      until the read happens.  **A live value is self-contained or it is not a
      value.**
    - **``overlay_id`` must equal the segment's ``subject``**
      (`lane_subject_mismatch`).  Redundant on purpose, and the redundancy is
      the check -- the same shape as ``region`` beside ``offset`` on a release.
      On a disagreement neither name is believed and the update is not applied,
      because there is nothing to choose between them and applying either is
      how a mis-published lane becomes a mis-drawn one.
    """
    broken: list[tuple[str, str]] = []
    for buf in _iter_geom_buffers(update):
        source = buf.WhichOneof("source")
        if source != "inline":
            # §3.3 says every buffer MUST BE inline, which is not the same as
            # "must not name a segment": a buffer with NO source is not inline
            # either, and it is the shape an author reaches by building a
            # `GeomBuffer` and forgetting to fill it.  The Go SDK read the
            # imperative and this one read its explanation; the imperative is
            # the rule.
            named = (
                f"names geometry in segment {buf.segment.segment_id}"
                if source == "segment"
                else "carries a GeomBuffer with no source"
            )
            broken.append(
                (
                    "live_geometry_not_inline",
                    f"overlay {update.overlay_id!r} {named}; a live lane carries "
                    "inline geometry only",
                )
            )
            break
    if update.overlay_id != subject:
        broken.append(
            (
                "lane_subject_mismatch",
                f"overlay_id {update.overlay_id!r} disagrees with the lane's subject "
                f"{subject!r}; neither name is believed",
            )
        )
    return broken


def decode_live_overlay(payload: bytes, subject: str):
    """§3.3: one live lane value, as ``(update, violations)``.

    The payload is a **serialized ``OverlayUpdate`` and nothing else** -- no
    envelope, no framing, no length prefix, which is the whole reason the
    length lives in the Pulsed state header rather than in the bytes.  A
    non-empty violation list means the update is **not applied** and each entry
    is a ``ContractViolation``'s ``where``.

    Here because a plugin publishing the format is also the only implementation
    of it either end can check against, and a rule with a wire code and no
    caller is a rule the peer can break without being told.
    """
    update = pb2.OverlayUpdate()
    update.ParseFromString(payload)
    return update, check_live_overlay(update, subject)


#: How long the blocking pickers (`request_asset`, `request_save_path`) wait
#: by default.  The wait is on a PERSON choosing a file, and §8.9 says a
#: command MAY wait on a picker and an SDK MUST NOT refuse it; giving up at a
#: request's 30 s would refuse it in practice.  The async forms have no deadline.
PICKER_TIMEOUT_S = 30 * 60.0


class HostError(Exception):
    """The host answered a data-access request with ``host_error``, the
    reply was malformed, or it timed out.  ``kind`` is a ``pb2.Error.Kind``
    value (0 for timeouts/malformed replies)."""

    def __init__(self, kind: int, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class Refusal(Exception):
    """A reader or exporter refusing a request WITH A KIND -- the plugin-side
    twin of :class:`HostError`, raised from ``open`` / ``read_volume`` /
    ``begin`` / ``write`` / ``finish`` and forwarded as ``Error{kind,
    message}`` (`reader_writer_contract.md` §1.5: the kinds are mandated
    because a third-party host may branch on them).  ``kind`` is a
    ``pb2.Error.Kind`` value: ``INVALID_ARGUMENT`` for a file that is not this
    reader's format, ``UNSUPPORTED`` for one it recognises and cannot serve,
    ``OUT_OF_BOUNDS`` for a chunk that contradicts the declared geometry.

    Any OTHER exception a reader or exporter raises is forwarded as
    ``IO_ERROR`` -- §1.5's "the file failed under the reader" -- except a
    ``FileNotFoundError``, which is §1.5's "`uri` cannot be opened",
    ``NOT_FOUND``.  A :class:`HostError` that propagates out of an exporter
    reading its source keeps the kind the host gave it."""

    def __init__(self, kind: int, message: str) -> None:
        super().__init__(message)
        self.kind = kind


_READER_DTYPES = {"u8": np.dtype(np.uint8), "u16": np.dtype(np.uint16), "f32": np.dtype(np.float32)}


def _reader_opened(info) -> "tuple[pb2.ReaderOpened, tuple[int, ...], np.dtype]":
    """``ReaderOpened`` for ``info`` (without its handle), with the shape and
    numpy dtype every later deposit is checked against.  Raises ``ValueError``
    (or ``TypeError``/``KeyError``) naming what is wrong, before anything is
    committed."""
    shape = tuple(int(v) for v in info.shape_tczyx)
    if len(shape) != 5 or any(n <= 0 for n in shape):
        raise ValueError(f"shape_tczyx must be five positive sizes, got {shape}")
    if info.dtype not in _READER_DTYPES:
        raise ValueError(f"dtype {info.dtype!r} is not one of {sorted(_READER_DTYPES)}")
    np_dtype = _READER_DTYPES[info.dtype]
    opened = pb2.ReaderOpened(
        shape_tczyx=list(shape),
        dtype=_np_to_dtype()[np_dtype],
        little_endian=True,
        axis_inference=str(getattr(info, "axis_inference", "") or "plugin"),
    )
    spacing = getattr(info, "spacing_zyx", None)
    if spacing is not None:
        sz, sy, sx = spacing
        opened.spacing_z = float(sz or 0.0)
        opened.spacing_y = float(sy or 0.0)
        opened.spacing_x = float(sx or 0.0)
        opened.spacing_unit = str(getattr(info, "spacing_unit", "um") or "")
    increment = getattr(info, "time_increment", None)
    if increment is not None:
        opened.time_increment = float(increment)
        opened.time_unit = str(getattr(info, "time_unit", "s") or "")
    names = [str(n or "") for n in (getattr(info, "channel_names", None) or ())]
    if len(names) > shape[1]:
        raise ValueError(f"channel_names names {len(names)} channels of {shape[1]}")
    opened.channel_names.extend(names)
    return opened, shape, np_dtype


#: The most image bytes a panel holds while it cannot show them:
#: the panel segment's default arena, so everything held fits when it opens.
HELD_IMAGE_BYTES = 16 << 20

#: How long an instance that listed a file's series is kept for the host's
#: answer.  A person is choosing, possibly from thousands of entries;
#: past this the instance is closed and the chosen series is opened fresh.
SERIES_CHOICE_HOLD_S = 600.0


def _open_series(impl, uri: str, series: "str | None"):
    """``impl.open(uri, series)``, or ``impl.open(uri)`` for a reader written
    before series existed -- whose files hold one image, so a series is never
    asked of it.  A one-entry :class:`SeriesChoice` is opened directly: a list
    of one is not a choice."""
    if series is None:
        info = impl.open(uri)
    elif _takes_series(impl.open):
        info = impl.open(uri, series=series)
    else:
        info = impl.open(uri)
    if isinstance(info, SeriesChoice) and series is None and len(info.entries) == 1:
        info = _open_series(impl, uri, str(info.entries[0].id))
    return info


def _takes_series(fn) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "series" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def _close_quietly(impl, diagnose) -> None:
    closer = getattr(impl, "close", None)
    if closer is None:
        return
    try:
        closer()
    except Exception as exc:  # noqa: BLE001 -- the reader's own bug, reported not raised
        diagnose(f"closing a reader raised: {exc!r}")


#: Room for the `PluginMsg` envelope around a `SeriesList` and its `truncated`.
_SERIES_ENVELOPE_BYTES = 64


def _series_list(choice: "SeriesChoice") -> "pb2.SeriesList":
    """The wire form of ``choice``, cut to what one message carries (§9): the
    entries that fit, in order, and ``truncated`` when some did not."""
    entries = list(choice.entries)
    if not entries:
        raise ValueError("a SeriesChoice lists no series; a file with no image is refused")
    seen: set[str] = set()
    for entry in entries:
        if str(entry.id) in seen:
            # The host answers with the id alone, so two entries sharing one
            # cannot be told apart.
            raise ValueError(f"series id {str(entry.id)!r} is listed twice")
        seen.add(str(entry.id))
    dtypes = _np_to_dtype()
    budget = _control.MAX_MESSAGE_BYTES - _SERIES_ENVELOPE_BYTES
    listed = pb2.SeriesList()
    used = 0
    for entry in entries:
        wire = pb2.SeriesEntry(
            id=str(entry.id), label=str(entry.label or ""), description=str(entry.description or "")
        )
        if entry.shape_tczyx is not None:
            shape = [int(v) for v in entry.shape_tczyx]
            if len(shape) != 5 or any(n <= 0 for n in shape):
                raise ValueError(f"series {entry.id!r}: shape_tczyx must be five positive sizes")
            wire.shape_tczyx.extend(shape)
        if entry.dtype is not None:
            if entry.dtype not in _READER_DTYPES:
                raise ValueError(
                    f"series {entry.id!r}: dtype {entry.dtype!r} is not one of "
                    f"{sorted(_READER_DTYPES)}"
                )
            wire.dtype = dtypes[_READER_DTYPES[entry.dtype]]
        size = wire.ByteSize()
        size += 1 + _varint_len(size)
        if used + size > budget:
            listed.truncated = True
            break
        listed.entries.append(wire)
        used += size
    return listed


def _varint_len(n: int) -> int:
    length = 1
    while n >= 0x80:
        n >>= 7
        length += 1
    return length


def _refusal(exc: BaseException, prefix: str) -> "pb2.Error":
    """The ``Error`` a raised exception becomes on the wire -- see
    :class:`Refusal` for the mapping.  ``prefix`` names the step (``open
    failed``), which is what the host's log has always shown."""
    if isinstance(exc, (Refusal, HostError)):
        kind = exc.kind
    elif isinstance(exc, FileNotFoundError):
        kind = pb2.Error.NOT_FOUND
    else:
        kind = pb2.Error.IO_ERROR
    return pb2.Error(kind=kind, message=f"{prefix}: {exc}")


@dataclass(frozen=True)
class HostViewState:
    """The host's active-view snapshot — ``ctx.host.view_state()``.

    ``t``/``z``/``level`` are the REQUESTED navigation state.  **``c`` is
    DEPRECATED and always 0**: vizi has no C picker and no active channel, so
    nothing moves it.  It was documented as "the slider's channel", naming a
    control that does not exist — read ``visible_channels``.

    ``visible_channels`` is the set enabled for display — ascending, one entry
    under a solo.  It is host-wide, not per pane, and the 3D pane composites
    at most 8, so past eight enabled it shows a subset of this set while the
    2D panes show all of it.  A tool has the same set, current to its epoch,
    on its ``E`` records.  The crop
    box is ZYX in level-0 voxel coords (min inclusive, max exclusive) and is
    a render clip — reads return uncropped data.  ``levels`` lists readable
    levels as ``(level, (Z, Y, X))`` — level 0 for a TIFF, which has no other
    — and a read's reply is authoritative.
    **Those are SHAPES, not scales**: the factor from level 0 is
    ``Geometry.levels``' number (§8.11), and the ratio of two shapes is not
    it on any odd dimension (31 -> 15 is a factor of 2, not 2.0667) -- see
    :mod:`vizi_plugin_sdk.geometry`.

    Use :meth:`crop_box` rather than the raw indices for anything that changes
    level: the index form cannot be converted across a pyramid exactly, which
    is why ``Geometry.crop`` carries the same box in edge coordinates
    (``control_plane_protocol.md`` §8.11).

    ``spacing_zyx`` / ``spacing_unit`` / ``spacing_provenance`` are the voxel
    pitch, carried here since ``1.0`` because §8.7 sends a plugin with
    no tool to this message and it did not previously carry the pitch.  Polled
    like the rest of this snapshot, so a tick stale; a TOOL reads its pane's
    geometry, which is exact to the record.  A provenance this SDK does not
    know is its number as a string (``"7"``), which matches no name here and
    so is never mistaken for a measurement."""

    t: int
    z: int
    c: int
    level: int
    shape_tczyx: tuple[int, int, int, int, int]
    dtype: "np.dtype | None"
    has_crop: bool
    crop_min_zyx: tuple[int, int, int] | None
    crop_max_zyx: tuple[int, int, int] | None
    mip: bool
    levels: tuple[tuple[int, tuple[int, int, int]], ...]
    dataset_uri: str
    dataset_name: str
    open_generation: int
    visible_channels: tuple[int, ...] = ()
    #: The voxel pitch, ZYX -- real only when :attr:`spacing_known`, and then
    #: per axis: an axis the file did not record is NaN (XY known, Z
    #: not).  :attr:`measured_spacing_zyx` reads it that way.  ``None`` with no
    #: dataset open.
    spacing_zyx: tuple[float, float, float] | None = None
    spacing_unit: str = ""
    #: The wire's ``SpacingProvenance``, as its name: ``"FILE"``,
    #: ``"OVERRIDE"``, ``"DISPLAY_FALLBACK"`` or ``"UNSPECIFIED"``.
    spacing_provenance: str = "UNSPECIFIED"
    #: This plugin's folder for the open file: ``plugins/<id>/`` in the
    #: file's companion directory, beside the host's region saves.  State about
    #: this one dataset goes here and moves and dies with it; settings go in
    #: :func:`~vizi_plugin_sdk.settings_dir`.  **Named, not created** -- make it
    #: when you first write.  ``None`` when the dataset has no local path.
    file_state_dir: "Path | None" = None
    #: The filter applied to what the views SHOW, ``None`` when none.
    #: Not what a read returns -- reads are the file's own unless the user
    #: shares the filter with plugins; each array's :attr:`HostDataArray.filter`
    #: is the answer for it.
    filter: "AppliedFilter | None" = None
    #: The frame interval, ``None`` when the dataset declares none --
    #: never a guessed 1 s.
    time_increment: "float | None" = None
    time_unit: str = ""
    #: The region the person has selected, ``""`` for none.
    selected_region_id: str = ""
    #: Which image of :attr:`dataset_uri` is open, as its reader's id; ``None``
    #: when the file holds one -- ``""`` is an id a reader may use.
    #: Two images of one file share a URI.
    dataset_series: "str | None" = None
    #: Every channel, in index order, with the name the person sees and where
    #: it came from; empty with no dataset open.  A ``"PLACEHOLDER"``
    #: name labels a trace but is never written as the file's.
    channels: "tuple[ChannelName, ...]" = ()

    @property
    def spacing_known(self) -> bool:
        """Whether ANY axis's pitch is a measurement -- which does not license
        using every axis: check :attr:`axis_known_zyx` for each axis you use.
        §8.11: the display fallback is never a measurement."""
        return any(self.axis_known_zyx)

    @property
    def axis_known_zyx(self) -> "tuple[bool, bool, bool]":
        """Which axes' pitch is a measurement.  **Check it for every axis
        you use before using that axis's pitch: one known axis says nothing
        about the rest** -- a file may record XY and not Z, which the host sends
        as NaN on that axis.  An axis is known only when the unit is given, the
        provenance is a measurement's (``FILE`` or ``OVERRIDE``, never the
        display fallback), and its pitch is finite and positive."""
        if (
            not self.spacing_unit
            or self.spacing_provenance not in ("FILE", "OVERRIDE")
            or not self.spacing_zyx
        ):
            return (False, False, False)
        return tuple(math.isfinite(v) and v > 0 for v in self.spacing_zyx)  # type: ignore[return-value]

    @property
    def measured_spacing_zyx(self) -> "tuple[float | None, float | None, float | None]":
        """The pitch per axis in :attr:`spacing_unit` -- ``None`` exactly where
        :attr:`axis_known_zyx` is false, and positive and finite elsewhere;
        never NaN, never the display fallback.  Scale the known axes and
        leave the others in voxels."""
        known = self.axis_known_zyx
        values = self.spacing_zyx or (0.0, 0.0, 0.0)
        return tuple(float(v) if k else None for v, k in zip(values, known))  # type: ignore[return-value]

    def crop_box(self) -> "EdgeBox | None":
        """The crop in EDGE coordinates, or ``None`` when there is none.

        The conversion is ``edge = index - 0.5`` (§8.11), and from there
        :meth:`EdgeBox.at_scale` with the pane's declared ``level_scale``
        converts exactly to any pyramid level while an index cannot (the
        pane's own ``crop_at(level)`` is that, from ``Geometry.crop``).  These
        two fields cannot express a bound that is not on a pixel boundary, so
        this is exact for every crop they *can* express.
        """
        if not self.has_crop or self.crop_min_zyx is None or self.crop_max_zyx is None:
            return None
        return EdgeBox.from_index_box(self.crop_min_zyx, self.crop_max_zyx)


@dataclass(frozen=True)
class HostRequestStatus:
    """What the host says about one in-flight request.

    ``pending`` is bookkeeping: the host took the request in and has not
    answered it.  ``awaiting_user`` narrows that to "blocked on a person" --
    §8.9's file picker is up -- which is the one delay neither side can
    shorten and the signal to keep waiting rather than give up.
    """

    subject_id: str
    pending: bool
    awaiting_user: bool


class HostDataArray(np.ndarray):
    """A host-read array stamped with the dataset open generation, and with
    the :class:`AppliedFilter` its voxels passed through, ``None`` when they
    are the file's own.  An exporter's ``write`` volumes are these too,
    at ``open_generation`` 0: an export names no generation."""

    open_generation: int
    filter: "AppliedFilter | None"

    def __new__(cls, array, open_generation: int, filter: "AppliedFilter | None" = None):
        result = np.asarray(array).view(cls)
        result.open_generation = int(open_generation)
        result.filter = filter
        return result

    def __array_finalize__(self, source) -> None:
        self.open_generation = getattr(source, "open_generation", 0)
        self.filter = getattr(source, "filter", None)


@dataclass(frozen=True)
class ViewResult:
    """The one answer to a ``ViewRequest`` (§8.15).  ``outcome`` is
    ``"APPLIED"``, ``"DECLINED"`` or ``"REFUSED"``; ``reason`` names a
    refusal (``"NOT_ENABLED"``, ``"SUPERSEDED"``, ``"UNSUPPORTED"``, ...) and
    is ``"NONE"`` otherwise.  A plugin reads this, never a later
    ``ViewChanged``, to know what happened."""

    outcome: str
    reason: str
    detail: str = ""

    @property
    def applied(self) -> bool:
        return self.outcome == "APPLIED"


def _fire_cause(ev) -> tuple[int, int]:
    if getattr(ev, "phase", None) != _doorbell.PHASE_FIRE:
        raise ValueError("only a FIRE is a cause: a view must not move during a gesture")
    return int(ev.tool), int(ev.interaction)


class InclusionMask(HostDataArray):
    """:meth:`HostClient.read_roi_inclusion`'s mask, with the host's own
    count of the voxels it covers -- checked against the mask, so a
    plugin can report the host's measurement footprint without recounting."""

    voxel_count: int

    def __new__(cls, array, open_generation: int, voxel_count: int):
        result = super().__new__(cls, array, open_generation)
        result.voxel_count = int(voxel_count)
        return result

    def __array_finalize__(self, source) -> None:
        super().__array_finalize__(source)
        self.voxel_count = getattr(source, "voxel_count", 0)


def _save_path_result(hm) -> "Path | None":
    """What a ``SavePathResult`` reply means: the path, or ``None`` for a
    cancel."""
    if hm.WhichOneof("msg") != "save_path_result":
        raise HostError(0, f"expected save_path_result, got {hm.WhichOneof('msg')!r}")
    result = hm.save_path_result
    if result.declined:
        return None
    if not result.path:
        raise HostError(0, "save path reply has no path and is not declined")
    return Path(result.path)


def _merge_regions(older: "RegionsChange", newer: "RegionsChange") -> "RegionsChange":
    """Two region pushes as one, the newer push's entry winning per region --
    a deletion included, whose revision is 0: a reset in the
    newer one voids the older, and a reset in the older one survives."""
    if newer.reset or older.open_generation != newer.open_generation:
        return newer
    merged = {c.region_id: c for c in older.changes}
    merged.update((c.region_id, c) for c in newer.changes)
    return RegionsChange(newer.open_generation, older.reset, tuple(merged.values()))


class _NewestWorker:
    """Runs ``fn(value)`` on a thread of its own for the newest value offered.
    A value offered while ``fn`` runs replaces a waiting one, or is merged into
    it by ``merge(older, newer)``; nothing queues behind a slow handler."""

    def __init__(self, name: str, fn: Callable) -> None:
        self._fn = fn
        self._cond = threading.Condition()
        self._pending = None
        self._has_pending = False
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name=f"vizi-sdk-{name}", daemon=True)
        self._thread.start()

    def offer(self, value, merge: "Callable | None" = None) -> None:
        with self._cond:
            if self._has_pending and merge is not None:
                value = merge(self._pending, value)
            self._pending, self._has_pending = value, True
            self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._has_pending and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
                value, self._pending, self._has_pending = self._pending, None, False
            try:
                self._fn(value)
            except Exception:  # noqa: BLE001 -- a handler's failure is the plugin's to log
                import logging

                logging.getLogger(__name__).exception("subscribed-push handler failed")


def _asset_result(hm, *, slot: str) -> "Path | None":
    """What an ``AssetResult`` reply means for the slot that was asked for."""
    if hm.WhichOneof("msg") != "asset_result":
        raise HostError(
            0,
            f"expected asset_result, got {hm.WhichOneof('msg')!r}",
        )
    result = hm.asset_result
    if result.slot != slot:
        raise HostError(
            0,
            f"asset reply names slot {result.slot!r}, expected {slot!r}",
        )
    if result.declined:
        return None
    if not result.path:
        raise HostError(0, "asset reply has no imported path")
    return Path(result.path)


class PendingRequest:
    """One request to the host, in flight -- what the ``*_async`` forms
    of :class:`HostClient` return, shaped like a ``concurrent.futures.Future``.

    ``request_id`` is the id the request went out under, which is what
    :meth:`HostClient.request_status` asks about: a plugin waiting on a file
    picker can tell a person browsing from a host that stopped draining
    without the waiting thread being the one that asks.

    :meth:`result` waits for the reply; a wait that times out raises
    ``TimeoutError`` and leaves the request pending, so it can be waited on
    again.  :meth:`abandon` stops waiting for good -- a reply that arrives
    after it is dropped, as a timed-out blocking request's is.
    """

    def __init__(self, client: "HostClient", request_id: str, finish: Callable) -> None:
        self._client = client
        self.request_id = request_id
        self._finish = finish
        self._reply = None
        self._arrived = threading.Event()

    def _arrive(self, hm) -> None:
        """The dispatch loop's delivery.  The first reply is the answer and
        the waiter entry goes with it, whichever of :meth:`done` and
        :meth:`result` the plugin calls, in whatever order, from however
        many threads -- neither waits behind the other."""
        if self._arrived.is_set():
            return
        self._reply = hm
        self._client._forget(self.request_id)
        self._arrived.set()

    def done(self) -> bool:
        """Whether the host's reply has arrived."""
        return self._arrived.is_set()

    def result(self, timeout: "float | None" = None):
        """The request's result, waiting up to ``timeout`` seconds (``None``
        waits indefinitely).  A host refusal raises :class:`HostError`."""
        if not self._arrived.wait(timeout):
            raise TimeoutError(f"host has not answered request {self.request_id!r} yet")
        hm = self._reply
        if hm.WhichOneof("msg") == "host_error":
            raise HostError(hm.host_error.kind, hm.host_error.message)
        return self._finish(hm)

    def status(self, timeout: float = 2.0) -> "HostRequestStatus":
        """:meth:`HostClient.request_status` for this request."""
        return self._client.request_status(self.request_id, timeout=timeout)

    def abandon(self) -> None:
        """Stop waiting; a reply that arrives later is dropped."""
        self._client._forget(self.request_id)


class HostClient:
    """Plugin-initiated host requests — ``ctx.host``.

    Thread-safe, callable from any plugin thread EXCEPT the doorbell
    listener: tool handlers run there at event rate, so a blocking request
    would stall input + grants — spawn a worker for fetch+segment gestures.
    Command handlers run on the SDK's command worker and may block freely.
    Data reads require the manifest's ``data_access`` permission; ROI
    contribution and label compilation require ``contribute_rois``. Runtime
    asset selection also uses ``data_access``. The host answers every request (failures raise
    :class:`HostError`, never a hang); ``timeout`` is the backstop for a dead
    session."""

    def __init__(
        self,
        emit: Callable[["pb2.PluginMsg"], None],
        store: "_memory.SegmentStore",
        plane: "_control.ControlPlane",
    ) -> None:
        self._emit = emit
        self._store = store
        self._plane = plane
        self._seq = itertools.count()
        self._waiters: dict[str, PendingRequest] = {}
        self._lock = threading.Lock()

    @property
    def host_identity(self) -> "str | None":
        """The host's identity string -- ``Hello from <version string>`` (§3.1)
        -- once the handshake has settled, else ``None``.  Free-form and never
        parsed here; it exists so a plugin can log which host it spoke to,
        which is the whole reason the strings exist."""
        return self._plane.host_identity

    @property
    def host_protocol(self) -> "tuple[int, int] | None":
        """``(major, minor)`` the host's ``Hello`` announced, once it has.

        The minor is what tells a plugin which additions within its major the
        host can take: §3.2 makes a minor bump backward-compatible, so a plugin
        may use a newer message only when the host's minor says it exists."""
        return self._plane.host_protocol

    @property
    def plugin_identity(self) -> "str | None":
        """This plugin's own ``Ready; I am <version string>``, as sent."""
        return self._plane.plugin_identity

    def route_reply(self, hm) -> bool:
        """Deliver one host reply to its blocked requester (dispatch-loop
        side).  False when nobody waits on the id (a timed-out request's
        late reply — dropped)."""
        with self._lock:
            pending = self._waiters.get(hm.id)
        if pending is None:
            return False
        pending._arrive(hm)
        return True

    def _forget(self, rid: str) -> None:
        with self._lock:
            self._waiters.pop(rid, None)

    def _start(self, msg: "pb2.PluginMsg", finish: Callable = lambda hm: hm) -> PendingRequest:
        pending = PendingRequest(self, msg.id, finish)
        with self._lock:
            self._waiters[msg.id] = pending
        try:
            # Through the sender's §5.6 accounting like every push: a request
            # naming a pushed item's bytes (`CompileLabelVolume`) is a use.
            _emit_or_reclaim(self._emit, msg, self._store, self._plane.diagnose)
        except BaseException:
            self._forget(msg.id)
            raise
        return pending

    def _request(self, msg: "pb2.PluginMsg", timeout: float):
        pending = self._start(msg)
        try:
            return pending.result(timeout)
        except TimeoutError:
            raise HostError(
                0, f"host did not answer request {msg.id!r} within {timeout:.0f}s"
            ) from None
        finally:
            pending.abandon()

    def _copy_deposit(self, hm, ref) -> bytes:
        """Copy one deposit out of its segment, then dispose of the descriptor.

        There is no wait here and there cannot be one: §8.12 holds a descriptor
        whose capability has not landed *at the control plane*, unread, and
        delivers the reply only once the mapping exists — so a reply that
        reaches a waiter names bytes that are already mapped.  A wait would be
        waiting for something that has already happened.

        The ``ReleaseDeposit`` is the control plane's to send (§5.6): every
        disposition counts, the release is owed at zero declared uses, and the
        region a ``SegmentRef`` does not carry is resolved from our own table.
        """
        segment = self._store.segment(ref.segment_id)
        if segment is None:
            raise HostError(0, f"segment {ref.segment_id} is not installed")
        # §9.1: reading a deposit that has arrived is FINISHING, which a
        # retirement permits and waits for -- the copy holds a claim, so the
        # segment is not unmapped under it.
        segment._claimed(1)
        try:
            region = segment.resolve_deposit(ref.offset, ref.len)
            segment._guard_quarantine(region)
            ring = segment._deposits_unchecked(region)
            data = ring.take(ref.offset, ref.len, uses=ref.uses, held=True)
            _report_uses(self._plane, ring)
        except (ValueError, KeyError, _memory.Quarantined) as exc:
            # Still a disposition: the receiver is done with the bytes whatever
            # it did with them, so the loan is released before the raise.
            self._plane.dispose(hm)
            raise HostError(0, f"deposit {ref.offset}+{ref.len}: {exc}") from exc
        finally:
            segment._claimed(-1)
        self._plane.dispose(hm)
        return data

    def view_state(self, timeout: float = 30.0) -> HostViewState:
        """The active view/selection snapshot — see :class:`HostViewState`."""
        rid = f"q{next(self._seq)}"
        hm = self._request(pb2.PluginMsg(id=rid, get_view_state=pb2.GetViewState()), timeout)
        if hm.WhichOneof("msg") != "view_state":
            raise HostError(0, f"expected view_state, got {hm.WhichOneof('msg')!r}")
        vs = hm.view_state
        return HostViewState(
            t=vs.t,
            z=vs.z,
            c=vs.c,
            level=vs.level,
            shape_tczyx=tuple(vs.shape_tczyx),
            dtype=_dtype_to_np().get(vs.dtype),
            has_crop=vs.has_crop,
            crop_min_zyx=tuple(vs.crop_min_zyx) if vs.has_crop else None,
            crop_max_zyx=tuple(vs.crop_max_zyx) if vs.has_crop else None,
            mip=vs.mip,
            levels=tuple((lv.level, tuple(lv.shape_zyx)) for lv in vs.levels),
            dataset_uri=vs.dataset_uri,
            dataset_name=vs.dataset_name,
            open_generation=int(vs.open_generation),
            visible_channels=tuple(int(c) for c in vs.visible_channels),
            spacing_zyx=tuple(float(v) for v in vs.spacing_zyx) if vs.spacing_zyx else None,
            spacing_unit=vs.spacing_unit,
            spacing_provenance=_enum_name(
                pb2.SpacingProvenance, vs.spacing_provenance, "SPACING_PROVENANCE_"
            ),
            file_state_dir=_state_dir(vs.file_state_dir),
            filter=AppliedFilter.from_wire(vs),
            time_increment=vs.time_increment if vs.time_increment > 0 else None,
            time_unit=vs.time_unit,
            selected_region_id=vs.selected_region_id,
            dataset_series=vs.dataset_series if vs.HasField("dataset_series") else None,
            channels=tuple(ChannelName.from_wire(ch) for ch in vs.channels),
        )

    def request_status(self, subject_id: str, timeout: float = 2.0) -> "HostRequestStatus":
        """Is the host still holding request ``subject_id``?

        For a plugin that has been waiting a long time and cannot tell a
        person browsing a file picker from a host that has stopped draining.
        The short default timeout is the point: a liveness probe that waits
        30 s to answer "is anything happening" has answered too late, and a
        probe that times out is itself the signal.

        **It reports the host's bookkeeping and nothing more.**
        :attr:`~HostRequestStatus.pending` says the request was taken in and
        not answered -- never that it will finish, or when.  And `UNKNOWN` is
        not evidence the work failed: §9's *every timeout means unknown, never
        dead* governs the answer as much as its absence.
        """
        rid = f"q{next(self._seq)}"
        hm = self._request(
            pb2.PluginMsg(
                id=rid,
                request_status_query=pb2.RequestStatusQuery(subject_id=str(subject_id)),
            ),
            timeout,
        )
        if hm.WhichOneof("msg") != "request_status":
            raise HostError(0, f"expected request_status, got {hm.WhichOneof('msg')!r}")
        rs = hm.request_status
        return HostRequestStatus(
            subject_id=rs.subject_id,
            pending=rs.disposition == pb2.RequestStatus.PENDING,
            awaiting_user=bool(rs.awaiting_user),
        )

    def contribute_rois(
        self,
        rois,
        *,
        replace_all: bool = False,
        timeout: float = 30.0,
    ) -> None:
        """Contribute a batch of :class:`pb2.RoiContribution` messages to native
        Annotations.  ``contrib_id`` is stable within this plugin's namespace;
        re-emitting it updates that row.  With ``replace_all=True``, rows from
        this plugin that are absent from the batch are removed. Rejections
        raise :class:`HostError`."""
        rid = f"q{next(self._seq)}"
        hm = self._request(
            pb2.PluginMsg(
                id=rid,
                contribute_rois=pb2.ContributeRois(
                    rois=list(rois),
                    replace_all=bool(replace_all),
                ),
            ),
            timeout,
        )
        if hm.WhichOneof("msg") != "rois_contributed":
            raise HostError(
                0,
                f"expected rois_contributed, got {hm.WhichOneof('msg')!r}",
            )

    def get_rois(
        self,
        timeout: float = 30.0,
        *,
        pose_t_start: int | None = None,
        pose_t_end: int | None = None,
        region_ids=(),
    ) -> tuple:
        """Return the active dataset's native annotations as a tuple of
        ``pb2.RoiRecord`` messages.  Requires ``data_access``.

        With neither bound this is the shape it has always had: one ``Roi`` per
        region, posed at the frame on screen and its pose cleared.  With a
        window the REST form comes back instead, its dense pose rows
        clipped -- which is how a plugin reads back a pose it contributed.

        Every record of a posed region carries ``pose_domain_t_start``/``_t_end``
        either way: read without a window first to size one, and treat
        a windowed record as the whole pose only when its domain equals
        ``roi.t_start``/``t_end`` -- otherwise contributing it back truncates
        the region.  A window whose reply would pass the 32 MiB message
        ceiling is refused with a :class:`HostError` naming the size; ask for a
        narrower one.

        ``region_ids`` keeps only those regions -- what a
        :class:`RegionsChange` names; an id that no longer exists is simply
        absent.  Each record's ``revision`` is the one the change carried.
        """
        rid = f"q{next(self._seq)}"
        query = pb2.GetRois(region_ids=[str(r) for r in region_ids])
        if pose_t_start is not None:
            query.pose_t_start = int(pose_t_start)
        if pose_t_end is not None:
            query.pose_t_end = int(pose_t_end)
        hm = self._request(pb2.PluginMsg(id=rid, get_rois=query), timeout)
        if hm.WhichOneof("msg") != "rois":
            raise HostError(0, f"expected rois, got {hm.WhichOneof('msg')!r}")
        return tuple(hm.rois.rois)

    def extend_roi_poses(self, extensions, timeout: float = 30.0) -> tuple[int, int]:
        """Add or correct dense pose rows on regions this plugin contributed.

        ``extensions`` is a sequence of :class:`pb2.RoiPoseExtension`.  Returns
        ``(region_count, frame_count)``.  Requires ``contribute_rois``.

        The mechanism for a tracker that extends as it goes:
        ``contribute_rois`` takes the whole table, so re-sending it per
        contribution is quadratic in the frames tracked.  Overlap replaces; a
        GAP is refused, because a pose is dense by contract and a filled gap
        would read back as a measurement.  The components the extension carries
        must be exactly those the region already carries.
        """
        rid = f"q{next(self._seq)}"
        hm = self._request(
            pb2.PluginMsg(
                id=rid,
                extend_roi_poses=pb2.ExtendRoiPoses(extensions=list(extensions)),
            ),
            timeout,
        )
        if hm.WhichOneof("msg") != "roi_poses_extended":
            raise HostError(0, f"expected roi_poses_extended, got {hm.WhichOneof('msg')!r}")
        return (int(hm.roi_poses_extended.region_count), int(hm.roi_poses_extended.frame_count))

    def activate_tool(
        self,
        tool_id: str,
        *,
        deactivate: bool = False,
        timeout: float = 30.0,
    ) -> bool:
        """Activate or deactivate one tool declared by this plugin.

        The host rejects requests during an active gesture. No manifest
        permission is required because declared tools are already exposed
        through the host's Tools menu.
        """
        rid = f"q{next(self._seq)}"
        hm = self._request(
            pb2.PluginMsg(
                id=rid,
                activate_tool=pb2.ActivateTool(
                    tool_id=str(tool_id),
                    deactivate=bool(deactivate),
                ),
            ),
            timeout,
        )
        if hm.WhichOneof("msg") != "tool_activation_changed":
            raise HostError(
                0,
                f"expected tool_activation_changed, got {hm.WhichOneof('msg')!r}",
            )
        reply = hm.tool_activation_changed
        if reply.tool_id != tool_id:
            raise HostError(
                0,
                f"tool activation reply names {reply.tool_id!r}, expected {tool_id!r}",
            )
        return bool(reply.active)

    def request_asset(
        self,
        slot: str,
        *,
        force_reprompt: bool = False,
        timeout: float = PICKER_TIMEOUT_S,
    ) -> Path | None:
        """Ask the host to select and import one file for a persistent slot.

        A populated slot is reused across sessions without opening the picker.
        Set ``force_reprompt=True`` for an explicit "choose different file"
        action. A user cancellation returns ``None``; host/import failures
        raise :class:`HostError`. Requires ``data_access``.

        :meth:`request_asset_async` is the same request without the wait.
        The default ``timeout`` is :data:`PICKER_TIMEOUT_S`: the wait is on a
        person, so it is the ports' 30 minutes, not a request's 30 s.
        Waiting here from a command handler is allowed, but the command holds
        the session's one request slot while the person browses, so the host's
        other requests to this plugin -- reads of a dataset it serves, its next
        command -- wait too.  A plugin that serves those SHOULD use the async
        form and return promptly (``control_plane_protocol.md`` §8.9).
        """
        pending = self.request_asset_async(slot, force_reprompt=force_reprompt)
        try:
            return pending.result(timeout)
        except TimeoutError:
            raise HostError(
                0, f"host did not answer request {pending.request_id!r} within {timeout:.0f}s"
            ) from None
        finally:
            pending.abandon()

    def request_asset_async(self, slot: str, *, force_reprompt: bool = False) -> PendingRequest:
        """:meth:`request_asset`, returning at once with a
        :class:`PendingRequest` whose :meth:`~PendingRequest.result` is the
        imported path or ``None``.

        A picker waits on a person, for as long as they take; the handle lets
        a plugin keep that wait off its command worker, and lets another
        thread ask the host about it by ``request_id``."""
        rid = f"q{next(self._seq)}"
        return self._start(
            pb2.PluginMsg(
                id=rid,
                request_asset=pb2.RequestAsset(slot=str(slot), force_reprompt=bool(force_reprompt)),
            ),
            functools.partial(_asset_result, slot=slot),
        )

    def request_save_path(
        self,
        suggested_name: str = "",
        *,
        extension: str = "",
        title: str = "",
        timeout: float = PICKER_TIMEOUT_S,
    ) -> "Path | None":
        """Ask the host's save dialog for a path to write: folder
        browsing and the OS's overwrite prompt, which a plugin with no UI
        cannot show.  ``extension`` (``"csv"``) filters the dialog and is
        appended when the person leaves it off.  ``None`` when they cancel.
        The host creates nothing; write the file yourself.  No permission is
        needed, as for an exporter.

        :meth:`request_save_path_async` is the same request without the wait,
        for a person who may take longer than ``timeout`` to choose.  The
        default is :data:`PICKER_TIMEOUT_S`, as for :meth:`request_asset`."""
        pending = self.request_save_path_async(suggested_name, extension=extension, title=title)
        try:
            return pending.result(timeout)
        except TimeoutError:
            raise HostError(
                0, f"host did not answer request {pending.request_id!r} within {timeout:.0f}s"
            ) from None
        finally:
            pending.abandon()

    def request_save_path_async(
        self, suggested_name: str = "", *, extension: str = "", title: str = ""
    ) -> PendingRequest:
        """:meth:`request_save_path`, returning at once with a
        :class:`PendingRequest` whose result is the chosen path or ``None``."""
        rid = f"q{next(self._seq)}"
        return self._start(
            pb2.PluginMsg(
                id=rid,
                request_save_path=pb2.RequestSavePath(
                    suggested_name=str(suggested_name),
                    extension=str(extension),
                    title=str(title),
                ),
            ),
            _save_path_result,
        )

    def request_view(
        self,
        *,
        cause: int = 0,
        tool_cause: "tuple[int, int] | None" = None,
        decline: bool = False,
        t: "int | None" = None,
        z: "int | None" = None,
        select_region: "str | None" = None,
        frame_region: "str | None" = None,
        look_at=None,
        look_fit: float = 0.0,
        look_direction=None,
        orbit_point=None,
        timeout: float = 5.0,
    ) -> "ViewResult":
        """A ``ViewRequest`` (§8.15) under exactly one cause: a widget's
        ``cause`` or a FIRE's ``(tool, interaction)``.  Prefer
        ``ctx.move_view`` / ``ctx.decline_view``, which supply the cause."""
        if bool(cause) == (tool_cause is not None):
            raise ValueError("a view request names exactly one cause")
        req = pb2.ViewRequest(cause=int(cause), decline=bool(decline))
        if tool_cause is not None:
            req.tool_cause.tool, req.tool_cause.interaction = (int(v) for v in tool_cause)
        if t is not None:
            req.t = int(t)
        if z is not None:
            req.z = int(z)
        if select_region is not None:
            req.select_region = str(select_region)
        if frame_region is not None:
            req.frame_region = str(frame_region)
        if look_at is not None:
            req.look_at.target_zyx.extend(float(v) for v in look_at)
            req.look_at.fit_extent = float(look_fit)
            if look_direction is not None:
                req.look_at.direction_zyx.extend(float(v) for v in look_direction)
        if orbit_point is not None:
            req.orbit_point.zyx.extend(float(v) for v in orbit_point)
        rid = f"q{next(self._seq)}"
        hm = self._request(pb2.PluginMsg(id=rid, view_request=req), timeout)
        if hm.WhichOneof("msg") != "view_request_result":
            raise HostError(0, f"expected view_request_result, got {hm.WhichOneof('msg')!r}")
        r = hm.view_request_result
        return ViewResult(
            outcome=_enum_name(pb2.ViewOutcome, r.outcome, "VIEW_OUTCOME_"),
            reason=_enum_name(pb2.ViewRefusal, r.reason, "VIEW_REFUSAL_"),
            detail=r.detail,
        )

    def get_colormap(self, name: str, timeout: float = 30.0) -> "np.ndarray":
        """The ``(256, 4)`` u8 RGBA table vizi renders colormap ``name`` with
        -- what a ``colormap_picker`` names, built-in or the user's own
        -- so an image the plugin draws is coloured as vizi would colour it.
        An unknown name is a :class:`HostError` (``NOT_FOUND``)."""
        rid = f"q{next(self._seq)}"
        hm = self._request(
            pb2.PluginMsg(id=rid, get_colormap=pb2.GetColormap(name=str(name))), timeout
        )
        if hm.WhichOneof("msg") != "colormap_table":
            raise HostError(0, f"expected colormap_table, got {hm.WhichOneof('msg')!r}")
        rgba = hm.colormap_table.rgba
        if len(rgba) != 256 * 4:
            raise HostError(0, f"colormap table is {len(rgba)} bytes, not 1024")
        return np.frombuffer(rgba, dtype=np.uint8).reshape(256, 4)

    def read_mask_voxels(
        self,
        region_id: str,
        timeout: float = 30.0,
    ) -> "tuple[np.ndarray, tuple[int, int, int]]":
        """Read one mask annotation's bbox-local membership and ZYX origin.

        Membership is static across time: fetch each mask once, then fetch
        intensity volumes per timepoint with :meth:`read_volume` and reduce
        plugin-side. That keeps request count O(cells + timepoints), not
        O(cells × timepoints). Requires ``data_access``. The mask travels
        through the existing ``V`` data segment and is returned as a
        read-only boolean array.
        """
        rid = f"q{next(self._seq)}"
        hm = self._request(
            pb2.PluginMsg(
                id=rid,
                read_mask_voxels=pb2.ReadMaskVoxels(
                    region_id=str(region_id),
                ),
            ),
            timeout,
        )
        if hm.WhichOneof("msg") != "mask_voxels":
            raise HostError(
                0,
                f"expected mask_voxels, got {hm.WhichOneof('msg')!r}",
            )
        reply = hm.mask_voxels
        if reply.region_id != region_id:
            raise HostError(
                0,
                f"mask reply names region {reply.region_id!r}, expected {region_id!r}",
            )
        if len(reply.origin_zyx) != 3:
            raise HostError(0, "mask origin must contain 3 ZYX values")
        ref = reply.segment
        if ref.dtype != pb2.DTYPE_U8:
            raise HostError(0, f"mask segment has unsupported dtype {ref.dtype}")
        expected = int(np.prod(ref.shape_zyx))
        if len(ref.shape_zyx) != 3 or ref.len != expected:
            raise HostError(
                0,
                f"mask segment len {ref.len} != shape {tuple(ref.shape_zyx)} ({expected})",
            )
        data = self._copy_deposit(hm, ref)
        mask = np.frombuffer(data, dtype=np.uint8).reshape(tuple(ref.shape_zyx)).view(np.bool_)
        mask = HostDataArray(mask, reply.open_generation)
        return mask, tuple(reply.origin_zyx)

    def read_roi_inclusion(
        self,
        region_id: str,
        t: int,
        roi: tuple | None = None,
        timeout: float = 30.0,
    ) -> "tuple[np.ndarray, tuple[int, int, int]] | None":
        """Which voxels region ``region_id`` covers at frame ``t``, as
        the host computes it for its own measurement and drawing: the shape
        posed at ``t`` through its parent chain, rasterized by its inclusion
        mode, clipped to the dataset.  Any region :meth:`get_rois` lists; a
        mask answers its stored membership, the same at every ``t``.

        Returns ``(mask, origin_zyx)`` -- a read-only, bbox-local boolean
        :class:`InclusionMask` at level-0 voxel ``origin_zyx``, whose
        ``voxel_count`` is the host's count of the voxels it covers -- or
        ``None`` when the region
        does not exist at ``t``.  A region that exists and covers no voxel of
        the dataset is a ``(0, 0, 0)`` array.  Requires ``data_access``; a
        mode the host does not rasterize is a :class:`HostError`.

        ``roi`` is an optional level-0 ``((z0, y0, x0), (z1, y1, x1))`` box
        (min inclusive, max exclusive) to clip the answer to, as
        :meth:`read_volume`'s: the host builds only that part.
        """
        rid = f"q{next(self._seq)}"
        query = pb2.ReadRoiInclusion(region_id=str(region_id), t=int(t))
        if roi is not None:
            lo, hi = roi
            query.roi_min_zyx.extend(int(v) for v in lo)
            query.roi_max_zyx.extend(int(v) for v in hi)
        hm = self._request(pb2.PluginMsg(id=rid, read_roi_inclusion=query), timeout)
        if hm.WhichOneof("msg") != "roi_inclusion":
            raise HostError(0, f"expected roi_inclusion, got {hm.WhichOneof('msg')!r}")
        reply = hm.roi_inclusion
        if reply.region_id != region_id or reply.t != int(t):
            raise HostError(
                0,
                f"inclusion reply names {reply.region_id!r} at t={reply.t}, "
                f"expected {region_id!r} at t={t}",
            )
        if not reply.present:
            return None
        if not reply.HasField("segment"):
            if reply.voxel_count:
                raise HostError(0, f"inclusion reply counts {reply.voxel_count} voxels, sends none")
            empty = np.zeros((0, 0, 0), dtype=bool)
            return InclusionMask(empty, reply.open_generation, 0), (0, 0, 0)
        if len(reply.origin_zyx) != 3:
            raise HostError(0, "inclusion origin must contain 3 ZYX values")
        ref = reply.segment
        if ref.dtype != pb2.DTYPE_U8:
            raise HostError(0, f"inclusion segment has unsupported dtype {ref.dtype}")
        expected = int(np.prod(ref.shape_zyx))
        if len(ref.shape_zyx) != 3 or ref.len != expected:
            raise HostError(
                0,
                f"inclusion segment len {ref.len} != shape {tuple(ref.shape_zyx)} ({expected})",
            )
        data = self._copy_deposit(hm, ref)
        mask = np.frombuffer(data, dtype=np.uint8).reshape(tuple(ref.shape_zyx)).view(np.bool_)
        covered = int(np.count_nonzero(mask))
        if covered != reply.voxel_count:
            raise HostError(
                0, f"inclusion reply counts {reply.voxel_count} voxels, its mask covers {covered}"
            )
        return InclusionMask(mask, reply.open_generation, covered), tuple(reply.origin_zyx)

    def compile_label_volume(
        self,
        overlay_id: str,
        item,
        *,
        replace_all: bool = False,
        timeout: float = 30.0,
    ) -> int:
        """Compile a segment-backed label-volume item into native masks.

        ``item`` is the same descriptor passed to
        :meth:`OverlayHandle.set_items`; only that small descriptor is repeated
        on the request channel, while its label and LUT bytes stay in the
        overlay segment. Label 0 is background. The returned integer is the
        number of nonzero labels compiled.

        **This request is one more use of the item's loans** (§5.6), and the
        item must have been built declaring it -- ``uses=2`` for the ordinary
        push-then-compile.  An item built with the default and already pushed
        has been (or is about to be) released at the host, and this request
        then names bytes the arena is free to hand out again.  Nothing here
        can check that, because the count is the sender's alone.
        """
        rid = f"q{next(self._seq)}"
        hm = self._request(
            pb2.PluginMsg(
                id=rid,
                compile_label_volume=pb2.CompileLabelVolume(
                    overlay_id=str(overlay_id),
                    item=item,
                    replace_all=bool(replace_all),
                ),
            ),
            timeout,
        )
        if hm.WhichOneof("msg") != "label_volume_compiled":
            raise HostError(
                0,
                f"expected label_volume_compiled, got {hm.WhichOneof('msg')!r}",
            )
        return int(hm.label_volume_compiled.region_count)

    def read_volume(
        self,
        t: int,
        c: int,
        level: int = 0,
        roi: tuple | None = None,
        timeout: float = 30.0,
        *,
        raw: bool = False,
    ) -> "np.ndarray":
        """Read the open dataset's ``(Z, Y, X)`` volume at ``(t, c, level)``
        via the zero-copy data plane.  ``roi`` is an optional
        ``((z0, y0, x0), (z1, y1, x1))`` sub-box in the coords OF THE
        REQUESTED LEVEL (min inclusive, max exclusive) — the host slices
        before depositing.  ``raw`` asks for the file's own voxels even when
        the person shares a filter's output with plugins; the array's
        ``filter`` is then ``None``, and :attr:`ViewState.filter` still names
        what the views show.  Returns a read-only array; ``.copy()`` to
        mutate."""
        rid = f"q{next(self._seq)}"
        req = pb2.ReadDataVolume(t=t, c=c, level=level, raw=bool(raw))
        if roi is not None:
            rmin, rmax = roi
            req.roi_min_zyx.extend(int(v) for v in rmin)
            req.roi_max_zyx.extend(int(v) for v in rmax)
        hm = self._request(pb2.PluginMsg(id=rid, read_data_volume=req), timeout)
        if hm.WhichOneof("msg") != "data_volume":
            raise HostError(0, f"expected data_volume, got {hm.WhichOneof('msg')!r}")
        ref = hm.data_volume.segment
        np_dtype = _dtype_to_np().get(ref.dtype)
        if np_dtype is None:
            raise HostError(0, f"segment has unsupported dtype {ref.dtype}")
        expected = int(np.prod(ref.shape_zyx)) * np.dtype(np_dtype).itemsize
        if ref.len != expected:
            raise HostError(
                0, f"segment len {ref.len} != shape{tuple(ref.shape_zyx)}x{np_dtype} ({expected})"
            )
        data = self._copy_deposit(hm, ref)
        array = np.frombuffer(data, dtype=np_dtype).reshape(tuple(ref.shape_zyx))
        return HostDataArray(
            array, hm.data_volume.open_generation, AppliedFilter.from_wire(hm.data_volume)
        )


@dataclass
class ToolContext:
    """What a ``@tool`` handler receives alongside each event: overlay access
    for live feedback (and the ordinary handles for end-of-gesture pushes)."""

    _overlays: Callable[[str], OverlayHandle]
    _host: "HostClient" = None  # type: ignore[assignment]
    _control: "_control.ControlPlane | None" = None
    _panels: "Callable[[str], PanelHandle] | None" = None

    def move_view(self, ev, **targets) -> "ViewResult":
        """Move the person's view as the answer to a FIRE (§8.15),
        under the tool's ``view_targets``: the cause is ``ev``'s ``(tool,
        interaction)``.  A PRESS, DRAG or RELEASE is never a cause -- a view
        that moves during a drag fights the hand doing it.  Tool handlers run
        on the doorbell listener: call this from a worker."""
        return self._host.request_view(tool_cause=_fire_cause(ev), **targets)

    def decline_view(self, ev) -> "ViewResult":
        """Answer a FIRE's cause without moving anything."""
        return self._host.request_view(tool_cause=_fire_cause(ev), decline=True)

    def overlay(self, overlay_id: str) -> OverlayHandle:
        return self._overlays(overlay_id)

    def panel(self, panel_id: str) -> PanelHandle:
        """The patch handle for a manifest-declared panel id -- the same one
        a command gets -- so a tool can say *Lasso active* from
        ``activated``, key a refusal on the record it refused, or report a
        failed live publish, without a handle captured from whichever command
        happened to run first.  A patch is a queued
        push, so it is safe from the listener thread."""
        return self._panels(panel_id)

    @property
    def host(self) -> "HostClient":
        """Host data-access requests.  Tool handlers run ON the
        doorbell listener thread — never block it with a request; spawn a
        worker for fetch+segment gestures."""
        return self._host

    def pane(self, ev) -> "_control.PaneState | None":
        """The pane state an event was captured under -- geometry, spacing,
        levels, crop, the visible channels at index >= 15, the time mapping
        -- as this plugin acknowledged it (`control_plane_protocol.md` §8.10).
        Already installed, so no round trip and nothing to block on; ``None``
        only for an event under an epoch this plugin has moved past, which
        the gate makes rare and a cancel makes harmless.

        ``ev`` is the ``InputEvent`` (or a ``Cancellation``: its pane is the
        interaction's), or a bare ``view_id``."""
        if self._control is None:
            return None
        if isinstance(ev, int):
            return self._control.pane_state(ev)
        epoch = getattr(ev, "epoch", None)  # a Cancellation names the pane, not an epoch
        return self._control.pane_state(int(ev.view_id), None if epoch is None else int(epoch))

    def arrangement(self, ev):
        """The ``ToolBinding`` the host installed for a tool (§8.10): its
        assigned ``triggers`` and every ``action`` with the key it kept, so a
        tool can tell a user what engages it and what fires what.

        ``ev`` is an event (the binding of the handle it names) **or a tool's
        manifest id** (``ctx.arrangement("probe.point")``, the binding
        installed for that id -- the newest, should the host ever hold two).
        The id form is what ``activated`` needs: it runs with no event in
        hand, and the natural moment to say *P toggles, U undoes* is then,
        not on the first record.  ``None`` for a handle
        or an id this plugin has no arrangement for."""
        if self._control is None:
            return None
        if isinstance(ev, str):
            for handle in sorted(self._control.tools, reverse=True):
                binding = self._control.tools[handle]
                if str(binding.tool_id) == ev:
                    return binding
            return None
        return self._control.tools.get(int(getattr(ev, "tool", 0)))

    def action(self, ev) -> "str | None":
        """The manifest id of the action a ``FIRE`` names (`E` §1 ``action``),
        or ``None`` for the primary gesture -- a ``fire`` tool's click, or any
        press / drag / release.  An index the arrangement does not carry is
        answered ``None`` too: the host would be naming an action this manifest
        does not declare, which `E` §3 treats as forward compatibility, not a
        fault."""
        index = int(getattr(ev, "action", 0) or 0)
        if not index:
            return None
        binding = self.arrangement(ev)
        if binding is None:
            return None
        for entry in binding.actions:
            if int(entry.action) == index:
                return str(entry.action_id)
        return None


@dataclass
class PluginContext:
    """The session's handles, as a ``@reader`` or ``@exporter`` class receives
    them: **construct the class with one parameter and it is this** --
    ``def __init__(self, ctx)`` -- and a class with none is constructed bare,
    as every class was before.

    A format plugin's panel is updated from ``open()``, ``finish()`` and
    ``abort()`` -- a crate opened by ``OpenReader`` moves the panel's
    counters, an export's outcome lands in ``finish()`` -- and none of those
    run inside a command, so a handle captured from whichever command or
    ``@panel_opened`` ran first was the only way to one, and a file opened
    before the panel was ever shown reached it only at the panel's next
    open.  ``PanelPatch`` may be sent at any time (the IDL), so the handle
    is here from construction.  An exporter that reads its source has
    ``host`` for the same reason."""

    _panels: Callable[[str], PanelHandle]
    _overlays: Callable[[str], OverlayHandle] = None  # type: ignore[assignment]
    _host: "HostClient" = None  # type: ignore[assignment]
    #: A READER's folder for the file it is being constructed to open,
    #: as :attr:`HostViewState.file_state_dir`: named, not created, ``None``
    #: when the URI is not a local path.  ``None`` for an exporter, whose
    #: :class:`ExportDescriptor` carries its source's.
    file_state_dir: "Path | None" = None
    _control: "_control.ControlPlane | None" = None
    _work: "_work.WorkRegistry | None" = None

    def start_task(self, title: str, *, cancellable: bool = True) -> TaskHandle:
        """Begin background work the person can see -- and, when
        ``cancellable``, stop -- that blocks no request (§8.16): what a
        command that returns promptly goes on doing on its own thread.
        Report on the handle; :meth:`TaskHandle.done` ends it."""
        if self._work is None:
            raise RuntimeError("no session: a task needs a running plugin session")
        return self._work.start_task(title, cancellable)

    def panel(self, panel_id: str) -> PanelHandle:
        return self._panels(panel_id)

    def pane(self, view_id: int) -> "_control.PaneState | None":
        """The pane state this plugin last acknowledged for ``view_id`` --
        geometry, spacing, levels, crop, the visible channels at index >= 15,
        the time mapping -- as :meth:`ToolContext.pane` answers it.  A session
        with no tool holds one only when its manifest subscribes to
        ``"epochs"`` (`manifest_schema.md` §5.1); otherwise ``None``.

        It does NOT carry the current ``t``: the epoch excludes it by design,
        so the frame on screen comes from ``host.view_state()``
        (`control_plane_protocol.md` §8.7)."""
        if self._control is None:
            return None
        return self._control.pane_state(int(view_id))

    def panes(self) -> "tuple[int, ...]":
        """The view ids :meth:`pane` can answer for, ascending."""
        if self._control is None:
            return ()
        return self._control.pane_ids()

    def overlay(self, overlay_id: str) -> OverlayHandle:
        """The push handle for a manifest-declared overlay id (Phase 3)."""
        return self._overlays(overlay_id)

    @property
    def host(self) -> "HostClient":
        """Host data-access requests: ``view_state()`` /
        ``read_volume(t, c, level, roi)``.  Command, reader and exporter
        handlers run on SDK workers, so blocking here is fine — but a SLOW
        command should still return fast and continue on its own thread (the
        host's invoke timeout marks a wedged handle suspect).  A reader
        serving a dataset must not ask for that dataset
        (`reader_writer_contract.md` §1.9): the host refuses it rather than
        deadlock."""
        return self._host


@dataclass
class CommandContext(PluginContext):
    """What a ``@command`` handler receives: the session's handles and the
    invocation's ``args``.

    ``cause`` is set-view's token (§8.15): non-zero when the person's
    interaction with a ``moves_view`` widget fired this command, and the only
    thing :meth:`move_view` may move the view on behalf of."""

    args: dict = dataclasses.field(default_factory=dict)
    cause: int = 0
    _request: "_work._Work | None" = None

    def progress(self, fraction: "float | None" = None, text: str = "") -> None:
        """Report how far along this command is (§8.16): ``fraction`` in
        [0, 1], or ``None`` for no measure, and one line of ``text``.  Safe from
        any thread the handler hands the context to."""
        if self._request is not None:
            self._request.progress(fraction, text)

    @property
    # The module-level `cancelled` this "redefines" is the SDK helper imported
    # above for the package's lazy re-exports; this is the context's own.
    def cancelled(self) -> bool:  # noqa: F811
        """Whether the person asked this command to stop.  Check it between
        steps; return, or raise :class:`Cancelled`, to stop."""
        return self._request is not None and self._request.cancelled

    def move_view(self, **targets) -> "ViewResult":
        """Move the person's view as the answer to this interaction (§8.15):
        any of ``t``, ``z``, ``select_region``, ``frame_region``, ``look_at``
        (a ZYX voxel, with optional ``look_fit`` and ``look_direction``) and
        ``orbit_point``.  All or nothing; the result says which, and why not."""
        return self._host.request_view(cause=self._require_cause(), **targets)

    def decline_view(self) -> "ViewResult":
        """Answer this interaction's cause without moving anything -- a click
        on an empty part of a heatmap -- so the widget stops showing pending."""
        return self._host.request_view(cause=self._require_cause(), decline=True)

    def _require_cause(self) -> int:
        if not self.cause:
            raise ValueError(
                "this invocation carries no cause: only a moves_view widget's may move the view"
            )
        return self.cause


def _hook_takes_a_second(fn) -> bool:
    """Whether a bound hook's signature admits a second positional argument
    after the context -- ``activated(self, ctx, binding)`` does,
    ``activated(self, ctx)`` does not, ``*args`` does."""
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if any(p.kind is p.VAR_POSITIONAL for p in params):
        return True
    return len(positional) >= 2


def _plan_key(request) -> bytes:
    """A ``PlanStage``'s identity: stage, input and params.  Deterministic
    serialization sorts the map, so an arm restating them finds the plan."""
    return request.SerializeToString(deterministic=True)


def _arm_plan_key(arm) -> bytes:
    return _plan_key(pb2.PlanStage(stage_id=arm.stage_id, input=arm.input, params=arm.params))


def _construct(cls: type, ctx: PluginContext):
    """A reader or exporter class, constructed with the context when its
    signature asks for one and bare when it does not -- so a class written
    before `PluginContext` existed keeps working unchanged."""
    try:
        wants = bool(inspect.signature(cls).parameters)
    except (TypeError, ValueError):
        wants = False  # a builtin or C-implemented callable: construct bare
    return cls(ctx) if wants else cls()


#: Every host→plugin arm that ANSWERS a plugin request, routed to the
#: blocked requester by id.  It is a named set rather than a chain of branches
#: because `control_plane_implementation_details.md` §3.1 asks for dispatch by
#: kind with nothing falling through silently -- and this list is where that
#: went wrong: `asset_result` was missing, so every `request_asset` waited out
#: its 30 s timeout and raised, with the reply already in hand and discarded.
#: :attr:`_Servicer.unhandled_kinds` is the counter that makes the next
#: omission visible instead of silent.
_REPLY_KINDS = frozenset(
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
        "request_status",
        "host_error",
    }
)

#: `control_plane_protocol.md` §7: the drain interval both ends publish, and
#: what `doorbell_D_notification.md` §5's advisory exemption depends on.  Here
#: it is only a backstop -- `_emit` notifies the sender directly.
_DRAIN_INTERVAL_S = 0.04

_SHUTDOWN = object()


class _WakingOutbox(_control.Outbox):
    """The session's outbox, which wakes the sender on every admission.

    §7's published drain interval is a **backstop** and has to stay one.  The
    control plane puts acknowledgements straight onto this queue -- a
    ``GrantAck``, an ``EpochAck``, a ``ReleaseDeposit``, a
    ``ContractViolation`` -- without passing through the servicer, so a
    generator woken only by ``_emit`` holds every one of them until the plugin
    happens to send something of its own: the first `GrantAck` would arrive at
    teardown, behind the panel patch that finally woke the loop.
    """

    def __init__(self, notify: Callable[[], None], **kwargs) -> None:
        super().__init__(**kwargs)
        self._notify = notify

    def send(self, msg) -> None:
        super().send(msg)
        self._notify()


# Duck-types the generated PluginServicer (add_PluginServicer_to_server binds
# ``servicer.Session`` by name) — inheriting would force the grpc import at
# module scope, which must stay deferred for the host.
class _Servicer:
    def __init__(
        self,
        readers: "type | dict[str, type] | None",
        plugin_id: str,
        purposes=None,
        *,
        max_segment_bytes: int | None = None,
        version: str | None = None,
        build: str | None = None,
    ) -> None:
        #: §3.1: composed once, at start-up, and checked by the host's own rule
        #: -- a malformed one fails here with its reason rather than at the
        #: handshake with the host's.
        self._plugin_identity = compose_identity(plugin_id, version, build)
        #: §8.2: the reader contributions this session serves, by manifest id.
        #: A bare class is accepted for the caller that has exactly one and no
        #: id to give it, which is every test written before `reader_id`.
        if readers is None:
            self._readers: dict[str, type] = {}
        elif isinstance(readers, dict):
            self._readers = dict(readers)
        else:
            self._readers = {"": readers}
        self._plugin_id = plugin_id
        self._impl = None
        self._reader_shape: tuple[int, ...] = ()
        #: What `ReaderOpened` announced; every deposit is checked against it.
        self._reader_dtype: "np.dtype | None" = None
        #: §1.2's handshake: the open reader's handle, its URI, and a counter
        #: that never repeats a handle within the session -- a request naming
        #: an earlier one is a request for a reader this session replaced.
        self._reader_handle = ""
        self._reader_uri = ""
        self._reader_serial = 0
        #: The instance whose ``open`` answered a :class:`SeriesChoice`,
        #: as ``((uri, reader_id), impl, timer)``, until the host names a
        #: series, opens something else, or :data:`SERIES_CHOICE_HOLD_S` passes.
        self._pending_reader = None
        self._pending_token = None
        # `control_plane_protocol.md` §5.4: coalescing and one-shot may not
        # share a queue, and one FIFO carrying both is unsound for the mixture
        # even where it is sound for either member.  So the outbound path is
        # the control plane's `Outbox` -- acknowledgements and replies on a
        # never-drop queue, panel and overlay pushes coalesced per key -- and
        # this generator is what drains it.
        self._sendable = threading.Condition()
        self._pending = False
        self._closing = False
        #: How long to wait for the host's `Shutdown` after the doorbell is
        #: lost before ending the session anyway (see `_await_host_shutdown`).
        self._doorbell_backstop_s = DOORBELL_LOSS_BACKSTOP_S
        self._doorbell_backstop: threading.Timer | None = None
        #: Set when the session is over, however it ended -- `Shutdown`'s
        #: window closed, a fault, the stream gone because the host is gone.
        #: `serve()` waits on it and returns: a plugin process serves ONE
        #: session and exits with it (`control_plane_protocol.md` §3.3).
        self.session_ended = threading.Event()
        self.outbox = _WakingOutbox(self._wake_sender)
        #: `shared_memory_protocol.md` §3: `purpose` is host-defined and a
        #: receiver's recognised set is **the set it implements** -- there is
        #: nothing published and nothing to configure.  `None` takes §3's five,
        #: which is what this SDK implements; a narrower set models a receiver
        #: that implements less.
        self._memory = _memory.SegmentStore(purposes, max_segment_bytes=max_segment_bytes)
        # §9.1: a retirement held back by a write or read in
        # flight is settled when the last one ends, not at the next message.
        # On a timer thread: this fires from inside a push, under its locks.
        self._memory.on_drained = self._settle_soon
        #: §7 and §11: what the plugin can see the host get wrong, under a
        #: bound this end enforces itself.  The control plane feeds it and
        #: `report_due` drains it onto the outbox.
        self._reporter = _violations.ViolationReporter()
        #: The 1.0 binding: it classifies every inbound message, applies the
        #: arrangements, answers the grant path and routes replies.  Its
        #: `transport` is attached by `attach_doorbell` once the doorbell is
        #: open -- §5.5's install-before-ack calls go through it.
        self._control = _control.ControlPlane(
            memory=self._memory,
            reporter=self._reporter,
            outbox=self.outbox,
            deliver_reply=self._route_reply,
            on_deferred_request=self._deferred_request,
            on_tool_binding=self._on_tool_binding,
            on_chunks_wanted=self._on_chunks_wanted,
            on_subscribed=self._on_subscribed,
            on_release_loan=self._on_release_loan,
            on_stage_arm=self._on_stage_arm,
            on_installed=self._on_segment_installed,
        )
        self._control.declared_stages = frozenset(_STAGES)
        self._control.stage_arm_check = self._stage_arm_problem
        #: The v2 doorbell, once `serve()` has opened it.
        self._doorbell = None
        #: `processing_stages.md` §6's worker, started by the first arm.
        self.stages = _stages.StageRunner(
            memory=self._memory,
            control=self._control,
            stage_for=self._stage_instance,
            needs_for=self._stage_needs,
            notify=self._stage_handed_on,
        )
        self._stage_instances: dict[str, object] = {}
        self._stage_lock = threading.Lock()
        #: A plan's key (:func:`_plan_key`) -> the input channels it needs, so
        #: a missing one is refused before ``process`` runs (§7).
        self._stage_plans: dict[bytes, frozenset[int]] = {}
        #: ``(pipeline, position)`` -> ``(revision, needs)``, taken when that
        #: revision installed: §4 holds a stage to its arm, and the plan table
        #: moves whenever another position plans the same input.
        self._arm_needs: dict[tuple[int, int], tuple[int, "frozenset[int] | None"]] = {}
        #: §11a's unconditional re-check schedule, and the reason it is one
        #: object rather than one per region: `D` is advisory, so a plugin that
        #: polled only when hinted would stall on the first dropped hint --
        #: and the bound has to hold whether or not any hint ever arrives.
        #: Anything that consumes a host-produced Pulsed region registers with
        #: `reads.consume(...)`; a session with no such region never starts the
        #: thread, and `on_notification` still only ever brings a sweep FORWARD.
        self.reads = _memory.ReadSchedule()
        self._panel_handles: dict[str, PanelHandle] = {}
        self._exports: dict[str, object] = {}  # export handle -> exporter instance
        self._export_seq = 0
        self._overlay_handles: dict[str, OverlayHandle] = {}
        # One handle per overlay id, whichever thread asks first: a tool
        # handler on the doorbell listener and a command on the worker can
        # reach `_overlay` together, and two handles for one id would be two
        # revision counters -- the host's monotonic arbitration then drops one
        # of them for good.
        self._overlay_handles_lock = threading.Lock()
        #: §8.7's subscribed pushes: one newest-wins worker per kind.
        self._subscribed_workers: dict[str, _NewestWorker] = {}
        #: §8.8: drags whose commit or end has arrived -- no more proposals for
        #: them -- and the one worker computing the newest drag.
        self._shape_lock = threading.Lock()
        self._shape_closed: set[int] = set()
        self._shape_worker: "_NewestWorker | None" = None
        self._subscribed_lock = threading.Lock()
        #: Wakes `_pump` when a deferred violation report falls due.
        self._report_timer: "threading.Timer | None" = None
        self._report_timer_lock = threading.Lock()
        #: §8.15: the running Follows, by id, each driven on a thread of its own.
        self._follows: dict[int, Follow] = {}
        self._follows_lock = threading.Lock()
        self._tool_instances: dict[str, object] = {}
        # Windows TCP-localhost: when the host passed a session token
        # via the env, the Hello must repeat it — an imposter host that
        # connected to our localhost port gets its session dropped.
        self._expected_token: str = ""
        # Plugin-initiated host requests, answered by one-way HostMsgs
        # the dispatch loop routes by id.
        self._control.plugin_identity = self._plugin_identity
        self._host_client = HostClient(self._emit, self._memory, self._control)
        # FLEX-4 resolution: command handlers run on ONE worker — still
        # serialized (the documented contract), but OFF the dispatch loop, so
        # the loop stays free to route the data-access reply a blocked
        # handler is waiting on.
        self._invoke_pool = futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="vizi-sdk-command"
        )
        # §11a: the shutdown hook runs off the dispatch path and **on a
        # dedicated worker rather than the command pool** -- a command already
        # running would otherwise hold the window the hook was given.
        self._shutdown_pool = futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="vizi-sdk-shutdown"
        )
        #: Why the handshake failed, if it did (§3.1).
        self.handshake_error: str | None = None
        # `control_plane_protocol.md` §6 and
        # `control_plane_implementation_details.md` §3.1a: NEITHER END MAY RUN
        # WORK ON THE DISPATCH PATH, and this loop used to run seven handlers
        # inline — the three reads and the four export arms.  §9 gives an
        # export chunk 120 s, and anything queued behind one waited that long:
        # a GrantAck, an EpochAck, a DoorbellShutdown.  §9's acknowledgement
        # deadlines are *derived against* this rule, so an implementation that
        # dispatches inline is what makes them underivable.
        #
        # ONE WORKER PER CLASS, not a pool: within a class the order is the
        # stream's and must stay that way — export chunks are applied in order
        # (`@exporter` promises it), and a read must not overtake the
        # `open_reader` that created its handle.  Across classes they are
        # independent, so a 120 s export chunk no longer delays a read, which
        # is the property the split buys.
        self._reader_pool = futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="vizi-sdk-reader"
        )
        self._export_pool = futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="vizi-sdk-export"
        )
        # §8.8: a person is waiting on every `shape_commit`, so it is answered
        # on a worker of its own -- never behind a running command, nor behind
        # a slow proposal.  One worker keeps a commit and
        # its `ShapeEditEnded` in stream order.
        self._shape_commit_pool = futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="vizi-sdk-shape-commit"
        )
        self._inflight: set = set()
        #: Requests being served and background tasks, for progress and
        #: `CancelWork` (§8.16).
        self._work = _work.WorkRegistry(lambda msg: self._emit(msg))
        self._inflight_lock = threading.Lock()
        #: The host's ``Shutdown``, once it has arrived (§3.3).
        self.shutdown_notice: ShutdownNotice | None = None

    # -- the doorbell ---------------------------------------------------------

    def attach_doorbell(self, transport) -> None:
        """Adopt the open doorbell, before it is started.

        The control plane calls into it for §5.5's install-before-ack
        lifecycle inputs -- an epoch acknowledged, a tool activated or retired
        -- so the two are wired together before either sees a record.
        """
        self._doorbell = transport
        self._control.transport = transport

    def on_doorbell(self, outcome) -> None:
        """One outcome from the transport's listener thread.

        Everything below is a *supervisor* decision the doorbell deliberately
        does not make: which acknowledgement a grant deserves, which reports
        reach the control plane, and what a terminal event costs the session.
        """
        try:
            if isinstance(outcome, _doorbell.Frame):
                self._on_frame(outcome)
            elif isinstance(outcome, _doorbell.Cancellation):
                self._on_cancellation(outcome)
            elif isinstance(outcome, _doorbell.Fault):
                self.on_doorbell_fault(outcome)
            elif isinstance(outcome, _doorbell.Teardown):
                self._on_doorbell_teardown(outcome)
        except Exception as exc:  # noqa: BLE001 — see below
            # The transport already refuses to die of a raising consumer, but
            # it can only count that: `doorbell_protocol.md` §12 wants every
            # failure on a surface a developer can find, and a counter inside
            # the transport does not name which outcome or why.  The session
            # continues either way -- a supervisor bug is not a framing fault.
            self._control.diagnose(f"doorbell outcome {type(outcome).__name__}: {exc!r}")
        finally:
            # §2's bounded re-reporting only advances when someone asks, and
            # this thread is the one that produced the occurrences.  Settling
            # a retirement is here for the same reason: a Pulsed claim this
            # thread's consumer released is exactly what §9.1 was waiting for.
            self._pump()

    def _on_frame(self, frame) -> None:
        record = frame.record
        if frame.violation is not None:
            if frame.grant_failure:
                # `doorbell_M_memory.md` §7: everything but `zero_id` is a
                # GRANT failure and rides the acknowledgement, not a
                # `ContractViolation`.  Recording it against the segment id is
                # what stops §7's query table sending the host back around the
                # same rejected grant.
                self._control.reject_capability(
                    record.segment_id, frame.violation, record=frame.raw
                )
            else:
                # `zero_id` is the one report that goes out once per affected
                # grant rather than aggregated: a count cannot name WHICH
                # outstanding grant the host should close out (§2).
                self._control.emit_violation(
                    self._reporter.report(
                        frame.violation,
                        f"{frame.violation} on a {chr(frame.header.op) if frame.header else '?'} "
                        f"record",
                        record=frame.raw,
                        exempt=frame.violation == "zero_id",
                    )
                )
            return
        if frame.disposition is not _doorbell.Disposition.DECODED:
            return  # ignored, unknown op, or a frame the terminal already covers
        if isinstance(record, _doorbell.MemoryGrant):
            # §9a step 2: ownership is taken before anything can fail, and
            # `SegmentStore.capability` closes it on every path out.  Claiming
            # INSIDE this callback is the contract -- the transport closes an
            # unclaimed descriptor the moment this returns.
            fd = frame.descriptor.claim() if frame.descriptor is not None else None
            self._control.capability(
                record.segment_id,
                record.size,
                record.perms,
                fd=fd,
                name=record.name,
                record=frame.raw,
            )
        elif isinstance(record, _doorbell.InputEvent):
            self.on_input_event(record)
        elif isinstance(record, _doorbell.Notification):
            self.on_notification(record, frame.raw)

    def _on_cancellation(self, cancellation) -> None:
        """An interaction ended with no terminal of its own (``E`` §6).

        A tool that opened a gesture is told it is over, because otherwise the
        gesture's state is held forever by a plugin that never sees a release.
        """
        binding = self._control.tools.get(cancellation.tool)
        if binding is None:
            return
        self._run_tool(binding.tool_id, "cancel", cancellation)

    def on_doorbell_fault(self, fault) -> None:
        """§8.13: report it on the control plane WHILE THAT IS STILL USABLE,
        exactly once, then wait for the host to end the session.

        The host answers with `DoorbellShutdown`, lets its window run, and
        sends `Shutdown{session_fault}`; closing the stream here would race
        the report just queued and end the session under a reason the host
        could not see."""
        self._control.doorbell_fault(
            fault,
            plugin_id=self._plugin_id,
            sdk_version=_sdk_version(),  # the loaded source's, as the identity string's
            peer_protocol_major=PROTOCOL_MAJOR,
            peer_protocol_minor=PROTOCOL_MINOR,
            stream_healthy_at_detection=not self._closing,
            process_healthy_at_detection=True,
        )
        self._await_host_shutdown()

    def _await_host_shutdown(self) -> None:
        """The doorbell is gone; the host's `Shutdown` is what ends the session.

        A backstop, not a policy: a host that never sends it -- one that died
        with a live stream, or predates the rule -- would otherwise leave this
        process holding its segments forever.  Sized past the host's doorbell
        grace plus its shutdown window, so a conforming host always wins.
        """
        if self._doorbell_backstop is not None or self._closing:
            return
        timer = threading.Timer(self._doorbell_backstop_s, self._backstop_close)
        timer.daemon = True
        self._doorbell_backstop = timer
        timer.start()

    def _backstop_close(self) -> None:
        if self._closing:
            return
        self._control.diagnose(
            f"the doorbell closed and the host sent no Shutdown within "
            f"{self._doorbell_backstop_s:g} s; ending the session"
        )
        self._close_stream()

    def close(self) -> None:
        """Release everything the session held, in the order §9a step 15 wants.

        Process exit is the release (§9a step 18), so nothing here is owed to
        the host -- but a `serve()` that returns without exiting still has to
        unmap what it mapped, and the reports the rate deferred are worth one
        last pass while the stream is arguably still there.
        """
        if self._doorbell_backstop is not None:
            self._doorbell_backstop.cancel()
        # A stream that ended without `Shutdown` -- the host gone, a fault --
        # skipped `_finish_shutdown`; the reader and the open exports are
        # released here instead, before the memory they may deposit into.
        self._release_session_files()
        with self._overlay_handles_lock:
            handles = list(self._overlay_handles.values())
        for handle in handles:
            handle._stop_chunk_server()
        with self._subscribed_lock:
            workers = list(self._subscribed_workers.values())
        for worker in workers:
            worker.stop()
        with self._follows_lock:
            follows, self._follows = list(self._follows.values()), {}
        for running in follows:
            running._ended_by_host("SESSION_ENDED", "the session ended")
        self.stages.stop()
        try:
            self._control.report_remaining()
        except _control.SessionFault:
            # The queue is full at teardown, which is not a session to fault:
            # the stream this would have gone out on is already ending.
            pass
        self._memory.close()

    def _release_session_files(self) -> None:
        self._take_pending(None)
        exc = self._release_reader()
        if exc is not None:
            self._control.diagnose(f"the reader's close at shutdown raised: {exc!r}")
        self._abandon_exports()

    def _on_doorbell_shutdown(self, notice) -> None:
        """§8.13: the host has closed the doorbell; stop ours and say what we saw.

        **One channel's end, not the session's.**  `Shutdown` is the session's
        and this is the doorbell's (`control_plane_protocol.md` §3.3): a
        plugin contributing a panel, a reader or an exporter needs no doorbell
        at all, so the stream stays open and what stops is tool input.  The
        host stopped emitting at its send, so nothing more will arrive on the
        doorbell; ``stop()`` cancels every open gesture (each tool hears
        ``cancel``), latches the terminal and releases the socket on this
        thread.

        **This notice is the stop signal, not a courtesy.**  The host releases
        its socket only when the window closes -- on our `DoorbellFinalReport`
        or at ``deadline_ms`` -- and on macOS a datagram peer's close is not
        observable on the doorbell at all, so a listener waiting for the
        socket to tell it would wait forever there.  The final report is the
        one-shot diagnostic the window exists for, and sending it promptly is
        also what lets the host close the window early.  ``deadline_ms`` is
        measured from the host's send and is an upper bound; it is not waited
        on here because there is nothing to finish.

        **Consume this promptly, because the other order costs the session.**
        If the host's release at the deadline lands BEFORE this notice is read
        -- a dispatch loop two seconds behind -- the transport sees
        ``peer_closed`` and :meth:`_on_doorbell_teardown` ends the session,
        which is `doorbell_protocol.md` §8's reading; the `1.0` host is written
        for either outcome, so neither is wrong,
        but only this path keeps the panels.
        """
        reason = _enum_name(pb2.DoorbellShutdownReason, notice.reason)
        failure = (
            _enum_name(pb2.DoorbellFailureClass, notice.failure_class)
            if notice.HasField("failure_class")
            else None
        )
        self._control.diagnose(
            f"doorbell shutdown: {reason}"
            + (f" ({failure})" if failure else "")
            + f", {notice.deadline_ms} ms: {notice.detail}"
        )
        counters = {}
        if self._doorbell is not None:
            counters = dict(self._doorbell.counters)
            self._doorbell.stop()
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counters.items())) or "no counters"
        try:
            self._control.doorbell_final_report(
                f"listener stopped on {reason}"
                + (f" ({failure})" if failure else "")
                + f"; {summary}"
            )
        except _control.SessionFault as exc:
            # The window's report is best effort: a queue at capacity is a
            # session whose stream is not draining, which is its own fault
            # and not one to raise from inside another channel's teardown.
            self._control.diagnose(f"the final report was refused: {exc}")

    def _on_doorbell_teardown(self, teardown) -> None:
        """The doorbell ended without a fault.  Only the peer's own close
        matters here -- a local ``stop()`` is this end tearing down and already
        has a path -- and it ends the session the way a fault does: the host's
        `Shutdown` follows (`doorbell_protocol.md` §10), and a host that has
        gone altogether ends the stream, which is its own ending."""
        if teardown.reason in ("peer_closed", "peer_gone"):
            self._control.diagnose(f"doorbell teardown: {teardown.reason} {teardown.detail}")
            self._await_host_shutdown()

    # -- outbound plumbing ---------------------------------------------------

    def _check_hello(self, hm) -> "str | None":
        """§3.1: **both ends check**, and the match is on the MAJOR.

        Takes the whole ``HostMsg`` because the host's identity string rides
        its ``id``, not the ``Hello`` -- and that is checked for FORM here,
        after the version and before anything is trusted: the prefix, a
        non-empty tail, the byte bound, no control character.  Never matched,
        never parsed, never compared to anything (§3.1); a first message that
        fails it fails the session, naming both strings.  The classifier
        routes ``hello`` by kind before looking at the id, and that stays --
        the form check is this method's, not the classifier's.

        The host checks the plugin's `Ready` (`plugin_transport.rs`); this end
        did not, and `control_plane_implementation_details.md` §2 lists that as
        a defect rather than an asymmetry to live with.  It is load-bearing at
        1.0: while the host catches the mismatch in both skew directions the
        asymmetry costs nothing, but a plugin that must reject a 0.x host has
        no check to reject it with, and the plugin sees a stream close — which
        §3.3 classifies as an ordinary ending.

        **A differing MINOR is not a mismatch** from 1.0 on (§3.2): a minor
        bump is backward-compatible, an older peer skips field numbers it does
        not know and refuses kinds it does not implement, and that is §8's
        ordinary path rather than a session failure.
        """
        hello = hm.hello
        if not _doorbell.compatible(hello.protocol_major, hello.protocol_minor):
            return (
                f"vizi plugin protocol mismatch: host speaks "
                f"{hello.protocol_major}.{hello.protocol_minor}, this SDK speaks "
                f"{PROTOCOL_MAJOR}.{PROTOCOL_MINOR}; failing the session (§3.1)"
            )
        problem = check_identity(hm.id, HELLO_PREFIX)
        if problem is not None:
            return (
                f"vizi host identity {problem}: Hello.id {_shown(hm.id)}; expected "
                f"{HELLO_PREFIX!r} + version string (this plugin is "
                f"{self._plugin_identity!r}); failing the session (§3.1)"
            )
        self._control.host_identity = hm.id
        self._control.host_protocol = (int(hello.protocol_major), int(hello.protocol_minor))
        if self._expected_token and hello.session_token != self._expected_token:
            # An imposter host that connected to our localhost port.
            return "vizi session token mismatch; failing the session (§2.2)"
        if self._doorbell is not None:
            # §3.1a: no record may be sent before the handshake settles the
            # contract -- the version is what selects the framing, so a send
            # beforehand would be guessing which format the peer reads.  On a
            # token-authenticated transport this also verifies the echo (§3.2
            # step 8), and a mismatch there fails the session rather than the
            # launch, because the doorbell is already open by then.
            try:
                self._doorbell.handshake_complete(
                    hello.protocol_major,
                    hello.protocol_minor,
                    hello.session_token if self._expected_token else None,
                )
            except _doorbell.DoorbellSetupError as exc:
                return f"vizi doorbell handshake failed: {exc}"
        return None

    def _deferred_request(self, hm) -> None:
        """A request the plane held until its ride-along's mapping landed."""
        self._dispatch_request(hm.WhichOneof("msg"), hm)

    def _route_reply(self, hm) -> bool:
        """Whether a blocked requester took this reply (§4).

        A ``False`` here is what puts §4's resource rule into play: the control
        plane releases what the reply named and diagnoses the unmatched id,
        rather than dropping a message that owns a live ring reservation.
        """
        return self._host_client.route_reply(hm)

    @property
    def violations_received(self) -> list:
        """`ContractViolation`s the HOST reported about this plugin's traffic --
        symmetric since 1.0, and report-and-continue at both ends.  The first
        `MAX_VIOLATIONS_RETAINED` only; :attr:`violations_received_counts`
        totals every one per site."""
        return self._control.violations_received

    @property
    def violations_received_counts(self) -> dict:
        """Occurrences the host reported about us, totalled per `where`."""
        return self._control.violations_received_counts

    @property
    def unhandled_kinds(self) -> dict:
        """Message kinds nothing classified, counted rather than dropped."""
        return self._control.unknown_kinds

    def _wake_sender(self) -> None:
        with self._sendable:
            self._pending = True
            self._sendable.notify_all()

    def _emit(self, msg: "pb2.PluginMsg") -> None:
        """Classify one outbound message and wake the sender (§5.4, §7)."""
        self.outbox.send(msg)  # the outbox wakes the sender itself

    def _close_stream(self) -> None:
        """§3.3: closing the stream is what "done" means, and nothing else
        says it -- so this is the only way the generator ends, and the one
        place every ending of a session passes through: the shutdown window
        closing, the dispatch loop's iterator ending because the host closed
        or lost the stream, the doorbell reading the host's EOF."""
        with self._sendable:
            self._closing = True
            self._pending = True
            self._sendable.notify_all()
        self.session_ended.set()

    def _off_loop(self, pool, handler, hm) -> None:
        """Run one application handler on its own lane, never on the loop (§6).

        The reply is emitted from the worker, so the loop is free to route the
        arrangement, acknowledgement or one-way that arrives while the handler
        runs — which is the whole of what §6 asks for on the receiving side.
        """
        # Registered here, on the loop, so a `CancelWork` that overtakes the
        # handler's start still finds it (§8.16).
        self._work.begin_request(hm.id)
        future = pool.submit(self._run_off_loop, handler, hm)
        with self._inflight_lock:
            self._inflight.add(future)
        future.add_done_callback(self._retire_inflight)

    def _retire_inflight(self, future) -> None:
        with self._inflight_lock:
            self._inflight.discard(future)

    def _run_off_loop(self, handler, hm) -> None:
        kind = hm.WhichOneof("msg")
        work = self._work.request(hm.id)
        try:
            if work is not None and work.cancelled:
                raise Cancelled("cancelled before it started")
            _work._set_current(work)
            reply = handler(hm)
            expected = _control.REQUEST_REPLIES.get(kind)
            got = reply.WhichOneof("msg")
            alternate = _control.ALTERNATE_REPLIES.get(kind)
            if expected is not None and got not in (expected, alternate, "error"):
                # §8 pairs each request with the reply that answers it.  A
                # handler that returns another arm produces a message the
                # host's call cannot decode, and the failure would be
                # attributed to the protocol rather than to the handler.
                reply = pb2.PluginMsg(
                    id=hm.id,
                    error=pb2.Error(
                        message=f"handler answered {kind} with {got}, expected {expected}"
                    ),
                )
            _emit_or_reclaim(self._emit, reply, self._memory, self._control.diagnose)
        except Cancelled as exc:
            # §8.16: the host asked, the handler stopped.  Not a failure.
            self._emit(
                pb2.PluginMsg(
                    id=hm.id,
                    error=pb2.Error(kind=pb2.Error.CANCELLED, message=str(exc) or "cancelled"),
                )
            )
        except Exception as exc:  # noqa: BLE001 — a handler must not kill the lane
            # The handlers below already turn their own failures into Error
            # replies; this covers what is raised around them (a full segment,
            # an encode failure), which would otherwise leave the host waiting
            # on a reply that is never coming.
            self._emit(
                pb2.PluginMsg(id=hm.id, error=pb2.Error(message=f"{type(exc).__name__}: {exc}"))
            )
        finally:
            _work._set_current(None)
            self._work.end_request(hm.id)
            # §5.6: the handler is done with the bytes whatever it did with
            # them, so this is a disposition and the release is owed at zero.
            self._control.finish_request(hm)

    def _drain_inflight(self, deadline_s: float) -> None:
        """Let handlers already running finish before the stream closes.

        Bounded by the host's own window (§3.3): work that outruns it is cut
        off by the host regardless, and waiting longer here would only delay
        the close that says we are done.
        """
        with self._inflight_lock:
            pending = list(self._inflight)
        if pending:
            futures.wait(pending, timeout=deadline_s)

    def _plugin_context(self, file_state_dir: str = "") -> PluginContext:
        return PluginContext(
            _panels=self._panel,
            _overlays=self._overlay,
            _host=self._host_client,
            file_state_dir=_state_dir(file_state_dir),
            _control=self._control,
            _work=self._work,
        )

    def _panel(self, panel_id: str) -> PanelHandle:
        # Under a lock, as `_overlay` is: two threads asking for a new panel's
        # handle each made one, and the loser's held images were lost.
        with self._overlay_handles_lock:
            handle = self._panel_handles.get(panel_id)
            if handle is None:
                handle = self._panel_handles[panel_id] = PanelHandle(
                    panel_id, self._emit, self._memory, drop_rows=self.outbox.drop_rows
                )
            return handle

    def _overlay(self, overlay_id: str) -> OverlayHandle:
        with self._overlay_handles_lock:
            handle = self._overlay_handles.get(overlay_id)
            if handle is None:
                handle = OverlayHandle(
                    overlay_id,
                    self._emit,
                    self._memory,
                    transport=self._doorbell,
                    diagnose=self._control.diagnose,
                )
                self._overlay_handles[overlay_id] = handle
            return handle

    def _on_subscribed(self, kind: str, msg) -> None:
        """§8.7's subscribed pushes, handed to a worker of their own."""
        fn = _SUBSCRIBED.get(kind)
        if fn is None:
            return
        with self._subscribed_lock:
            worker = self._subscribed_workers.get(kind)
            if worker is None:
                worker = _NewestWorker(
                    kind, lambda change, fn=fn: fn(self._plugin_context(), change)
                )
                self._subscribed_workers[kind] = worker
        if kind == "view_changed":
            worker.offer(
                ViewChange(
                    int(msg.open_generation),
                    int(msg.t),
                    int(msg.z),
                    tuple(int(c) for c in msg.visible_channels),
                    msg.selected_region_id,
                    AppliedFilter.from_wire(msg),
                )
            )
        else:
            worker.offer(
                RegionsChange(
                    int(msg.open_generation),
                    bool(msg.reset),
                    tuple(
                        RegionChange(c.region_id, int(c.revision), c.origin, bool(c.deleted))
                        for c in msg.changes
                    ),
                ),
                merge=_merge_regions,
            )

    # -- region types this plugin shapes (§8.8) ----------------------------

    def _on_shape(self, kind: str, msg) -> None:
        """A drag, a commit, or the end of an edit.  Drags go to one worker that
        computes only the newest; a commit is always answered, on its own worker."""
        drag_id = int(msg.drag_id)
        if kind == "shape_edit_ended":
            with self._shape_lock:
                self._shape_closed.add(drag_id)
            outcome = _enum_name(pb2.ShapeEditEnded.Outcome, msg.outcome, "")
            for fn in list(_SHAPE_ENDED):
                self._shape_commit_pool.submit(
                    fn, self._plugin_context(), drag_id, outcome, msg.detail
                )
            return
        if kind == "shape_commit":
            # §8.8: once the commit is here, no more proposals for this drag.
            with self._shape_lock:
                self._shape_closed.add(drag_id)
            self._shape_commit_pool.submit(self._answer_shape_commit, msg)
            return
        with self._shape_lock:
            if drag_id in self._shape_closed:
                return  # a drag after its commit or end: not answered (§8.8)
            worker = self._shape_worker
            if worker is None:
                worker = self._shape_worker = _NewestWorker("shape", self._propose_shape)
        worker.offer(msg)

    def _propose_shape(self, msg) -> None:
        result = self._run_shaper(msg, final=False)
        if result is None:
            return
        with self._shape_lock:
            if int(msg.drag_id) in self._shape_closed:
                return  # its commit or end arrived while this was computing
        self._emit(pb2.PluginMsg(shape_proposal=result))

    def _answer_shape_commit(self, msg) -> None:
        try:
            result = self._run_shaper(msg, final=True)
        except Exception as exc:  # noqa: BLE001 -- §8.8: every commit is answered
            result = pb2.ShapeResult(
                drag_id=int(msg.drag_id),
                resolution=roi_pb2.REJECTED,
                message=f"the plugin could not compute the shape: {exc}",
            )
        if result is None:  # §8.8: every commit is answered, never silence
            result = pb2.ShapeResult(
                drag_id=int(msg.drag_id),
                resolution=roi_pb2.REJECTED,
                message=f"this plugin registers no shaper for type {msg.type_name!r}",
            )
        result.seq = 0
        self._emit(pb2.PluginMsg(shape_answer=result))

    def _run_shaper(self, msg, *, final: bool):
        """Run the registered shaper on one edit: the reply, or None when no
        shaper is registered for its type.  A shaper's error, or a shape of the
        wrong size, is answered REJECTED with what went wrong."""
        fn = _SHAPERS.get(msg.type_name)
        if fn is None:
            return None
        move = msg.move

        def vec(v) -> "np.ndarray":
            return np.array([v.z, v.y, v.x], dtype=float)

        positions = np.array(
            [[v.position.z, v.position.y, v.position.x] for v in move.roi.vertices]
        )
        edit = ShapeEdit(
            region_id=msg.region_id,
            contrib_id=msg.contrib_id,
            type_name=msg.type_name,
            drag_id=int(msg.drag_id),
            seq=int(msg.seq),
            final=final,
            roi=move.roi,
            positions=positions.reshape(-1, 3),
            handle=int(move.handle),
            mode=int(move.mode),
            axis=vec(move.axis),
            p0=vec(move.p0),
            p1=vec(move.p1),
        )
        out = pb2.ShapeResult(drag_id=int(msg.drag_id), seq=int(msg.seq))
        try:
            answer = fn(self._plugin_context(), edit)
        except Exception as exc:  # the plugin's own error: no shape for this edit
            self._control.diagnose(f"@region_shaper({msg.type_name!r}) raised {exc!r}")
            out.resolution = roi_pb2.REJECTED
            out.message = f"the shaper failed: {exc}"
            return out
        if isinstance(answer, Rejected):
            out.resolution = roi_pb2.REJECTED
            out.message = answer.message
            return out
        clamped = isinstance(answer, Shape) and answer.clamped
        try:
            pts = np.asarray(answer.positions if isinstance(answer, Shape) else answer, dtype=float)
        except (TypeError, ValueError) as exc:
            # A ragged or non-numeric return is the shaper's error like any other:
            # answered REJECTED, never left unanswered (§8.8).
            out.resolution = roi_pb2.REJECTED
            out.message = f"the shaper returned something that is not positions: {exc}"
            return out
        if pts.shape != positions.reshape(-1, 3).shape or not np.all(np.isfinite(pts)):
            out.resolution = roi_pb2.REJECTED
            out.message = (
                f"the shaper returned {pts.shape} positions for a region of "
                f"{len(positions)} vertices, or a non-finite one"
            )
            return out
        out.resolution = roi_pb2.CLAMPED if clamped else roi_pb2.OK
        for z, y, x in pts:
            out.positions.add(z=float(z), y=float(y), x=float(x))
        return out

    def _on_follow(self, kind: str, msg) -> None:
        """§8.15: a Follow the person started, run on a thread of its own, or
        the end of one."""
        fid = int(msg.follow_id)
        if kind == "follow_ended":
            with self._follows_lock:
                running = self._follows.pop(fid, None)
            if running is not None:
                running._ended_by_host(
                    _enum_name(pb2.FollowEndReason, msg.reason, "FOLLOW_END_"), msg.detail
                )
            return
        fn = _FOLLOWS.get(msg.panel_id)
        run = Follow(fid, msg.panel_id, msg.targets, msg.resumed, self._emit)
        if fn is None:
            self._control.diagnose(
                f"a Follow started from panel {msg.panel_id!r} and no @follow drives it; ending it"
            )
            run.end()
            return
        with self._follows_lock:
            self._follows[fid] = run

        def drive() -> None:
            try:
                fn(self._plugin_context(), run)
            except Exception as exc:  # the plugin's own error ends its Follow
                self._control.diagnose(f"@follow({msg.panel_id!r}) raised {exc!r}; ending it")
            finally:
                with self._follows_lock:
                    self._follows.pop(fid, None)
                run.end()

        threading.Thread(target=drive, name=f"vizi-follow-{fid}", daemon=True).start()

    def _on_chunks_wanted(self, overlay_id: str, wants: list) -> None:
        """The host restated what it wants from a chunked overlay (§5.7)."""
        source = _CHUNK_SOURCES.get(overlay_id)
        if source is None and overlay_id not in self._overlay_handles:
            self._control.diagnose(
                f"the host wants chunks of overlay {overlay_id!r} and no @chunk_source is "
                "registered; publish_chunk by hand or register one"
            )
        self._overlay(overlay_id)._wanted(
            [ChunkWant(level, t) for level, t in wants], source, self._plugin_context
        )

    def _on_segment_installed(self, segment_id: int) -> None:
        """A panel's `P` segment is installed: its held images can go now.
        `PanelOpened` may have come first and found no segment, and a
        relaunched session's panels may never hear it."""
        segment = next((s for s in self._memory.installed() if s.segment_id == segment_id), None)
        if segment is None or segment.purpose != _memory.PURPOSE_PANEL:
            return
        with self._overlay_handles_lock:
            handle = self._panel_handles.get(segment.subject)
        if handle is None:
            return

        def flush() -> None:
            try:
                handle._flush_held_images()
            except Exception as exc:  # noqa: BLE001 -- reported, not raised
                self._control.diagnose(
                    f"panel {segment.subject!r}: a held image could not be sent: {exc}"
                )

        try:
            self._invoke_pool.submit(flush)
        except RuntimeError:  # the session is ending
            pass

    def _on_release_loan(self, segment_id: int) -> None:
        with self._overlay_handles_lock:
            handles = list(self._overlay_handles.values())
        for handle in handles:
            handle._arena_freed()

    # -- input events (`doorbell_E_input.md`) ------------------------------------

    #: `E` §1's phases, as the handler names a plugin author writes.  `FIRE` is
    #: a whole interaction in one record -- it opens and closes at once -- so it
    #: has no drag and no release, and a tool that only implements `press` sees
    #: nothing from it.
    _PHASE_HANDLERS = {
        _doorbell.PHASE_PRESS: "press",
        _doorbell.PHASE_DRAG: "drag",
        _doorbell.PHASE_RELEASE: "release",
        _doorbell.PHASE_CANCEL: "cancel",
        _doorbell.PHASE_FIRE: "fire",
    }

    def on_input_event(self, ev) -> None:
        """Route one accepted ``E`` to the tool its handle names.

        **The handle, not a string.**  `E` records carry a ``tool`` handle and
        the manifest id it stands for arrives once, as the ``ToolBinding``
        arrangement the control plane installed (§8.10) — so an event for a
        handle with no binding is one the arrangement did not precede, which
        the transport has already reported as ``unknown_tool``.

        Runs at event rate on the listener thread: handlers push live feedback
        through ``ctx.overlay(id).set_items_live``, never gRPC.
        """
        binding = self._control.tools.get(ev.tool)
        if binding is None:
            return
        ev = self._check_context(ev)
        handler = self._PHASE_HANDLERS.get(ev.phase)
        if handler == "fire" and ev.action and hasattr(_TOOLS.get(binding.tool_id), "action"):
            # A FIRE naming an action (§8.10): the key or pane button fired INTO
            # the engaged tool, mid-gesture or not.  `action` takes it when the
            # class has one; otherwise it is a `fire` whose `ctx.action(ev)`
            # says which.
            handler = "action"
        if handler is not None:
            self._run_tool(binding.tool_id, handler, ev)

    def _check_context(self, ev):
        """`E` §3's third terminal row -- the supervisor's half of the check
        the transport cannot make.

        The transport rules on what the record alone can show; whether its
        ``level``, ``t`` and coordinate reconcile with ARRANGED state is this
        end's, because this end holds the arrangement.  A record that does not
        is still delivered -- a terminal closes what it closes, and the
        supervisor may not reject one -- but ``usable`` is cleared and the
        host is told (``unusable_context``), so a handler never has to guess
        whether a coordinate it was handed had been checked.

        What is checked: ``level`` against the epoch's declared ``levels``; a
        non-``MISS`` voxel-naming coordinate (``VOXEL``, ``PICKED``, the pick of
        ``RAY_AND_PICKED``) against the clip box at that level -- a ray's eye
        and far point lie outside the box by construction and are exempt; and
        ``t`` against the time table when one was arranged, since ``Geometry``
        carries no ``T`` and silence about time means unknown.  A record
        under a pane this end holds no state for cannot be checked and is
        delivered as usable: the epoch gate makes that a record the transport
        already refused.
        """
        state = self._control.pane_state(int(ev.view_id), int(ev.epoch))
        if state is None:
            return ev
        problem = None
        levels = state.geometry.levels
        if ev.level >= len(levels) and levels:
            problem = f"level {ev.level} is not one of the {len(levels)} the epoch declares"
        elif state.time_table is not None and ev.t >= len(state.time_table):
            problem = f"t {ev.t} is past the arranged time table of {len(state.time_table)}"
        elif not ev.miss and ev.position is not None:
            voxel = None
            if isinstance(ev.position, Voxel):
                voxel = ev.position
            elif isinstance(ev.position, RayAndPicked):
                voxel = ev.position.picked
            if voxel is not None and levels:
                # The clip box at the record's level, with the factor the host
                # declared per axis: §8.11's map is affine per axis, and the
                # crop travels in level-0 edges.
                box = state.crop_at(ev.level)
                if not box.contains(voxel.z, voxel.y, voxel.x):
                    problem = (
                        f"a non-MISS coordinate ({voxel.z}, {voxel.y}, {voxel.x}) at level "
                        f"{ev.level} lies outside the clip box {box.as_tuple()}"
                    )
        if problem is None:
            return ev
        self._control.report(
            "unusable_context",
            f"pane {ev.view_id} epoch {ev.epoch} interaction {ev.interaction} seq {ev.seq}: "
            f"{problem}",
        )
        return dataclasses.replace(ev, usable=False)

    def _run_tool(self, tool_id: str, handler: str, *args, _optional=None) -> None:
        """Call ``handler`` on the tool with the context and ``args``.
        ``_optional`` is passed too when the hook's signature has room for it
        after the context -- the binding an activation hook may take."""
        cls = _TOOLS.get(tool_id)
        if cls is None:
            return
        impl = self._tool_instances.get(tool_id)
        if impl is None:
            impl = self._tool_instances[tool_id] = cls()
        fn = getattr(impl, handler, None)
        if fn is None:
            return
        if _optional is not None and _hook_takes_a_second(fn):
            args = (*args, _optional)
        ctx = ToolContext(
            _overlays=self._overlay,
            _host=self._host_client,
            _control=self._control,
            _panels=self._panel,
        )
        try:
            fn(ctx, *args)
        except Exception as exc:  # noqa: BLE001 — a handler bug is not a fault
            # `doorbell_protocol.md` §8: framing faults tear down, everything
            # else reports and continues.  A tool that raises is the plugin's
            # own bug and must not stop the listener draining the next grant.
            self._control.diagnose(f"tool {tool_id!r} {handler} raised: {exc!r}")

    def _on_tool_binding(self, tool_id: str, active: bool, binding) -> None:
        """The host activated or retired one of this plugin's tools
        (`ToolBinding`, §8.10): the user checked the menu entry, pressed
        `Esc`, or activated another plugin's tool.  After the control plane's
        own handling -- the handle is installed or gone, an open gesture
        already cancelled -- the tool class hears ``activated(ctx)`` or
        ``deactivated(ctx)``, optional like every other handler, so a panel
        can show whether its tool is live.

        **A hook that takes a second parameter is handed the binding** --
        ``activated(self, ctx, binding)`` -- the arrangement just installed,
        or at ``deactivated`` the one just withdrawn, which
        ``ctx.arrangement`` can no longer find.  Arity is the switch, as it
        is for `PluginContext`: a hook written as ``activated(self, ctx)``
        keeps working unchanged."""
        self._run_tool(tool_id, "activated" if active else "deactivated", _optional=binding)

    def on_notification(self, note, note_raw: bytes | None = None) -> None:
        """A ``D`` from the host: look at these regions of this segment now.

        A hint can only bring a check FORWARD (`doorbell_D_notification.md`
        §5).  The unconditional schedule is :class:`ReadSchedule`'s, and this
        is the wake that lets it run early; the discipline check belongs to the
        memory layer, which is the only end that knows one.
        """
        hint = self._memory.hint(
            note.segment_id,
            note.regions(),
            epoch=note.epoch,
            installed_epoch=self._control.epochs.get(note.view_id),
        )
        # §3's own reporting unit is the RECORD: one `D` naming three Owned
        # regions is one occurrence, not three, so they batch.
        self._control.report_records(
            self._reporter.report_record(
                note_raw,
                (
                    (where, f"D named region {index} of segment {note.segment_id}")
                    for where, index in hint.violations
                ),
            )
        )
        if hint.scan:
            self.reads.wake()
        if hint.turns:
            self.stages.wake()

    def on_panel_opened(self, panel_id: str) -> None:
        handle = self._panel_handles.get(panel_id)
        if handle is not None:
            try:
                handle._flush_held_images()
            except Exception as exc:  # noqa: BLE001 -- the handler still runs
                self._control.diagnose(
                    f"panel {panel_id!r}: an image held until it opened could not be sent: {exc}"
                )
        fn = _PANEL_OPENED.get(panel_id)
        if fn is None:
            return
        fn(
            CommandContext(
                args={},
                _panels=self._panel,
                _overlays=self._overlay,
                _host=self._host_client,
                _control=self._control,
                _work=self._work,
            )
        )

    # -- the session stream ----------------------------------------------------

    def Session(self, request_iterator, context):
        # §3.1: `Ready.id` is this build's identity string, the one push whose
        # id is non-empty by rule -- and the host fails the session on any
        # other form, so this line is the whole of what stands between the
        # `1.0` host and `serve()`.
        self._emit(
            pb2.PluginMsg(
                id=self._plugin_identity,
                ready=pb2.Ready(
                    protocol_major=PROTOCOL_MAJOR,
                    protocol_minor=PROTOCOL_MINOR,
                    plugin_id=self._plugin_id,
                ),
            )
        )
        threading.Thread(target=self._dispatch_loop, args=(request_iterator,), daemon=True).start()
        while True:
            batch = self.outbox.drain()
            if batch:
                yield from batch
                # Whatever accumulated while gRPC was writing that batch is
                # drained next -- and a coalescing key that arrived twice in
                # that window is now ONE message, which is the whole of §5.4's
                # value on this side: it costs nothing when the stream keeps
                # up and bounds the backlog when it does not.
                continue
            with self._sendable:
                if not self._pending and not self._closing:
                    # §7's published drain interval, as a backstop: the outbox
                    # notifies on every admission, so this only ever covers a
                    # sender that reached the queue without going through it.
                    # It must WAIT ONCE and then re-check the queue -- looping
                    # here until `_pending` makes the interval decorative and
                    # the backstop nothing at all.
                    self._sendable.wait(_DRAIN_INTERVAL_S)
                self._pending = False
                if self._closing and not self.outbox.drain_ready():
                    break

    def _dispatch_loop(self, request_iterator) -> None:
        """Consume host requests on a worker thread, enqueueing replies —
        handler-emitted panel patches interleave on the same queue, which is
        what lets the plugin PUSH."""
        try:
            for hm in request_iterator:
                try:
                    if not self._route_one(hm):
                        break
                finally:
                    # §11a names this loop as one acceptable mechanism for the
                    # periodic work a session owes: the reports §2's interval
                    # has made due, and the retirements §9.1 could not settle
                    # while a claim was still held.  Neither has a timer of its
                    # own, and neither may wait for the next inbound message to
                    # be a particular kind.
                    self._pump()
        except Exception:  # noqa: BLE001 — host teardown cancels the stream
            # mid-iteration (grpc raises out of the request iterator); the
            # finally below already unblocks the outbound generator, and a
            # traceback on every clean session close is just plugin-log noise.
            pass
        finally:
            self._close_stream()

    def _pump(self) -> None:
        """The per-pass work no message triggers.

        Neither half may raise into a caller: this runs in a ``finally`` on the
        dispatch loop and on the doorbell's listener thread, and a session
        already at §7's one-shot capacity is one whose stream is not draining --
        which is a fault the *sender* raises, not something to re-raise from
        the bookkeeping that happened to notice.
        """
        try:
            self._control.report_due()
            for segment_id in self._control.settle_retirements():
                self._control.diagnose(f"segment {segment_id} released once its claim finished")
        except _control.SessionFault as exc:
            self._control.diagnose(f"the outbound queue refused periodic work: {exc}")
        self._arm_report_timer()

    def _settle_soon(self) -> None:
        if self.session_ended.is_set():
            return
        timer = threading.Timer(0.0, self._pump)
        timer.daemon = True
        timer.start()

    def _arm_report_timer(self) -> None:
        """A report the rate deferred goes out at its deadline even
        if no further traffic arrives -- the rate delays reports, it does not
        refuse them, and a quiet session would otherwise hold one forever."""
        wait = self._control.next_report_due()
        with self._report_timer_lock:
            if wait is None or self.session_ended.is_set() or self._report_timer is not None:
                return
            timer = threading.Timer(wait + 0.01, self._on_report_timer)
            timer.daemon = True
            self._report_timer = timer
        timer.start()

    def _on_report_timer(self) -> None:
        with self._report_timer_lock:
            self._report_timer = None
        if not self.session_ended.is_set():
            self._pump()

    def _route_one(self, hm) -> bool:
        """Classify and route one ``HostMsg``.  ``False`` ends the loop.

        `control_plane_protocol.md` §4 rule 3: CLASSIFY BY KIND, and do it
        before any id matching.  The control plane applies the arrangements,
        answers the grant path, routes replies (and releases what an abandoned
        one names), and hands back only what is this loop's.
        """
        routing = self._control.receive(hm)
        kind = routing.kind
        action = routing.action
        if action is _control.Action.HELLO:
            problem = self._check_hello(hm)
            if problem is not None:
                self.handshake_error = problem
                # §12: the host retains a redacted tail of stderr and shows it
                # in Plugin Manager, so this is the surface §3.1's "a
                # diagnostic naming both versions" means at this end -- there
                # is no message class for it.
                print(problem, file=sys.stderr, flush=True)
                return False
            return True
        if action is _control.Action.SHUTDOWN:
            # The loop does NOT stop here.  §3.3: "new work is refused at both
            # ends", and a request that arrives inside the window is answered
            # with an `Error` rather than run -- which needs this loop still
            # reading and the control plane still refusing.  What ends the
            # session is `_finish_shutdown`, on its own thread.
            self._on_shutdown(hm)
            return True
        if action is _control.Action.DOORBELL_SHUTDOWN:
            self._on_doorbell_shutdown(hm.doorbell_shutdown)
            return True
        if action is _control.Action.FOLLOW:
            self._on_follow(kind, getattr(hm, kind))
            return True
        if action is _control.Action.SHAPE:
            self._on_shape(kind, getattr(hm, kind))
            return True
        if action is _control.Action.CANCEL_WORK:
            # On the loop, never a pool: the pool may be busy running the very
            # work this cancels (§8.16).
            self._work.cancel(hm.cancel_work)
            return True
        if action is _control.Action.PANEL_OPENED:
            # §8.4: it moves here from doorbell op `L` in 1.0, and §6 keeps
            # the handler off this loop.
            self._invoke_pool.submit(self.on_panel_opened, hm.panel_opened.panel_id)
            return True
        if action is _control.Action.HOST_REQUEST:
            self._dispatch_request(kind, hm)
        return True  # otherwise: applied, acknowledged, routed, held or counted

    def _dispatch_request(self, kind: str, hm) -> None:
        """One host request onto its class's worker (§6).

        Also the destination for a request the control plane held until its
        ride-along's capability landed: the message is the same and so is the
        lane, which is why this is a method rather than a chain in the loop.
        """
        lane = _REQUEST_LANES.get(kind)
        if lane is None:  # pragma: no cover - the partition is asserted at import
            self._control.diagnose(f"no lane for host request {kind!r}")
            return
        self._off_loop(getattr(self, lane[0]), getattr(self, lane[1]), hm)

    def _on_shutdown(self, hm) -> None:
        """§3.3: open the window -- start the hook, and hand the rest to a thread.

        The dispatch loop must go on reading while the window is open, so
        that a request arriving inside it is refused rather than run, which
        is why nothing here waits.  The window is the host's ``deadline_ms``
        measured from its *send*, an upper bound rather than a budget.
        """
        if not hm.shutdown.HasField("deadline_ms"):
            # §9: the deadline is *"ALWAYS set by the host"*, so a missing one
            # is a host defect the plugin **reports** -- site 25 -- and does not
            # stall on.  Adopting the 5 s is the first half and this is the
            # second; substituting silently agrees with a peer that is wrong and
            # tells nobody.
            #
            # **PRESENCE, never truthiness.**  The field became `optional` for
            # exactly this: as a plain `uint32` an absent value and a `0` are
            # identical bytes, so `if not deadline_ms` would fire on a
            # conforming host that meant no grace at all -- reporting a
            # violation against a peer that committed none.  This SDK asked for
            # the site and did not spot that the site was unimplementable
            # without the presence bit.
            self._control.report(
                "missing_deadline",
                f"Shutdown named no deadline_ms; using §9's " f"{DEFAULT_SHUTDOWN_GRACE_S:g} s",
            )
        notice = ShutdownNotice(
            reason=_SHUTDOWN_REASONS.get(hm.shutdown.reason, "unspecified"),
            deadline_ms=(hm.shutdown.deadline_ms if hm.shutdown.HasField("deadline_ms") else None),
            detail=hm.shutdown.detail,
        )
        self.shutdown_notice = notice
        started = time.monotonic()
        hook = self._shutdown_pool.submit(_SHUTDOWN_HOOK[0], notice) if _SHUTDOWN_HOOK else None
        threading.Thread(
            target=self._finish_shutdown,
            args=(notice, hook, started),
            name="vizi-sdk-teardown",
            daemon=True,
        ).start()

    def _finish_shutdown(self, notice: ShutdownNotice, hook, started: float) -> None:
        """§3.3, in the specification's order: the hook's outcome, then what
        is in flight, then the report, then the close.

        **Closing the stream is what "done" means** and the close cannot say
        why, so ``ShutdownReport`` rides ahead of it: hook completed, hook
        raised, no hook registered, and hook still running are one event at
        the socket and four here.  Nothing is acknowledged and nothing is
        sent after the close.
        """
        if hook is None:
            outcome, detail = pb2.ShutdownReport.NO_HOOK, ""
        else:
            # The window is an upper bound rather than a budget: a hook that
            # overruns is not given more time, it is cut off — the host tears
            # down at its own deadline either way.
            done, _ = futures.wait([hook], timeout=notice.grace_seconds)
            if hook not in done:
                outcome = pb2.ShutdownReport.OVERRAN
                detail = f"the hook was still running after {notice.grace_seconds:g} s"
            elif hook.exception() is not None:
                exc = hook.exception()
                outcome, detail = pb2.ShutdownReport.RAISED, f"{type(exc).__name__}: {exc}"
            else:
                outcome, detail = pb2.ShutdownReport.COMPLETED, ""
        # Handlers already running keep their replies, within what is left of
        # the window: a reply emitted after the stream closes is lost, and a
        # host that has said "5 s" will not wait for the sixth.
        self._drain_inflight(max(0.0, notice.grace_seconds - (time.monotonic() - started)))
        # `reader_writer_contract.md` §1.8: a file handle that matters is
        # released "at shutdown when [CloseReader] does not" come -- after the
        # in-flight read that may still be using it, and before the report so
        # a close that raises is in the sweep.  Process
        # exit would release the descriptor; a lock file or a sidecar written
        # on close would not get its chance.
        self._release_session_files()
        # §2: at teardown the next interval never arrives, so what the rate
        # deferred goes out now -- ahead of the report, which is the last thing
        # on the stream.
        self._control.report_remaining()
        report = pb2.ShutdownReport(outcome=outcome, detail=detail)
        self._emit(pb2.PluginMsg(shutdown_report=report))
        self._close_stream()

    # -- command dispatch ------------------------------------------------------

    def _on_plan_stage(self, hm) -> "pb2.PluginMsg":
        """§3: what this stage will produce from the offered input."""
        request = hm.plan_stage
        name = f"stage {request.stage_id!r}"

        def error(kind, message):
            return pb2.PluginMsg(id=hm.id, error=pb2.Error(kind=kind, message=message))

        if request.stage_id not in _STAGES:
            return error(pb2.Error.NOT_FOUND, f"{name} is not declared by this plugin")
        try:
            inp = _stages.stage_input(request.input)
        except ValueError as exc:
            return error(pb2.Error.PROTOCOL, f"{name}: {exc}")
        try:
            params = _stages.check_params(_STAGE_PARAMS.get(request.stage_id, ()), request.params)
        except ValueError as exc:
            return error(pb2.Error.INVALID_ARGUMENT, f"{name}: {exc}")
        try:
            plan = self._stage_instance(request.stage_id).plan(inp, params)
        except Refusal as refusal:
            return error(refusal.kind, f"{name}: {refusal}")
        except Exception as exc:
            return error(pb2.Error.INTERNAL, f"{name}: plan raised {exc!r}")
        try:
            reply = _stages.plan_reply(plan, inp, pb2)
        except _stages.ChannelIndexTooLarge as exc:
            return error(pb2.Error.PROTOCOL, f"{name}: {exc}")
        except (TypeError, ValueError) as exc:
            return error(pb2.Error.INTERNAL, f"{name}: the stage's plan is invalid: {exc}")
        with self._stage_lock:
            self._stage_plans[_plan_key(request)] = plan.needs()
        return pb2.PluginMsg(id=hm.id, stage_planned=reply)

    def _stage_instance(self, stage_id: str):
        cls = _STAGES.get(stage_id)
        if cls is None:
            return None
        with self._stage_lock:
            impl = self._stage_instances.get(stage_id)
            if impl is None:
                impl = self._stage_instances[stage_id] = _construct(cls, self._plugin_context())
            return impl

    def _stage_needs(self, arm):
        """The input channels ``arm`` was planned to need, as they stood when
        that revision installed."""
        with self._stage_lock:
            taken = self._arm_needs.get((arm.pipeline, arm.position))
            if taken is not None and taken[0] == arm.revision:
                return taken[1]
            return self._stage_plans.get(_arm_plan_key(arm))

    def _stage_arm_problem(self, arm) -> "str | None":
        """§4: an arm whose params do not match the declaration, or that no
        answered ``PlanStage`` stands behind, is unusable -- the host plans
        before it arms, and the plan is where the arm's needs come from."""
        try:
            _stages.check_params(_STAGE_PARAMS.get(arm.stage_id, ()), arm.params)
        except ValueError as exc:
            return str(exc)
        with self._stage_lock:
            planned = _arm_plan_key(arm) in self._stage_plans
        if not planned:
            return "no PlanStage for this input and params was answered"
        return None

    def _on_stage_arm(self, arm) -> None:
        # On the dispatch thread, like every PlanStage: no later plan can land
        # between the install and this snapshot.
        with self._stage_lock:
            self._arm_needs[(arm.pipeline, arm.position)] = (
                arm.revision,
                self._stage_plans.get(_arm_plan_key(arm)),
            )
        self.stages.start()
        self.stages.wake()

    def _stage_handed_on(self, segment_id: int, regions: "list[int]") -> None:
        """§6.2: the hint to the host.  A refused send is dropped; the host's
        own re-check of the turn word is what guarantees the handoff."""
        if self._doorbell is not None:
            self._doorbell.send_notification(segment_id, regions)

    def _on_invoke(self, hm) -> "pb2.PluginMsg":
        cid = hm.invoke_command.command_id
        fn = _COMMANDS.get(cid)
        if fn is None:
            return pb2.PluginMsg(
                id=hm.id,
                command_done=pb2.CommandDone(
                    command_id=cid, ok=False, message=f"no handler registered for {cid!r}"
                ),
            )
        args = {k: _value_to_python(v) for k, v in hm.invoke_command.args.items()}
        try:
            reported = fn(
                CommandContext(
                    args=args,
                    cause=int(hm.invoke_command.cause),
                    _panels=self._panel,
                    _overlays=self._overlay,
                    _host=self._host_client,
                    _control=self._control,
                    _work=self._work,
                    _request=self._work.request(hm.id),
                )
            )
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the session
            return pb2.PluginMsg(
                id=hm.id,
                command_done=pb2.CommandDone(command_id=cid, ok=False, message=str(exc)),
            )
        # The handler's return value IS the success message.  Until this,
        # the host's success report was unreachable from the SDK: the return was
        # discarded and `message` hardcoded empty, so a command invoked from the
        # Tools menu could only speak through a panel the user had to have open.
        # A wrong type reports FAILURE rather than being dropped — a silently
        # ignored return is the exact defect this closes.
        #
        # `bool` and `int` are carved out of that rule because they are the
        # wrong types an author reaches for by accident: `ctx.host.activate_tool`
        # returns bool, and `compile_label_volume` / `contribute_rois` return an
        # int count, so a handler whose trailing expression is one of those
        # calls would be told its working command failed.  True and any int are
        # a silent success -- a count's effect is already visible where the
        # count landed; False still fails, because swallowing it would drop the
        # outcome the callee reported.
        if reported is None:
            message = ""
        elif isinstance(reported, bool):
            if not reported:
                return pb2.PluginMsg(
                    id=hm.id,
                    command_done=pb2.CommandDone(
                        command_id=cid,
                        ok=False,
                        message="command handler returned False",
                    ),
                )
            message = ""
        elif isinstance(reported, int):
            message = ""
        elif isinstance(reported, str):
            message = reported
        else:
            return pb2.PluginMsg(
                id=hm.id,
                command_done=pb2.CommandDone(
                    command_id=cid,
                    ok=False,
                    message=(
                        f"command handler returned {type(reported).__name__}; return a str "
                        "to report success, or None to stay silent"
                    ),
                ),
            )
        return pb2.PluginMsg(
            id=hm.id, command_done=pb2.CommandDone(command_id=cid, ok=True, message=message)
        )

    # -- exporter ops (Phase 3: the reader deposit reversed) --------------------

    def _on_open_export(self, hm) -> "pb2.PluginMsg":
        oe = hm.open_export
        cls = _EXPORTERS.get(oe.exporter_id)
        if cls is None:
            return pb2.PluginMsg(
                id=hm.id,
                error=pb2.Error(
                    kind=pb2.Error.NOT_FOUND,
                    message=f"no exporter registered for {oe.exporter_id!r}",
                ),
            )
        dtype = _dtype_to_np().get(oe.dtype)
        if dtype is None or len(oe.shape_tczyx) != 5:
            # The host declares the artifact once (§2.2), and a declaration
            # this SDK cannot interpret is refused by kind, not by traceback.
            return pb2.PluginMsg(
                id=hm.id,
                error=pb2.Error(
                    kind=pb2.Error.INVALID_ARGUMENT,
                    message=f"OpenExport: dtype {oe.dtype} / shape {list(oe.shape_tczyx)}",
                ),
            )
        desc = ExportDescriptor(
            locator=oe.locator,
            shape_tczyx=tuple(oe.shape_tczyx),
            dtype=dtype,
            little_endian=oe.little_endian,
            source_metadata_form=oe.source_metadata.form,
            source_metadata_text=oe.source_metadata.text,
            spacing_zyx=(
                tuple(v if v > 0 else None for v in (oe.spacing_z, oe.spacing_y, oe.spacing_x))
                if oe.spacing_unit
                else None
            ),
            spacing_unit=oe.spacing_unit,
            time_increment=oe.time_increment if oe.time_increment > 0 else None,
            time_unit=oe.time_unit,
            file_state_dir=_state_dir(oe.file_state_dir),
            filter=AppliedFilter.from_wire(oe),
            channels=tuple(ChannelName.from_wire(ch) for ch in oe.channels),
            provenance=_provenance(oe.provenance),
        )
        try:
            impl = _construct(cls, self._plugin_context())
            impl.begin(desc)
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the session
            return pb2.PluginMsg(id=hm.id, error=_refusal(exc, "begin failed"))
        handle = f"x{self._export_seq}"
        self._export_seq += 1
        self._exports[handle] = (impl, desc)
        return pb2.PluginMsg(id=hm.id, export_opened=pb2.ExportOpened(handle=handle))

    def _on_export_chunk(self, hm) -> "pb2.PluginMsg":
        ec = hm.export_chunk
        entry = self._exports.get(ec.handle)
        if entry is None:
            return pb2.PluginMsg(
                id=hm.id,
                error=pb2.Error(
                    kind=pb2.Error.NOT_FOUND, message=f"unknown export handle {ec.handle!r}"
                ),
            )
        impl, desc = entry
        try:
            source = ec.WhichOneof("source")
            if source == "segment":
                ref = ec.segment
                # §8.12's ride-along was already applied by the control plane
                # -- "apply the arrangement, THEN interpret the rest of the
                # message" -- and a chunk whose capability had not landed was
                # held rather than dispatched, so the mapping exists by here.
                segment = self._memory.segment(ref.segment_id)
                if segment is None:
                    raise RuntimeError(f"segment {ref.segment_id} is not installed")
                region = segment.resolve_deposit(ref.offset, ref.len)
                ring = segment.deposits(region)
                raw = ring.take(ref.offset, ref.len, uses=ref.uses)
                _report_uses(self._control, ring)
                shape_zyx = tuple(ref.shape_zyx)
                dtype = _dtype_to_np()[ref.dtype]
            elif source == "inline":
                # Inline carries no SegmentRef; the chunk geometry is the
                # artifact's declared per-volume shape/dtype.
                raw = ec.inline
                shape_zyx = tuple(desc.shape_tczyx[2:])
                dtype = desc.dtype
            else:
                return pb2.PluginMsg(
                    id=hm.id,
                    error=pb2.Error(
                        kind=pb2.Error.INVALID_ARGUMENT, message="export chunk: no source"
                    ),
                )
            channels = list(ec.channels)
            filtered = set(ec.filtered_channels)
            if filtered and desc.filter is None:
                raise Refusal(
                    pb2.Error.INVALID_ARGUMENT,
                    "chunk marks channels filtered but the export names no filter",
                )
            if not filtered <= set(channels):
                raise Refusal(
                    pb2.Error.INVALID_ARGUMENT,
                    f"chunk marks channels {sorted(filtered - set(channels))} filtered "
                    f"that it does not carry",
                )
            n = max(len(channels), 1)
            expected = n * int(np.prod(shape_zyx)) * dtype.itemsize
            if len(raw) != expected:
                raise RuntimeError(
                    f"chunk payload {len(raw)}B != channels {n} x {shape_zyx} x {dtype} "
                    f"({expected}B)"
                )
            arr = np.frombuffer(raw, dtype=dtype).reshape((n, *shape_zyx))
            for i, c in enumerate(channels or [0]):
                applied = desc.filter if c in filtered else None
                impl.write(ec.t, c, HostDataArray(arr[i], 0, applied))
        except Exception as exc:  # noqa: BLE001
            return pb2.PluginMsg(id=hm.id, error=_refusal(exc, "write failed"))
        return pb2.PluginMsg(
            id=hm.id, chunk_written=pb2.ChunkWritten(handle=ec.handle, total_bytes=len(raw))
        )

    def _on_finish_export(self, hm) -> "pb2.PluginMsg":
        handle = hm.finish_export.handle
        entry = self._exports.pop(handle, None)
        if entry is None:
            return pb2.PluginMsg(
                id=hm.id,
                error=pb2.Error(
                    kind=pb2.Error.NOT_FOUND, message=f"unknown export handle {handle!r}"
                ),
            )
        try:
            entry[0].finish()
        except Exception as exc:  # noqa: BLE001
            return pb2.PluginMsg(id=hm.id, error=_refusal(exc, "finish failed"))
        return pb2.PluginMsg(id=hm.id, export_done=pb2.ExportDone(handle=handle))

    def _on_abort_export(self, hm) -> "pb2.PluginMsg":
        handle = hm.abort_export.handle
        entry = self._exports.pop(handle, None)
        if entry is not None:
            try:
                entry[0].abort()
            except Exception:  # noqa: BLE001 — abort is best-effort cleanup
                pass
        # Abort acks as ExportDone so the host's id-matched call completes;
        # the artifact is abandoned either way.
        return pb2.PluginMsg(id=hm.id, export_done=pb2.ExportDone(handle=handle))

    # -- reader ops (unchanged behavior) ----------------------------------------

    def _on_open(self, hm) -> "pb2.PluginMsg":
        """§8.2: ``OpenReader{uri, reader_id}`` -> ``ReaderOpened`` | ``Error``.

        **The host names which reader contribution is being opened**, and that
        is the whole point of the field: the host matched a contribution in
        order to send this message at all, and without it a plugin declaring
        two readers had to re-derive from the URI the decision the host had
        already made -- an agreement this protocol asks for nowhere else, and
        one two implementations would reach differently, since extension
        lists, magic bytes and directory shapes all disagree exactly where
        formats overlap.

        **Empty means the host named none**, which a plugin declaring exactly
        one reader may treat as that one.  **A plugin declaring several refuses
        an empty `reader_id`** rather than picking: the ambiguity is the
        host's to resolve, and a guess is how one SDK opens a file another
        rejects.
        """
        reader_cls, refusal = self._reader_for(str(hm.open_reader.reader_id))
        if reader_cls is None:
            return pb2.PluginMsg(id=hm.id, error=pb2.Error(kind=refusal[0], message=refusal[1]))
        # `reader_writer_contract.md` §1.2's handshake: a second open REPLACES
        # the open reader, and **`Error` means nothing changed** -- so the next
        # file is opened FIRST and the previous reader closed only once it
        # has.  The one exception is a reopen of the file the open reader
        # holds: an exclusively locked file cannot be opened twice,
        # so that one closes first, and a reopen that
        # fails leaves no reader -- which §1.2 says.  The replaced reader's
        # close never fails the open: diagnosed, and the open proceeds.
        uri = hm.open_reader.uri
        series = hm.open_reader.series if hm.open_reader.HasField("series") else None
        pending_key = (uri, str(hm.open_reader.reader_id))
        # The instance that listed this file's series is the one that
        # opens the chosen one, so whatever the listing opened stays open.  Any
        # other open retires it -- first, since it may hold the file.
        impl = self._take_pending(pending_key if series is not None else None)
        if self._impl is not None and uri == self._reader_uri:
            exc = self._release_reader()
            if exc is not None:
                self._control.diagnose(f"the replaced reader's close raised: {exc!r}")
        try:
            if impl is None:
                impl = _construct(reader_cls, self._plugin_context(hm.open_reader.file_state_dir))
            info = _open_series(impl, uri, series)
        except Exception as exc:  # surface as a protocol Error, not a crash
            return pb2.PluginMsg(id=hm.id, error=_refusal(exc, "open failed"))
        if isinstance(info, SeriesChoice):
            if series is not None:
                _close_quietly(impl, self._control.diagnose)
                return pb2.PluginMsg(
                    id=hm.id,
                    error=pb2.Error(
                        kind=pb2.Error.INVALID_ARGUMENT,
                        message=f"open failed: asked for series {series!r}, the reader "
                        "answered with a list",
                    ),
                )
            try:
                listed = _series_list(info)
            except (TypeError, ValueError, KeyError) as exc:
                _close_quietly(impl, self._control.diagnose)
                return pb2.PluginMsg(
                    id=hm.id,
                    error=pb2.Error(
                        kind=pb2.Error.INVALID_ARGUMENT,
                        message=f"open failed: the reader's SeriesChoice is not valid: {exc}",
                    ),
                )
            self._hold_pending(pending_key, impl)
            return pb2.PluginMsg(id=hm.id, series_list=listed)
        # **The reply is built BEFORE anything is committed** (§1.2 H2: `Error`
        # means nothing changed).  Every conversion that can fail -- a dtype
        # the wire has no word for, a shape or spacing that is not numeric --
        # used to run after the open reader was released and the new one
        # installed, so the host kept a reader this end had already closed
        # and every read of it came back NOT_FOUND.
        try:
            opened, shape, np_dtype = _reader_opened(info)
        except (TypeError, ValueError, KeyError) as exc:
            closer = getattr(impl, "close", None)
            if closer is not None:
                try:
                    closer()
                except Exception as close_exc:  # noqa: BLE001 -- reported, not raised
                    self._control.diagnose(f"closing a refused reader raised: {close_exc!r}")
            return pb2.PluginMsg(
                id=hm.id,
                error=pb2.Error(
                    kind=pb2.Error.INVALID_ARGUMENT,
                    message=f"open failed: the reader's ReaderInfo is not valid: {exc}",
                ),
            )
        exc = self._release_reader()
        if exc is not None:
            self._control.diagnose(f"the replaced reader's close raised: {exc!r}")
        self._impl = impl
        self._reader_uri = uri
        self._reader_serial += 1
        self._reader_handle = f"r{self._reader_serial - 1}"
        self._reader_shape = shape
        self._reader_dtype = np_dtype
        opened.handle = self._reader_handle
        return pb2.PluginMsg(id=hm.id, reader_opened=opened)

    def _take_pending(self, key) -> object:
        """The instance held for ``key``'s series choice, or ``None``; any
        instance held for another key is closed, since a different open is the
        end of that choice."""
        held, self._pending_reader = self._pending_reader, None
        if held is None:
            return None
        held_key, impl, timer = held
        timer.cancel()
        if held_key == key:
            return impl
        _close_quietly(impl, self._control.diagnose)
        return None

    def _hold_pending(self, key, impl) -> None:
        token = object()

        def expire() -> None:
            try:
                self._reader_pool.submit(self._expire_pending, token)
            except RuntimeError:  # the pool is gone: the session is ending
                pass

        timer = threading.Timer(SERIES_CHOICE_HOLD_S, expire)
        timer.daemon = True
        self._pending_reader = (key, impl, timer)
        self._pending_token = token
        timer.start()

    def _expire_pending(self, token) -> None:
        if self._pending_reader is not None and self._pending_token is token:
            self._take_pending(None)

    def _reader_for(self, reader_id: str):
        """§8.2's routing, as ``(class, None)`` or ``(None, (kind, message))``."""
        if not self._readers:
            return None, (pb2.Error.NOT_FOUND, "no reader registered")
        if reader_id:
            cls = self._readers.get(reader_id)
            if cls is None:
                # Named something this build does not serve.  NOT_FOUND rather
                # than a guess: the host believes it matched a contribution,
                # and opening a different one would be worse than failing.
                return None, (
                    pb2.Error.NOT_FOUND,
                    f"no reader contribution {reader_id!r}; this session serves "
                    f"{sorted(self._readers)}",
                )
            return cls, None
        if len(self._readers) == 1:
            return next(iter(self._readers.values())), None
        return None, (
            pb2.Error.INVALID_ARGUMENT,
            f"OpenReader named no reader_id and this plugin declares "
            f"{sorted(self._readers)}; the ambiguity is the host's to resolve (§8.2)",
        )

    def _on_metadata(self, hm) -> "pb2.PluginMsg":
        """§8.2: ``Metadata{handle}`` -> ``MetadataResult`` | ``Error``.

        A reader with no metadata document answers with the empty ``form``,
        which the IDL defines as exactly that — an answer, not a failure.  §11a
        is why this exists at all: **answer every host request**, and this arm
        was silently dropped, so the host waited out its timeout and marked the
        session suspect (§5.1).
        """
        refusal = self._no_reader(hm.id, hm.metadata.handle)
        if refusal is not None:
            return refusal
        fn = getattr(self._impl, "metadata", None)
        if fn is None:
            return pb2.PluginMsg(id=hm.id, metadata=pb2.MetadataResult(form="", text=""))
        try:
            doc = fn()
        except Exception as exc:
            return pb2.PluginMsg(id=hm.id, error=_refusal(exc, "metadata failed"))
        form, text = doc if isinstance(doc, tuple) else ("", str(doc or ""))
        return pb2.PluginMsg(id=hm.id, metadata=pb2.MetadataResult(form=form, text=text))

    def _on_close_reader(self, hm) -> "pb2.PluginMsg":
        """§8.2: ``CloseReader{handle}`` -> ``Closed`` | ``Error``, and the same
        rule — the host blocks a caller on this reply (§5.1)."""
        # Idempotent: closing a handle nothing is open under is done, not an
        # error -- the host's close is best effort and reads nothing from the
        # reply (`reader_writer_contract.md` §1.8).  And it is the NAMED
        # handle that closes: one this session has since replaced is already
        # closed, and closing "whatever is open" for it closed its successor.
        if self._impl is not None and hm.close_reader.handle != self._reader_handle:
            self._control.diagnose(
                f"CloseReader named {hm.close_reader.handle!r}; the open reader is "
                f"{self._reader_handle!r} (a replaced handle, §1.2): nothing to close"
            )
            return pb2.PluginMsg(id=hm.id, closed=pb2.Closed(handle=hm.close_reader.handle))
        exc = self._release_reader()
        if exc is not None:
            return pb2.PluginMsg(id=hm.id, error=_refusal(exc, "close failed"))
        return pb2.PluginMsg(id=hm.id, closed=pb2.Closed(handle=hm.close_reader.handle))

    def _release_reader(self) -> "Exception | None":
        """Close the open reader, if any, and forget it: what a deliberate
        `CloseReader`, the next `OpenReader` (§1.2) and the session's end
        (§1.8) have in common.  Idempotent; a close that raises is returned
        for the caller to answer or diagnose, and the reader is forgotten
        either way -- the second attempt would be on a closed file."""
        impl, self._impl = self._impl, None
        self._reader_shape = None
        self._reader_dtype = None
        self._reader_handle = ""
        self._reader_uri = ""
        closer = getattr(impl, "close", None)
        if closer is None:
            return None
        try:
            closer()
        except Exception as exc:  # noqa: BLE001 -- the reader's own bug, reported not raised
            return exc
        return None

    def _abandon_exports(self) -> None:
        """`abort()` every export still open, best effort as `AbortExport`'s
        own is: at the session's end the host is not going to finish them,
        and an exporter that never hears so leaves its partial artifact as
        whatever it was mid-chunk."""
        while self._exports:
            handle, (impl, _desc) = self._exports.popitem()
            try:
                impl.abort()
            except Exception as exc:  # noqa: BLE001 -- abort is best-effort cleanup
                self._control.diagnose(f"export {handle!r} abort at shutdown raised: {exc!r}")

    def _no_reader(self, msg_id: str, handle: str) -> "pb2.PluginMsg | None":
        """`reader_writer_contract.md` §1.8: a request naming a handle no
        reader is open under is ``NOT_FOUND`` -- a host log needs the handle,
        not a Python attribute error from a ``None`` implementation.

        A request naming a handle this session has REPLACED is the same
        answer and also a diagnostic: §1.2's handshake has the host send
        nothing for a reader once it has asked for the next, so one arriving
        is the host out of step, and a read answered from the wrong file is
        what the handshake exists to make impossible."""
        if self._impl is not None and handle == self._reader_handle:
            return None
        if self._impl is not None:
            self._control.diagnose(
                f"request named handle {handle!r}, which this session replaced; the open "
                f"reader is {self._reader_handle!r} (§1.2: the host sends nothing for a "
                "reader after asking for the next)"
            )
        return pb2.PluginMsg(
            id=msg_id,
            error=pb2.Error(
                kind=pb2.Error.NOT_FOUND, message=f"no reader is open for handle {handle!r}"
            ),
        )

    def _index_refusal(self, msg_id: str, **indices: int) -> "pb2.PluginMsg | None":
        """`reader_writer_contract.md` §1.5: an index outside the declared
        `shape_tczyx` is ``OUT_OF_BOUNDS`` before the reader class is asked --
        so the answer names the axis and the bound rather than whatever the
        class happens to raise."""
        axes = {"t": 0, "c": 1, "z": 2}
        for name, value in indices.items():
            bound = self._reader_shape[axes[name]]
            if not 0 <= int(value) < bound:
                return pb2.PluginMsg(
                    id=msg_id,
                    error=pb2.Error(
                        kind=pb2.Error.OUT_OF_BOUNDS,
                        message=f"{name}={value} is outside the declared {name} extent {bound}",
                    ),
                )
        return None

    @staticmethod
    def _level_refusal(msg_id: str, level: int) -> "pb2.PluginMsg | None":
        """`reader_writer_contract.md` §1.6: a reader declares no pyramid, so a
        level it cannot serve is ``OUT_OF_BOUNDS`` -- never level-0 data under
        a level-N question, which is an array of the wrong shape the host has
        no way to notice."""
        if level == 0:
            return None
        return pb2.PluginMsg(
            id=msg_id,
            error=pb2.Error(
                kind=pb2.Error.OUT_OF_BOUNDS,
                message=f"level {level}: this reader serves level 0 only",
            ),
        )

    def _on_read_volume(self, hm) -> "pb2.PluginMsg":
        rv = hm.read_volume
        refusal = (
            self._no_reader(hm.id, rv.handle)
            or self._level_refusal(hm.id, rv.level)
            or self._index_refusal(hm.id, t=rv.t, c=rv.c)
        )
        if refusal is not None:
            return refusal
        try:
            arr = self._impl.read_volume(rv.t, rv.c)
            _t, _c, z, y, x = self._reader_shape
            return self._reply_with_array(hm.id, np.asarray(arr), (z, y, x))
        except Exception as exc:
            return pb2.PluginMsg(id=hm.id, error=_refusal(exc, "read failed"))

    def _on_read_plane(self, hm) -> "pb2.PluginMsg":
        rp = hm.read_plane
        refusal = (
            self._no_reader(hm.id, rp.handle)
            or self._level_refusal(hm.id, rp.level)
            or self._index_refusal(hm.id, t=rp.t, c=rp.c, z=rp.z)
        )
        if refusal is not None:
            return refusal
        try:
            if hasattr(self._impl, "read_plane"):
                arr = self._impl.read_plane(rp.t, rp.z, rp.c)
            else:
                # Derive from the volume when the reader doesn't implement planes.
                arr = self._impl.read_volume(rp.t, rp.c)[rp.z]
            arr = np.asarray(arr)
            # Planes carry a leading Z=1 (the SegmentRef shape is Z,Y,X).
            if arr.ndim == 2:
                arr = arr[np.newaxis, :, :]
            _t, _c, _z, y, x = self._reader_shape
            return self._reply_with_array(hm.id, arr, (1, y, x))
        except Exception as exc:
            return pb2.PluginMsg(id=hm.id, error=_refusal(exc, "read failed"))

    def _reply_with_array(
        self, msg_id: str, arr: np.ndarray, shape_zyx: "tuple[int, int, int]"
    ) -> "pb2.PluginMsg":
        """A reader's answer, deposited into the reader arena the host granted.

        `G` is the reader's deposit and is **plugin-produced** -- §3's table
        puts it opposite `V`, which carries the same voxels the other way -- so
        the bytes stay lent until ``ReleaseLoan`` names them (§5.6), and a
        ``ReleaseDeposit`` here would be the release naming the wrong side.

        **Checked against `ReaderOpened` before anything is reserved.**  A
        reply of the wrong shape or dtype reached the host as a silently wrong
        array, and a dtype the wire has no word for raised after
        the reservation, which stayed taken, so every later read reported
        "reader arena is full".  The same values in the other byte
        order are converted: `>u2` is a u16 volume, stored differently.
        """
        expected = self._reader_dtype
        if expected is not None and arr.dtype != expected:
            if arr.dtype.newbyteorder("=") == expected:
                arr = arr.astype(expected)
            else:
                return pb2.PluginMsg(
                    id=msg_id,
                    error=pb2.Error(
                        kind=pb2.Error.INTERNAL,
                        message=f"the reader returned {arr.dtype}; its ReaderOpened "
                        f"announced {expected}",
                    ),
                )
        if tuple(arr.shape) != tuple(shape_zyx):
            return pb2.PluginMsg(
                id=msg_id,
                error=pb2.Error(
                    kind=pb2.Error.INTERNAL,
                    message=f"the reader returned shape {tuple(arr.shape)}; this read "
                    f"is {tuple(shape_zyx)} by its ReaderOpened",
                ),
            )
        arr = np.ascontiguousarray(arr)
        payload = arr.tobytes()
        try:
            segment, _region, arena = _arena(self._memory, _memory.PURPOSE_READER)
            reserved = arena.reserve(payload)
        except (NoRegion, _memory.Retiring) as exc:
            return pb2.PluginMsg(id=msg_id, error=pb2.Error(message=str(exc)))
        if reserved is None:
            pending = arena.take_shortfall()
            if pending is not None:
                self._emit(
                    pb2.PluginMsg(
                        segment_shortfall=pb2.SegmentShortfall(
                            segment_id=pending.segment_id,
                            region=pending.region,
                            wanted_bytes=pending.wanted_bytes,
                            largest_free_bytes=pending.largest_free_bytes,
                            dropped=pending.dropped,
                        )
                    )
                )
            return pb2.PluginMsg(
                id=msg_id,
                error=pb2.Error(
                    message=f"reader arena is full ({arena.largest_free} bytes free, "
                    f"{len(payload)} wanted)"
                ),
            )
        offset, length = reserved
        return pb2.PluginMsg(
            id=msg_id,
            segment=pb2.SegmentRef(
                segment_id=segment.segment_id,
                offset=offset,
                len=length,
                shape_zyx=list(arr.shape),
                dtype=_np_to_dtype()[arr.dtype],
            ),
        )


def default_plugin_id() -> str:
    """The manifest's ``id`` from the working directory, else ``"plugin"``.

    The host launches a plugin in its own directory (`manifest_schema.md`
    §2.2), so ``vizi-plugin.json`` is there to read; a bare ``serve()`` used
    to announce ``I am plugin``, a valid string naming nothing, and the guide
    had every author repeat the manifest id into the call to avoid it."""
    identity = _manifest_identity()
    return identity[0] if identity is not None else "plugin"


def serve(
    reader_id: str | None = None,
    plugin_id: str | None = None,
    *,
    purposes=None,
    max_segment_bytes: int | None = None,
    version: str | None = None,
    build: str | None = None,
) -> None:
    """Run the plugin: bind the gRPC server at the host's endpoint, open the
    doorbell, and serve until the host closes the session.

    unix: reads ``VIZI_PLUGIN_UDS`` (gRPC bind path) + ``VIZI_PLUGIN_DOORBELL_FD``
    (the inherited doorbell fd) from the env the Broker set.

    Windows: reads ``VIZI_PLUGIN_DOORBELL_ADDR`` (connect the doorbell
    to the host's localhost listener), ``VIZI_PLUGIN_TOKEN`` (sent on the
    doorbell connect, and required back in the host's ``Hello``), and
    ``VIZI_PLUGIN_PORT_FILE`` (where to publish the gRPC port this process
    bound on 127.0.0.1 — the host's readiness signal).

    ``reader_id`` selects which registered reader to serve (defaults to the
    only one registered; a commands/panels-only plugin needs none).

    ``plugin_id`` defaults to the manifest's ``id`` in the working directory
    (:func:`default_plugin_id`); pass it for a manifest that lives elsewhere.

    ``purposes`` is the set of segment purposes this plugin recognises, and it
    defaults to `shared_memory_protocol.md` §3's five — which is what this SDK
    implements.  **There is nothing to configure**: §3 settled `purpose` as
    host-defined with a receiver's recognised set being *"the set it
    implements"*, because a receiver's ability to honour a grant is a property
    of its code and not of a list it was handed.  Pass a narrower set only to
    model a plugin that implements less.

    ``version`` and ``build`` feed the handshake's identity string,
    ``Ready; I am <plugin_id> <version>; build <build>; vizi-plugin-sdk
    <version>`` (§3.1), which the host writes to the plugin's log so a
    session names the builds that spoke.  **Both are optional**: the
    version defaults to the manifest's when ``vizi-plugin.json`` in the
    working directory carries this ``plugin_id``, and the SDK names itself.
    Pass ``version`` when the manifest is elsewhere, and ``build`` for what
    only your build step knows -- a release hash, a CI build number.  The SDK
    never guesses a revision from the filesystem.  The host's own string is
    ``ctx.host.host_identity`` once the session is up.

    ``max_segment_bytes`` is §2's *"a peer MAY also refuse a grant larger than
    it is willing to map"* — local policy with no default; leave it unset to
    accept what the host budgeted.

    **A declared plugin (:func:`plugin`) also answers its command line** --
    ``--manifest[=DIR]``, ``--install[=DIR]``, ``--force``, ``--help``
    (:mod:`.package`) -- and exits instead of serving; with no arguments it
    serves, as the host launches it.  An undeclared plugin's arguments are its
    own.  A declared plugin's identity defaults to the declared one."""
    from . import package as _package

    status = _package.dispatch(sys.argv[1:])
    if status is not None:
        raise SystemExit(status)
    declared = _declare.identity()
    if declared is not None:
        if plugin_id is None:
            plugin_id = declared[0]
        if version is None and plugin_id == declared[0]:
            version = declared[1]
        stale = _package.stale_manifest_warning()
        if stale is not None:
            print(stale, file=sys.stderr, flush=True)

    try:
        import grpc
    except ImportError as exc:
        raise ImportError(_MISSING_DEPS) from exc

    # §8.2 at `1.0`: WHICH reader is opened is the HOST's to say, per
    # `OpenReader`, so several registered readers are no longer a reason to
    # refuse to start.  Naming one here narrows the served set to it, exactly
    # as a narrower `purposes` models a plugin that implements less.
    readers = dict(_REGISTERED)
    if reader_id is not None:
        readers = {reader_id: _REGISTERED[reader_id]}
    if plugin_id is None:
        plugin_id = default_plugin_id()

    servicer = _Servicer(
        readers,
        plugin_id,
        purposes,
        max_segment_bytes=max_segment_bytes,
        version=version,
        build=build,
    )

    # The doorbell is opened UNSTARTED so its listener can be ordered against
    # readiness: `doorbell_protocol.md` §11a requires a plugin to be draining
    # before it signals it is ready, because session-setup grants ride setup
    # and macOS's stock queue is 32 records.
    transport = _doorbell.open_doorbell(
        servicer.on_doorbell,
        # transport §8: the socket is released once the fault has been ACCEPTED
        # on the control plane -- never waiting on `on_outcome`, which may be
        # blocked inside a slow handler.  The same fault also arrives as the
        # session's one terminal outcome, and the control plane's one-per-event
        # latch is what keeps that from being reported twice.
        fault_sink=servicer.on_doorbell_fault,
        name="vizi-doorbell",
    )
    servicer.attach_doorbell(transport)
    if transport.transport_class == "stream":
        # The token this connection authenticated with is what `Hello`
        # must echo back, and an imposter host that reached our localhost port
        # gets its session dropped.
        servicer._expected_token = os.environ["VIZI_PLUGIN_TOKEN"]
    transport.start()

    # `control_plane_protocol.md` §9: a decoded message may be 32 MiB and an
    # inline byte field 8 MiB.  grpc-python defaults to a 4 MiB receive limit,
    # which is BELOW the protocol's own bound — so an `ExportChunk` carrying a
    # legal 8 MiB `inline` would be refused by the transport before the rule
    # that permits it ever applied.
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=2),
        options=(
            ("grpc.max_receive_message_length", _control.MAX_MESSAGE_BYTES),
            ("grpc.max_send_message_length", _control.MAX_MESSAGE_BYTES),
        ),
    )
    pb2_grpc.add_PluginServicer_to_server(servicer, server)
    if os.name == "nt":
        bound_port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        # Publish the bound port ATOMICALLY (write-then-rename) — the file's
        # appearance is the host's readiness signal, so it must never be
        # observable half-written.
        port_file = os.environ["VIZI_PLUGIN_PORT_FILE"]
        tmp = port_file + ".tmp"
        with open(tmp, "w", encoding="ascii") as f:
            f.write(str(bound_port))
        os.replace(tmp, port_file)
    else:
        server.add_insecure_port(f"unix://{os.environ['VIZI_PLUGIN_UDS']}")
        server.start()
    try:
        _serve_until_session_ends(server, servicer)
    finally:
        transport.stop()
        servicer.reads.stop()
        servicer.close()


#: How long `serve()` lets the session stream finish after the session ended
#: -- the `ShutdownReport` and whatever else the outbox still holds -- before
#: the gRPC server is stopped regardless.  Generous: a plugin lingering a few
#: seconds costs nothing, a plugin that never exits costs a host its cleanup.
SESSION_STREAM_FLUSH_S = 5.0


def _serve_until_session_ends(server, servicer: "_Servicer") -> None:
    """Block while the session lives, then stop the server so `serve()`
    returns and the process ends.

    **A plugin process serves one session and exits when it ends, however it
    ended** (`control_plane_protocol.md` §3.3): after `Shutdown`'s window,
    after a fault, and after the stream simply went away because the host
    crashed or was killed.  The host's own containment (a process group on
    unix, a job object on Windows) reaches a plugin that does not exit while
    the host is alive; a host that DIED reaches nothing on unix -- the plugin
    is in a session of its own by design -- so this end has to leave on its
    own, and every SDK is asked to.  The reference used to wait on
    ``server.wait_for_termination()``, which nothing ever called: a plugin
    outlived a dead host indefinitely, holding its mapped segments.

    The stop waits ``SESSION_STREAM_FLUSH_S`` for the stream to carry what is
    still queued (the `ShutdownReport` is the last thing on it), then ends
    whatever is left.
    """
    servicer.session_ended.wait()
    server.stop(grace=SESSION_STREAM_FLUSH_S).wait()
