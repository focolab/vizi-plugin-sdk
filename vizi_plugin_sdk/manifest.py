"""``vizi-plugin.json`` — parsed and validated as data: the REFERENCE parser.

**``docs/spec/manifest_schema.md`` is the normative description of what this
module accepts**, and its fixtures are run against :func:`parse_manifest`.  A
rule added here is added there in the same change, with a fixture that
exercises it.

It ships in the SDK because the host scans with it: a plugin that
declares its interface in code (:mod:`.declare`) has the manifest it generates
checked by exactly the rules the host will apply, before anything is written.

The host reads every manifest in its plugins directory **without launching any
plugin process** (npe2/VS Code's lazy-discovery lesson; never npe1's
import-to-discover). A manifest declares identity, the minimum host protocol it
needs (``engines.vizi``), the launch command, its contributions (readers,
commands, panels, exporters, overlays, tools), its assets, and its requested
permissions for host-mediated capabilities.

This module is pure data and standard library only — no transport, no
subprocess, no gRPC — so it imports on every platform and in an environment
without the plugin runtime's dependencies. Within a major the protocol is
append-only, so a plugin is compatible when the host's major matches and the
host's minor is at least the declared minimum.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from .doorbell import MOD_ALT, MOD_CTRL, MOD_META, MOD_SHIFT, PROTOCOL_MAJOR, PROTOCOL_MINOR

#: `control_plane_protocol.md` §8.10's `MouseButton`, by the name a manifest
#: writes.  ``0`` is *no mouse button* -- a key-only binding -- and is what an
#: absent ``button`` means.
BUTTONS: dict[str, int] = {"left": 1, "right": 2, "middle": 3}

#: §8.10's modifier set, which is `doorbell_E_input.md` §3's and the SAME BITS.
#:
#: **Imported rather than restated.**  An author writes one binding and reads it
#: back off an ``E`` record; two encodings for one concept is a conversion
#: nobody remembers is there, and two *definitions* of one encoding is the
#: drift that makes the conversion wrong.
MODIFIERS: dict[str, int] = {
    "shift": MOD_SHIFT,
    "ctrl": MOD_CTRL,
    "alt": MOD_ALT,
    "meta": MOD_META,
}

#: The file each plugin ships at its root; the Broker scans for these.
MANIFEST_FILENAME = "vizi-plugin.json"

#: Permission strings the host ENFORCES a grant for.  ``data_access``
#: gates ``get_view_state`` / ``read_data_volume`` / ``get_rois`` and
#: runtime asset requests; ``contribute_rois`` gates native
#: annotation ingestion. Declared = granted (the user sees it in the Plugin
#: Manager); there is no per-plugin consent prompt.
#:
#: Each maps to the protocol minor that introduced it.  A name is honored only
#: when the manifest's ``engines.vizi`` minor is at least that; otherwise it is
#: warned about and ignored like an unknown name, so a plugin using a name a
#: later 1.x added says so in ``engines.vizi``.  Append-only within 1.x.
PERMISSION_INTRODUCED: dict[str, int] = {"contribute_rois": 0, "data_access": 0}
KNOWN_PERMISSIONS: frozenset[str] = frozenset(PERMISSION_INTRODUCED)

#: Host pushes a session can ASK to be arranged, which it would not otherwise
#: receive.  ``"epochs"`` puts a session on the `EpochChange` chain
#: without it declaring a tool: `control_plane_protocol.md` §8.7 arranges the
#: epoch for tool sessions alone, so a panel-only plugin drawing a per-frame
#: overlay could learn the frame on screen only by polling `ViewState`.
#:
#: A SUBSCRIPTION IS NOT A PERMISSION and does not live in that list: a
#: permission says what a session may *do*, and every existing manifest
#: declaring ``data_access`` would have been opted in by conflating them --
#: then held to §8.10's acknowledgement contract, with a 10 s deadline and
#: escalation for a plugin that never acks.  A named set rather than a boolean
#: per push, because vizi has never had an explicit subscribe: §1a speaks of
#: "on subscribing", and the host inferred one from ``manifest.tools``.
#:
#: Each maps to the protocol minor that introduced it, as permissions do.
SUBSCRIPTION_INTRODUCED: dict[str, int] = {"epochs": 0, "view": 0, "regions": 0}
KNOWN_SUBSCRIPTIONS: frozenset[str] = frozenset(SUBSCRIPTION_INTRODUCED)
#: The subscriptions whose pushes carry what ``data_access`` gates:
#: arranged only for a session that declares it.
DATA_SUBSCRIPTIONS: frozenset[str] = frozenset({"view", "regions"})

#: The one ``manifest_version`` there is.  A 1.x addition raises
#: ``engines.vizi``, never ``manifest_version``: where a field is and what it
#: means are settled by the protocol minor that introduced it.
SUPPORTED_MANIFEST_VERSION = 1

#: The plugin id (§2): lower-case reverse-DNS with at least one dot, 3 to 64
#: characters.  Lower case and the closed alphabet make the folder an id
#: installs to (§2.3) the id itself, so two ids never share a folder on a
#: case-insensitive file system.
_ID_RE = re.compile(r"[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*)+")
_ID_LENGTHS = range(3, 65)
#: The names Windows refuses as a file or folder name, in any case and with any
#: extension.  An id's first part may not be one (§1): the id is the folder
#: an installer names, and ``con.example`` is the device ``CON`` there.
WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"} | {f"{stem}{n}" for stem in ("com", "lpt") for n in range(10)}
)

#: ``min_vizi`` (§2): a PEP 440 version in its normalized public form without
#: an epoch or a local label -- release numbers, then optionally ``aN``/``bN``/
#: ``rcN``, ``.postN`` and ``.devN`` (``1.0.0``, ``1.0rc1``, ``1.2.post3``).
_MIN_VIZI_RE = re.compile(r"[0-9]+(\.[0-9]+)*((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?")


def valid_plugin_id(plugin_id: str) -> bool:
    """Is ``plugin_id`` a plugin id (§1): lower-case reverse-DNS, 3 to 64 characters,
    its first part not a Windows device name?"""
    return (
        _device_name(plugin_id) is None
        and len(plugin_id) in _ID_LENGTHS
        and _ID_RE.fullmatch(plugin_id) is not None
    )


def _device_name(plugin_id: str) -> "str | None":
    """``plugin_id``'s first part when it is a Windows device name in any case, else None."""
    first = plugin_id.split(".", 1)[0]
    return first if first.lower() in WINDOWS_DEVICE_NAMES else None


def plugin_id_message(plugin_id: str) -> str:
    """The one sentence for an id that is not a plugin id, said the same by every SDK."""
    part = _device_name(plugin_id)
    if part is not None:
        return (
            f"id {plugin_id!r} is not a plugin id: "
            f"its first part {part!r} is a Windows device name"
        )
    return (
        f"id {plugin_id!r} is not a plugin id: lower-case reverse-DNS, 3 to 64 characters, "
        r"matching ^[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*)+$"
    )


#: Host plugin-protocol version — MIRRORS the host transport and
#: ``server.PROTOCOL_*``. A major bump breaks and a minor bump does not
#: (`control_plane_protocol.md` §3.2); bump in lockstep with both.
HOST_API_MAJOR = PROTOCOL_MAJOR
HOST_API_MINOR = PROTOCOL_MINOR


class ManifestError(Exception):
    """A manifest is missing, malformed, or declares an unsupported schema."""


def engines_message(
    plugin: str,
    version: str,
    engines: str,
    host: tuple[int, int] = (HOST_API_MAJOR, HOST_API_MINOR),
) -> str:
    """The one sentence for a plugin this host's protocol cannot run -- said
    the same by the plugin scan, a catalog install and a launch."""
    return f"{plugin} {version} needs vizi protocol {engines}; this vizi speaks {host[0]}.{host[1]}"


class EnginesIncompatible(ManifestError):
    """The manifest's ``engines.vizi`` is not one this host's protocol runs.

    Raised as soon as ``engines`` is read -- before anything else in the
    manifest is -- so that a plugin built for a newer 1.x is refused for
    THAT, not for the first thing a newer 1.x added (a widget kind this host
    has never heard of).  ``engines`` is read first in every
    ``manifest_version``: where it is and what it looks like are frozen."""

    def __init__(self, plugin_id: str, name: str, version: str, engines: str) -> None:
        self.plugin_id, self.name, self.version, self.engines = plugin_id, name, version, engines
        super().__init__(engines_message(name or plugin_id, version, engines))


@dataclass(frozen=True)
class ReaderContribution:
    """A ``readers`` contribution: "I open files matching these extensions."

    The plugin is launched lazily when the user opens a path whose suffix
    matches; ``id`` addresses the reader within the plugin (a plugin may
    contribute several).  ``fallback_extensions`` are suffixes it can read but
    should not be chosen for unasked: it is offered, never picked over a reader
    that declared the suffix in ``extensions``.
    """

    id: str
    name: str
    extensions: tuple[str, ...]
    fallback_extensions: tuple[str, ...] = ()

    def match(self, path: Path | str) -> "tuple[str, bool] | None":
        """``(suffix, fallback)`` for the longest declared suffix ``path``'s
        name ends with, case-insensitively, or ``None``.  Longest, so
        ``.ome.tif`` is a suffix of its own and not only ``.tif``."""
        name = Path(path).name.lower()
        best: "tuple[str, bool] | None" = None
        for suffix, fallback in [(e, False) for e in self.extensions] + [
            (e, True) for e in self.fallback_extensions
        ]:
            if len(name) > len(suffix) and name.endswith(suffix):
                if best is None or len(suffix) > len(best[0]):
                    best = (suffix, fallback)
        return best

    def handles(self, path: Path | str) -> bool:
        """True if this reader claims ``path`` by a declared suffix."""
        return self.match(path) is not None


#: A label's `text_format`: plain unless the author asks.
TEXT_FORMATS = ("plain", "rich")

#: Widget kinds the declarative-panel vocabulary understands (Phase 2).
#: Append-only: new kinds take new names; the host rejects unknown kinds at
#: parse time so a stale host fails a too-new manifest loudly.
PANEL_WIDGET_KINDS = frozenset(
    {
        "label",
        "button",
        "slider",
        "dropdown",
        "checkbox",
        "line_edit",
        "image",
        "progress",
        "row",
        "column",
        # Host pickers: the choices are the host's, not the manifest's.
        "channel_picker",
        "color_picker",
        "colormap_picker",
        "roi_picker",
        # Rows the plugin supplies at runtime, from a declared template.
        "list",
        # Rows of typed cells under declared columns, shown model/view.
        "table",
    }
)
_PICKER_KINDS = frozenset({"channel_picker", "color_picker", "colormap_picker", "roi_picker"})
_INTERACTIVE_KINDS = frozenset(
    {"button", "slider", "dropdown", "checkbox", "line_edit"} | _PICKER_KINDS
)
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
#: Set-view (control_plane_protocol.md §8.15): the closed list of what a
#: plugin may move, declared per panel and per tool.  Append-only.
VIEW_TARGETS: tuple[str, ...] = (
    "t",
    "z",
    "select_region",
    "frame_region",
    "look_at",
    "orbit_point",
)
#: A `roi_picker`'s `filter`: every region, or only this plugin's own.
_ROI_PICKER_FILTERS = ("all", "mine")
_CONTAINER_KINDS = frozenset({"row", "column"})
#: A list's row template holds leaf widgets only: no list, row or column.
_NOT_IN_A_LIST_ROW = frozenset({"list", "table", "row", "column"})
#: The kinds whose rows a person may select.
_ROW_KINDS = frozenset({"list", "table"})
#: `list` bounds: `max_rows` defaults to and may not pass these, so the
#: host's coalescing key space stays fixed before the plugin starts (§5.4).
LIST_DEFAULT_MAX_ROWS = 256
LIST_MAX_ROWS = 1024
LIST_DEFAULT_VISIBLE_ROWS = 8
#: The most widgets one list may realize, ``max_rows`` × its row's cells:
#: each row is built whole on the host's UI thread, and the
#: per-panel widget cap counts a row's template only once.  The same bound
#: holds for ALL of a panel's lists together: one
#: PanelPatch, or the held replay when the panel opens, may fill every list
#: in one go on the UI thread -- 28 lists at the per-list cap was ~16 s.
LIST_MAX_CELLS = 4096
#: The `min_vizi` a declared plugin's manifest states unless it says another:
#: the first vizi release that speaks protocol 1.0 (§2.4).
DEFAULT_MIN_VIZI = "1.0.0"
#: `table` bounds.  Its rows are a model, not widgets, so they do not
#: count against `LIST_MAX_CELLS`; a whole-table statement past these is
#: refused whole, and one `PanelPatch` carrying a table op is at most
#: `TABLE_MAX_PATCH_BYTES` encoded.
TABLE_MAX_ROWS = 100_000
TABLE_MAX_COLUMNS = 32
TABLE_MAX_PATCH_BYTES = 1 << 20
TABLE_DEFAULT_PRECISION = 3
#: Rows (and deleted keys) one table op carries: half §9's 65,536 bound on a
#: repeated field, so a run of short keys stays well inside it whatever its
#: bytes.
TABLE_MAX_ROWS_PER_MESSAGE = 32_768
TABLE_COLUMN_TYPES = ("string", "number", "bool")
#: A table row key, like a list's: non-empty and at most this long.
TABLE_MAX_KEY_CHARS = 128
#: Untrusted-manifest DoS guards.
_PANEL_MAX_DEPTH = 8
_PANEL_MAX_WIDGETS = 256
_PANEL_MENU_LABEL_MAX_CHARS = 128
#: Menus a plugin command or tool may target (the Tools menu only).
_COMMAND_MENUS = frozenset({"tools"})
#: A pane button's tooltip is a sentence, like a menu label.
_PANE_TOOLTIP_MAX_CHARS = 128
#: A pane icon is a small file the host reads at load: a vector or a bitmap,
#: and never big enough that a plugin directory doubles as an image store.
_PANE_ICON_SUFFIXES = frozenset({".svg", ".png"})
_PANE_ICON_MAX_BYTES = 64 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#: `doorbell_E_input.md` §1: ``action`` is a u16 on the wire and 0 names the
#: primary gesture, so a tool's actions are indexed 1..N.  N is capped well
#: under the field because a pane strip with more than this many buttons for
#: ONE tool is not a pane strip.
_TOOL_MAX_ACTIONS = 64


@dataclass(frozen=True)
class CommandContribution:
    """A ``commands`` contribution: an addressable behavior the plugin runs.

    Registered host-side as ``Command(source=<plugin id>)`` on the window's
    registry; the Tools menu, a manifest keybinding, and panel widgets all
    reference it by ``id``.  ``default_keybinding`` is an unvalidated string
    here (this module stays Qt-free); the host resolves it and the plugin
    LOSES any conflict with a core binding.  Set ``menu`` to ``None`` for a
    panel-backing command that stays registered but is not listed."""

    id: str
    title: str
    description: str = ""
    default_keybinding: str | None = None
    menu: str | None = "tools"
    #: A momentary pane button that invokes the command (§8.10); absent means
    #: menu only.
    pane: "PaneButton | None" = None


@dataclass(frozen=True)
class TableColumn:
    """One ``table`` column: a cell of each row, in declaration order."""

    id: str
    header: str
    type: str = "string"  # one of TABLE_COLUMN_TYPES
    #: number: decimals shown.
    precision: int = TABLE_DEFAULT_PRECISION
    #: number: a unit shown in the header, after its text.
    unit: str = ""


@dataclass(frozen=True)
class PanelWidget:
    """One node of a declared panel tree.  Which fields apply depends on
    ``kind`` (see ``PANEL_WIDGET_KINDS``); parse-time validation enforces the
    per-kind requirements so the host-side builder can trust the shape."""

    kind: str
    widget_id: str
    text: str = ""  # label / button caption
    #: label: "plain" (the default) shows its text as written; "rich" lets Qt
    #: read markup in it.  Plain because a label often shows what a plugin
    #: relays from data -- a region's name -- and markup there is an accident.
    text_format: str = "plain"
    command: str | None = None  # interactive kinds: the command to fire
    minimum: float = 0.0  # slider
    maximum: float = 1.0  # slider
    step: float = 1.0  # slider
    #: slider: the manifest declared ``step`` (a float slider with none has
    #: 1000 positions, not one per 1.0 of its range).
    has_step: bool = False
    value: Any = None  # slider/checkbox/dropdown/line_edit/progress initial value
    indeterminate: bool = False  # progress: length not known in advance
    is_float: bool = False  # slider: float vs int
    options: tuple[str, ...] = ()  # dropdown
    placeholder: str = ""  # line_edit
    children: tuple["PanelWidget", ...] = field(default=())  # row/column
    multiple: bool = False  # channel_picker: several channels; table: several rows
    template: tuple["PanelWidget", ...] = field(default=())  # list: one row's widgets
    max_rows: int = LIST_DEFAULT_MAX_ROWS  # list
    visible_rows: int = LIST_DEFAULT_VISIBLE_ROWS  # list
    selectable: bool = False  # list, table: rows may be selected, firing `command`
    columns: tuple[TableColumn, ...] = field(default=())  # table
    sortable: bool = False  # table: a header click sorts, on the host's side
    #: Set-view: this widget's interactions mint a cause.
    moves_view: bool = False
    follows_selection: bool = False  # roi_picker: shows, and sets, vizi's selection
    filter: str = "all"  # roi_picker: "all" regions, or only this plugin's "mine"


@dataclass(frozen=True)
class PanelContribution:
    """A ``panels`` contribution: a declarative side-panel the HOST builds as
    real Qt widgets in a StageDock ("plugins propose, host disposes")."""

    id: str
    title: str
    root: PanelWidget
    menu_label: str | None = None
    #: Set-view: what this panel's `moves_view` widgets may move.
    view_targets: tuple[str, ...] = ()
    #: Follow (§8.15): what a Follow started from this panel may drive.
    follow_targets: tuple[str, ...] = ()

    def widget_count(self) -> int:
        """Declared widgets in this panel, root included.

        The host sizes the session's coalescing queue from this
        (``control_plane_protocol.md`` §5.4: the key space is structural, so
        overflow is unreachable rather than recoverable).  Parse-time
        validation caps it at ``_PANEL_MAX_WIDGETS`` **per panel** and does not
        cap panels, so the aggregate is a manifest's to decide and the host's
        to read — never a constant."""

        def walk(widget: PanelWidget) -> int:
            # A list's cells are widgets too, one set per row it may hold.
            cells = widget.max_rows * sum(walk(cell) for cell in widget.template)
            return 1 + cells + sum(walk(child) for child in widget.children)

        return walk(self.root)


@dataclass(frozen=True)
class OverlayContribution:
    """An ``overlays`` contribution: "I draw into the views".

    Declared so the Manager can attribute overlays, the overlay segment is
    granted only to overlay-declaring plugins, and pushes naming undeclared
    ids are rejected.  ``segment_mib`` sizes the plugin-RW overlay segment
    (geometry scale is plugin-specific; the default suits ~10^5 f64 points).

    A CHUNKED overlay (`control_plane_protocol.md` §5.7) carries one label
    volume per (level, t) that the host asks for, instead of a draw list; its
    arena is sized by the host from the open dataset, so ``segment_mib`` is
    refused on it.  ``time`` says whether one volume per level serves every
    timepoint (``"static"``) or the host wants one per t (``"per_t"``)."""

    id: str
    name: str
    segment_mib: int = 32
    chunked: bool = False
    time: str = "per_t"


OVERLAY_TIME_KINDS = frozenset({"per_t", "static"})


@dataclass(frozen=True)
class Trigger:
    """One request to ENGAGE a tool (`control_plane_protocol.md` §8.10): a key
    chord or a mouse chord, and how it holds.

    A request, never an assignment: the host owns the namespace and either
    honors this or reports why not.  Two plugins asking for the same trigger
    leaves **neither** bound, and a trigger the host itself uses is refused
    with the host keeping it.

    **Written as NAMES and stored in the wire's neutral encoding.**  ``button``
    is ``"left" | "right" | "middle"``, ``modifiers`` a list from
    ``"shift" | "ctrl" | "meta"`` (``"alt"`` is refused on a mouse trigger:
    most window managers take Alt+drag before the application sees it), and
    ``key`` a portable key name (``"Ctrl+J"``).  A manifest never carries a
    platform code: that is `doorbell_E_input.md` §3's rule -- *"a plugin never
    sees a platform-specific code"* -- and it binds the document a third party
    writes as much as the wire, since an author writing ``"button": 4`` is
    looking up ``Qt::MiddleButton``.

    **A key holds either way; a mouse chord is momentary.**  ``"toggle"`` is
    the sticky pane button by keyboard -- press once to engage, again to
    disengage; ``"momentary"`` engages the tool only while the chord is held
    and hands the pointer back to whatever was engaged before.  A mouse chord
    is down or up and nothing else, so it is always momentary, and the bare
    primary button is never a trigger: it means whatever the ENGAGED tool
    means, which is the whole point of engaging one.
    """

    #: A portable key name, ``""`` for a mouse trigger.
    key: str = ""
    #: `MouseButton`: 0 none, 1 left, 2 right, 3 middle.
    button: int = 0
    #: `doorbell_E_input.md` §3's bits: SHIFT 0x01, CTRL 0x02, ALT 0x04, META 0x08.
    mods: int = 0
    hold: str = "toggle"  # "toggle" | "momentary"


@dataclass(frozen=True)
class PaneButton:
    """A button on the control pane (§8.10), declared on a tool, an action or a
    command.  ``icon`` is a path relative to the manifest directory (``.svg``
    or ``.png``, at most ``_PANE_ICON_MAX_BYTES``), checked at parse when the
    directory is known; the host renders it at registration and REFUSES the
    plugin if it cannot -- a pane with half its buttons blank is worse than no
    plugin (a tool installs whole or not at all).

    ``hold`` is meaningful on a TOOL's button: ``"toggle"`` is the sticky
    button (tap on, tap off); ``"momentary"`` engages the tool for one gesture
    and disengages after it.  An action's or a command's button is momentary
    by nature -- it fires once on click -- so ``hold`` there is fixed.
    """

    icon: str
    tooltip: str = ""
    hold: str = "toggle"  # "toggle" | "momentary"


@dataclass(frozen=True)
class ToolAction:
    """Something the ENGAGED tool can be told to do, from a key or a pane
    button, WITHOUT ending its gesture (§8.10; `doorbell_E_input.md` §1's
    ``action``).

    The standard modality: the mouse is the target and a
    key is the fire -- a node placed on a polygon mid-drag.  The host delivers
    it as a ``FIRE`` record naming this action's 1-based index and carrying
    the pointer's current sample; an open gesture is neither closed nor
    disturbed.  An action's key is live only while its tool is engaged, so two
    plugins may both use ``"N"`` -- only a key the host or another plugin's
    TRIGGER holds globally is refused.
    """

    id: str
    name: str
    key: str = ""
    pane: PaneButton | None = None


@dataclass(frozen=True)
class ToolContribution:
    """A ``tools`` contribution: a live interaction mode.

    Registered as a CHECKABLE Command; ``menu=None`` hides its Tools-menu
    action without affecting programmatic activation.  While engaged the tool
    owns the primary pointer in its target ``views`` and the host forwards
    press/drag/release as voxel-space doorbell events; its ``actions`` reach it
    the same way, as ``FIRE`` records.

    ``triggers``, ``actions`` and ``pane`` are the author's REQUEST and are
    absent in a ``v0`` manifest -- and absent is not a defect: the tool is
    engaged from the Tools menu.
    """

    id: str
    name: str
    views: tuple[str, ...] = ("2d", "3d")
    menu: str | None = "tools"
    mode: str = "gesture"  # "fire" | "gesture"
    cancel_on_camera_move: bool = True
    #: Whether the tool wants the host's surface pick on a 3D opening record
    #: (`E` §3).  True, the default, asks for it.
    #: False saves the host a trace per press for a tool that resolves its own
    #: anchor from the ray.  It is a REQUEST about what the host computes, not
    #: a promise about what arrives: a tool takes a record with or without a
    #: pick whichever it declared.
    wants_pick: bool = True
    triggers: tuple[Trigger, ...] = ()
    actions: tuple[ToolAction, ...] = ()
    pane: PaneButton | None = None
    #: Set-view: what a FIRE of this tool may move; empty = nothing.
    view_targets: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExporterContribution:
    """An ``exporters`` contribution: "I write artifact format Y".

    ``id``/``name`` label the entry in the Export Data dialog's format
    selector; ``extension`` drives the save-dialog filename filter.  The
    voxels reach the plugin through the export data plane (the reversed
    reader deposit — see the exporters section of ``vizi_plugin.proto``)."""

    id: str
    name: str
    extension: str


#: §3.7: a stage parameter's ``type``, and the fields each one takes beside
#: ``key``, ``name``, ``type`` and ``value``.  The type fixes the wire
#: ``Value`` kind: float ``f``, int ``i``, bool ``b``, choice and string ``s``.
STAGE_PARAM_FIELDS: dict[str, tuple[str, ...]] = {
    "float": ("min", "max"),
    "int": ("min", "max"),
    "bool": (),
    "choice": ("options",),
    "string": ("max_length",),
}

#: §3.7: the most UTF-8 bytes a ``string`` param may hold, and the largest
#: ``max_length`` it may declare.  Saved output is keyed by its params, so an
#: unbounded one would make that key unbounded too.
STRING_PARAM_MAX_BYTES = 4096


@dataclass(frozen=True)
class StageParam:
    """One declared stage parameter (§3.7).  ``value`` is the default."""

    key: str
    name: str
    type: str
    value: "float | int | bool | str"
    min: "float | int | None" = None
    max: "float | int | None" = None
    options: tuple[str, ...] = ()
    #: ``string`` only: the most UTF-8 bytes a value may hold.
    max_length: int = STRING_PARAM_MAX_BYTES


@dataclass(frozen=True)
class StageContribution:
    """A ``stages`` contribution (§3.7, ``processing_stages.md``): a stage
    that edits a volume in place in a chain.  Where it runs is the host's, and
    with what ``params`` is decided per position."""

    id: str
    name: str
    params: tuple[StageParam, ...] = ()


@dataclass(frozen=True)
class RegionTypeContribution:
    """A ``region_types`` contribution (§3.8): a ``Roi.type_name`` whose
    regions this plugin shapes -- the host asks it where every vertex goes
    when the person drags one of their handles."""

    id: str
    name: str


#: ``Roi.type_name`` values the host enacts itself; a plugin may not claim one.
BUILTIN_REGION_TYPES: frozenset[str] = frozenset({"box", "ellipsoid", "cylinder", "surfq6"})
#: Reserved for the host's future built-ins, so a plugin's type never changes
#: meaning under it.
RESERVED_REGION_TYPE_PREFIX = "vizi."


@dataclass(frozen=True)
class AssetContribution:
    """An immutable model or data file provisioned by the host.

    ``source_kind`` is ``manual`` (the user imports a local file) or
    ``https`` (the Manager may download ``source_url``). Every publication is
    accepted only after its exact size and SHA-256 match the declaration.
    """

    id: str
    name: str
    filename: str
    size: int
    sha256: str
    source_kind: str
    source_url: str | None = None


@dataclass(frozen=True)
class PluginManifest:
    """A validated ``vizi-plugin.json`` v0.

    ``requires`` is the parsed ``engines.vizi`` minimum as ``(major, minor)``.
    ``source_dir`` is where the manifest was found — the Broker resolves the
    launch command and the plugin's working directory against it.
    """

    id: str
    name: str
    version: str
    requires: tuple[int, int]
    command: tuple[str, ...]
    readers: tuple[ReaderContribution, ...] = ()
    commands: tuple[CommandContribution, ...] = ()
    panels: tuple[PanelContribution, ...] = ()
    exporters: tuple[ExporterContribution, ...] = ()
    overlays: tuple[OverlayContribution, ...] = ()
    tools: tuple[ToolContribution, ...] = ()
    stages: tuple[StageContribution, ...] = ()
    region_types: tuple[RegionTypeContribution, ...] = ()
    assets: tuple[AssetContribution, ...] = ()
    permissions: tuple[str, ...] = ()
    subscriptions: tuple[str, ...] = ()
    source_dir: Path | None = None
    engines_vizi: str = ""
    #: ``entry.startup_s``: how long the plugin expects to take to start, in
    #: seconds.  Advisory -- a host may honor, cap or ignore it.
    startup_s: float | None = None
    #: ``min_vizi``: the oldest vizi release the plugin runs in, a PEP 440
    #: version, or ``None`` when the manifest does not say.
    min_vizi: str | None = None
    #: ``sdk_version``: the SDK release that built the plugin, or ``None``.
    sdk_version: str | None = None

    def is_host_compatible(
        self, host_major: int = HOST_API_MAJOR, host_minor: int = HOST_API_MINOR
    ) -> bool:
        """True if this host can run the plugin: same major, host minor ≥ declared.

        Append-only-within-major means a host can always serve a plugin built
        against an *older* minor of the same major, never a newer major.
        """
        req_major, req_minor = self.requires
        return host_major == req_major and host_minor >= req_minor

    def reader_for(self, path: Path | str) -> ReaderContribution | None:
        """The first reader contribution that claims ``path``, or ``None``."""
        return next((r for r in self.readers if r.handles(path)), None)

    def wants_epochs(self) -> bool:
        """Should this session be on the `EpochChange` chain (§8.7)?

        A tool session is, whether or not it says so: its `E` records are
        uninterpretable without an epoch, so the declaration would be a
        formality that an older manifest could not have made.
        """
        return bool(self.tools) or "epochs" in self.subscriptions

    def subscribes(self, name: str) -> bool:
        """Is ``name`` (``"view"``, ``"regions"``) arranged for this session?
        Declared, and for a data subscription, permitted."""
        if name not in self.subscriptions:
            return False
        return name not in DATA_SUBSCRIPTIONS or "data_access" in self.permissions


_SINT64_MIN, _SINT64_MAX = -(2**63), 2**63 - 1


def _finite(v) -> "float | None":
    """``v`` (already checked numeric, not bool) as a finite float, or
    ``None``.  JSON integers are unbounded and ``float()`` / ``math.isfinite``
    raise ``OverflowError`` past ~1e308, which ``Broker.scan`` does not catch:
    one 400-digit number took every plugin in the folder down."""
    try:
        f = float(v)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def _require(data: dict[str, Any], key: str, types: type | tuple[type, ...], ctx: str) -> Any:
    if key not in data:
        raise ManifestError(f"{ctx}: missing required field {key!r}")
    value = data[key]
    wants = types if isinstance(types, tuple) else (types,)
    # `bool` is an `int` subclass: `"manifest_version": true` read as 1 here,
    # where every other SDK refuses it.
    if not isinstance(value, types) or (
        isinstance(value, bool) and bool not in wants and int in wants
    ):
        names = types.__name__ if isinstance(types, type) else "/".join(t.__name__ for t in types)
        raise ManifestError(f"{ctx}: field {key!r} must be {names}, got {type(value).__name__}")
    return value


#: `manifest_schema.md` §2.1: at most one of ``>=``, ``^`` or ``~``, then
#: ``MAJOR``, ``MAJOR.MINOR`` or ``MAJOR.x``, each part ASCII digits.  Nothing
#: else -- no whitespace, sign, underscore, other digits, second prefix, third
#: component, ``*`` or ``X`` -- because Python's ``int()`` takes several of
#: those, and a host reading ``"1.0_0"`` or ``"١.٠"`` as 1.0 lists a plugin
#: its own SDK then refuses at startup.  The Go, Rust and Java SDKs' grammar.
_ENGINES_RE = re.compile(r"(?:>=|\^|~)?([0-9]+)(?:\.([0-9]+|x))?", re.ASCII)
#: The same shape with a '-' on a component: refused with §2.1's own words.
_ENGINES_NEGATIVE_RE = re.compile(r"(?:>=|\^|~)?-?[0-9]+(?:\.(?:-?[0-9]+|x))?", re.ASCII)
#: A component past a u32 is refused, never wrapped.
_ENGINES_COMPONENT_MAX = 0xFFFFFFFF


def _parse_engine_min(spec: str, ctx: str) -> tuple[int, int]:
    """Parse ``engines.vizi`` (``"1.0"``, ``"1"``, ``"1.x"``, ``"^1.0"``) →
    ``(major, minor)``; ``"1"`` and ``"1.x"`` are minor 0.  The value is a
    *minimum*, and within-major append-only makes the prefix immaterial."""
    match = _ENGINES_RE.fullmatch(spec)
    if match is None:
        if "-" in spec and _ENGINES_NEGATIVE_RE.fullmatch(spec):
            raise ManifestError(f"{ctx}: engines.vizi {spec!r} has a negative component")
        raise ManifestError(f"{ctx}: engines.vizi {spec!r} is not a version")
    minor_part = match.group(2)
    major = int(match.group(1))
    minor = int(minor_part) if minor_part not in (None, "x") else 0
    if major > _ENGINES_COMPONENT_MAX or minor > _ENGINES_COMPONENT_MAX:
        raise ManifestError(f"{ctx}: engines.vizi {spec!r} is not a version")
    return major, minor


def _parse_readers(contributions: dict[str, Any], ctx: str) -> tuple[ReaderContribution, ...]:
    raw = contributions.get("readers", [])
    if not isinstance(raw, list):
        raise ManifestError(f"{ctx}: contributions.readers must be a list")
    readers: list[ReaderContribution] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        rctx = f"{ctx}: readers[{i}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{rctx}: must be an object")
        rid = _require(entry, "id", str, rctx)
        if not rid.strip():
            raise ManifestError(f"{rctx}: id must be non-empty")
        # `OpenReader` routes on `reader_id` (`control_plane_protocol.md`
        # §8.2), so two readers with one id are unaddressable -- the same
        # rule every other kind already had.
        if rid in seen:
            raise ManifestError(f"{rctx}: duplicate reader id {rid!r}")
        seen.add(rid)
        exts = _suffixes(entry.get("extensions", []), "extensions", rctx)
        fallback = _suffixes(entry.get("fallback_extensions", []), "fallback_extensions", rctx)
        if not exts and not fallback:
            raise ManifestError(f"{rctx}: extensions must be non-empty")
        both = sorted(set(exts) & set(fallback))
        if both:
            raise ManifestError(
                f"{rctx}: a suffix is in both extensions and fallback_extensions: {both}"
            )
        readers.append(
            ReaderContribution(
                id=rid,
                name=str(entry.get("name", rid)),
                extensions=exts,
                fallback_extensions=fallback,
            )
        )
    return tuple(readers)


#: What a suffix may not consist of alone (§3.1): ASCII
#: whitespace, so every language's notion of "blank" agrees.
_ASCII_SPACE = " \t\n\r\v\f"


def _suffixes(raw: Any, key: str, rctx: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ManifestError(f"{rctx}: {key} must be a list")
    out = []
    for ext in raw:
        if not isinstance(ext, str) or not ext.lstrip(".").strip(_ASCII_SPACE):
            if key == "extensions":
                raise ManifestError(f"{rctx}: each extension must be a non-empty string")
            raise ManifestError(f"{rctx}: each fallback extension must be a non-empty string")
        # Normalize: lowercase, ensure a leading dot so suffix-matching works.
        out.append(("." + ext.lstrip(".")).lower())
    return tuple(out)


_ASSET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")


def _parse_assets(data: dict[str, Any], ctx: str) -> tuple[AssetContribution, ...]:
    raw = data.get("assets", [])
    if not isinstance(raw, list):
        raise ManifestError(f"{ctx}: assets must be a list")
    assets: list[AssetContribution] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        actx = f"{ctx}: assets[{i}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{actx}: must be an object")
        asset_id = _require(entry, "id", str, actx)
        if not _ASSET_ID.fullmatch(asset_id):
            raise ManifestError(
                f"{actx}: id must start with an alphanumeric and contain only "
                "letters, digits, '.', '_', or '-'"
            )
        if asset_id in seen:
            raise ManifestError(f"{actx}: duplicate asset id {asset_id!r}")
        seen.add(asset_id)
        filename = _require(entry, "filename", str, actx)
        if (
            not filename
            or filename.startswith(".")
            or filename in (".", "..")
            or "/" in filename
            or "\\" in filename
        ):
            raise ManifestError(f"{actx}: filename must be one plain file name")
        size = _require(entry, "size", int, actx)
        if isinstance(size, bool) or size < 1:
            raise ManifestError(f"{actx}: size must be a positive integer")
        sha256 = _require(entry, "sha256", str, actx)
        if not _SHA256.fullmatch(sha256):
            raise ManifestError(f"{actx}: sha256 must be exactly 64 hexadecimal characters")
        source = _require(entry, "source", dict, actx)
        kind = _require(source, "kind", str, f"{actx}: source")
        if kind not in ("manual", "https"):
            raise ManifestError(f"{actx}: source.kind must be 'manual' or 'https'")
        url = source.get("url")
        if kind == "https":
            if not isinstance(url, str):
                raise ManifestError(f"{actx}: HTTPS source needs a string url")
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ManifestError(f"{actx}: source.url must be an HTTPS URL without credentials")
        elif url is not None:
            raise ManifestError(f"{actx}: manual source must not declare a url")
        assets.append(
            AssetContribution(
                id=asset_id,
                name=str(entry.get("name", asset_id)),
                filename=filename,
                size=size,
                sha256=sha256.lower(),
                source_kind=kind,
                source_url=url,
            )
        )
    return tuple(assets)


def _parse_exporters(contributions: dict[str, Any], ctx: str) -> tuple[ExporterContribution, ...]:
    raw = contributions.get("exporters", [])
    if not isinstance(raw, list):
        raise ManifestError(f"{ctx}: contributions.exporters must be a list")
    exporters: list[ExporterContribution] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        ectx = f"{ctx}: exporters[{i}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{ectx}: must be an object")
        eid = _require(entry, "id", str, ectx)
        if not eid.strip():
            raise ManifestError(f"{ectx}: id must be non-empty")
        if eid in seen:
            raise ManifestError(f"{ectx}: duplicate exporter id {eid!r}")
        seen.add(eid)
        ext = _require(entry, "extension", str, ectx)
        # A reader's suffix rule (§3.1): leading dots stripped, then
        # neither empty nor only ASCII whitespace -- so "." is refused, and a
        # Unicode space is a suffix here exactly as it is for a reader.
        if not ext.lstrip(".").strip(_ASCII_SPACE):
            raise ManifestError(f"{ectx}: extension must be non-empty")
        exporters.append(
            ExporterContribution(
                id=eid,
                name=str(entry.get("name", eid)),
                extension=("." + ext.lstrip(".")).lower(),
            )
        )
    return tuple(exporters)


def _parse_stages(contributions: dict[str, Any], ctx: str) -> tuple[StageContribution, ...]:
    raw = contributions.get("stages", [])
    if not isinstance(raw, list):
        raise ManifestError(f"{ctx}: contributions.stages must be a list")
    stages: list[StageContribution] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        sctx = f"{ctx}: stages[{i}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{sctx}: must be an object")
        sid = _require(entry, "id", str, sctx)
        if not sid.strip():
            raise ManifestError(f"{sctx}: id must be non-empty")
        if sid in seen:
            raise ManifestError(f"{sctx}: duplicate stage id {sid!r}")
        seen.add(sid)
        name = entry.get("name", sid)
        if not isinstance(name, str):
            raise ManifestError(f"{sctx}: name must be a string")
        params = parse_stage_params(entry.get("params", []), sctx)
        stages.append(StageContribution(id=sid, name=name, params=params))
    return tuple(stages)


def _parse_region_types(
    contributions: dict[str, Any], ctx: str
) -> tuple[RegionTypeContribution, ...]:
    raw = contributions.get("region_types", [])
    if not isinstance(raw, list):
        raise ManifestError(f"{ctx}: contributions.region_types must be a list")
    types: list[RegionTypeContribution] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        rctx = f"{ctx}: region_types[{i}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{rctx}: must be an object")
        tid = _require(entry, "id", str, rctx)
        if not _ASSET_ID.fullmatch(tid):
            raise ManifestError(
                f"{rctx}: region type id must start with a letter or digit and contain only "
                "letters, digits, '.', '_' and '-'"
            )
        if tid in BUILTIN_REGION_TYPES:
            raise ManifestError(f"{rctx}: region type id {tid!r} is a built-in type")
        if tid.startswith(RESERVED_REGION_TYPE_PREFIX):
            raise ManifestError(
                f"{rctx}: region type id {tid!r} is reserved: ids beginning "
                f"{RESERVED_REGION_TYPE_PREFIX!r} are the host's"
            )
        if tid in seen:
            raise ManifestError(f"{rctx}: duplicate region type id {tid!r}")
        seen.add(tid)
        name = entry.get("name", tid)
        if not isinstance(name, str):
            raise ManifestError(f"{rctx}: name must be a string")
        types.append(RegionTypeContribution(id=tid, name=name))
    return tuple(types)


def parse_stage_params(raw: Any, ctx: str) -> tuple[StageParam, ...]:
    """§3.7's ``params``: a flat list, one entry per key.  Public so a stage
    declared in code is held to exactly these rules at declaration."""
    if not isinstance(raw, list):
        raise ManifestError(f"{ctx}: params must be a list")
    params: list[StageParam] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        pctx = f"{ctx}: params[{i}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{pctx}: must be an object")
        key = _require(entry, "key", str, pctx)
        if not key.strip():
            raise ManifestError(f"{pctx}: key must be non-empty")
        if key in seen:
            raise ManifestError(f"{pctx}: duplicate param key {key!r}")
        seen.add(key)
        name = entry.get("name", key)
        if not isinstance(name, str):
            raise ManifestError(f"{pctx}: name must be a string")
        kind = _require(entry, "type", str, pctx)
        if kind not in STAGE_PARAM_FIELDS:
            raise ManifestError(f"{pctx}: type must be one of {list(STAGE_PARAM_FIELDS)}")
        if "value" not in entry:
            raise ManifestError(f"{pctx}: needs a value, the default")
        extra = set(entry) - {"key", "name", "type", "value", *STAGE_PARAM_FIELDS[kind]}
        if extra:
            raise ManifestError(f"{pctx}: a {kind} param does not take {sorted(extra)}")
        params.append(_stage_param(entry, key, name, kind, pctx))
    return tuple(params)


def _stage_param(entry: dict, key: str, name: str, kind: str, pctx: str) -> StageParam:
    value = entry["value"]
    if kind == "bool":
        if not isinstance(value, bool):
            raise ManifestError(f"{pctx}: value must be a boolean")
        return StageParam(key, name, kind, value)
    if kind == "choice":
        options = entry.get("options")
        if (
            not isinstance(options, list)
            or not options
            or not all(isinstance(o, str) for o in options)
            or len(set(options)) != len(options)
        ):
            raise ManifestError(f"{pctx}: options must be a non-empty list of distinct strings")
        if value not in options:
            raise ManifestError(f"{pctx}: value must be one of the options")
        return StageParam(key, name, kind, value, options=tuple(options))
    if kind == "string":
        limit = entry.get("max_length", STRING_PARAM_MAX_BYTES)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= STRING_PARAM_MAX_BYTES
        ):
            raise ManifestError(
                f"{pctx}: max_length must be an integer in [1, {STRING_PARAM_MAX_BYTES}]"
            )
        if not isinstance(value, str):
            raise ManifestError(f"{pctx}: value must be a string")
        if len(value.encode("utf-8")) > limit:
            raise ManifestError(f"{pctx}: value is longer than max_length ({limit} UTF-8 bytes)")
        return StageParam(key, name, kind, value, max_length=limit)
    whole = kind == "int"
    bounds: dict[str, Any] = {}
    for bound in ("value", "min", "max"):
        if bound not in entry:
            continue
        v = entry[bound]
        numeric = isinstance(v, int) if whole else isinstance(v, (int, float))
        if isinstance(v, bool) or not numeric or (not whole and _finite(v) is None):
            raise ManifestError(
                f"{pctx}: numeric field {bound!r} must be "
                + ("an integer" if whole else "a finite number")
            )
        if whole and not _SINT64_MIN <= v <= _SINT64_MAX:
            # `Value.i` is sint64: a default past it cannot be sent, and a
            # bound past it bounds nothing the wire can carry.
            raise ManifestError(f"{pctx}: numeric field {bound!r} must fit a signed 64-bit integer")
        bounds[bound] = v if whole else float(v)
    lo, hi = bounds.get("min"), bounds.get("max")
    if lo is not None and hi is not None and lo > hi:
        raise ManifestError(f"{pctx}: needs min <= max")
    if (lo is not None and bounds["value"] < lo) or (hi is not None and bounds["value"] > hi):
        raise ManifestError(f"{pctx}: value is outside [min, max]")
    return StageParam(key, name, kind, bounds["value"], lo, hi)


def _parse_overlays(contributions: dict[str, Any], ctx: str) -> tuple[OverlayContribution, ...]:
    raw = contributions.get("overlays", [])
    if not isinstance(raw, list):
        raise ManifestError(f"{ctx}: contributions.overlays must be a list")
    overlays: list[OverlayContribution] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        octx = f"{ctx}: overlays[{i}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{octx}: must be an object")
        oid = _require(entry, "id", str, octx)
        if not oid.strip():
            raise ManifestError(f"{octx}: id must be non-empty")
        if oid in seen:
            raise ManifestError(f"{octx}: duplicate overlay id {oid!r}")
        seen.add(oid)
        mib = entry.get("segment_mib", 32)
        if isinstance(mib, bool) or not isinstance(mib, int) or mib < 1 or mib > 512:
            raise ManifestError(f"{octx}: segment_mib must be an int in [1, 512]")
        chunked = entry.get("chunked", False)
        if not isinstance(chunked, bool):
            raise ManifestError(f"{octx}: chunked must be a bool")
        time = entry.get("time", "per_t")
        if time not in OVERLAY_TIME_KINDS:
            raise ManifestError(f"{octx}: time must be one of {sorted(OVERLAY_TIME_KINDS)}")
        if chunked and "segment_mib" in entry:
            raise ManifestError(
                f"{octx}: a chunked overlay's arena is sized by the host from the "
                "dataset; segment_mib does not apply"
            )
        if not chunked and "time" in entry:
            raise ManifestError(f"{octx}: time applies only to a chunked overlay")
        overlays.append(
            OverlayContribution(
                id=oid,
                name=str(entry.get("name", oid)),
                segment_mib=mib,
                chunked=chunked,
                time=time,
            )
        )
    return tuple(overlays)


_TOOL_MODES = frozenset({"fire", "gesture"})
_HOLDS = frozenset({"toggle", "momentary"})


def _parse_hold(raw: Any, ctx: str, default: str = "toggle") -> str:
    if raw is None:
        return default
    if not isinstance(raw, str) or raw not in _HOLDS:
        raise ManifestError(f"{ctx}: hold {raw!r} not in {sorted(_HOLDS)}")
    return raw


def _parse_key_name(raw: Any, ctx: str) -> str:
    if not isinstance(raw, str):
        raise ManifestError(f"{ctx}: key must be a key-name string")
    return raw.strip()


def is_contained_relative(path: str) -> bool:
    """Whether ``path`` stays inside the directory it is joined to, read as a
    POSIX path AND as a Windows one, whichever OS asks.  On Windows
    ``C:x.svg`` (drive-relative) and ``\\x.svg`` (rooted) are not
    ``is_absolute()``, yet joining either leaves the directory; a manifest
    written on one OS is installed and read on another."""
    for flavour in (PurePosixPath, PureWindowsPath):
        rel = flavour(path)
        if rel.anchor or ".." in rel.parts:
            return False
    return True


def _parse_pane(
    entry: dict[str, Any], ctx: str, *, source_dir: Path | None, sticky: bool
) -> "PaneButton | None":
    """``pane`` on a tool (``sticky``), an action or a command.

    The icon is checked here as far as a Qt-free module can -- the path is
    inside the plugin directory, the file exists with a suffix the host renders,
    it is small, and its first bytes are what its suffix claims -- so an author
    learns at load, not on the pane, that the file is wrong.  The render itself
    happens at registration, and a failure THERE refuses the plugin too.
    """
    raw = entry.get("pane")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError(f"{ctx}: pane must be an object")
    icon = _require(raw, "icon", str, f"{ctx}: pane").strip()
    if not icon:
        raise ManifestError(f"{ctx}: pane.icon must be non-empty")
    rel = Path(icon)
    if not is_contained_relative(icon):
        raise ManifestError(f"{ctx}: pane.icon {icon!r} must be a path inside the plugin directory")
    if rel.suffix.lower() not in _PANE_ICON_SUFFIXES:
        raise ManifestError(
            f"{ctx}: pane.icon {icon!r} is not one of {sorted(_PANE_ICON_SUFFIXES)}"
        )
    if source_dir is not None:
        path = Path(source_dir) / rel
        if not path.is_file():
            raise ManifestError(f"{ctx}: pane.icon {icon!r} not found in the plugin directory")
        size = path.stat().st_size
        if size > _PANE_ICON_MAX_BYTES:
            raise ManifestError(
                f"{ctx}: pane.icon {icon!r} is {size} bytes; the limit is {_PANE_ICON_MAX_BYTES}"
            )
        head = path.read_bytes()[: max(len(_PNG_SIGNATURE), 512)]
        if rel.suffix.lower() == ".png" and not head.startswith(_PNG_SIGNATURE):
            raise ManifestError(f"{ctx}: pane.icon {icon!r} is not a PNG file")
        if rel.suffix.lower() == ".svg" and b"<svg" not in head:
            raise ManifestError(f"{ctx}: pane.icon {icon!r} is not an SVG file")
    tooltip = raw.get("tooltip", "")
    if not isinstance(tooltip, str):
        raise ManifestError(f"{ctx}: pane.tooltip must be a string")
    if len(tooltip) > _PANE_TOOLTIP_MAX_CHARS:
        raise ManifestError(f"{ctx}: pane.tooltip longer than {_PANE_TOOLTIP_MAX_CHARS} characters")
    hold = _parse_hold(raw.get("hold"), f"{ctx}: pane", default="toggle" if sticky else "momentary")
    if not sticky and hold != "momentary":
        raise ManifestError(
            f"{ctx}: pane.hold {hold!r}: an action or command button fires once on click; "
            "only a tool's button can be sticky"
        )
    return PaneButton(icon=icon, tooltip=tooltip.strip(), hold=hold)


def _parse_triggers(entry: dict[str, Any], tctx: str) -> tuple[Trigger, ...]:
    raw = entry.get("triggers", [])
    if not isinstance(raw, list):
        raise ManifestError(f"{tctx}: triggers must be a list")
    # **NAMES, not platform codes** (§8.10).  An author writing `"button": 4`
    # is looking up `Qt::MiddleButton`, which is vizi's implementation detail
    # leaking into a document a third party writes -- the same leak the wire
    # had, and `doorbell_E_input.md` §3's rule applies to both ends of it.

    triggers: list[Trigger] = []
    seen: set[tuple] = set()
    for i, item in enumerate(raw):
        ctx = f"{tctx}: triggers[{i}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{ctx}: must be an object")
        key = _parse_key_name(item.get("key", ""), ctx)
        raw_button = item.get("button", "")
        if not isinstance(raw_button, str):
            raise ManifestError(f"{ctx}: button must be a string")
        button = BUTTONS.get(raw_button.strip().lower(), 0) if raw_button.strip() else 0
        if raw_button.strip() and not button:
            raise ManifestError(f"{ctx}: button {raw_button!r} not in {sorted(BUTTONS)}")
        if bool(key) == bool(button):
            raise ManifestError(f"{ctx}: a trigger names a key or a button, not both or neither")
        raw_mods = item.get("modifiers", [])
        if isinstance(raw_mods, str):
            raw_mods = [raw_mods]
        if not isinstance(raw_mods, list) or not all(isinstance(m, str) for m in raw_mods):
            raise ManifestError(f"{ctx}: modifiers must be a list of modifier names")
        mods = 0
        for name in raw_mods:
            bit = MODIFIERS.get(name.strip().lower())
            if bit is None:
                raise ManifestError(f"{ctx}: modifiers {name!r} not in {sorted(MODIFIERS)}")
            mods |= bit
        if key and mods:
            raise ManifestError(
                f"{ctx}: a key trigger carries its modifiers in the key name ({key!r}); "
                "modifiers qualify a button"
            )
        if button:
            if mods & MOD_ALT:
                raise ManifestError(
                    f"{ctx}: alt is not a mouse modifier -- most window managers take "
                    "Alt+drag before the application sees it"
                )
            if button == BUTTONS["left"] and not mods:
                raise ManifestError(
                    f"{ctx}: the bare left button is never a trigger: it means whatever the "
                    "engaged tool means"
                )
        hold = _parse_hold(item.get("hold"), ctx, default="momentary" if button else "toggle")
        if button and hold != "momentary":
            raise ManifestError(
                f"{ctx}: a mouse trigger is momentary (down engages, up releases); "
                "a sticky engage is the pane's"
            )
        ident = ("key", key.lower()) if key else ("mouse", button, mods)
        if ident in seen:
            raise ManifestError(f"{ctx}: duplicate trigger")
        seen.add(ident)
        triggers.append(Trigger(key=key, button=button, mods=mods, hold=hold))
    return tuple(triggers)


def _parse_actions(
    entry: dict[str, Any], tctx: str, trigger_keys: set[str], *, source_dir: Path | None
) -> tuple[ToolAction, ...]:
    raw = entry.get("actions", [])
    if not isinstance(raw, list):
        raise ManifestError(f"{tctx}: actions must be a list")
    if len(raw) > _TOOL_MAX_ACTIONS:
        raise ManifestError(f"{tctx}: more than {_TOOL_MAX_ACTIONS} actions")
    actions: list[ToolAction] = []
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    for i, item in enumerate(raw):
        ctx = f"{tctx}: actions[{i}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{ctx}: must be an object")
        aid = _require(item, "id", str, ctx)
        if not aid.strip():
            raise ManifestError(f"{ctx}: id must be non-empty")
        if aid in seen_ids:
            raise ManifestError(f"{ctx}: duplicate action id {aid!r}")
        seen_ids.add(aid)
        key = _parse_key_name(item.get("key", ""), ctx)
        pane = _parse_pane(item, ctx, source_dir=source_dir, sticky=False)
        if not key and pane is None:
            raise ManifestError(f"{ctx}: an action needs a key or a pane button to be reachable")
        if key:
            if key.lower() in trigger_keys:
                raise ManifestError(f"{ctx}: key {key!r} is also one of this tool's triggers")
            if key.lower() in seen_keys:
                raise ManifestError(f"{ctx}: key {key!r} is also another action's of this tool")
            seen_keys.add(key.lower())
        actions.append(ToolAction(id=aid, name=str(item.get("name", aid)), key=key, pane=pane))
    return tuple(actions)


def _parse_tools(
    contributions: dict[str, Any],
    ctx: str,
    *,
    source_dir: Path | None = None,
) -> tuple[ToolContribution, ...]:
    raw = contributions.get("tools", [])
    if not isinstance(raw, list):
        raise ManifestError(f"{ctx}: contributions.tools must be a list")
    tools: list[ToolContribution] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        tctx = f"{ctx}: tools[{i}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{tctx}: must be an object")
        tid = _require(entry, "id", str, tctx)
        if not tid.strip():
            raise ManifestError(f"{tctx}: id must be non-empty")
        if tid in seen:
            raise ManifestError(f"{tctx}: duplicate tool id {tid!r}")
        seen.add(tid)
        views_raw = entry.get("views", ["2d", "3d"])
        if (
            not isinstance(views_raw, list)
            or not views_raw
            or not all(v in ("2d", "3d") for v in views_raw)
        ):
            raise ManifestError(f"{tctx}: views must be a non-empty list from ['2d', '3d']")
        menu = entry.get("menu", "tools")
        if menu is not None and (not isinstance(menu, str) or menu not in _COMMAND_MENUS):
            raise ManifestError(
                f"{tctx}: menu {menu!r} not allowed (expected one of {sorted(_COMMAND_MENUS)})"
            )
        if "binding" in entry:
            raise ManifestError(
                f"{tctx}: binding is not a field; a tool declares triggers, actions and a "
                "pane button (§8.10)"
            )
        mode = str(entry.get("mode", "gesture"))
        if mode not in _TOOL_MODES:
            raise ManifestError(f"{tctx}: mode {mode!r} not in {sorted(_TOOL_MODES)}")
        cancel = entry.get("cancel_on_camera_move", True)
        if not isinstance(cancel, bool):
            raise ManifestError(f"{tctx}: cancel_on_camera_move must be a boolean")
        wants_pick = entry.get("wants_pick", True)
        if not isinstance(wants_pick, bool):
            raise ManifestError(f"{tctx}: wants_pick must be a boolean")
        triggers = _parse_triggers(entry, tctx)
        trigger_keys = {t.key.lower() for t in triggers if t.key}
        actions = _parse_actions(entry, tctx, trigger_keys, source_dir=source_dir)
        tools.append(
            ToolContribution(
                id=tid,
                name=str(entry.get("name", tid)),
                views=tuple(views_raw),
                menu=menu,
                mode=mode,
                cancel_on_camera_move=cancel,
                wants_pick=wants_pick,
                triggers=triggers,
                actions=actions,
                pane=_parse_pane(entry, tctx, source_dir=source_dir, sticky=True),
                view_targets=_parse_view_targets(entry, tctx),
            )
        )
    return tuple(tools)


def _parse_view_targets(entry: dict, ctx: str, key: str = "view_targets") -> tuple[str, ...]:
    """Set-view's declaration: what this surface may move -- or, for
    ``follow_targets``, what a Follow from it may drive."""
    raw = entry.get(key, [])
    if (
        not isinstance(raw, list)
        or not all(isinstance(t, str) and t in VIEW_TARGETS for t in raw)
        or len(set(raw)) != len(raw)
    ):
        raise ManifestError(f"{ctx}: {key} must be distinct names from {list(VIEW_TARGETS)}")
    return tuple(raw)


def _parse_commands(
    contributions: dict[str, Any],
    ctx: str,
    *,
    source_dir: Path | None = None,
) -> tuple[CommandContribution, ...]:
    raw = contributions.get("commands", [])
    if not isinstance(raw, list):
        raise ManifestError(f"{ctx}: contributions.commands must be a list")
    commands: list[CommandContribution] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        cctx = f"{ctx}: commands[{i}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{cctx}: must be an object")
        cid = _require(entry, "id", str, cctx)
        if not cid.strip():
            raise ManifestError(f"{cctx}: id must be non-empty")
        if cid in seen:
            raise ManifestError(f"{cctx}: duplicate command id {cid!r}")
        seen.add(cid)
        title = _require(entry, "title", str, cctx)
        key = entry.get("default_keybinding")
        if key is not None and (not isinstance(key, str) or not key.strip()):
            raise ManifestError(f"{cctx}: default_keybinding must be a non-empty string")
        menu = entry.get("menu", "tools")
        if menu is not None and (not isinstance(menu, str) or menu not in _COMMAND_MENUS):
            raise ManifestError(
                f"{cctx}: menu {menu!r} not allowed (expected one of {sorted(_COMMAND_MENUS)})"
            )
        commands.append(
            CommandContribution(
                id=cid,
                title=title,
                description=str(entry.get("description", "")),
                default_keybinding=key,
                menu=menu,
                pane=_parse_pane(entry, cctx, source_dir=source_dir, sticky=False),
            )
        )
    return tuple(commands)


def _parse_panel_widget(
    entry: Any,
    ctx: str,
    *,
    depth: int,
    counter: list[int],
    ids: set[str],
    command_ids: set[str],
) -> PanelWidget:
    if depth > _PANEL_MAX_DEPTH:
        raise ManifestError(f"{ctx}: panel tree deeper than {_PANEL_MAX_DEPTH}")
    counter[0] += 1
    if counter[0] > _PANEL_MAX_WIDGETS:
        raise ManifestError(f"{ctx}: panel has more than {_PANEL_MAX_WIDGETS} widgets")
    if not isinstance(entry, dict):
        raise ManifestError(f"{ctx}: widget must be an object")
    kind = _require(entry, "kind", str, ctx)
    if kind not in PANEL_WIDGET_KINDS:
        raise ManifestError(
            f"{ctx}: unknown widget kind {kind!r} (host understands {sorted(PANEL_WIDGET_KINDS)})"
        )
    wid = _require(entry, "id", str, ctx)
    if not wid.strip():
        raise ManifestError(f"{ctx}: widget id must be non-empty")
    if wid in ids:
        raise ManifestError(f"{ctx}: duplicate widget id {wid!r}")
    ids.add(wid)

    command = entry.get("command")
    selectable = entry.get("selectable", False)
    if not isinstance(selectable, bool):
        raise ManifestError(f"{ctx}: {kind} {wid!r} selectable must be a boolean")
    if selectable and kind not in _ROW_KINDS:
        raise ManifestError(f"{ctx}: {kind} {wid!r} does not take selectable")
    if kind in _ROW_KINDS and not selectable and command is not None:
        raise ManifestError(f"{ctx}: {kind} {wid!r} binds a command only when selectable")
    if kind == "image" and command is not None:
        # A clickable image: a click reports the pixel.
        if not isinstance(command, str) or command not in command_ids:
            raise ManifestError(
                f"{ctx}: image widget {wid!r} must bind a declared command (got {command!r})"
            )
    elif kind in _INTERACTIVE_KINDS or (kind in _ROW_KINDS and selectable):
        if not isinstance(command, str) or command not in command_ids:
            raise ManifestError(
                f"{ctx}: {kind} widget {wid!r} must bind a declared command " f"(got {command!r})"
            )
    children: tuple[PanelWidget, ...] = ()
    if kind in _CONTAINER_KINDS:
        raw_children = _require(entry, "children", list, ctx)
        if not raw_children:
            raise ManifestError(f"{ctx}: {kind} {wid!r} has no children")
        children = tuple(
            _parse_panel_widget(
                c,
                f"{ctx}: {wid}[{j}]",
                depth=depth + 1,
                counter=counter,
                ids=ids,
                command_ids=command_ids,
            )
            for j, c in enumerate(raw_children)
        )

    template: tuple[PanelWidget, ...] = ()
    max_rows, visible_rows = LIST_DEFAULT_MAX_ROWS, LIST_DEFAULT_VISIBLE_ROWS
    if kind == "list":
        raw_row = _require(entry, "row", list, ctx)
        if not raw_row:
            raise ManifestError(f"{ctx}: list {wid!r} has an empty row")
        for j, cell in enumerate(raw_row):
            kind_of = cell.get("kind") if isinstance(cell, dict) else None
            if isinstance(kind_of, str) and kind_of in _NOT_IN_A_LIST_ROW:
                raise ManifestError(
                    f"{ctx}: list {wid!r} row[{j}] is a {cell.get('kind')}; "
                    "a row holds leaf widgets only"
                )
        template = tuple(
            _parse_panel_widget(
                c,
                f"{ctx}: {wid}.row[{j}]",
                depth=depth + 1,
                counter=counter,
                ids=ids,
                command_ids=command_ids,
            )
            for j, c in enumerate(raw_row)
        )
        for name, default, ceiling in (
            ("max_rows", LIST_DEFAULT_MAX_ROWS, LIST_MAX_ROWS),
            ("visible_rows", LIST_DEFAULT_VISIBLE_ROWS, LIST_MAX_ROWS),
        ):
            bound = entry.get(name, default)
            if isinstance(bound, bool) or not isinstance(bound, int) or not 1 <= bound <= ceiling:
                raise ManifestError(
                    f"{ctx}: list {wid!r} {name} must be an integer in [1, {ceiling}]"
                )
            if name == "max_rows":
                max_rows = bound
            else:
                visible_rows = bound
        if max_rows * len(template) > LIST_MAX_CELLS:
            raise ManifestError(
                f"{ctx}: list {wid!r} may realize at most {LIST_MAX_CELLS} cells "
                f"(max_rows × row cells), not {max_rows} × {len(template)}"
            )
    columns: tuple[TableColumn, ...] = ()
    sortable = False
    if kind == "table":
        columns = _parse_table_columns(entry, ctx, wid)
        visible_rows = entry.get("visible_rows", LIST_DEFAULT_VISIBLE_ROWS)
        if (
            isinstance(visible_rows, bool)
            or not isinstance(visible_rows, int)
            or not 1 <= visible_rows <= LIST_MAX_ROWS
        ):
            raise ManifestError(
                f"{ctx}: table {wid!r} visible_rows must be an integer in [1, {LIST_MAX_ROWS}]"
            )
        sortable = entry.get("sortable", False)
        if not isinstance(sortable, bool):
            raise ManifestError(f"{ctx}: table {wid!r} sortable must be a boolean")
    elif "columns" in entry or "sortable" in entry:
        raise ManifestError(f"{ctx}: {kind} {wid!r} does not take columns or sortable")

    # Typed, not coerced: `float("low")` would escape as a bare ValueError,
    # which `Broker.scan` does not catch -- one malformed slider would take
    # every other plugin's discovery down with it.
    numbers = {}
    for field_name, default in (("min", 0.0), ("max", 1.0), ("step", 1.0)):
        raw_number = entry.get(field_name, default)
        if isinstance(raw_number, bool) or not isinstance(raw_number, (int, float)):
            raise ManifestError(f"{ctx}: {kind} {wid!r} {field_name} must be numeric")
        number = _finite(raw_number)
        if number is None:
            raise ManifestError(f"{ctx}: {kind} {wid!r} {field_name} must be a finite number")
        numbers[field_name] = number
    minimum, maximum, step = numbers["min"], numbers["max"], numbers["step"]
    for field_name in ("text", "placeholder"):
        if not isinstance(entry.get(field_name, ""), str):
            raise ManifestError(f"{ctx}: {kind} {wid!r} {field_name} must be a string")
    text_format = entry.get("text_format", "plain")
    if text_format not in TEXT_FORMATS:
        raise ManifestError(
            f"{ctx}: {kind} {wid!r} text_format must be one of {list(TEXT_FORMATS)}"
        )
    if "text_format" in entry and kind != "label":
        raise ManifestError(f"{ctx}: {kind} {wid!r} does not take text_format; only a label does")
    for flag in ("float", "indeterminate", "multiple"):
        if not isinstance(entry.get(flag, False), bool):
            raise ManifestError(f"{ctx}: {kind} {wid!r} {flag} must be a boolean")
    options_raw = entry.get("options", [])
    if not isinstance(options_raw, list) or not all(isinstance(o, str) for o in options_raw):
        raise ManifestError(f"{ctx}: options must be a list of strings")
    value = entry.get("value")
    if kind == "slider":
        if minimum >= maximum:
            raise ManifestError(f"{ctx}: slider {wid!r} needs min < max")
        if step <= 0:
            raise ManifestError(f"{ctx}: slider {wid!r} needs step > 0")
        if not entry.get("float", False) and step != int(step):
            raise ManifestError(
                f"{ctx}: slider {wid!r} step {step:g} is not a whole number; "
                "an integer slider moves by whole steps (or declare it float)"
            )
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ManifestError(f"{ctx}: slider {wid!r} value must be numeric")
    if kind == "progress":
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ManifestError(f"{ctx}: progress {wid!r} value must be numeric")
        fraction = None if value is None else _finite(value)
        if value is not None and (fraction is None or not 0.0 <= fraction <= 1.0):
            raise ManifestError(f"{ctx}: progress {wid!r} value must be within [0, 1]")
    if kind == "dropdown":
        if not options_raw:
            raise ManifestError(f"{ctx}: dropdown {wid!r} needs non-empty options")
        if value is not None and value not in options_raw:
            raise ManifestError(f"{ctx}: dropdown {wid!r} default {value!r} not in options")
    for flag in ("moves_view", "follows_selection"):
        if not isinstance(entry.get(flag, False), bool):
            raise ManifestError(f"{ctx}: {kind} {wid!r} {flag} must be a boolean")
    moves_view = bool(entry.get("moves_view", False))
    if moves_view and not (
        kind in _INTERACTIVE_KINDS
        or (kind in _ROW_KINDS and selectable)
        or (kind == "image" and command is not None)
    ):
        raise ManifestError(f"{ctx}: {kind} {wid!r} is not interactive, so it cannot move_view")
    follows_selection = bool(entry.get("follows_selection", False))
    roi_filter = entry.get("filter", "all")
    if kind != "roi_picker" and (follows_selection or "filter" in entry):
        raise ManifestError(f"{ctx}: {kind} {wid!r} does not take follows_selection or filter")
    if roi_filter not in _ROI_PICKER_FILTERS:
        raise ManifestError(
            f"{ctx}: roi_picker {wid!r} filter must be one of {list(_ROI_PICKER_FILTERS)}"
        )
    multiple = bool(entry.get("multiple", False))
    if multiple and kind not in ("channel_picker", "table"):
        raise ManifestError(f"{ctx}: {kind} {wid!r} does not take multiple")
    if multiple and kind == "table" and not selectable:
        raise ManifestError(f"{ctx}: table {wid!r} takes multiple only when selectable")
    if kind == "channel_picker" and value is not None:
        indices = value if multiple and isinstance(value, list) else [value]
        if (not multiple and isinstance(value, list)) or not all(
            isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in indices
        ):
            raise ManifestError(
                f"{ctx}: channel_picker {wid!r} value must be a channel index"
                + (", or a list of them" if multiple else "")
            )
    if kind == "color_picker" and value is not None:
        if not isinstance(value, str) or not _HEX_COLOR.match(value):
            raise ManifestError(f"{ctx}: color_picker {wid!r} value must be a #RRGGBB color")
    if kind in ("colormap_picker", "roi_picker") and value is not None:
        if not isinstance(value, str):
            raise ManifestError(f"{ctx}: {kind} {wid!r} value must be a string")

    return PanelWidget(
        kind=kind,
        widget_id=wid,
        text=str(entry.get("text", "")),
        text_format=text_format,
        command=command if isinstance(command, str) else None,
        minimum=minimum,
        maximum=maximum,
        step=step,
        has_step="step" in entry,
        value=value,
        is_float=bool(entry.get("float", False)),
        indeterminate=bool(entry.get("indeterminate", False)),
        options=tuple(options_raw),
        placeholder=str(entry.get("placeholder", "")),
        children=children,
        multiple=multiple,
        template=template,
        max_rows=max_rows,
        visible_rows=visible_rows,
        selectable=selectable,
        columns=columns,
        sortable=sortable,
        moves_view=moves_view,
        follows_selection=follows_selection,
        filter=str(roi_filter),
    )


def _parse_table_columns(entry: dict, ctx: str, wid: str) -> "tuple[TableColumn, ...]":
    raw = _require(entry, "columns", list, ctx)
    if not 1 <= len(raw) <= TABLE_MAX_COLUMNS:
        raise ManifestError(
            f"{ctx}: table {wid!r} needs 1 to {TABLE_MAX_COLUMNS} columns, not {len(raw)}"
        )
    seen: set[str] = set()
    columns = []
    for j, col in enumerate(raw):
        cctx = f"{ctx}: table {wid!r} columns[{j}]"
        if not isinstance(col, dict):
            raise ManifestError(f"{cctx} must be an object")
        cid = _require(col, "id", str, cctx)
        if not cid.strip() or cid in seen:
            raise ManifestError(f"{cctx}: id {cid!r} must be non-empty and distinct")
        seen.add(cid)
        header = col.get("header", cid)
        if not isinstance(header, str):
            raise ManifestError(f"{cctx}: header must be a string")
        ctype = col.get("type", "string")
        if ctype not in TABLE_COLUMN_TYPES:
            raise ManifestError(f"{cctx}: type must be one of {list(TABLE_COLUMN_TYPES)}")
        precision = col.get("precision", TABLE_DEFAULT_PRECISION)
        unit = col.get("unit", "")
        if ctype != "number" and ("precision" in col or "unit" in col):
            raise ManifestError(f"{cctx}: only a number column takes precision or unit")
        if (
            isinstance(precision, bool)
            or not isinstance(precision, int)
            or not 0 <= precision <= 15
        ):
            raise ManifestError(f"{cctx}: precision must be an integer in [0, 15]")
        if not isinstance(unit, str):
            raise ManifestError(f"{cctx}: unit must be a string")
        columns.append(TableColumn(cid, header, ctype, precision, unit))
    return tuple(columns)


def _list_cells(widget: PanelWidget) -> int:
    """The widgets every list under ``widget`` may realize, together."""
    own = widget.max_rows * len(widget.template) if widget.kind == "list" else 0
    return own + sum(_list_cells(w) for w in widget.children)


def _any_moves_view(widget: PanelWidget) -> bool:
    return widget.moves_view or any(
        _any_moves_view(w) for w in (*widget.children, *widget.template)
    )


def _parse_panels(
    contributions: dict[str, Any], ctx: str, command_ids: set[str]
) -> tuple[PanelContribution, ...]:
    raw = contributions.get("panels", [])
    if not isinstance(raw, list):
        raise ManifestError(f"{ctx}: contributions.panels must be a list")
    panels: list[PanelContribution] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        pctx = f"{ctx}: panels[{i}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{pctx}: must be an object")
        pid = _require(entry, "id", str, pctx)
        if not pid.strip():
            raise ManifestError(f"{pctx}: id must be non-empty")
        if pid in seen:
            raise ManifestError(f"{pctx}: duplicate panel id {pid!r}")
        seen.add(pid)
        title = _require(entry, "title", str, pctx)
        menu_label = None
        if "menu_label" in entry:
            menu_label = entry["menu_label"]
            if not isinstance(menu_label, str) or not menu_label.strip():
                raise ManifestError(f"{pctx}: menu_label must be a non-empty string")
            if len(menu_label) > _PANEL_MENU_LABEL_MAX_CHARS:
                raise ManifestError(
                    f"{pctx}: menu_label must be at most "
                    f"{_PANEL_MENU_LABEL_MAX_CHARS} characters"
                )
        root_raw = _require(entry, "root", dict, pctx)
        root = _parse_panel_widget(
            root_raw,
            f"{pctx}: root",
            depth=1,
            counter=[0],
            ids=set(),
            command_ids=command_ids,
        )
        cells = _list_cells(root)
        if cells > LIST_MAX_CELLS:
            raise ManifestError(
                f"{pctx}: panel {pid!r}'s lists may realize at most {LIST_MAX_CELLS} cells "
                f"together (each list's max_rows × row cells), not {cells}"
            )
        view_targets = _parse_view_targets(entry, pctx)
        if not view_targets and _any_moves_view(root):
            raise ManifestError(
                f"{pctx}: panel {pid!r} has moves_view widgets and declares no view_targets"
            )
        panels.append(
            PanelContribution(
                id=pid,
                title=title,
                root=root,
                menu_label=menu_label,
                view_targets=view_targets,
                follow_targets=_parse_view_targets(entry, pctx, "follow_targets"),
            )
        )
    return tuple(panels)


#: Every top-level key this host reads.
#: What `parse_manifest` reads under `contributions` and under `entry`; any
#: other name is ignored (§7), and logged at this host's own minor.
_CONTRIBUTION_KINDS = frozenset(
    {"readers", "commands", "panels", "exporters", "overlays", "tools", "stages", "region_types"}
)
_ENTRY_FIELDS = frozenset({"command", "startup_s"})

_TOP_LEVEL_KEYS = frozenset(
    {
        "manifest_version",
        "id",
        "name",
        "version",
        "engines",
        "entry",
        "contributions",
        "assets",
        "permissions",
        "subscriptions",
        "min_vizi",
        "sdk_version",
    }
)


def parse_manifest(data: dict[str, Any], source_dir: Path | None = None) -> PluginManifest:
    """Validate a manifest dict into a :class:`PluginManifest`.

    Raises :class:`ManifestError` with a specific message on any problem — the
    Broker logs it and skips the plugin rather than crashing the host.
    """
    ctx = f"manifest at {source_dir}" if source_dir else "manifest"

    # `engines` first, whatever else the manifest holds.
    engines_raw = data.get("engines") if isinstance(data, dict) else None
    engines_vizi = engines_raw.get("vizi") if isinstance(engines_raw, dict) else None
    if isinstance(engines_vizi, str):
        declared = _parse_engine_min(engines_vizi, ctx)
        if not (declared[0] == HOST_API_MAJOR and declared[1] <= HOST_API_MINOR):
            raise EnginesIncompatible(
                str(data.get("id", "")),
                str(data.get("name", "")),
                str(data.get("version", "")),
                engines_vizi,
            )

    schema = _require(data, "manifest_version", int, ctx)
    if schema != SUPPORTED_MANIFEST_VERSION:
        raise ManifestError(
            f"{ctx}: manifest_version {schema} unsupported "
            f"(this host reads [{SUPPORTED_MANIFEST_VERSION}]; a 1.x addition raises "
            f"engines.vizi, which this one declares as {engines_vizi!r}, never manifest_version)"
        )

    plugin_id = _require(data, "id", str, ctx)
    if not plugin_id.strip():
        raise ManifestError(f"{ctx}: id must be non-empty")
    if not valid_plugin_id(plugin_id):
        raise ManifestError(f"{ctx}: {plugin_id_message(plugin_id)}")
    name = _require(data, "name", str, ctx)
    version = _require(data, "version", str, ctx)

    # Omitted, `engines` is the lowest floor, 1.0 (§2.1): never refused for
    # its floor, as the SDK reads a plugin that declares none.
    engines = data["engines"] if "engines" in data else {"vizi": "1.0"}
    if not isinstance(engines, dict):
        raise ManifestError(f"{ctx}: engines must be an object")
    engines_vizi = _require(engines, "vizi", str, ctx)
    requires = _parse_engine_min(engines_vizi, ctx)

    # Clause 2's facts about the release (§2): optional, and a 1.x addition
    # beside the required fields, never in place of one.
    min_vizi = data.get("min_vizi")
    if "min_vizi" in data:
        if not isinstance(min_vizi, str):
            raise ManifestError(f"{ctx}: min_vizi must be a string")
        if _MIN_VIZI_RE.fullmatch(min_vizi) is None:
            raise ManifestError(f"{ctx}: min_vizi {min_vizi!r} is not a PEP 440 version")
    sdk_version = data.get("sdk_version")
    if "sdk_version" in data and (not isinstance(sdk_version, str) or not sdk_version):
        raise ManifestError(f"{ctx}: sdk_version must be a non-empty string")

    entry = _require(data, "entry", dict, ctx)
    command_raw = _require(entry, "command", list, ctx)
    if not command_raw or not all(isinstance(c, str) for c in command_raw):
        raise ManifestError(f"{ctx}: entry.command must be a non-empty list of strings")
    startup_s = entry.get("startup_s")
    if startup_s is not None and (
        isinstance(startup_s, bool)
        or not isinstance(startup_s, (int, float))
        or not math.isfinite(startup_s)
        or startup_s <= 0
    ):
        raise ManifestError(f"{ctx}: entry.startup_s must be a positive number of seconds")

    contributions = data.get("contributions", {})
    if not isinstance(contributions, dict):
        raise ManifestError(f"{ctx}: contributions must be an object")
    readers = _parse_readers(contributions, ctx)
    commands = _parse_commands(contributions, ctx, source_dir=source_dir)
    panels = _parse_panels(contributions, ctx, {c.id for c in commands})
    exporters = _parse_exporters(contributions, ctx)
    overlays = _parse_overlays(contributions, ctx)
    tools = _parse_tools(contributions, ctx, source_dir=source_dir)
    # Set-view: a panel of the plugin's that declares `view_targets` must be
    # up while the view moves -- so a tool that may move it needs such a panel.
    if not any(p.view_targets for p in panels):
        for tool in tools:
            if tool.view_targets:
                raise ManifestError(
                    f"{ctx}: tool {tool.id!r} declares view_targets but no panel does; the "
                    "move-the-view row lives on the plugin's panel"
                )
    stages = _parse_stages(contributions, ctx)
    region_types = _parse_region_types(contributions, ctx)
    assets = _parse_assets(data, ctx)
    # An overlay is drawn only while its plugin has a session, and a session
    # starts only when something the person does needs one.  Without such a
    # thing the plugin installs, loads, and never runs -- refused here, where
    # the author and the Plugin Manager's "Why?" both see it.  Only what the
    # host always offers counts: a keybinding or a trigger can be lost to
    # another binding, so on its own it is not a way in.
    if overlays and not (
        panels
        or readers
        or exporters
        or stages
        or any(c.menu is not None or c.pane is not None for c in commands)
        or any(t.menu is not None or t.pane is not None for t in tools)
    ):
        raise ManifestError(
            f"{ctx}: overlays are drawn only while the plugin runs, and nothing declared "
            "here starts it; declare a panel (or a command or tool in the Tools menu or "
            "on the pane, a reader, an exporter or a stage)"
        )

    permissions_raw = data.get("permissions", [])
    if not isinstance(permissions_raw, list) or not all(
        isinstance(p, str) for p in permissions_raw
    ):
        raise ManifestError(f"{ctx}: permissions must be a list of strings")
    # A known name is honored from the minor that introduced it (§5): to a
    # manifest declaring an earlier minor it is an unknown name, warned about
    # and dropped like one.
    early_permissions = [
        p for p in permissions_raw if PERMISSION_INTRODUCED.get(p, 0) > requires[1]
    ]
    permissions_raw = [p for p in permissions_raw if p not in early_permissions]
    if region_types and "contribute_rois" not in permissions_raw:
        # A type exists to be the type of regions the plugin contributes (§3.8).
        raise ManifestError(f"{ctx}: region_types need the contribute_rois permission")
    # KNOWN_PERMISSIONS are enforced by their owning host service (v0:
    # `data_access` gates reads and `contribute_rois` gates annotation ingestion
    # — declared = granted, visible in the Plugin Manager).
    # Unknown strings warn and are ignored (append-only posture).
    for perm in [*permissions_raw, *early_permissions]:
        if perm not in KNOWN_PERMISSIONS or perm in early_permissions:
            logging.getLogger(__name__).warning(
                "%s: permission %r is declared but NOT enforced in this version "
                "(unknown permission strings are ignored)",
                ctx,
                perm,
            )

    subscriptions_raw = data.get("subscriptions", [])
    if not isinstance(subscriptions_raw, list) or not all(
        isinstance(sub, str) for sub in subscriptions_raw
    ):
        raise ManifestError(f"{ctx}: subscriptions must be a list of strings")
    early_subscriptions = [
        s for s in subscriptions_raw if SUBSCRIPTION_INTRODUCED.get(s, 0) > requires[1]
    ]
    subscriptions_raw = [s for s in subscriptions_raw if s not in early_subscriptions]
    # Warned and ignored, `permissions`' posture: a host that does not arrange
    # the named push still runs every other part of the plugin, which is what
    # makes the field additive without a `manifest_version` bump (§7).
    for sub in [*subscriptions_raw, *early_subscriptions]:
        if sub not in KNOWN_SUBSCRIPTIONS or sub in early_subscriptions:
            logging.getLogger(__name__).warning(
                "%s: subscription %r is declared but NOT arranged by this version "
                "(unknown subscription names are ignored)",
                ctx,
                sub,
            )
        elif sub in DATA_SUBSCRIPTIONS and "data_access" not in permissions_raw:
            # What they push is what `ViewState` and `GetRois` answer, which
            # `data_access` gates; the same posture as an unknown name.
            logging.getLogger(__name__).warning(
                "%s: subscription %r needs the data_access permission and is NOT arranged",
                ctx,
                sub,
            )

    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    unknown += [f"contributions.{k}" for k in sorted(set(contributions) - _CONTRIBUTION_KINDS)]
    unknown += [f"entry.{k}" for k in sorted(set(entry) - _ENTRY_FIELDS)]
    if unknown and requires[1] == HOST_API_MINOR:
        # At this host's own minor nothing should be new to it: a typo, or a
        # field of a later 1.x the manifest claims not to need.  Above it, the
        # name is a later minor's and stays quiet.
        logging.getLogger(__name__).info(
            "plugin %s (%s): %s not known to this host and ignored",
            plugin_id,
            ctx,
            ", ".join(unknown),
        )
    return PluginManifest(
        id=plugin_id,
        name=name,
        version=version,
        requires=requires,
        command=tuple(command_raw),
        readers=readers,
        commands=commands,
        panels=panels,
        exporters=exporters,
        overlays=overlays,
        tools=tools,
        stages=stages,
        region_types=region_types,
        assets=assets,
        permissions=tuple(permissions_raw),
        subscriptions=tuple(subscriptions_raw),
        source_dir=source_dir,
        engines_vizi=engines_vizi,
        startup_s=float(startup_s) if startup_s is not None else None,
        min_vizi=min_vizi,
        sdk_version=sdk_version,
    )


def load_manifest(path: Path | str) -> PluginManifest:
    """Read + validate a ``vizi-plugin.json`` from disk.

    ``path`` may be the manifest file itself or the plugin directory containing
    it. Raises :class:`ManifestError` if missing or invalid.
    """
    p = Path(path)
    if p.is_symlink() and not p.exists():
        raise ManifestError(f"plugin path {p} is a broken symlink")
    manifest_path = p / MANIFEST_FILENAME if p.is_dir() else p
    try:
        manifest_path = manifest_path.resolve(strict=True)
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {manifest_path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest {manifest_path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"manifest {manifest_path} must be a JSON object")
    return parse_manifest(data, source_dir=manifest_path.parent)
