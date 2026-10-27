"""Declare a plugin's interface in code, beside its handlers.

A plugin that calls :func:`plugin` is **declared**: its ``vizi-plugin.json`` is
derived from the code rather than written by hand, and the plugin answers
``--manifest`` / ``--install`` from its own ``serve()`` (:mod:`.package`)::

    plugin(id="org.lab.demo", name="Demo", version="0.1.0")

    @command("recompute", title="Recompute plot")
    def recompute(ctx): ...

    panel("demo", title="Demo", root=widgets.column(
        "root",
        widgets.slider("threshold", command=recompute, min=0, max=255),
        widgets.image("plot"),
    ))

The decorators (``@reader``, ``@exporter``, ``@tool``, ``@command``,
``@chunk_source``) take the manifest's own field names as keyword arguments;
this module holds what they declare, plus the contributions that have no
handler (:func:`panel`, :func:`overlay`, :func:`asset`, :func:`region_type`)
and the small builders
(:func:`pane`, :func:`trigger`, :func:`action`).

**Only what the author wrote is emitted** -- a field left out stays the host's
default (``manifest_schema.md``), so the SDK never states a default the host
might later change.  The one convenience is Go's: a command's ``title``
defaults to its id.  Nothing here validates beyond what building needs; the
built manifest goes through :func:`.manifest.parse_manifest`, the parser the
host scans with, before anything is written.

A plugin that never calls :func:`plugin` is unaffected: its decorators still
record here, and nothing reads it.  Standard library only, like
:mod:`.manifest`, so ``--manifest`` works without the runtime's dependencies.
"""

from __future__ import annotations

import contextlib
from typing import Any, Callable, Iterator

from . import manifest as _manifest


class _Unset:
    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


#: "Not said" -- distinct from ``None``, which the schema gives meaning to
#: (``menu=None`` registers a command or tool without listing it).
UNSET: Any = _Unset()


class DeclarationError(ValueError):
    """The declared interface cannot be built into a manifest."""


_KINDS = (
    "readers",
    "commands",
    "panels",
    "exporters",
    "overlays",
    "tools",
    "stages",
    "region_types",
)


class _Declaration:
    """Everything declared in one process.  One per process in use; tests swap
    in a fresh one with :func:`_fresh`."""

    def __init__(self) -> None:
        self.identity: dict[str, Any] | None = None
        #: What the last :func:`plugin` call asked for, before it was checked:
        #: a failure report names the plugin even when its declaration failed.
        self.requested: tuple[str, str] | None = None
        self.contributions: dict[str, dict[str, dict[str, Any]]] = {k: {} for k in _KINDS}
        self.assets: dict[str, dict[str, Any]] = {}
        #: ``@command`` handlers by id, so a widget may bind the function itself.
        self.command_handlers: dict[str, Callable] = {}
        #: ``@panel_opened`` ids, checked against the declared panels at build.
        self.panel_handlers: set[str] = set()


_STATE = _Declaration()


@contextlib.contextmanager
def _fresh() -> Iterator[_Declaration]:
    """Swap in an empty declaration for the duration (tests: many plugins are
    declared in one process)."""
    global _STATE
    saved, _STATE = _STATE, _Declaration()
    try:
        yield _STATE
    finally:
        _STATE = saved


def _given(**fields: Any) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v is not UNSET}


def _record(kind: str, contribution_id: str, fields: dict[str, Any], *, merge=False) -> None:
    """Declare one contribution.  A second declaration of an id replaces the
    first, as a second handler registration does; ``merge`` folds fields into
    an existing one instead (``@chunk_source`` onto an ``overlay()``)."""
    entries = _STATE.contributions[kind]
    if merge and contribution_id in entries:
        entries[contribution_id].update(fields)
    else:
        entries[contribution_id] = {"id": contribution_id, **fields}


def _record_command_handler(command_id: str, fn: Callable) -> None:
    _STATE.command_handlers[command_id] = fn


def _record_panel_handler(panel_id: str) -> None:
    _STATE.panel_handlers.add(panel_id)


def declared() -> bool:
    """Whether :func:`plugin` has been called in this process."""
    return _STATE.identity is not None


def identity() -> "tuple[str, str] | None":
    """The declared ``(id, version)``, or ``None`` for an undeclared plugin."""
    if _STATE.identity is None:
        return None
    return _STATE.identity["id"], _STATE.identity["version"]


def engines() -> "str | None":
    """The declared ``engines`` (``"MAJOR.MINOR"``), or ``None`` for an
    undeclared plugin."""
    if _STATE.identity is None:
        return None
    return _STATE.identity["engines"]


def requested_identity() -> "tuple[str, str] | None":
    """The ``(id, version)`` :func:`plugin` was last called with, accepted or not."""
    return identity() or _STATE.requested


# ---------------------------------------------------------------------------
# What an author calls
# ---------------------------------------------------------------------------


def plugin(
    id: str,
    *,
    name: str,
    version: str,
    permissions: "list[str] | tuple[str, ...]" = (),
    subscriptions: "list[str] | tuple[str, ...]" = (),
    engines: "str | None" = None,
    startup_s: "float | None" = None,
    min_vizi: "str | None" = _manifest.DEFAULT_MIN_VIZI,
) -> None:
    """Declare the plugin's identity, which makes it a **declared** plugin.

    ``permissions`` are ``manifest_schema.md`` §5's (``"data_access"``,
    ``"contribute_rois"``); ``subscriptions`` are §5.1's (``"epochs"``).
    ``engines`` is the floor, ``"MAJOR.MINOR"``: the oldest host protocol
    this plugin was tested against.  By default this SDK's own, which a host
    older than the SDK then refuses even when the plugin uses nothing newer.
    It cannot exceed the SDK's.  Anything newer than the floor is used behind
    ``ctx.host.supports(...)``.
    ``startup_s`` is ``entry.startup_s``: how long the plugin
    expects to take to start, in seconds -- advisory, for a plugin with a slow
    start (a heavy import); vizi honors it within 10 s to 300 s.
    ``min_vizi`` is the oldest vizi release the plugin runs in, a PEP 440
    version: by default ``"1.0.0"``, the first host that speaks protocol 1.0;
    a later one for a plugin that needs a newer host fix.  The manifest also states
    ``sdk_version``, this SDK's release, which the author does not set.
    Calling it again replaces the identity.

    Under ``--install`` / ``--manifest``, a declaration refused from here on --
    at import, before ``serve()`` runs -- is reported, not a traceback."""
    from . import package as _package

    _package.report_uncaught_failures()
    _STATE.requested = (str(id), str(version))
    newest = (_manifest.HOST_API_MAJOR, _manifest.HOST_API_MINOR)
    if engines is None:
        wanted = newest
    else:
        try:
            major, minor = (int(part) for part in str(engines).split("."))
        except ValueError:
            raise DeclarationError(f"engines {engines!r} is not MAJOR.MINOR") from None
        wanted = (major, minor)
        if major != newest[0] or not 0 <= minor <= newest[1]:
            raise DeclarationError(
                f"engines {engines!r}: this SDK speaks host API {newest[0]}.0 to "
                f"{newest[0]}.{newest[1]}"
            )
    _STATE.identity = {
        "id": id,
        "name": name,
        "version": version,
        "permissions": list(permissions),
        "subscriptions": list(subscriptions),
        "engines": f"{wanted[0]}.{wanted[1]}",
        "startup_s": startup_s,
        "min_vizi": min_vizi,
    }


def panel(
    id: str,
    *,
    title: str,
    root: dict,
    menu_label: str = UNSET,
    view_targets: "list[str]" = UNSET,
    follow_targets: "list[str]" = UNSET,
) -> None:
    """Declare a panel (§3.3).  ``root`` is a widget tree built with
    :mod:`.widgets` (or the same dicts written by hand).  ``@panel_opened``
    registers what runs when it opens.  ``view_targets`` is what the panel's
    ``moves_view`` widgets may move (set-view); ``follow_targets`` what
    a Follow the person starts from it may drive (§8.15)."""
    _record(
        "panels",
        id,
        _given(
            title=title,
            menu_label=menu_label,
            root=root,
            view_targets=view_targets,
            follow_targets=follow_targets,
        ),
    )


def overlay(
    id: str,
    *,
    name: str = UNSET,
    segment_mib: int = UNSET,
    chunked: bool = UNSET,
    time: str = UNSET,
) -> None:
    """Declare an overlay (§3.5).  A chunked overlay served by a
    ``@chunk_source`` is declared by the decorator and needs no call here."""
    _record(
        "overlays",
        id,
        _given(name=name, segment_mib=segment_mib, chunked=chunked, time=time),
        merge=True,
    )


def region_type(id: str, *, name: str = UNSET) -> None:
    """Declare a region type this plugin shapes (§3.8) without a shaper
    for it -- every commit of such a region is answered ``REJECTED``, so its
    shape cannot change.  A type with a shaper is declared by
    ``@region_shaper`` and needs no call here; a call adds its ``name``."""
    _record("region_types", id, _given(name=name), merge=True)


def asset(
    id: str,
    *,
    filename: str,
    size: int,
    sha256: str,
    source: dict,
    name: str = UNSET,
) -> None:
    """Declare an asset the host provisions (§4): ``source`` is
    ``{"kind": "manual"}`` or ``{"kind": "https", "url": ...}``."""
    _STATE.assets[id] = {
        "id": id,
        **_given(name=name),
        "filename": filename,
        "size": size,
        "sha256": sha256,
        "source": source,
    }


def pane(icon: str, *, tooltip: str = UNSET, hold: str = UNSET) -> dict:
    """A control-pane button (§3.6.3) for a ``@tool``, an :func:`action` or a
    ``@command``.  ``icon`` is relative to the directory of the script being
    run; ``--install`` copies it beside the manifest, where the host looks."""
    return {"icon": icon, **_given(tooltip=tooltip, hold=hold)}


def trigger(
    *,
    key: str = UNSET,
    button: str = UNSET,
    modifiers: "list[str] | str" = UNSET,
    hold: str = UNSET,
) -> dict:
    """What engages a tool (§3.6.1): a ``key`` (``"Ctrl+J"``) or a ``button``
    (``"right"``, ``"middle"``, or ``"left"`` with ``modifiers``)."""
    return _given(key=key, button=button, modifiers=modifiers, hold=hold)


def action(
    id: str,
    *,
    name: str = UNSET,
    key: str = UNSET,
    pane: dict = UNSET,
) -> dict:
    """What an engaged tool can be told mid-gesture (§3.6.2); it needs a
    ``key`` or a ``pane`` button."""
    return {"id": id, **_given(name=name, key=key, pane=pane)}


# ---------------------------------------------------------------------------
# Building the manifest
# ---------------------------------------------------------------------------


def _command_id_of(ref: Any, where: str) -> Any:
    if not callable(ref):
        return ref
    for command_id, fn in _STATE.command_handlers.items():
        if fn is ref:
            return command_id
    raise DeclarationError(
        f"{where} binds {getattr(ref, '__name__', ref)!r}, which is not a @command handler"
    )


def _resolve_widget(widget: Any, where: str) -> Any:
    if not isinstance(widget, dict):
        return widget
    out = dict(widget)
    here = f"{where} widget {out.get('id')!r}"
    if "command" in out:
        out["command"] = _command_id_of(out["command"], here)
    if isinstance(out.get("children"), (list, tuple)):
        out["children"] = [_resolve_widget(c, where) for c in out["children"]]
    return out


def pane_icons(data: dict) -> list[str]:
    """Every pane icon a manifest names, in order and without repeats -- the
    files ``--install`` carries beside it."""
    icons: list[str] = []
    contributions = data.get("contributions", {})
    holders = list(contributions.get("commands", []))
    for tool in contributions.get("tools", []):
        holders.append(tool)
        holders.extend(tool.get("actions", []) or [])
    for holder in holders:
        icon = (holder.get("pane") or {}).get("icon")
        if isinstance(icon, str) and icon not in icons:
            icons.append(icon)
    return icons


def build(command: "list[str]") -> dict:
    """The manifest this process declares, launched by ``command``.

    Not validated here: the caller runs :func:`.manifest.parse_manifest`, so a
    declaration is held to exactly the host's rules and diagnostics."""
    if _STATE.identity is None:
        raise DeclarationError(
            "this plugin declares no interface: call plugin(id=..., name=..., version=...) "
            "to derive its manifest from code, or write vizi-plugin.json by hand"
        )
    from . import __version__ as _sdk_version

    ident = _STATE.identity
    data: dict[str, Any] = {
        "manifest_version": _manifest.SUPPORTED_MANIFEST_VERSION,
        "id": ident["id"],
        "name": ident["name"],
        "version": ident["version"],
        "engines": {"vizi": ident["engines"]},
    }
    if ident.get("min_vizi") is not None:
        data["min_vizi"] = ident["min_vizi"]
    data["sdk_version"] = _sdk_version
    data["entry"] = {"command": list(command)}
    if ident.get("startup_s") is not None:
        data["entry"]["startup_s"] = ident["startup_s"]
    contributions: dict[str, list] = {}
    for kind in _KINDS:
        entries = [dict(e) for e in _STATE.contributions[kind].values()]
        if kind == "panels":
            for entry in entries:
                entry["root"] = _resolve_widget(entry.get("root"), f"panel {entry['id']!r}")
        if entries:
            contributions[kind] = entries
    missing = sorted(_STATE.panel_handlers - set(_STATE.contributions["panels"]))
    if missing:
        raise DeclarationError(f"@panel_opened names {missing}, which no panel() declares")
    if contributions:
        data["contributions"] = contributions
    if _STATE.assets:
        data["assets"] = [dict(a) for a in _STATE.assets.values()]
    if ident["permissions"]:
        data["permissions"] = list(ident["permissions"])
    if ident["subscriptions"]:
        data["subscriptions"] = list(ident["subscriptions"])
    return data
